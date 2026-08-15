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


def test_two_cameras_isolated():
    eng = _engine()
    cam_a = Camera(id="cam-a", name="a", last_ip="10.0.0.1")
    cam_b = Camera(id="cam-b", name="b", last_ip="10.0.0.2")

    assert eng.observe(cam_a, 1, 0.9, now=0.0) == []  # candidato A desde 0.0
    assert eng.observe(cam_b, 1, 0.9, now=0.5) == []  # candidato B desde 0.5

    # A llega a su propio umbral (2s desde 0.0); B, observado en el mismo
    # instante, no debe verse afectado por el estado de A.
    events_a = eng.observe(cam_a, 1, 0.9, now=2.0)
    assert len(events_a) == 1
    assert events_a[0].type == "person_detected"
    assert events_a[0].camera_id == "cam-a"
    assert eng.observe(cam_b, 1, 0.9, now=2.0) == []  # a B aún le faltan 0.5s

    # B llega a su propio umbral (2s desde 0.5); A, ya reportado, no reemite.
    events_b = eng.observe(cam_b, 1, 0.9, now=2.5)
    assert len(events_b) == 1
    assert events_b[0].type == "person_detected"
    assert events_b[0].camera_id == "cam-b"
    assert eng.observe(cam_a, 1, 0.9, now=2.5) == []  # sin cambios, ya reportado


def test_flap_resets_timer():
    eng = _engine()
    assert eng.observe(CAM, 1, 0.9, now=0.0) == []    # candidato 1 desde 0.0
    assert eng.observe(CAM, 2, 0.9, now=1.0) == []    # flap a candidato 2 desde 1.0
    assert eng.observe(CAM, 1, 0.9, now=1.5) == []    # flap de vuelta a candidato 1 desde 1.5

    # Si el temporizador no se hubiese reiniciado en el último flap (1.5),
    # 3.4 - 0.0 = 3.4s ya superaría el umbral de 2s y emitiría de más.
    assert eng.observe(CAM, 1, 0.9, now=3.4) == []    # 3.4 - 1.5 = 1.9s < 2s
    events = eng.observe(CAM, 1, 0.9, now=3.5)         # 3.5 - 1.5 = 2.0s >= 2s
    assert len(events) == 1
    assert events[0].type == "person_detected"
