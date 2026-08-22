from vitahub.analytics.event_engine import EventEngine
from vitahub.analytics.fall_engine import FallEngine
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


NOSE, L_SH, R_SH, L_HIP, R_HIP = 0, 5, 6, 11, 12


def _pose(nose, shoulders, hips, conf=0.9):
    kps = [(0.0, 0.0, 0.0)] * 17
    kps[NOSE] = (*nose, conf)
    kps[L_SH] = (shoulders[0] - 10, shoulders[1], conf)
    kps[R_SH] = (shoulders[0] + 10, shoulders[1], conf)
    kps[L_HIP] = (hips[0] - 10, hips[1], conf)
    kps[R_HIP] = (hips[0] + 10, hips[1], conf)
    return tuple(kps)


LYING_KPS = _pose(nose=(10, 110), shoulders=(30, 110), hips=(90, 110))
LYING_BOX = (0, 90, 120, 120)


def test_process_frame_without_fall_engine_is_unchanged():
    engine = EventEngine(hub_id="hub-1", present_after_s=2.0, absent_after_s=5.0)
    sink = _RecordingSink()
    detector = StubDetector(person_count=1, keypoints=LYING_KPS, bbox=LYING_BOX)
    process_frame(CAM, None, detector, engine, sink, now=0.0, fall_engine=None)
    emitted = process_frame(CAM, None, detector, engine, sink, now=2.0, fall_engine=None)
    assert [e.type for e in emitted] == ["person_detected"]


def test_process_frame_emits_presence_and_fall_through_same_sink():
    engine = EventEngine(hub_id="hub-1", present_after_s=2.0, absent_after_s=5.0)
    fall = FallEngine(hub_id="hub-1", min_score=0.3)
    sink = _RecordingSink()
    detector = StubDetector(person_count=1, keypoints=LYING_KPS, bbox=LYING_BOX)
    all_events = []
    now = 0.0
    for _ in range(8):
        all_events += process_frame(CAM, None, detector, engine, sink, now, fall_engine=fall)
        now += 0.5
    types = [e.type for e in all_events]
    assert "person_detected" in types
    assert "fall_detected" in types
    assert sink.events == all_events


def test_fall_engine_exception_does_not_break_presence(caplog):
    class _Boom:
        def observe(self, camera, detections, now):
            raise RuntimeError("boom")

    engine = EventEngine(hub_id="hub-1", present_after_s=2.0, absent_after_s=5.0)
    sink = _RecordingSink()
    detector = StubDetector(person_count=1)
    process_frame(CAM, None, detector, engine, sink, now=0.0, fall_engine=_Boom())
    emitted = process_frame(CAM, None, detector, engine, sink, now=2.0, fall_engine=_Boom())
    assert [e.type for e in emitted] == ["person_detected"]
    assert sink.events == emitted
    assert sum("analítica de caídas" in r.message for r in caplog.records) == 1
