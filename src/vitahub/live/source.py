from __future__ import annotations

import asyncio
import fractions
import time
from collections.abc import Callable
from dataclasses import dataclass

import numpy as np
from aiortc import MediaStreamTrack
from aiortc.contrib.media import MediaRelay
from av import VideoFrame

from vitahub.ingest.rtsp import with_credentials
from vitahub.logging_setup import get_logger
from vitahub.models import Camera

_log = get_logger("live.source")
_VIDEO_CLOCK = 90000


@dataclass(frozen=True)
class LiveTarget:
    camera_id: str
    name: str
    uri: str


def live_targets(cameras: list[Camera], user: str, password: str) -> list[LiveTarget]:
    """Cámaras que se pueden ver en directo, con su URI con credenciales.

    Siempre el substream si existe (spec §4.2): cada viewer es un flujo de
    subida del hogar y el principal multiplicaría el ancho de banda.
    """
    targets = []
    for cam in cameras:
        raw = cam.rtsp_sub or cam.rtsp_main
        if cam.enabled and raw:
            targets.append(LiveTarget(cam.id, cam.name, with_credentials(raw, user, password)))
    return targets


class SourceUnavailable(Exception):
    pass


class FpsLimitedTrack(MediaStreamTrack):  # type: ignore[misc]  # aiortc sin tipos (Any)
    kind = "video"

    def __init__(
        self,
        source: MediaStreamTrack,
        max_fps: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        super().__init__()
        self._source = source
        self._min_interval = 1.0 / max_fps
        self._clock = clock
        self._last: float | None = None

    async def recv(self) -> VideoFrame:
        while True:
            frame: VideoFrame = await self._source.recv()
            now = self._clock()
            if self._last is None or now - self._last >= self._min_interval:
                self._last = now
                return frame


class IdleTrack(MediaStreamTrack):  # type: ignore[misc]  # aiortc sin tipos (Any)
    """Negro a 1 fps mientras no hay cámara elegida (o antes del `auth`).

    Hace falta una pista desde la respuesta SDP para que `replaceTrack` pueda
    cambiar a la cámara sin renegociar. Y debe producir frames: el emisor de
    aiortc espera en `recv()` de la pista actual, y una que nunca devolviera
    nada bloquearía el cambio. Negro, nunca imagen de cámara: "sin auth válido
    no se envía ningún frame" (spec §3.4).
    """

    kind = "video"

    def __init__(self, interval_s: float = 1.0) -> None:
        super().__init__()
        self._interval = interval_s
        self._pts = 0

    async def recv(self) -> VideoFrame:
        await asyncio.sleep(self._interval)
        frame = VideoFrame.from_ndarray(np.zeros((180, 320, 3), dtype=np.uint8), format="bgr24")
        frame.pts = self._pts
        frame.time_base = fractions.Fraction(1, _VIDEO_CLOCK)
        self._pts += int(self._interval * _VIDEO_CLOCK)
        return frame


@dataclass
class _Entry:
    source: MediaStreamTrack
    relay: MediaRelay
    refs: int


class SourcePool:
    """Una conexión RTSP por cámara vista, compartida entre sus viewers.

    Es una conexión aparte de la de inferencia: el bucle de `worker.py` y su
    muestreo a ~2 fps no se tocan.
    """

    def __init__(self, opener: Callable[[str], MediaStreamTrack], max_fps: float) -> None:
        self._opener = opener
        self._max_fps = max_fps
        self._entries: dict[str, _Entry] = {}
        self._lock = asyncio.Lock()

    async def acquire(self, target: LiveTarget) -> MediaStreamTrack:
        async with self._lock:
            entry = self._entries.get(target.camera_id)
            if entry is None:
                loop = asyncio.get_running_loop()
                try:
                    # Abrir RTSP bloquea (handshake, hasta el timeout de
                    # ffmpeg): al executor, o congelaría a todos los viewers.
                    source = await loop.run_in_executor(None, self._opener, target.uri)
                except Exception as exc:  # cualquier fallo de apertura = no disponible
                    _log.warning("live: cam %s no abre: %s", target.camera_id, exc)
                    raise SourceUnavailable(target.camera_id) from exc
                entry = _Entry(source=source, relay=MediaRelay(), refs=0)
                self._entries[target.camera_id] = entry
            entry.refs += 1
            return FpsLimitedTrack(
                entry.relay.subscribe(entry.source, buffered=False), self._max_fps
            )

    def release(self, camera_id: str) -> None:
        entry = self._entries.get(camera_id)
        if entry is None:
            return
        entry.refs -= 1
        if entry.refs <= 0:
            del self._entries[camera_id]
            entry.source.stop()

    def close_all(self) -> None:
        for entry in self._entries.values():
            entry.source.stop()
        self._entries.clear()


def open_rtsp_track(uri: str) -> MediaStreamTrack:
    """Runtime only. No cubierto por unit tests (requiere stream real)."""
    from aiortc.contrib.media import MediaPlayer

    player = MediaPlayer(
        uri, format="rtsp", options={"rtsp_transport": "tcp", "timeout": "5000000"}
    )
    if player.video is None:
        raise SourceUnavailable(uri)
    track: MediaStreamTrack = player.video
    return track
