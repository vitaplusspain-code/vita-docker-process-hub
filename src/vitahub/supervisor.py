from __future__ import annotations

import threading
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

    def apply(self, cameras: list[Camera], uris: dict[str, str]) -> SupervisorChange:
        change = SupervisorChange()
        with self._lock:
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

    def stop_all(self) -> None:
        with self._lock:
            for handle in self._workers.values():
                handle.stop.set()
            for camera_id, handle in self._workers.items():
                handle.thread.join(timeout=self._join_timeout)
                if handle.thread.is_alive():
                    _log.warning(
                        "cam %s no terminó en %.0fs", camera_id, self._join_timeout
                    )
            self._workers.clear()
