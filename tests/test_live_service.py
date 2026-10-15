import asyncio
import time
from collections.abc import Callable

import jwt
from aiortc import MediaStreamTrack
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


def _opener(uri: str) -> MediaStreamTrack:
    _OPENED.append(uri)
    if uri == "rtsp://b":
        raise OSError("cámara apagada")
    return IdleTrack(interval_s=0.02)


async def _service(max_viewers: int = 3, clock=time.time, auth_timeout_s: float = 5.0):  # type: ignore[no-untyped-def]
    _OPENED.clear()
    sig, sink, stop = _FakeSignaling(), _Sink(), asyncio.Event()
    svc = LiveService(
        hub_id="hub-1", max_viewers=max_viewers, keys=_KEYS,
        signaling_factory=lambda: sig, sources=SourcePool(_opener, max_fps=15),
        targets=lambda: _TARGETS, sink=sink, clock=clock,
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
