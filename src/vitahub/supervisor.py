from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from vitahub.logging_setup import get_logger
from vitahub.models import Camera

_log = get_logger("supervisor")

# Firma del bucle de cámara: recibe su cámara, su URI y su PROPIO event de parada.
WorkerFn = Callable[[Camera, str, threading.Event], None]


@dataclass
class SupervisorChange:
    started: list[str] = field(default_factory=list)
    restarted: list[str] = field(default_factory=list)


@dataclass
class _WorkerHandle:
    thread: threading.Thread
    stop: threading.Event
    uri: str


class CameraSupervisor:
    """Dueño de los hilos de cámara. `apply` converge hacia el estado deseado.

    Es idempotente: llamarlo dos veces con la misma entrada no hace nada. Eso
    permite usar el mismo camino para el rescan inicial y para los periódicos.
    """

    def __init__(self, worker_fn: WorkerFn, join_timeout: float = 15.0) -> None:
        self._worker_fn = worker_fn
        self._join_timeout = join_timeout
        self._workers: dict[str, _WorkerHandle] = {}
        self._lock = threading.Lock()
        self._stopped = False

    def apply(self, cameras: list[Camera], uris: dict[str, str]) -> SupervisorChange:
        change = SupervisorChange()
        with self._lock:
            # Una vez llamado stop_all(), cualquier apply() posterior (un
            # rescan periódico o una petición HTTP que ya estaba en vuelo) no
            # debe arrancar workers nuevos: nadie volverá a pararlos.
            if self._stopped:
                _log.info("apply() ignorado: el supervisor ya está parado")
                return change
            for camera in cameras:
                self._apply_one(camera, uris.get(camera.id), change)
        return change

    def _apply_one(
        self, camera: Camera, uri: str | None, change: SupervisorChange
    ) -> None:
        handle = self._workers.get(camera.id)
        # Un worker que murió por su cuenta se olvida, para poder relanzarlo.
        if handle is not None and not handle.thread.is_alive():
            del self._workers[camera.id]
            handle = None

        if not camera.enabled:
            if handle is not None:
                self._stop(camera.id, handle)
            return

        # Sin URI (conocida pero no descubierta en este ciclo) no se toca nada:
        # un probe ONVIF perdido no debe apagar una cámara que funciona.
        if not uri:
            return

        if handle is None:
            self._start(camera, uri)
            change.started.append(camera.id)
            return

        if handle.uri != uri:
            # Nunca se arranca el reemplazo antes de confirmar que el anterior
            # murió: dos workers de la misma cámara duplicarían eventos.
            if not self._stop(camera.id, handle):
                _log.warning(
                    "cam %s: el worker anterior no termina, reintento en el próximo"
                    " ciclo",
                    camera.id,
                )
                return
            self._start(camera, uri)
            change.restarted.append(camera.id)

    def _start(self, camera: Camera, uri: str) -> None:
        stop = threading.Event()
        thread = threading.Thread(
            target=self._worker_fn,
            args=(camera, uri, stop),
            name=f"cam-{camera.id}",
            daemon=True,
        )
        self._workers[camera.id] = _WorkerHandle(thread=thread, stop=stop, uri=uri)
        thread.start()

    def _stop(self, camera_id: str, handle: _WorkerHandle) -> bool:
        """Señala y espera. Devuelve False si el hilo sigue vivo tras el timeout."""
        handle.stop.set()
        handle.thread.join(timeout=self._join_timeout)
        if handle.thread.is_alive():
            return False
        del self._workers[camera_id]
        return True

    def stop_all(self, deadline_s: float | None = None) -> None:
        """Para todos los workers y bloquea el supervisor (ver `apply`).

        `deadline_s`, si se da, acota el tiempo TOTAL de esta llamada, no el
        de cada cámara: con `join_timeout` por cámara, varias cámaras
        atascadas sumarían minutos de espera, muy por encima de lo que
        Docker concede antes del SIGKILL. Un hilo que no muere a tiempo se
        abandona sin más — son hilos daemon, no hace falta matarlos.
        """
        with self._lock:
            self._stopped = True
            for handle in self._workers.values():
                handle.stop.set()
            deadline = None if deadline_s is None else time.monotonic() + deadline_s
            for camera_id, handle in self._workers.items():
                timeout = self._join_timeout
                if deadline is not None:
                    timeout = max(0.0, min(timeout, deadline - time.monotonic()))
                handle.thread.join(timeout=timeout)
                if handle.thread.is_alive():
                    _log.warning(
                        "cam %s no terminó a tiempo, se abandona (hilo daemon)", camera_id
                    )
            self._workers.clear()
