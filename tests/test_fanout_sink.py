from vitahub.models import Event
from vitahub.sinks.base import EventSink
from vitahub.sinks.fanout import FanoutSink


def _event() -> Event:
    return Event(
        hub_id="hub-1",
        camera_id="onvif-abc",
        camera_name="salon",
        type="person_detected",
        severity="info",
        timestamp="2026-08-21T10:00:00+00:00",
        payload={"person_count": 1},
    )


class _RecordingSink(EventSink):
    def __init__(self) -> None:
        self.emitted: list[Event] = []
        self.closed = False

    def emit(self, event: Event) -> None:
        self.emitted.append(event)

    def close(self) -> None:
        self.closed = True


class _BrokenSink(EventSink):
    def emit(self, event: Event) -> None:
        raise RuntimeError("sink roto")

    def close(self) -> None:
        raise RuntimeError("cierre roto")


def test_emits_through_every_sink():
    a, b = _RecordingSink(), _RecordingSink()
    FanoutSink([a, b]).emit(_event())
    assert len(a.emitted) == 1
    assert len(b.emitted) == 1


def test_a_failing_sink_does_not_starve_the_others():
    # El caso real: el uplink a AWS caído no puede dejar además sin traza a
    # `docker logs`, que es lo único que tiene un técnico delante del Jetson.
    good = _RecordingSink()
    FanoutSink([_BrokenSink(), good]).emit(_event())
    assert len(good.emitted) == 1


def test_emit_never_propagates():
    FanoutSink([_BrokenSink()]).emit(_event())


def test_close_closes_every_sink_even_if_one_fails():
    good = _RecordingSink()
    FanoutSink([_BrokenSink(), good]).close()
    assert good.closed
