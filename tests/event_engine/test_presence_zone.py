from datetime import datetime, timezone
from pathlib import Path

from event_engine.rules.presence_zone import PresenceZoneRule
from event_engine.rules_config import load_rules_config
from event_engine.state import HubState

FIXTURES = Path(__file__).parent / "fixtures"
AT = datetime(2026, 7, 10, 10, 0, 0, tzinfo=timezone.utc)


def _config():
    return load_rules_config(FIXTURES / "valid_rules.yaml")


def test_fires_when_room_zone_becomes_occupied():
    config = _config()
    state = HubState()
    state.set_zone_occupied("cam_salon", "cocina", True, AT)
    event = PresenceZoneRule().evaluate("cam_salon", "cocina", state, config, AT)
    assert event is not None
    assert event.type == "presence_zone"
    assert event.zone == "cocina"
    assert event.start_time == AT


def test_none_when_zone_not_occupied():
    config = _config()
    state = HubState()
    event = PresenceZoneRule().evaluate("cam_salon", "cocina", state, config, AT)
    assert event is None


def test_none_for_door_zones():
    config = _config()
    state = HubState()
    state.set_zone_occupied("cam_salon", "puerta", True, AT)
    event = PresenceZoneRule().evaluate("cam_salon", "puerta", state, config, AT)
    assert event is None


def test_none_for_unknown_camera_or_zone():
    config = _config()
    state = HubState()
    assert PresenceZoneRule().evaluate("cam_unknown", "cocina", state, config, AT) is None
    assert PresenceZoneRule().evaluate("cam_salon", "unknown_zone", state, config, AT) is None
