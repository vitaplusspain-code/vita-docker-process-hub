import asyncio
import json

from tests.live_viewer import LocalViewer
from vitahub.live.peer import ViewerPeer
from vitahub.live.source import IdleTrack


def test_negotiates_and_exchanges_messages():
    async def scenario() -> None:
        got: list[str | bytes] = []
        opened = asyncio.Event()
        closed = asyncio.Event()

        async def on_message(peer: ViewerPeer, raw: str | bytes) -> None:
            got.append(raw)
            peer.send('{"type":"ready","cameras":[]}')

        async def on_closed(peer: ViewerPeer) -> None:
            closed.set()

        peer = ViewerPeer("c1", "v1", [], lambda p: opened.set(), on_message, on_closed)
        viewer = LocalViewer()
        await viewer.accept(await peer.answer(await viewer.offer()))
        await asyncio.wait_for(viewer.opened.wait(), 10)
        await asyncio.wait_for(opened.wait(), 10)
        viewer.send({"type": "auth", "token": "t"})
        assert await viewer.next() == {"type": "ready", "cameras": []}
        assert got == ['{"type": "auth", "token": "t"}']

        # Cambiar de pista no renegocia: siguen llegando frames. El emisor de
        # aiortc está bloqueado en el `recv()` de la pista anterior (1 s en la
        # IdleTrack inicial), así que el primer frame puede tardar hasta ~1 s.
        baseline = viewer.frames
        peer.set_track(IdleTrack(interval_s=0.02))
        for _ in range(100):
            if viewer.frames > baseline:
                break
            await asyncio.sleep(0.05)
        assert viewer.frames > baseline

        await viewer.close()
        await asyncio.wait_for(closed.wait(), 15)
        await peer.close()
        await peer.close()  # idempotente

    asyncio.run(scenario())


async def _noop_msg(peer: ViewerPeer, raw: str | bytes) -> None:
    return None


async def _noop_closed(peer: ViewerPeer) -> None:
    return None


def test_second_control_channel_is_rejected():
    async def scenario() -> None:
        opens: list[ViewerPeer] = []
        got: list[str | bytes] = []

        async def on_message(peer: ViewerPeer, raw: str | bytes) -> None:
            got.append(raw)

        peer = ViewerPeer("c1", "v1", [], opens.append, on_message, _noop_closed)
        viewer = LocalViewer()
        await viewer.accept(await peer.answer(await viewer.offer()))
        await asyncio.wait_for(viewer.opened.wait(), 10)
        await asyncio.sleep(0.2)
        second = viewer.pc.createDataChannel("control")
        await asyncio.sleep(0.5)
        if second.readyState == "open":
            second.send(json.dumps({"type": "auth", "token": "x"}))
        await asyncio.sleep(0.5)
        assert len(opens) == 1
        assert got == []
        await viewer.close()
        await peer.close()

    asyncio.run(scenario())


def test_on_open_exception_does_not_escape():
    async def scenario() -> None:
        def boom(peer: ViewerPeer) -> None:
            raise RuntimeError("fallo")

        peer = ViewerPeer("c1", "v1", [], boom, _noop_msg, _noop_closed)
        viewer = LocalViewer()
        await viewer.accept(await peer.answer(await viewer.offer()))
        await asyncio.wait_for(viewer.opened.wait(), 10)
        await asyncio.sleep(0.3)
        viewer.send({"type": "auth"})  # la ruta SCTP sigue viva
        await viewer.close()
        await peer.close()

    asyncio.run(scenario())


def test_on_closed_is_one_shot_and_close_still_closes_pc():
    async def scenario() -> None:
        calls: list[int] = []

        async def on_closed(peer: ViewerPeer) -> None:
            calls.append(1)

        peer = ViewerPeer("c1", "v1", [], lambda p: None, _noop_msg, on_closed)
        await peer._report_closed()
        await peer._report_closed()
        assert calls == [1]
        await peer.close()
        assert peer._pc.connectionState == "closed"

    asyncio.run(scenario())


def test_on_closed_exception_is_contained():
    async def scenario() -> None:
        async def on_closed(peer: ViewerPeer) -> None:
            raise RuntimeError("fallo")

        peer = ViewerPeer("c1", "v1", [], lambda p: None, _noop_msg, on_closed)
        await peer._report_closed()
        await peer.close()

    asyncio.run(scenario())


def test_malformed_candidates_never_raise():
    async def scenario() -> None:
        peer = ViewerPeer("c1", "v1", [], lambda p: None, _noop_msg, _noop_closed)
        for payload in (
            {"candidate": "basura"},
            {"candidate": 5},
            {"candidate": "candidate:1 1 udp 1 1.2.3.4 99 typ host", "sdpMid": 3},
            {"candidate": "candidate:1 1 udp 1 1.2.3.4 99 typ host", "sdpMLineIndex": "0"},
            {"candidate": "candidate:1 1 udp x 1.2.3.4 99 typ host", "sdpMid": "0"},
            {"candidate": "candidate:1 1 udp 1 1.2.3.4 99 typ host"},
            {},
        ):
            await peer.add_remote_candidate(payload)
        await peer.close()

    asyncio.run(scenario())
