import asyncio

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
        peer.set_track(IdleTrack(interval_s=0.02))
        for _ in range(100):
            if viewer.frames > 0:
                break
            await asyncio.sleep(0.05)
        assert viewer.frames > 0

        await viewer.close()
        await asyncio.wait_for(closed.wait(), 15)
        await peer.close()
        await peer.close()  # idempotente

    asyncio.run(scenario())
