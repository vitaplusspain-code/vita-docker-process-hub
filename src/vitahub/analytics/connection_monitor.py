from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from vitahub.models import Camera, Event


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass
class _ConnState:
    last_ok: float
    reported_down: bool = False


class ConnectionMonitor:
    """Convierte éxitos y fallos de conexión en eventos.

    El umbral es holgado a propósito: un tirón de cable tarda ~30s solo en que
    el watchdog de FFmpeg lo detecte, más el backoff de reconexión. Por debajo
    de eso saldrían avisos falsos cada vez que alguien desenchufa algo.
    """

    def __init__(
        self,
        hub_id: str,
        unreachable_after_s: float = 300.0,
        clock: Callable[[], datetime] = _utcnow,
    ) -> None:
        self._hub_id = hub_id
        self._unreachable_after_s = unreachable_after_s
        self._clock = clock
        # Compartido entre los hilos de cámara (uno por `camera.id`), sin lock:
        # cada hilo solo lee/escribe la entrada de SU PROPIA cámara (su propia
        # clave), nunca la de otro hilo, así que no hay carrera posible entre
        # hilos sobre la misma entrada.
        self._states: dict[str, _ConnState] = {}

    def on_frame(self, camera: Camera, now: float) -> list[Event]:
        """Registra la evidencia de que la cámara funciona: un fotograma válido.

        Deliberadamente NO se llama al abrir el socket (`open_capture`): una
        cámara puede abrir la conexión sin llegar a entregar vídeo (slot de
        stream agotado, RTP filtrado, firmware colgado), y ahí el `open` por
        sí solo no demuestra nada.
        """
        state = self._states.setdefault(camera.id, _ConnState(last_ok=now))
        was_down = state.reported_down
        state.last_ok = now
        state.reported_down = False
        if not was_down:
            return []
        return [self._event(camera, "camera_reachable", "info", {"last_ip": camera.last_ip})]

    def on_failed(self, camera: Camera, now: float) -> list[Event]:
        # El reloj arranca en el primer fallo, que ocurre a los pocos segundos
        # del arranque: así una cámara que NUNCA llega a conectar también acaba
        # reportándose.
        state = self._states.setdefault(camera.id, _ConnState(last_ok=now))
        if state.reported_down:
            return []
        down_for = now - state.last_ok
        if down_for < self._unreachable_after_s:
            return []
        state.reported_down = True
        return [
            self._event(
                camera,
                "camera_unreachable",
                "medium",
                {"last_ip": camera.last_ip, "minutes_down": round(down_for / 60.0, 1)},
            )
        ]

    def _event(
        self, camera: Camera, event_type: str, severity: str, payload: dict[str, object]
    ) -> Event:
        return Event(
            hub_id=self._hub_id,
            camera_id=camera.id,
            camera_name=camera.name,
            type=event_type,
            severity=severity,
            timestamp=self._clock().isoformat(),
            payload=payload,
        )
