from vitahub.analytics.event_engine import EventEngine
from vitahub.inference.stub import StubDetector
from vitahub.models import Camera, Event
from vitahub.worker import process_frame


class _RecordingSink:
    def __init__(self):
        self.events: list[Event] = []

    def emit(self, event: Event) -> None:
        self.events.append(event)

    def close(self) -> None:
        pass


CAM = Camera(id="onvif-a", name="salon", last_ip="10.0.0.5")


def test_process_frame_emits_person_detected_after_threshold():
    engine = EventEngine(hub_id="hub-1", present_after_s=2.0, absent_after_s=5.0)
    sink = _RecordingSink()
    detector = StubDetector(person_count=1)

    assert process_frame(CAM, None, detector, engine, sink, now=0.0) == []
    emitted = process_frame(CAM, None, detector, engine, sink, now=2.0)

    assert len(emitted) == 1
    assert emitted[0].type == "person_detected"
    assert sink.events == emitted
