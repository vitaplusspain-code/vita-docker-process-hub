from __future__ import annotations

import asyncio
import fractions
import time
from collections.abc import Callable
from dataclasses import dataclass, field

import numpy as np
from aiortc import MediaStreamTrack
from aiortc.mediastreams import MediaStreamError
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

    def stop(self) -> None:
        # Soltar la suscripción: sin esto el suscriptor seguiría en el fan-out
        # tras cerrarse el viewer.
        super().stop()
        self._source.stop()


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


# Origen común a todas las SwitchTrack: el fan-out entrega el *mismo* objeto
# frame a varios viewers y cada uno le reescribe el pts; con un reloj compartido
# todos escriben (casi) el mismo valor y ningún emisor ve el tiempo retroceder.
_PTS_ORIGIN = time.monotonic()


class SwitchTrack(MediaStreamTrack):  # type: ignore[misc]  # aiortc sin tipos (Any)
    """La pista que ve el emisor de aiortc, una por conexión: nunca cambia.

    `replaceTrack` no sirve para pasar de una cámara a otra: el emisor sigue
    esperando en `recv()` de la pista vieja, y al soltarla esa espera lanza
    `MediaStreamError`, el emisor sale de su bucle para siempre y además para
    la pista nueva. Aquí el cambio es interno: `set_source` despierta el `recv`
    pendiente, se cancela la lectura de la fuente vieja y se sigue con la nueva.
    Sin fuente (o si la fuente muere) entrega negro; solo termina con `stop()`.
    """

    kind = "video"

    def __init__(
        self,
        on_source_ended: Callable[[], None] | None = None,
        idle_interval_s: float = 1.0,
    ) -> None:
        super().__init__()
        self._on_source_ended = on_source_ended
        self._idle = IdleTrack(interval_s=idle_interval_s)
        self._source: MediaStreamTrack | None = None
        self._changed = asyncio.Event()
        self._last_pts = -1

    def set_source(self, track: MediaStreamTrack | None) -> None:
        self._source = track
        self._changed.set()

    def stop(self) -> None:
        # Solo ella misma: la fuente es del servicio, que la devuelve al pool.
        super().stop()
        self._changed.set()

    async def recv(self) -> VideoFrame:
        while True:
            if self.readyState != "live":
                raise MediaStreamError
            source = self._source
            self._changed.clear()
            reading = asyncio.ensure_future((source or self._idle).recv())
            # Si al final se descarta, que su excepción no quede "sin recoger".
            reading.add_done_callback(_consume)
            waking = asyncio.ensure_future(self._changed.wait())
            try:
                await asyncio.wait({reading, waking}, return_when=asyncio.FIRST_COMPLETED)
            finally:
                waking.cancel()
                if not reading.done():
                    reading.cancel()
            if not reading.done() or reading.cancelled():
                continue  # cambio de fuente o stop: se vuelve a mirar
            try:
                frame: VideoFrame = reading.result()
            except Exception as exc:  # (sin noqa: re-lanza) cualquier fallo de la fuente = caída
                if source is None:
                    raise  # la IdleTrack no falla: si lo hace es un bug, que se vea
                if self._source is source:
                    _log.info("live: la fuente terminó (%s), se pasa a negro", type(exc).__name__)
                    self._source = None
                    self._notify_ended()
                continue
            return self._stamp(frame)

    def _notify_ended(self) -> None:
        if self.readyState != "live" or self._on_source_ended is None:
            return
        try:
            self._on_source_ended()
        except Exception:  # noqa: BLE001 — subiría al emisor de aiortc y lo pararía
            _log.exception("live: error en on_source_ended")

    def _stamp(self, frame: VideoFrame) -> VideoFrame:
        # Cada fuente trae su propio reloj (pts y time_base sin relación entre
        # cámaras): el codificador debe ver siempre un tiempo que avanza.
        pts = int((time.monotonic() - _PTS_ORIGIN) * _VIDEO_CLOCK)
        pts = max(pts, self._last_pts + 1)
        self._last_pts = pts
        frame.pts = pts
        frame.time_base = fractions.Fraction(1, _VIDEO_CLOCK)
        return frame


def _consume(task: asyncio.Future[VideoFrame]) -> None:
    if not task.cancelled():
        task.exception()


class _Subscriber(MediaStreamTrack):  # type: ignore[misc]  # aiortc sin tipos (Any)
    """Un viewer del fan-out: solo guarda el último frame.

    Un viewer lento se salta frames en vez de acumularlos (memoria acotada y
    sin retraso creciente). Al terminar el fan-out, `recv()` lanza
    `MediaStreamError` en vez de colgarse.
    """

    kind = "video"

    def __init__(self, on_stop: Callable[[_Subscriber], None]) -> None:
        super().__init__()
        self._on_stop = on_stop
        self._latest: VideoFrame | None = None
        self._event = asyncio.Event()
        self._ended = False

    def push(self, frame: VideoFrame) -> None:
        self._latest = frame
        self._event.set()

    def end(self) -> None:
        self._ended = True
        self._event.set()

    async def recv(self) -> VideoFrame:
        while True:
            if self._latest is not None:
                frame, self._latest = self._latest, None
                return frame
            if self._ended:
                raise MediaStreamError
            self._event.clear()
            await self._event.wait()

    def stop(self) -> None:
        super().stop()
        self._on_stop(self)
        self.end()


@dataclass
class _Entry:
    camera_id: str
    source: MediaStreamTrack
    refs: int = 0
    subs: set[_Subscriber] = field(default_factory=set)
    reader: asyncio.Task[None] | None = None


def _stop_source(source: MediaStreamTrack) -> None:
    """Parar la fuente sin bloquear el bucle de eventos.

    `PlayerStreamTrack.stop()` hace un `thread.join()` síncrono que puede
    tardar hasta el timeout RTSP: va al executor y solo se registra el fallo.
    """

    def _safe() -> None:
        try:
            source.stop()
        except Exception as exc:  # noqa: BLE001 — parar es best-effort, nunca debe propagar
            _log.warning("live: error parando la fuente: %s", exc)

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        _safe()
        return
    loop.run_in_executor(None, _safe)


class SourcePool:
    """Una conexión RTSP por cámara vista, compartida entre sus viewers.

    Es una conexión aparte de la de inferencia: el bucle de `worker.py` y su
    muestreo a ~2 fps no se tocan. El reparto a los viewers es propio (no
    `MediaRelay`): al cerrar la fuente hay que poder terminar a los suscriptores,
    y el relay dejaba su lector bloqueado en `recv()` para siempre.
    """

    def __init__(self, opener: Callable[[str], MediaStreamTrack], max_fps: float) -> None:
        self._opener = opener
        self._max_fps = max_fps
        self._entries: dict[str, _Entry] = {}
        # Pista entregada -> su entrada: `release` actúa sobre la entrada de
        # *esa* pista, no sobre la que haya ahora para la cámara (que puede ser
        # otra si la anterior murió y se reabrió).
        self._tracks: dict[MediaStreamTrack, _Entry] = {}
        self._lock = asyncio.Lock()

    async def acquire(self, target: LiveTarget) -> MediaStreamTrack:
        async with self._lock:
            entry = self._entries.get(target.camera_id)
            if entry is None:
                loop = asyncio.get_running_loop()
                # Abrir RTSP bloquea (handshake, hasta el timeout de ffmpeg):
                # al executor, o congelaría a todos los viewers. `shield` deja
                # terminar la apertura aunque nos cancelen, para poder cerrarla.
                fut = asyncio.ensure_future(loop.run_in_executor(None, self._opener, target.uri))
                try:
                    source = await asyncio.shield(fut)
                except asyncio.CancelledError:
                    fut.add_done_callback(self._stop_orphan)
                    raise
                except Exception as exc:  # (sin noqa: BLE001 no salta al re-lanzar) cualquier fallo de apertura = no disponible
                    _log.warning("live: cam %s no abre: %s", target.camera_id, exc)
                    raise SourceUnavailable(target.camera_id) from exc
                entry = _Entry(camera_id=target.camera_id, source=source)
                entry.reader = asyncio.create_task(self._pump(target.camera_id, entry))
                self._entries[target.camera_id] = entry
            entry.refs += 1
            sub = _Subscriber(entry.subs.discard)
            entry.subs.add(sub)
            track = FpsLimitedTrack(sub, self._max_fps)
            self._tracks[track] = entry
            return track

    @staticmethod
    def _stop_orphan(fut: asyncio.Future[MediaStreamTrack]) -> None:
        # La apertura terminó tras cancelarse `acquire`: nadie la usará.
        if not fut.cancelled() and fut.exception() is None:
            _stop_source(fut.result())

    async def _pump(self, camera_id: str, entry: _Entry) -> None:
        try:
            while True:
                frame: VideoFrame = await entry.source.recv()
                for sub in list(entry.subs):
                    sub.push(frame)
                # Una fuente que no suspende no debe acaparar el bucle.
                await asyncio.sleep(0)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — la fuente murió (MediaStreamError, ffmpeg...)
            _log.warning("live: cam %s se quedó sin vídeo: %s", camera_id, exc)
            if self._entries.get(camera_id) is entry:
                del self._entries[camera_id]
            self._teardown(entry)

    @staticmethod
    def _teardown(entry: _Entry) -> None:
        if entry.reader is not None and entry.reader is not asyncio.current_task():
            entry.reader.cancel()
        for sub in list(entry.subs):
            sub.end()
        entry.subs.clear()
        _stop_source(entry.source)

    def release(self, track: MediaStreamTrack) -> None:
        """Devuelve la pista que dio `acquire` (la misma instancia).

        También la para: quien llama solo tiene que hacer `release`. Es
        idempotente, y si la entrada de la pista ya murió o fue sustituida no
        toca la entrada actual de la cámara.
        """
        entry = self._tracks.pop(track, None)
        if entry is None:
            return
        track.stop()
        entry.refs -= 1
        if entry.refs <= 0 and self._entries.get(entry.camera_id) is entry:
            del self._entries[entry.camera_id]
            self._teardown(entry)

    def close_all(self) -> None:
        entries = list(self._entries.values())
        self._entries.clear()
        self._tracks.clear()
        for entry in entries:
            self._teardown(entry)


def open_rtsp_track(uri: str) -> MediaStreamTrack:
    """Runtime only. No cubierto por unit tests (requiere stream real)."""
    from aiortc.contrib.media import MediaPlayer

    player = MediaPlayer(
        uri, format="rtsp", options={"rtsp_transport": "tcp", "timeout": "5000000"}
    )
    if player.audio is not None:
        # Nadie lee el audio y su cola es ilimitada: el worker del player la
        # llenaría sin fin (fuga de memoria). Se para nada más abrir.
        player.audio.stop()
    if player.video is None:
        raise SourceUnavailable(uri)
    track: MediaStreamTrack = player.video
    return track
