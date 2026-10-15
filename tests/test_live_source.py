import asyncio
import fractions
import itertools
import threading

import pytest
from aiortc import MediaStreamTrack
from aiortc.mediastreams import MediaStreamError
from av import VideoFrame

from vitahub.live.source import (
    FpsLimitedTrack,
    IdleTrack,
    LiveTarget,
    SourcePool,
    SourceUnavailable,
    SwitchTrack,
    live_targets,
)
from vitahub.models import Camera


class _FakeTrack(MediaStreamTrack):
    kind = "video"

    def __init__(self) -> None:
        super().__init__()
        self.n = 0

    async def recv(self) -> VideoFrame:
        self.n += 1
        frame = VideoFrame(width=16, height=16)
        frame.pts, frame.time_base = self.n, fractions.Fraction(1, 90000)
        return frame


def test_live_targets_prefer_substream_and_skip_disabled():
    cams = [
        Camera("a", "salón", "1.1.1.1", rtsp_main="rtsp://h/main", rtsp_sub="rtsp://h/sub"),
        Camera("b", "cocina", "1.1.1.2", rtsp_main="rtsp://h/main2"),
        Camera("c", "baño", "1.1.1.3", enabled=False, rtsp_sub="rtsp://h/sub3"),
        Camera("d", "pasillo", "1.1.1.4"),
    ]
    assert live_targets(cams, "u", "p") == [
        LiveTarget("a", "salón", "rtsp://u:p@h/sub"),
        LiveTarget("b", "cocina", "rtsp://u:p@h/main2"),
    ]


def test_fps_limiter_drops_frames():
    # El reloj avanza una lectura por frame de la fuente: frames 1..5 llegan
    # en t = 0, 0.05, 0.11, 0.15, 0.22. A 10 fps solo pasa uno cada 0.1 s.
    ticks = iter([0.0, 0.05, 0.11, 0.15, 0.22])
    limited = FpsLimitedTrack(_FakeTrack(), max_fps=10, clock=lambda: next(ticks))

    async def scenario() -> list[int]:
        return [(await limited.recv()).pts for _ in range(3)]

    assert asyncio.run(scenario()) == [1, 3, 5]


def test_idle_track_yields_frames():
    async def scenario() -> VideoFrame:
        return await asyncio.wait_for(IdleTrack(interval_s=0.01).recv(), 1)

    frame = asyncio.run(scenario())
    assert frame.width > 0


def test_pool_shares_one_source_per_camera():
    opened: list[str] = []
    tracks: list[_FakeTrack] = []

    def opener(uri: str) -> MediaStreamTrack:
        opened.append(uri)
        tracks.append(_FakeTrack())
        return tracks[-1]

    pool = SourcePool(opener, max_fps=15)
    target = LiveTarget("a", "salón", "rtsp://x")

    async def scenario() -> None:
        t1 = await pool.acquire(target)
        t2 = await pool.acquire(target)
        assert opened == ["rtsp://x"]
        pool.release(t1)
        assert tracks[0].readyState == "live"
        pool.release(t2)
        # Con el último visor se cierra el RTSP: no se lee vídeo que nadie mira.
        # La parada va al executor, así que se espera un momento.
        await asyncio.sleep(0.1)
        assert tracks[0].readyState == "ended"

    asyncio.run(scenario())


def test_pool_maps_open_failure_to_unavailable():
    def opener(uri: str) -> MediaStreamTrack:
        raise OSError("connection refused")

    pool = SourcePool(opener, max_fps=15)
    with pytest.raises(SourceUnavailable):
        asyncio.run(pool.acquire(LiveTarget("a", "salón", "rtsp://x")))


def _pool() -> tuple[SourcePool, list[_FakeTrack]]:
    tracks: list[_FakeTrack] = []

    def opener(uri: str) -> MediaStreamTrack:
        tracks.append(_FakeTrack())
        return tracks[-1]

    return SourcePool(opener, max_fps=30), tracks


_T = LiveTarget("a", "salón", "rtsp://x")


def test_two_subscribers_receive_from_one_source():
    pool, tracks = _pool()

    async def scenario() -> None:
        t1, t2 = await pool.acquire(_T), await pool.acquire(_T)
        f1 = await asyncio.wait_for(t1.recv(), 1)
        f2 = await asyncio.wait_for(t2.recv(), 1)
        assert f1.width == f2.width == 16
        assert len(tracks) == 1
        pool.close_all()

    asyncio.run(scenario())


def test_last_release_ends_pending_recv():
    pool, _ = _pool()

    async def scenario() -> None:
        track = await pool.acquire(_T)
        pending = asyncio.ensure_future(_drain(track))
        await asyncio.sleep(0.05)
        pool.release(track)
        with pytest.raises(MediaStreamError):
            await asyncio.wait_for(pending, 1)

    asyncio.run(scenario())


async def _drain(track: MediaStreamTrack) -> None:
    while True:
        await track.recv()


def test_limited_track_stop_unsubscribes_only_itself():
    pool, _ = _pool()

    async def scenario() -> None:
        t1, t2 = await pool.acquire(_T), await pool.acquire(_T)
        t1.stop()
        assert t1.readyState == "ended"
        assert len(pool._entries["a"].subs) == 1
        await asyncio.wait_for(t2.recv(), 1)
        pool.close_all()

    asyncio.run(scenario())


def test_close_all_ends_every_subscriber():
    pool, _ = _pool()

    async def scenario() -> None:
        tracks = [await pool.acquire(_T), await pool.acquire(_T)]
        pending = [asyncio.ensure_future(_drain(t)) for t in tracks]
        await asyncio.sleep(0.05)
        pool.close_all()
        for p in pending:
            with pytest.raises(MediaStreamError):
                await asyncio.wait_for(p, 1)

    asyncio.run(scenario())


def test_source_stop_runs_off_event_loop():
    seen: list[int] = []

    class _Track(_FakeTrack):
        def stop(self) -> None:
            seen.append(threading.get_ident())
            super().stop()

    pool = SourcePool(lambda uri: _Track(), max_fps=30)

    async def scenario() -> None:
        pool.release(await pool.acquire(_T))
        await asyncio.sleep(0.1)
        assert seen and seen[0] != threading.get_ident()

    asyncio.run(scenario())


class _DyingTrack(_FakeTrack):
    async def recv(self) -> VideoFrame:
        if self.n >= 1:
            raise MediaStreamError
        return await super().recv()


def test_reader_death_ends_subscribers_and_reopens():
    made: list[MediaStreamTrack] = []

    def opener(uri: str) -> MediaStreamTrack:
        made.append(_DyingTrack() if not made else _FakeTrack())
        return made[-1]

    pool = SourcePool(opener, max_fps=30)

    async def scenario() -> None:
        old = await pool.acquire(_T)
        with pytest.raises(MediaStreamError):
            await asyncio.wait_for(_drain(old), 1)
        assert "a" not in pool._entries
        new = await pool.acquire(_T)
        assert len(made) == 2
        await asyncio.wait_for(new.recv(), 1)
        pool.close_all()

    asyncio.run(scenario())


def test_stale_release_does_not_touch_new_entry():
    made: list[MediaStreamTrack] = []

    def opener(uri: str) -> MediaStreamTrack:
        made.append(_DyingTrack() if not made else _FakeTrack())
        return made[-1]

    pool = SourcePool(opener, max_fps=30)

    async def scenario() -> None:
        old = await pool.acquire(_T)
        with pytest.raises(MediaStreamError):
            await asyncio.wait_for(_drain(old), 1)
        new = await pool.acquire(_T)
        pool.release(old)
        assert pool._entries["a"].refs == 1
        await asyncio.wait_for(new.recv(), 1)
        assert made[1].readyState == "live"
        pool.close_all()

    asyncio.run(scenario())


def test_double_release_is_noop():
    pool, tracks = _pool()

    async def scenario() -> None:
        t1, t2 = await pool.acquire(_T), await pool.acquire(_T)
        pool.release(t1)
        pool.release(t1)
        assert pool._entries["a"].refs == 1
        await asyncio.wait_for(t2.recv(), 1)
        assert tracks[0].readyState == "live"
        pool.close_all()

    asyncio.run(scenario())


class _NeverTrack(MediaStreamTrack):
    """Fuente que nunca entrega un frame (cámara colgada)."""

    kind = "video"

    async def recv(self) -> VideoFrame:
        await asyncio.Event().wait()
        raise AssertionError("inalcanzable")


def test_switch_track_idle_without_source():
    async def scenario() -> None:
        track = SwitchTrack(idle_interval_s=0.01)
        frame = await asyncio.wait_for(track.recv(), 1)
        assert frame.width == 320 and frame.height == 180
        track.stop()

    asyncio.run(scenario())


def test_switch_interrupts_pending_recv_on_hung_source():
    async def scenario() -> None:
        track = SwitchTrack(idle_interval_s=0.01)
        track.set_source(_NeverTrack())
        pending = asyncio.ensure_future(track.recv())
        await asyncio.sleep(0.05)
        assert not pending.done()
        fresh = _FakeTrack()
        track.set_source(fresh)
        frame = await asyncio.wait_for(pending, 1)
        assert frame.width == 16 and fresh.n == 1
        track.stop()

    asyncio.run(scenario())


def test_switch_source_death_calls_back_once_and_falls_back_to_idle():
    async def scenario() -> None:
        ended: list[int] = []
        track = SwitchTrack(on_source_ended=lambda: ended.append(1), idle_interval_s=0.01)
        track.set_source(_DyingTrack())
        first = await asyncio.wait_for(track.recv(), 1)
        assert first.width == 16
        # La fuente muere: nunca se propaga, sigue el negro.
        for _ in range(3):
            frame = await asyncio.wait_for(track.recv(), 1)
            assert frame.width == 320
        assert ended == [1]
        track.stop()

    asyncio.run(scenario())


def test_switch_track_pts_monotonic_across_switches():
    class _Backwards(_FakeTrack):
        async def recv(self) -> VideoFrame:
            frame = await super().recv()
            frame.pts = 1_000_000 - self.n  # reloj propio que va hacia atrás
            frame.time_base = fractions.Fraction(1, 1000)
            return frame

    async def scenario() -> None:
        track = SwitchTrack(idle_interval_s=0.01)
        pts = [(await track.recv()).pts]
        track.set_source(_Backwards())
        pts += [(await track.recv()).pts for _ in range(3)]
        track.set_source(_FakeTrack())
        pts += [(await track.recv()).pts for _ in range(3)]
        track.set_source(None)
        pts.append((await track.recv()).pts)
        assert all(b > a for a, b in itertools.pairwise(pts))
        track.stop()

    asyncio.run(scenario())


def test_switch_track_stop_ends_recv_without_stopping_source():
    async def scenario() -> None:
        source = _NeverTrack()
        track = SwitchTrack()
        track.set_source(source)
        pending = asyncio.ensure_future(track.recv())
        await asyncio.sleep(0.02)
        track.stop()
        with pytest.raises(MediaStreamError):
            await asyncio.wait_for(pending, 1)
        assert source.readyState == "live"

    asyncio.run(scenario())


class _OneShotTrack(MediaStreamTrack):
    """Entrega un único frame y luego se queda esperando (como una cámara lenta)."""

    kind = "video"

    def __init__(self) -> None:
        super().__init__()
        self.sent: list[VideoFrame] = []
        self.go = asyncio.Event()

    async def recv(self) -> VideoFrame:
        if self.sent:
            await asyncio.Event().wait()
        await self.go.wait()
        frame = VideoFrame(width=16, height=16)
        frame.pts, frame.time_base = 7, fractions.Fraction(1, 90000)
        self.sent.append(frame)
        return frame


def test_each_subscriber_gets_its_own_frame_object():
    sources: list[_OneShotTrack] = []

    def opener(uri: str) -> MediaStreamTrack:
        sources.append(_OneShotTrack())
        return sources[-1]

    pool = SourcePool(opener, max_fps=30)

    async def scenario() -> None:
        t1, t2 = await pool.acquire(_T), await pool.acquire(_T)
        sources[0].go.set()
        f1 = await asyncio.wait_for(t1.recv(), 1)
        f2 = await asyncio.wait_for(t2.recv(), 1)
        assert len(sources[0].sent) == 1  # el mismo frame de la fuente, repartido
        assert f1 is not f2
        assert (f1.to_ndarray() == f2.to_ndarray()).all()
        assert f1.pts == f2.pts == 7 and f1.time_base == f2.time_base
        f1.pts = 999
        f1.time_base = fractions.Fraction(1, 1000)
        assert f2.pts == 7 and f2.time_base == fractions.Fraction(1, 90000)
        pool.close_all()

    asyncio.run(scenario())


def test_single_subscriber_gets_frame_without_copy():
    sources: list[_OneShotTrack] = []

    def opener(uri: str) -> MediaStreamTrack:
        sources.append(_OneShotTrack())
        return sources[-1]

    pool = SourcePool(opener, max_fps=30)

    async def scenario() -> None:
        track = await pool.acquire(_T)
        sources[0].go.set()
        frame = await asyncio.wait_for(track.recv(), 1)
        assert frame is sources[0].sent[0]
        pool.close_all()

    asyncio.run(scenario())
