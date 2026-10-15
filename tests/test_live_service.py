import asyncio
import time
from collections.abc import Callable

import jwt
from aiortc import MediaStreamTrack
from aiortc.mediastreams import MediaStreamError
from av import VideoFrame
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from tests.live_viewer import LocalViewer
from vitahub.live.service import LiveService
from vitahub.live.signaling import SignalingClosed, SignalingMessage
from vitahub.live.source import IdleTrack, LiveTarget, SourcePool
from vitahub.live.token import load_session_keys
from vitahub.models import Event
from vitahub.sinks.base import EventSink

_PRIV = ec.generate_private_key(ec.SECP256R1())
_KEYS = load_session_keys(
    {"k1": _PRIV.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    ).decode()}
)
_TARGETS = [LiveTarget("a", "salón", "rtsp://a"), LiveTarget("b", "cocina", "rtsp://b")]
# "c" funciona; "d" entrega unos frames y luego la cámara muere (MediaStreamError).
_MORE_TARGETS = [
    *_TARGETS, LiveTarget("c", "pasillo", "rtsp://c"), LiveTarget("d", "garaje", "rtsp://d")
]
# URIs que el pool intentó abrir: prueba de que sin `auth` no se toca ninguna cámara.
_OPENED: list[str] = []
# La pista inicial (IdleTrack del peer) va a 1 fps; la de cámara a ~15 fps. Pedir
# más frames de los que el negro puede dar en el plazo prueba que llega la cámara.
_CAMERA_FRAMES = 10


def _token(sub: str, sid: str = "s", exp: float | None = None) -> str:
    now = time.time()
    return jwt.encode(
        {"sid": sid, "sub": sub, "hub": "hub-1", "iat": int(now), "exp": exp or now + 600},
        _PRIV, algorithm="ES256", headers={"kid": "k1"},
    )


class _Sink(EventSink):
    def __init__(self) -> None:
        self.events: list[Event] = []

    def emit(self, event: Event) -> None:
        self.events.append(event)

    def close(self) -> None:
        pass


class _FakeSignaling:
    """Lo que haría KVS, en memoria: entrega ofertas y recoge respuestas."""

    def __init__(self) -> None:
        self.ice_servers: list[object] = []
        self.inbox: asyncio.Queue[SignalingMessage | None] = asyncio.Queue()
        self.answers: dict[str, asyncio.Future[str]] = {}
        self.opens = 0

    async def open(self) -> None:
        self.opens += 1

    async def recv(self) -> SignalingMessage:
        msg = await self.inbox.get()
        if msg is None:
            raise SignalingClosed("cortado")
        return msg

    async def fresh_ice_servers(self) -> list[object]:
        return self.ice_servers

    async def send_answer(self, client_id: str, sdp: str) -> None:
        self.answers[client_id].set_result(sdp)

    async def close(self) -> None:
        pass

    async def connect(self, viewer: LocalViewer, client_id: str) -> None:
        self.answers[client_id] = asyncio.get_running_loop().create_future()
        await self.inbox.put(
            SignalingMessage("SDP_OFFER", client_id, {"type": "offer", "sdp": await viewer.offer()})
        )
        await viewer.accept(await asyncio.wait_for(self.answers[client_id], 10))
        await asyncio.wait_for(viewer.opened.wait(), 10)


class _DyingSource(IdleTrack):
    """Cámara que se cae a mitad de directo."""

    def __init__(self, frames: int) -> None:
        super().__init__(interval_s=0.02)
        self._left = frames

    async def recv(self) -> VideoFrame:
        if self._left <= 0:
            raise MediaStreamError
        self._left -= 1
        return await super().recv()


def _opener(uri: str) -> MediaStreamTrack:
    _OPENED.append(uri)
    if uri == "rtsp://b":
        raise OSError("cámara apagada")
    if uri == "rtsp://d":
        return _DyingSource(frames=20)
    return IdleTrack(interval_s=0.02)


class _BrokenSink(_Sink):
    def emit(self, event: Event) -> None:
        raise RuntimeError("uplink caído")


async def _service(  # type: ignore[no-untyped-def]
    max_viewers: int = 3,
    clock=time.time,
    auth_timeout_s: float = 5.0,
    targets: list[LiveTarget] = _TARGETS,
    sink: _Sink | None = None,
):
    _OPENED.clear()
    sig, stop = _FakeSignaling(), asyncio.Event()
    sink = sink or _Sink()
    svc = LiveService(
        hub_id="hub-1", max_viewers=max_viewers, keys=_KEYS,
        signaling_factory=lambda: sig, sources=SourcePool(_opener, max_fps=15),
        targets=lambda: targets, sink=sink, clock=clock,
        auth_timeout_s=auth_timeout_s, tick_s=0.05,
    )
    task = asyncio.ensure_future(svc.run(stop))
    return svc, sig, sink, stop, task


async def _auth(sig: _FakeSignaling, sub: str, client_id: str, **kw: object) -> LocalViewer:
    viewer = LocalViewer()
    await sig.connect(viewer, client_id)
    viewer.send({"type": "auth", "token": _token(sub, **kw)})  # type: ignore[arg-type]
    return viewer


async def _until(cond: Callable[[], bool], timeout_s: float = 5.0) -> bool:
    """Sondea `cond` hasta `timeout_s`: replaceTrack puede tardar ~1 s (spec del peer)."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if cond():
            return True
        await asyncio.sleep(0.05)
    return cond()


def test_valid_auth_gets_ready_and_video():
    async def scenario() -> None:
        _, sig, sink, stop, task = await _service()
        viewer = await _auth(sig, "acc-1", "v1")
        assert await viewer.next() == {
            "type": "ready",
            "cameras": [{"id": "a", "name": "salón"}, {"id": "b", "name": "cocina"}],
        }
        baseline = viewer.frames
        viewer.send({"type": "select", "camera_id": "a"})
        assert await _until(lambda: viewer.frames > baseline + _CAMERA_FRAMES)
        assert [e.type for e in sink.events] == ["live_view_started"]
        assert sink.events[0].payload == {"sid": "s", "account_id": "acc-1", "camera_id": "a"}
        stop.set()
        await task

    asyncio.run(scenario())


def test_no_auth_closes_invalid_without_camera_frames():
    async def scenario() -> None:
        _, sig, sink, stop, task = await _service(auth_timeout_s=0.3)
        viewer = LocalViewer()
        await sig.connect(viewer, "v1")
        assert await viewer.next() == {"type": "closed", "reason": "invalid"}
        assert sink.events == []  # nunca hubo sesión, no hay auditoría que emitir
        assert _OPENED == []  # ninguna cámara se abrió para un peer sin auth
        stop.set()
        await task

    asyncio.run(scenario())


def test_select_before_auth_opens_no_camera():
    async def scenario() -> None:
        _, sig, sink, stop, task = await _service(auth_timeout_s=0.5)
        viewer = LocalViewer()
        await sig.connect(viewer, "v1")
        viewer.send({"type": "select", "camera_id": "a"})
        assert await viewer.next() == {"type": "closed", "reason": "invalid"}
        assert _OPENED == []
        assert sink.events == []
        stop.set()
        await task

    asyncio.run(scenario())


def test_bad_token_closes_invalid():
    async def scenario() -> None:
        _, sig, _, stop, task = await _service()
        viewer = LocalViewer()
        await sig.connect(viewer, "v1")
        viewer.send({"type": "auth", "token": "basura"})
        assert await viewer.next() == {"type": "closed", "reason": "invalid"}
        stop.set()
        await task

    asyncio.run(scenario())


def test_same_account_second_device_replaces_first():
    async def scenario() -> None:
        _, sig, sink, stop, task = await _service()
        first = await _auth(sig, "acc-1", "v1", sid="s1")
        await first.next()  # ready
        second = await _auth(sig, "acc-1", "v2", sid="s2")
        assert await first.next() == {"type": "closed", "reason": "replaced"}
        assert (await second.next())["type"] == "ready"
        ended = [e for e in sink.events if e.type == "live_view_ended"]
        assert ended[0].payload["reason"] == "replaced"
        assert ended[0].payload["sid"] == "s1"
        stop.set()
        await task

    asyncio.run(scenario())


def test_limit_rejects_other_account():
    async def scenario() -> None:
        _, sig, _, stop, task = await _service(max_viewers=1)
        first = await _auth(sig, "acc-1", "v1")
        await first.next()
        second = await _auth(sig, "acc-2", "v2")
        assert await second.next() == {"type": "closed", "reason": "limit"}
        stop.set()
        await task

    asyncio.run(scenario())


def test_expiry_closes_and_renewal_extends():
    async def scenario() -> None:
        now = [time.time()]
        _, sig, sink, stop, task = await _service(clock=lambda: now[0])
        viewer = await _auth(sig, "acc-1", "v1", exp=now[0] + 10)
        await viewer.next()
        viewer.send({"type": "auth", "token": _token("acc-1", sid="s2", exp=now[0] + 20)})
        assert (await viewer.next())["type"] == "ready"  # renovación: ready otra vez
        now[0] += 15  # pasado el primer exp, dentro del segundo
        await asyncio.sleep(0.3)
        assert viewer.inbox.empty()
        now[0] += 10
        assert await viewer.next() == {"type": "closed", "reason": "expired"}
        ended = [e for e in sink.events if e.type == "live_view_ended"]
        assert [e.payload["reason"] for e in ended] == ["expired"]
        stop.set()
        await task

    asyncio.run(scenario())


def test_select_unavailable_camera_keeps_connection():
    async def scenario() -> None:
        _, sig, _, stop, task = await _service()
        viewer = await _auth(sig, "acc-1", "v1")
        await viewer.next()
        viewer.send({"type": "select", "camera_id": "b"})
        assert await viewer.next() == {"type": "closed", "reason": "camera_unavailable"}
        baseline = viewer.frames
        viewer.send({"type": "select", "camera_id": "a"})
        assert await _until(lambda: viewer.frames > baseline + _CAMERA_FRAMES)
        stop.set()
        await task

    asyncio.run(scenario())


def test_signaling_drop_keeps_existing_viewer():
    async def scenario() -> None:
        _, sig, _, stop, task = await _service()
        viewer = await _auth(sig, "acc-1", "v1")
        await viewer.next()
        viewer.send({"type": "select", "camera_id": "a"})
        await sig.inbox.put(None)  # el WSS se corta
        assert await _until(lambda: sig.opens >= 2)  # backoff del primer reintento (1 s)
        frames = viewer.frames
        # El P2P no depende de la señalización: la cámara sigue llegando.
        assert await _until(lambda: viewer.frames > frames + _CAMERA_FRAMES)
        assert viewer.inbox.empty()
        stop.set()
        await task

    asyncio.run(scenario())


def test_stop_sends_shutdown_and_emits_ended():
    async def scenario() -> None:
        _, sig, sink, stop, task = await _service()
        viewer = await _auth(sig, "acc-1", "v1")
        await viewer.next()
        stop.set()
        assert await viewer.next() == {"type": "closed", "reason": "shutdown"}
        await asyncio.wait_for(task, 5)
        assert sink.events[-1].type == "live_view_ended"
        assert sink.events[-1].payload["reason"] == "shutdown"

    asyncio.run(scenario())


def test_switching_cameras_keeps_video_flowing():
    async def scenario() -> None:
        _, sig, sink, stop, task = await _service(targets=_MORE_TARGETS)
        viewer = await _auth(sig, "acc-1", "v1")
        await viewer.next()
        for camera_id in ("a", "c", "a"):
            baseline = viewer.frames
            viewer.send({"type": "select", "camera_id": camera_id})
            assert await _until(lambda b=baseline: viewer.frames > b + _CAMERA_FRAMES)
            # Y sigue llegando: un emisor muerto tras el cambio se pararía aquí.
            frames = viewer.frames
            await asyncio.sleep(2.0)
            assert viewer.frames > frames + _CAMERA_FRAMES
        assert viewer.inbox.empty()
        started = [e.payload["camera_id"] for e in sink.events if e.type == "live_view_started"]
        assert started == ["a", "c", "a"]
        stop.set()
        await task

    asyncio.run(scenario())


def test_camera_death_notifies_and_reselect_resumes():
    async def scenario() -> None:
        _, sig, sink, stop, task = await _service(targets=_MORE_TARGETS)
        viewer = await _auth(sig, "acc-1", "v1")
        await viewer.next()
        viewer.send({"type": "select", "camera_id": "d"})
        assert await viewer.next() == {"type": "closed", "reason": "camera_unavailable"}
        baseline = viewer.frames
        viewer.send({"type": "select", "camera_id": "a"})
        assert await _until(lambda: viewer.frames > baseline + _CAMERA_FRAMES)
        assert not [e for e in sink.events if e.type == "live_view_ended"]
        stop.set()
        await task

    asyncio.run(scenario())


def test_ended_after_camera_death_names_the_dead_camera():
    async def scenario() -> None:
        _, sig, sink, stop, task = await _service(targets=_MORE_TARGETS)
        viewer = await _auth(sig, "acc-1", "v1")
        await viewer.next()
        viewer.send({"type": "select", "camera_id": "d"})
        assert await viewer.next() == {"type": "closed", "reason": "camera_unavailable"}
        stop.set()
        assert await viewer.next() == {"type": "closed", "reason": "shutdown"}
        await asyncio.wait_for(task, 5)
        started = [e for e in sink.events if e.type == "live_view_started"]
        ended = [e for e in sink.events if e.type == "live_view_ended"]
        assert [(e.camera_id, e.camera_name) for e in started] == [("d", "garaje")]
        assert [(e.camera_id, e.camera_name) for e in ended] == [("d", "garaje")]

    asyncio.run(scenario())


def test_renewal_does_not_reemit_started():
    async def scenario() -> None:
        _, sig, sink, stop, task = await _service()
        viewer = await _auth(sig, "acc-1", "v1")
        await viewer.next()
        viewer.send({"type": "select", "camera_id": "a"})
        assert await _until(lambda: any(e.type == "live_view_started" for e in sink.events))
        viewer.send({"type": "auth", "token": _token("acc-1", sid="s2")})
        assert (await viewer.next())["type"] == "ready"
        assert [e.type for e in sink.events] == ["live_view_started"]
        stop.set()
        await task

    asyncio.run(scenario())


def test_failing_sink_does_not_break_auth_or_select():
    async def scenario() -> None:
        _, sig, _, stop, task = await _service(sink=_BrokenSink())
        viewer = await _auth(sig, "acc-1", "v1")
        assert (await viewer.next())["type"] == "ready"
        baseline = viewer.frames
        viewer.send({"type": "select", "camera_id": "a"})
        assert await _until(lambda: viewer.frames > baseline + _CAMERA_FRAMES)
        stop.set()
        assert await viewer.next() == {"type": "closed", "reason": "shutdown"}
        await asyncio.wait_for(task, 5)

    asyncio.run(scenario())


def test_stop_before_loop_exists_is_not_lost():
    svc = LiveService(
        hub_id="hub-1", max_viewers=3, keys=_KEYS,
        signaling_factory=_FakeSignaling, sources=SourcePool(_opener, max_fps=15),
        targets=lambda: _TARGETS, sink=_Sink(), tick_s=0.05,
    )
    svc.stop(timeout_s=0.1)  # aún no hay hilo ni loop
    svc.start()
    assert svc._thread is not None
    svc._thread.join(timeout=2.0)
    assert not svc._thread.is_alive()


def test_start_and_stop_in_own_thread():
    sig, sink = _FakeSignaling(), _Sink()
    svc = LiveService(
        hub_id="hub-1", max_viewers=3, keys=_KEYS,
        signaling_factory=lambda: sig, sources=SourcePool(_opener, max_fps=15),
        targets=lambda: _TARGETS, sink=sink, tick_s=0.05,
    )
    svc.start()
    t0 = time.monotonic()
    svc.stop(timeout_s=5.0)
    assert time.monotonic() - t0 < 5.0


def test_reoffer_same_client_keeps_authenticated_connection():
    # Una SDP_OFFER con el client_id de una conexión autenticada no puede
    # cortarla sin auth ni `closed`: la sustitución la decide el `auth` (regla 4).
    async def scenario() -> None:
        svc, sig, _, stop, task = await _service()
        first = await _auth(sig, "acc-1", "v1", sid="s1")
        await first.next()  # ready
        first.send({"type": "select", "camera_id": "a"})
        assert await _until(lambda: first.frames > _CAMERA_FRAMES)
        second = LocalViewer()
        await sig.connect(second, "v1")
        assert len(svc._conns) == 2
        frames = first.frames
        assert await _until(lambda: first.frames > frames + _CAMERA_FRAMES)
        assert first.inbox.empty()
        # Cuando el nuevo se autentica con la misma cuenta, ahora sí sustituye.
        second.send({"type": "auth", "token": _token("acc-1", sid="s2")})
        assert await first.next() == {"type": "closed", "reason": "replaced"}
        assert (await second.next())["type"] == "ready"
        stop.set()
        await task

    asyncio.run(scenario())


def test_reoffer_same_client_closes_unauthenticated_connection():
    async def scenario() -> None:
        svc, sig, _, stop, task = await _service()
        first = LocalViewer()
        await sig.connect(first, "v1")
        old_conn = next(iter(svc._conns))
        second = LocalViewer()
        await sig.connect(second, "v1")
        assert old_conn not in svc._conns and len(svc._conns) == 1
        stop.set()
        await task

    asyncio.run(scenario())


def test_backoff_resets_after_long_lived_signaling(monkeypatch):
    # Un canal que estuvo abierto un buen rato sin ofertas (lo normal en un
    # hogar) y se corta no debe heredar el backoff de cortes anteriores.
    import vitahub.live.service as service_mod

    attempts: list[int] = []

    def fake_backoff(attempt: int) -> float:
        attempts.append(attempt)
        return 0.01

    monkeypatch.setattr(service_mod, "backoff_delay", fake_backoff)
    monkeypatch.setattr(service_mod, "_STABLE_SIGNALING_S", 0.2)
    holds = iter([0.0, 0.3, 0.0])

    class _Dropping(_FakeSignaling):
        def __init__(self) -> None:
            super().__init__()
            self._hold = next(holds, 10.0)

        async def recv(self) -> SignalingMessage:
            await asyncio.sleep(self._hold)
            raise SignalingClosed("cortado")

    async def scenario() -> None:
        stop = asyncio.Event()
        svc = LiveService(
            hub_id="hub-1", max_viewers=3, keys=_KEYS, signaling_factory=_Dropping,
            sources=SourcePool(_opener, max_fps=15), targets=lambda: _TARGETS,
            sink=_Sink(), tick_s=0.05,
        )
        task = asyncio.ensure_future(svc.run(stop))
        assert await _until(lambda: len(attempts) >= 3)
        stop.set()
        await task
        # corte rápido → 0; el siguiente aguantó 0,3 s ≥ umbral → vuelve a 0;
        # corte rápido otra vez → 1 (no 2).
        assert attempts[:3] == [0, 0, 1]

    asyncio.run(scenario())


def test_select_same_camera_or_too_fast_is_ignored():
    async def scenario() -> None:
        _, sig, sink, stop, task = await _service(targets=_MORE_TARGETS)
        viewer = await _auth(sig, "acc-1", "v1")
        await viewer.next()
        viewer.send({"type": "select", "camera_id": "a"})
        assert await _until(lambda: any(e.type == "live_view_started" for e in sink.events))
        viewer.send({"type": "select", "camera_id": "a"})  # la que ya se ve
        viewer.send({"type": "select", "camera_id": "c"})  # < 1 s del último aceptado
        await asyncio.sleep(0.3)
        assert _OPENED == ["rtsp://a"]
        started = [e.payload["camera_id"] for e in sink.events if e.type == "live_view_started"]
        assert started == ["a"]
        await asyncio.sleep(1.0)
        viewer.send({"type": "select", "camera_id": "c"})  # pasado el segundo: vale
        assert await _until(lambda: _OPENED == ["rtsp://a", "rtsp://c"])
        stop.set()
        await task

    asyncio.run(scenario())


def test_unauthenticated_peers_are_capped():
    # Sin token no se ocupa plaza de viewer, pero cada peer cuesta memoria y un
    # puerto UDP: más de 2 × max_viewers sin autenticar y las ofertas se ignoran.
    async def scenario() -> None:
        svc, sig, _, stop, task = await _service(max_viewers=1)
        for client in ("v1", "v2"):
            await sig.connect(LocalViewer(), client)
        extra = LocalViewer()
        sig.answers["v3"] = asyncio.get_running_loop().create_future()
        await sig.inbox.put(
            SignalingMessage("SDP_OFFER", "v3", {"type": "offer", "sdp": await extra.offer()})
        )
        await asyncio.sleep(0.5)
        assert not sig.answers["v3"].done()
        assert len(svc._conns) == 2
        stop.set()
        await task

    asyncio.run(scenario())


class _FakePeer:
    def __init__(self, send_fails: bool = False, close_hangs: bool = False) -> None:
        self.conn_id = "c1"
        self.client_id = "v1"
        self.closed = False
        self._send_fails = send_fails
        self._close_hangs = close_hangs

    def send(self, text: str) -> None:
        if self._send_fails:
            raise RuntimeError("canal roto")

    async def close(self) -> None:
        if self._close_hangs:
            await asyncio.Event().wait()
        self.closed = True


def _bare_service() -> LiveService:
    return LiveService(
        hub_id="hub-1", max_viewers=3, keys=_KEYS, signaling_factory=_FakeSignaling,
        sources=SourcePool(_opener, max_fps=15), targets=lambda: _TARGETS, sink=_Sink(),
    )


def test_close_runs_peer_close_even_if_send_fails():
    from vitahub.live.service import _Conn

    async def scenario() -> None:
        svc = _bare_service()
        peer = _FakePeer(send_fails=True)
        svc._conns["c1"] = _Conn(peer=peer, started_at=0.0)  # type: ignore[arg-type]
        await svc._close("c1", "invalid")
        assert peer.closed

    asyncio.run(scenario())


def test_close_does_not_hang_on_stuck_peer(monkeypatch):
    import vitahub.live.service as service_mod
    from vitahub.live.service import _Conn

    monkeypatch.setattr(service_mod, "_PEER_CLOSE_TIMEOUT_S", 0.1)

    async def scenario() -> None:
        svc = _bare_service()
        svc._conns["c1"] = _Conn(peer=_FakePeer(close_hangs=True), started_at=0.0)  # type: ignore[arg-type]
        await asyncio.wait_for(svc._close("c1", "shutdown"), 1.0)

    asyncio.run(scenario())
