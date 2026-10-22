from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from vitahub.clips.buffer import BufferedPacket, PacketBuffer
from vitahub.clips.trigger import ClipRequest, ClipTriggerSink
from vitahub.models import Event
from vitahub.sinks.base import EventSink


class _MemSink(EventSink):
    def __init__(self) -> None:
        self.events: list[Event] = []
        self.closed = False
        self.log: list[str] = []

    def emit(self, event: Event) -> None:
        self.events.append(event)
        self.log.append(f"emit:{event.type}")

    def close(self) -> None:
        self.closed = True
        self.log.append("close")


class _Clock:
    def __init__(self, t: float = 100.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


def _full_buffer() -> PacketBuffer:
    buf = PacketBuffer()
    buf.reset(object())
    t = 60.0
    while t <= 130.0:
        buf.append(BufferedPacket(t=t, keyframe=t % 2.0 == 0.0, packet=object()))
        t += 0.5
    return buf


class _Rig:
    def __init__(self, buffer: PacketBuffer | None = None) -> None:
        self.inner = _MemSink()
        self.mono = _Clock()
        self.wall_now = datetime(2026, 10, 22, 12, 0, 0, tzinfo=UTC)
        self.clips: list[ClipRequest] = []
        self.buffer = _full_buffer() if buffer is None else buffer
        self.sink = ClipTriggerSink(
            self.inner,
            "hub1",
            {"cam1"},
            lambda cam: self.buffer if cam == "cam1" else None,
            self.clips.append,
            monotonic=self.mono,
            wall=lambda: self.wall_now,
        )

    def fall(self, at: float, ep: str = "e1", speed: object = 0.9, cam: str = "cam1") -> None:
        self.mono.t = at
        self.sink.emit(_fall(ep, speed, cam))

    def resolve(self, at: float, ep: str = "e1", reason: str = "upright") -> None:
        self.mono.t = at
        self.sink.emit(_resolved(ep, reason))

    def tick(self, at: float) -> None:
        self.mono.t = at
        self.sink.tick()

    def own(self) -> list[Event]:
        return [e for e in self.inner.events if e.type.startswith("fall_clip_")]


def _fall(ep: str, speed: object, cam: str = "cam1", signals: object = "default") -> Event:
    sig = {"drop_speed": speed} if signals == "default" else signals
    payload: dict[str, object] = {"episode_id": ep, "score": 0.9}
    if sig is not None:
        payload["signals"] = sig
    return Event("hub1", cam, "Salón", "fall_detected", "critical", "ts", payload)


def _resolved(ep: str, reason: str) -> Event:
    return Event("hub1", "cam1", "Salón", "fall_resolved", "info", "ts",
                 {"episode_id": ep, "reason": reason})


def test_forwards_every_event_to_inner_first() -> None:
    r = _Rig()
    ev = Event("hub1", "cam9", "X", "other", "info", "ts", {})
    r.sink.emit(ev)
    r.fall(100)
    assert r.inner.events[0] is ev
    assert r.inner.events[1].type == "fall_detected"


def test_risk_closes_at_10s_and_calls_on_clip() -> None:
    r = _Rig()
    r.fall(100)
    r.tick(109.9)
    assert r.clips == []
    r.tick(110)
    assert len(r.clips) == 1
    req = r.clips[0]
    assert req.episode_ids == ["e1"]
    assert req.camera_id == "cam1"
    assert req.camera_name == "Salón"
    assert req.snapshot.packets[0].t <= 85
    assert req.clip_start == r.wall_now - timedelta(seconds=110 - 85)
    r.tick(111)
    assert len(r.clips) == 1


def test_slow_is_discarded_without_snapshot() -> None:
    r = _Rig()
    r.fall(100, speed=0.2)
    r.tick(110)
    assert r.clips == []
    (ev,) = r.own()
    assert ev.type == "fall_clip_discarded"
    assert ev.payload["reason"] == "slow"
    assert ev.payload["episode_ids"] == ["e1"]
    assert ev.payload["drop_speed"] == 0.2
    assert ev.severity == "info"


def test_got_up_before_deadline() -> None:
    r = _Rig()
    r.fall(100, speed=0.9)
    r.resolve(106, reason="upright")
    r.tick(110)
    assert r.clips == []
    (ev,) = r.own()
    assert ev.type == "fall_clip_discarded"
    assert ev.payload["reason"] == "got_up"


def test_resolved_after_deadline_is_ignored() -> None:
    r = _Rig()
    r.fall(100)
    r.resolve(111, reason="upright")  # tras el deadline y antes del tick
    r.tick(111)
    assert len(r.clips) == 1
    r.resolve(112, reason="upright")  # ventana ya cerrada
    assert len(r.clips) == 1


def test_track_lost_counts_as_risk() -> None:
    r = _Rig()
    r.fall(100, speed=None)
    r.resolve(105, reason="track_lost")
    r.tick(110)
    assert len(r.clips) == 1


def test_second_episode_extends_window() -> None:
    r = _Rig()
    r.fall(100, "e1")
    r.fall(108, "e2")
    r.tick(110)
    assert r.clips == []
    r.tick(118)
    assert [q.episode_ids for q in r.clips] == [["e1", "e2"]]


def test_extension_capped_at_30s() -> None:
    r = _Rig()
    for i, t in enumerate(range(100, 130, 5)):
        r.fall(t, f"e{i}")
    r.tick(129.9)
    assert r.clips == []
    r.tick(130)
    assert len(r.clips) == 1
    assert len(r.clips[0].episode_ids) == 6


def test_no_video() -> None:
    r = _Rig(buffer=PacketBuffer())
    r.fall(100)
    r.tick(110)
    assert r.clips == []
    (ev,) = r.own()
    assert ev.type == "fall_clip_failed"
    assert ev.payload["reason"] == "no_video"
    assert ev.payload["episode_ids"] == ["e1"]


def test_camera_not_enabled_is_ignored() -> None:
    r = _Rig()
    r.fall(100, cam="cam2")
    r.tick(200)
    assert r.clips == []
    assert r.own() == []
    assert len(r.inner.events) == 1


@pytest.mark.parametrize("signals", [None, "texto", [1, 2]])
def test_missing_signals_counts_as_risk(signals: object) -> None:
    r = _Rig()
    r.mono.t = 100
    r.sink.emit(_fall("e1", None, signals=signals))
    r.tick(110)
    assert len(r.clips) == 1


def test_wall_clock_jump_does_not_affect_window() -> None:
    r = _Rig()
    r.fall(100)
    r.wall_now -= timedelta(hours=1)
    r.tick(109.9)
    assert r.clips == []
    r.tick(110)
    assert len(r.clips) == 1


def test_bad_on_clip_does_not_stop_other_windows() -> None:
    r = _Rig()
    seen: list[ClipRequest] = []

    def on_clip(req: ClipRequest) -> None:
        if req.camera_id == "cam1":
            raise RuntimeError("boom")
        seen.append(req)

    r.sink = ClipTriggerSink(
        r.inner, "hub1", {"cam1", "cam2"}, lambda cam: _full_buffer(), on_clip,
        monotonic=r.mono, wall=lambda: r.wall_now,
    )
    r.fall(100, cam="cam1")
    r.fall(100, "e2", cam="cam2")
    r.tick(110)
    assert len(seen) == 1


def test_inner_emit_exception_propagates() -> None:
    r = _Rig()

    def boom(event: Event) -> None:
        raise OSError("disco")

    r.inner.emit = boom  # type: ignore[method-assign]
    with pytest.raises(OSError):
        r.sink.emit(_fall("e1", 0.9))


def test_close_emits_shutdown_then_closes_inner() -> None:
    r = _Rig()
    r.fall(100)
    r.sink.close()
    assert r.inner.log[-2:] == ["emit:fall_clip_failed", "close"]
    ev = r.own()[0]
    assert ev.payload["reason"] == "shutdown"
    assert ev.payload["episode_ids"] == ["e1"]
    assert r.inner.closed
