import json

from vitahub.models import Camera, Detection, Event


def test_detection_is_frozen():
    d = Detection(label="person", confidence=0.9, bbox=(0, 0, 10, 20))
    assert d.bbox == (0, 0, 10, 20)


def test_event_to_json_is_single_line_and_ordered():
    ev = Event(
        hub_id="hub-1",
        camera_id="onvif-abc",
        camera_name="salon",
        type="person_detected",
        severity="info",
        timestamp="2026-08-15T10:00:00Z",
        payload={"person_count": 1, "confidence": 0.82},
    )
    line = ev.to_json()
    assert "\n" not in line
    parsed = json.loads(line)
    assert parsed["schema_version"] == 1
    assert parsed["type"] == "person_detected"
    assert parsed["payload"]["person_count"] == 1
    assert next(iter(parsed.keys())) == "schema_version"


def test_camera_defaults_enabled():
    c = Camera(id="onvif-abc", name="salon", last_ip="192.168.1.190")
    assert c.enabled is True
