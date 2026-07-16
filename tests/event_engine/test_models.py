from datetime import datetime, timezone

from event_engine.models import Event


def _event(**overrides):
    kwargs = dict(
        hub_id="hub_dev_001",
        user_id="usr_dev_001",
        camera_id="cam_salon",
        type="presence_zone",
        zone="cocina",
        start_time=datetime(2026, 7, 10, 10, 32, 5, tzinfo=timezone.utc),
    )
    kwargs.update(overrides)
    return Event(**kwargs)


def test_event_to_dict_matches_contract():
    data = _event().to_dict()
    assert data["hub_id"] == "hub_dev_001"
    assert data["user_id"] == "usr_dev_001"
    assert data["camera_id"] == "cam_salon"
    assert data["type"] == "presence_zone"
    assert data["severity"] == "info"
    assert data["confidence"] == 1.0
    assert data["zone"] == "cocina"
    assert data["start_time"] == "2026-07-10T10:32:05Z"
    assert data["end_time"] is None
    assert data["metadata"] == {}
    assert data["evidence"] == {"clip_ref": None, "uploaded": False}
    assert data["schema_version"] == "1.0"
    assert data["event_id"].startswith("evt_")


def test_each_event_gets_a_unique_id():
    assert _event().event_id != _event().event_id


def test_end_time_serializes_when_present():
    data = _event(end_time=datetime(2026, 7, 10, 10, 33, 40, tzinfo=timezone.utc)).to_dict()
    assert data["end_time"] == "2026-07-10T10:33:40Z"
