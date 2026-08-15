from datetime import UTC, datetime

from vitahub.analytics.event_engine import EventEngine
from vitahub.models import Camera

CAM = Camera(id="onvif-abc", name="salon", last_ip="10.0.0.5")


def _engine() -> EventEngine:
    return EventEngine(
        hub_id="hub-1",
        present_after_s=2.0,
        absent_after_s=5.0,
        clock=lambda: datetime(2026, 8, 15, 10, 0, 0, tzinfo=UTC),
    )


def test_no_event_before_threshold():
    eng = _engine()
    assert eng.observe(CAM, person_count=1, confidence=0.9, now=0.0) == []
    assert eng.observe(CAM, person_count=1, confidence=0.9, now=1.9) == []


def test_person_detected_after_present_threshold():
    eng = _engine()
    eng.observe(CAM, 1, 0.9, now=0.0)
    events = eng.observe(CAM, 1, 0.9, now=2.0)
    assert len(events) == 1
    ev = events[0]
    assert ev.type == "person_detected"
    assert ev.severity == "info"
    assert ev.payload["person_count"] == 1
    assert ev.camera_id == "onvif-abc"
    assert ev.timestamp == "2026-08-15T10:00:00+00:00"


def test_flicker_below_threshold_emits_nothing():
    eng = _engine()
    eng.observe(CAM, 1, 0.9, now=0.0)   # candidato presencia
    eng.observe(CAM, 0, 0.0, now=1.0)   # vuelve a 0 antes de 2s -> cancela
    assert eng.observe(CAM, 0, 0.0, now=10.0) == []  # sigue en 0 reportado


def test_absent_after_absent_threshold():
    eng = _engine()
    eng.observe(CAM, 1, 0.9, now=0.0)
    eng.observe(CAM, 1, 0.9, now=2.0)   # confirmado presente
    eng.observe(CAM, 0, 0.0, now=3.0)   # candidato ausencia
    assert eng.observe(CAM, 0, 0.0, now=7.0) == []       # <5s
    events = eng.observe(CAM, 0, 0.0, now=8.0)           # >=5s
    assert len(events) == 1
    assert events[0].type == "person_absent"


def test_count_changed_between_nonzero():
    eng = _engine()
    eng.observe(CAM, 1, 0.9, now=0.0)
    eng.observe(CAM, 1, 0.9, now=2.0)   # presente=1
    eng.observe(CAM, 2, 0.9, now=3.0)   # candidato 2
    events = eng.observe(CAM, 2, 0.9, now=5.0)  # +2s
    assert len(events) == 1
    assert events[0].type == "person_count_changed"
    assert events[0].payload["person_count"] == 2
