import asyncio
import fractions

import pytest
from aiortc import MediaStreamTrack
from av import VideoFrame

from vitahub.live.source import (
    FpsLimitedTrack,
    IdleTrack,
    LiveTarget,
    SourcePool,
    SourceUnavailable,
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
        await pool.acquire(target)
        await pool.acquire(target)
        assert opened == ["rtsp://x"]
        pool.release("a")
        assert tracks[0].readyState == "live"
        pool.release("a")
        # Con el último visor se cierra el RTSP: no se lee vídeo que nadie mira.
        assert tracks[0].readyState == "ended"

    asyncio.run(scenario())


def test_pool_maps_open_failure_to_unavailable():
    def opener(uri: str) -> MediaStreamTrack:
        raise OSError("connection refused")

    pool = SourcePool(opener, max_fps=15)
    with pytest.raises(SourceUnavailable):
        asyncio.run(pool.acquire(LiveTarget("a", "salón", "rtsp://x")))
