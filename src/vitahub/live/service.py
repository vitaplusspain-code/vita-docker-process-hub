from __future__ import annotations

import asyncio
import threading
import time
import uuid
from collections.abc import Callable, Coroutine, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from aiortc import MediaStreamTrack
from cryptography.hazmat.primitives.asymmetric import ec

from vitahub.ingest.rtsp import backoff_delay, next_attempt
from vitahub.live.events import live_view_ended, live_view_started
from vitahub.live.peer import ViewerPeer
from vitahub.live.protocol import (
    AuthMsg,
    SelectMsg,
    closed_message,
    parse_client_message,
    ready_message,
)
from vitahub.live.sessions import Rejected, SessionBook
from vitahub.live.signaling import Signaling, SignalingClosed, SignalingMessage
from vitahub.live.source import LiveTarget, SourcePool, SourceUnavailable, SwitchTrack
from vitahub.live.token import SessionClaims, TokenRejected, verify_token
from vitahub.logging_setup import get_logger
from vitahub.models import Event
from vitahub.sinks.base import EventSink

_log = get_logger("live.service")
# Margen para que el `closed` salga por el DataChannel antes de cerrar el peer:
# si se cierra en el acto, el viewer ve un corte sin motivo.
_CLOSED_FLUSH_S = 0.05


@dataclass
class _Conn:
    peer: ViewerPeer
    started_at: float
    camera: LiveTarget | None = None
    # La pista exacta que dio `SourcePool.acquire`: `release` la necesita a ella,
    # no la cámara (la entrada de la cámara puede haber muerto y reabierto).
    track: MediaStreamTrack | None = None
    # La pista fija del emisor (se crea con el primer select): los cambios de
    # cámara pasan por ella, nunca por `replaceTrack` (ver SwitchTrack).
    switch: SwitchTrack | None = None
    # La última cámara que se llegó a ver: `camera` es "la que se emite ahora"
    # y se vacía si la cámara muere, pero `live_view_ended` debe nombrar la
    # misma cámara que su `live_view_started` (auditoría emparejada).
    last_camera: LiveTarget | None = None
    timers: list[asyncio.TimerHandle] = field(default_factory=list)


class LiveService:
    """Master WebRTC del hogar y árbitro de las reglas de sesión (spec §3.4).

    Corre en su propio hilo con su propio loop: nada de lo que pase aquí
    (señalización caída, un peer roto, una cámara que no abre) puede tocar la
    inferencia ni los workers de cámara.
    """

    def __init__(
        self,
        hub_id: str,
        max_viewers: int,
        keys: Mapping[str, ec.EllipticCurvePublicKey],
        signaling_factory: Callable[[], Signaling],
        sources: SourcePool,
        targets: Callable[[], list[LiveTarget]],
        sink: EventSink,
        clock: Callable[[], float] = time.time,
        auth_timeout_s: float = 5.0,
        connect_timeout_s: float = 30.0,
        tick_s: float = 1.0,
    ) -> None:
        self._hub_id = hub_id
        self._keys = keys
        self._signaling_factory = signaling_factory
        self._sources = sources
        self._targets = targets
        self._sink = sink
        self._clock = clock
        self._auth_timeout = auth_timeout_s
        self._connect_timeout = connect_timeout_s
        self._tick = tick_s
        self._book = SessionBook(max_viewers)
        self._conns: dict[str, _Conn] = {}
        self._by_client: dict[str, str] = {}
        # Tareas lanzadas desde callbacks síncronos (timers): se guardan para
        # que el GC no las recoja a medias y para esperarlas al apagar.
        self._background: set[asyncio.Task[None]] = set()
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop: asyncio.Event | None = None
        # `stop()` puede llegar antes de que exista el loop: el aviso no se pierde.
        self._stop_requested = threading.Event()

    # --- ciclo de vida ------------------------------------------------------

    def start(self) -> None:
        ready = threading.Event()

        def _main() -> None:
            async def _run() -> None:
                self._loop = asyncio.get_running_loop()
                self._stop = asyncio.Event()
                if self._stop_requested.is_set():
                    self._stop.set()
                ready.set()
                await self.run(self._stop)

            try:
                asyncio.run(_run())
            except Exception:  # noqa: BLE001 — el directo caído no tumba el hub
                _log.exception("live: el servicio terminó por un error")
            finally:
                ready.set()  # si `_run` falló antes de avisar, `start` no espera en vano

        self._thread = threading.Thread(target=_main, name="live", daemon=True)
        self._thread.start()
        ready.wait(timeout=5.0)

    def stop(self, timeout_s: float = 5.0) -> None:
        self._stop_requested.set()
        if self._loop is not None and self._stop is not None:
            try:
                self._loop.call_soon_threadsafe(self._stop.set)
            except RuntimeError:
                pass  # el loop ya terminó: no queda nada que parar
        if self._thread is not None:
            self._thread.join(timeout=timeout_s)
            if self._thread.is_alive():
                _log.warning("live: el servicio no paró en %.1f s", timeout_s)

    async def run(self, stop: asyncio.Event) -> None:
        ticker = asyncio.ensure_future(self._tick_loop(stop))
        signaling = asyncio.ensure_future(self._signaling_loop(stop))
        await stop.wait()
        # Primero dejar de aceptar ofertas: un viewer que llegara durante el
        # apagado quedaría sin `shutdown` ni `live_view_ended`.
        signaling.cancel()
        ticker.cancel()
        await asyncio.gather(signaling, ticker, return_exceptions=True)
        for conn_id in list(self._conns):
            await self._close(conn_id, "shutdown")
        if self._background:
            await asyncio.gather(*self._background, return_exceptions=True)
        self._sources.close_all()

    def _spawn(self, coro: Coroutine[Any, Any, None]) -> None:
        task = asyncio.ensure_future(coro)
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    # --- señalización -------------------------------------------------------

    async def _signaling_loop(self, stop: asyncio.Event) -> None:
        attempt = 0
        while not stop.is_set():
            signaling = self._signaling_factory()
            try:
                await signaling.open()
                _log.info("live: conectado al canal de señalización")
                while True:
                    msg = await signaling.recv()
                    # El backoff se reinicia solo con un canal que de verdad
                    # entrega: un `open` que acepta y cae al instante no cuenta.
                    attempt = 0
                    if msg.type in ("GO_AWAY", "RECONNECT_ICE_SERVER"):
                        _log.info("live: KVS pide reconectar (%s)", msg.type)
                        break
                    await self._on_signal(signaling, msg)
            except asyncio.CancelledError:
                await self._close_signaling(signaling)
                raise
            except SignalingClosed as exc:
                _log.warning("live: señalización cerrada (%s)", exc)
            except Exception:  # noqa: BLE001 — credenciales, red, AWS: se reintenta
                _log.exception("live: fallo en la señalización")
            await self._close_signaling(signaling)
            # Los viewers ya conectados siguen: el P2P no pasa por aquí.
            delay = backoff_delay(attempt)
            attempt = next_attempt(attempt)
            try:
                await asyncio.wait_for(stop.wait(), delay)
            except TimeoutError:
                pass

    @staticmethod
    async def _close_signaling(signaling: Signaling) -> None:
        try:
            await signaling.close()
        except Exception as exc:  # noqa: BLE001 — cerrar algo ya roto no importa
            _log.debug("live: error cerrando la señalización: %s", exc)

    async def _on_signal(self, signaling: Signaling, msg: SignalingMessage) -> None:
        if msg.type == "SDP_OFFER":
            await self._on_offer(signaling, msg)
        elif msg.type == "ICE_CANDIDATE":
            conn_id = self._by_client.get(msg.client_id)
            conn = self._conns.get(conn_id) if conn_id is not None else None
            if conn is not None:
                # El peer descarta (con log) los candidatos malformados: no lanza.
                await conn.peer.add_remote_candidate(msg.payload)

    async def _on_offer(self, signaling: Signaling, msg: SignalingMessage) -> None:
        previous = self._by_client.get(msg.client_id)
        if previous is not None and self._book.claims_for(previous) is None:
            # El mismo navegador reconectando antes de autenticarse: la
            # conexión vieja ya no sirve. Si estaba autenticada no se toca: una
            # oferta no prueba nada (el client_id lo elige el viewer) y cortar
            # sin `closed` ni `live_view_ended` dejaría a alguien sin vídeo.
            # La sustituirá el `auth` de la nueva (regla 4) o morirá por ICE;
            # solo se remapea `_by_client` a la nueva conexión, abajo.
            await self._close(previous, None)
        conn_id = uuid.uuid4().hex
        peer = ViewerPeer(
            conn_id, msg.client_id, signaling.ice_servers,
            self._on_open, self._on_message, self._on_peer_closed,
        )
        conn = _Conn(peer=peer, started_at=self._clock())
        self._conns[conn_id] = conn
        self._by_client[msg.client_id] = conn_id
        # Un peer que negocia y nunca abre el canal no debe quedarse ocupando
        # memoria y un puerto UDP para siempre.
        loop = asyncio.get_running_loop()
        conn.timers.append(loop.call_later(self._connect_timeout, self._expire_unauth, conn_id))
        try:
            answer = await peer.answer(str(msg.payload.get("sdp", "")))
            await signaling.send_answer(msg.client_id, answer)
        except SignalingClosed:
            # Sin respuesta entregada el peer no llegará a conectar.
            await self._close(conn_id, None)
            raise
        except Exception:  # noqa: BLE001 — una oferta mala no tumba la señalización
            _log.exception("live: oferta inválida de %s", msg.client_id)
            await self._close(conn_id, None)

    # --- callbacks del peer -------------------------------------------------

    def _on_open(self, peer: ViewerPeer) -> None:
        conn = self._conns.get(peer.conn_id)
        if conn is not None:
            # El plazo del `auth` corre desde que abre el canal (spec §3.4).
            loop = asyncio.get_running_loop()
            conn.timers.append(
                loop.call_later(self._auth_timeout, self._expire_unauth, peer.conn_id)
            )

    def _expire_unauth(self, conn_id: str) -> None:
        if conn_id in self._conns and self._book.claims_for(conn_id) is None:
            self._spawn(self._close(conn_id, "invalid"))

    async def _on_message(self, peer: ViewerPeer, raw: str | bytes) -> None:
        conn_id = peer.conn_id
        if conn_id not in self._conns:
            return
        msg = parse_client_message(raw)
        if isinstance(msg, AuthMsg):
            await self._on_auth(conn_id, msg)
        elif isinstance(msg, SelectMsg):
            await self._on_select(conn_id, msg)

    async def _on_auth(self, conn_id: str, msg: AuthMsg) -> None:
        verdict = verify_token(msg.token, self._keys, self._hub_id, self._clock())
        if isinstance(verdict, TokenRejected):
            _log.info("live: token rechazado (%s)", verdict.reason)
            await self._close(conn_id, "invalid")
            return
        # SessionBook saca a los sustituidos del libro antes de devolver: sus
        # claims se guardan aquí para que su live_view_ended no se pierda.
        before = {c: self._book.claims_for(c) for c in list(self._conns)}
        result = self._book.admit(conn_id, verdict)
        if isinstance(result, Rejected):
            await self._close(conn_id, result.reason)
            return
        for old in result.replaced:
            await self._close(old, "replaced", claims=before.get(old))
        conn = self._conns.get(conn_id)
        if conn is None:
            return  # el viewer se fue mientras se cerraba a los sustituidos
        cameras = [(t.camera_id, t.name) for t in self._targets()]
        conn.peer.send(ready_message(cameras))

    async def _on_select(self, conn_id: str, msg: SelectMsg) -> None:
        claims = self._book.claims_for(conn_id)
        conn = self._conns.get(conn_id)
        if claims is None or conn is None:
            return  # select antes del auth: se ignora, el plazo del auth ya corre
        if claims.exp <= self._clock():
            return  # caducada a falta de tick: no se abre cámara, el tick la cierra
        target = next((t for t in self._targets() if t.camera_id == msg.camera_id), None)
        if target is None:
            conn.peer.send(closed_message("camera_unavailable"))
            return
        try:
            track = await self._sources.acquire(target)
        except SourceUnavailable:
            # No cierra la conexión (spec §3.3): el viewer puede elegir otra.
            conn.peer.send(closed_message("camera_unavailable"))
            return
        if self._conns.get(conn_id) is not conn:
            # La conexión se cerró durante la apertura: nadie verá esta pista.
            self._sources.release(track)
            return
        if conn.switch is None:
            # Una sola vez por conexión: la lectura en curso es de la IdleTrack
            # inicial, que nunca se para, así que el emisor pasa a esta en ~1 s.
            conn.switch = SwitchTrack(
                on_source_ended=lambda: self._on_source_ended(conn_id),
            )
            conn.peer.set_track(conn.switch)
        # Primero la nueva y luego soltar la vieja: `set_source` ya canceló la
        # lectura pendiente de la vieja, así que pararla no llega al emisor.
        old = conn.track
        conn.switch.set_source(track)
        conn.track = track
        conn.camera = target
        conn.last_camera = target
        if old is not None:
            self._sources.release(old)
        self._emit(
            live_view_started(self._hub_id, claims, target.camera_id, target.name, self._now())
        )

    def _on_source_ended(self, conn_id: str) -> None:
        # La cámara murió a mitad de directo: la SwitchTrack ya ha vuelto al
        # negro. Se avisa sin cerrar (spec §3.3) y el viewer puede elegir otra.
        conn = self._conns.get(conn_id)
        if conn is None or conn.track is None:
            return
        self._sources.release(conn.track)
        conn.track = None
        conn.camera = None
        conn.peer.send(closed_message("camera_unavailable"))

    async def _on_peer_closed(self, peer: ViewerPeer) -> None:
        await self._close(peer.conn_id, None, ended_reason="disconnected")

    # --- cierre y caducidad -------------------------------------------------

    async def _tick_loop(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            await asyncio.sleep(self._tick)
            try:
                for conn_id in self._book.expired(self._clock()):
                    # Cada cierre espera: una renovación puede haber llegado
                    # entre tanto y no debe cortarse a quien ya renovó.
                    claims = self._book.claims_for(conn_id)
                    if claims is None or claims.exp > self._clock():
                        continue
                    await self._close(conn_id, "expired")
            except Exception:  # noqa: BLE001 — un fallo puntual no debe parar la caducidad
                _log.exception("live: error revisando caducidades")

    async def _close(
        self,
        conn_id: str,
        reason: str | None,
        claims: SessionClaims | None = None,
        ended_reason: str | None = None,
    ) -> None:
        # Idempotente: el `pop` decide quién cierra; cualquier otro camino
        # (timer, peer cerrado, apagado) que llegue después no hace nada.
        conn = self._conns.pop(conn_id, None)
        if conn is None:
            return
        if self._by_client.get(conn.peer.client_id) == conn_id:
            del self._by_client[conn.peer.client_id]
        for timer in conn.timers:
            timer.cancel()
        claims = self._book.release(conn_id) or claims
        if conn.switch is not None:
            # Antes que la fuente: parada la SwitchTrack, soltar la fuente ya
            # no se confunde con una cámara caída (no hay `camera_unavailable`).
            conn.switch.stop()
        if conn.track is not None:
            self._sources.release(conn.track)
        if claims is not None:
            cam_id = conn.last_camera.camera_id if conn.last_camera else ""
            cam_name = conn.last_camera.name if conn.last_camera else ""
            self._emit(live_view_ended(
                self._hub_id, claims, cam_id, cam_name, ended_reason or reason or "closed",
                self._clock() - conn.started_at, self._now(),
            ))
        if reason is not None:
            conn.peer.send(closed_message(reason))
            await asyncio.sleep(_CLOSED_FLUSH_S)
        await conn.peer.close()

    def _emit(self, event: Event) -> None:
        try:
            self._sink.emit(event)
        except Exception:  # noqa: BLE001 — la auditoría caída no corta el vídeo
            _log.exception("live: no se pudo emitir el evento")

    def _now(self) -> datetime:
        return datetime.fromtimestamp(self._clock(), tz=UTC)
