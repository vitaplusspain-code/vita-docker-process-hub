"""Buffer circular de paquetes de vídeo ya codificados, uno por cámara."""
from __future__ import annotations

import threading
from collections import deque
from dataclasses import dataclass

from vitahub.clips.constants import BUFFER_S


@dataclass(frozen=True)
class BufferedPacket:
    t: float
    keyframe: bool
    packet: object


@dataclass(frozen=True)
class Snapshot:
    template: object
    packets: list[BufferedPacket]


class PacketBuffer:
    """Últimos `max_age_s` segundos de paquetes, siempre empezando en keyframe.

    Lo llena el hilo grabador de la cámara y lo leen el disparador y el
    ensamblador desde otros hilos, de ahí el lock. Guardar solo desde un
    keyframe garantiza que cualquier recorte sea decodificable sin referencias
    a paquetes ya descartados.
    """

    def __init__(self, max_age_s: float = BUFFER_S) -> None:
        self._max_age_s = max_age_s
        self._lock = threading.Lock()
        self._packets: deque[BufferedPacket] = deque()
        self._template: object | None = None

    def reset(self, template: object) -> None:
        """Vacía y fija el stream plantilla; se llama en cada (re)conexión.

        Los paquetes del stream anterior no se pueden mezclar con los del
        nuevo (otros parámetros de códec y otra base de tiempos).
        """
        with self._lock:
            self._packets.clear()
            self._template = template

    def clear(self, template: object) -> None:
        """Vacía paquetes y plantilla al desconectar el grabador dueño de `template`.

        Un buffer sin vídeo nuevo no debe seguir entregando los últimos 45 s
        como si fueran de ahora. Soltar la plantilla además deja que el
        contenedor se libere cuando ningún Snapshot en curso la use. Si la
        plantilla vigente ya es otra, el buffer es de un grabador más nuevo (el
        supervisor reinició el worker) y no se toca.
        """
        with self._lock:
            if self._template is not template:
                return
            self._packets.clear()
            self._template = None

    def append(self, p: BufferedPacket) -> None:
        with self._lock:
            if self._template is None:
                return  # sin plantilla no hay con qué montar un clip
            if not self._packets and not p.keyframe:
                return  # sin keyframe inicial no hay nada decodificable
            self._packets.append(p)
            self._trim(p.t - self._max_age_s)

    def _trim(self, limit: float) -> None:
        # Se corta en el último keyframe con t <= limit; si no hay ninguno, no
        # se descarta nada para no dejar el buffer empezando en un delta.
        cut = 0
        for i, q in enumerate(self._packets):
            if q.t > limit:
                break
            if q.keyframe:
                cut = i
        for _ in range(cut):
            self._packets.popleft()

    def snapshot(self, start: float, end: float) -> Snapshot | None:
        """Paquetes desde el último keyframe con t <= start hasta t <= end."""
        with self._lock:
            if self._template is None or not self._packets:
                return None
            if self._packets[0].t > end:
                return None
            first = 0
            for i, q in enumerate(self._packets):
                if q.t > start:
                    break
                if q.keyframe:
                    first = i
            out = [q for q in list(self._packets)[first:] if q.t <= end]
            return Snapshot(template=self._template, packets=out)
