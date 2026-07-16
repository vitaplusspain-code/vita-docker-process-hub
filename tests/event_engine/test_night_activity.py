from datetime import datetime, timezone
from pathlib import Path

from event_engine.rules.night_activity import NightActivityUnusualRule
from event_engine.rules_config import load_rules_config
from event_engine.state import HubState

FIXTURES = Path(__file__).parent / "fixtures"
NIGHT = datetime(2026, 7, 10, 2, 0, 0, tzinfo=timezone.utc)
DAY = datetime(2026, 7, 10, 12, 0, 0, tzinfo=timezone.utc)


def _config():
    return load_rules_config(FIXTURES / "valid_rules.yaml")


def test_fires_when_occupied_during_sleep_window():
    state = HubState()
    state.set_zone_occupied("cam_salon", "cocina", True, NIGHT)
    event = NightActivityUnusualRule().evaluate("cam_salon", "cocina", state, _config(), NIGHT)
    assert event is not None
    assert event.type == "night_activity_unusual"


def test_none_outside_sleep_window():
    state = HubState()
    state.set_zone_occupied("cam_salon", "cocina", True, DAY)
    event = NightActivityUnusualRule().evaluate("cam_salon", "cocina", state, _config(), DAY)
    assert event is None


def test_none_when_zone_not_occupied():
    state = HubState()
    event = NightActivityUnusualRule().evaluate("cam_salon", "cocina", state, _config(), NIGHT)
    assert event is None


def test_fires_only_once_while_continuously_occupied():
    state = HubState()
    state.set_zone_occupied("cam_salon", "cocina", True, NIGHT)
    rule = NightActivityUnusualRule()
    assert rule.evaluate("cam_salon", "cocina", state, _config(), NIGHT) is not None
    later = NIGHT.replace(minute=30)
    assert rule.evaluate("cam_salon", "cocina", state, _config(), later) is None


def test_fires_again_after_zone_clears_and_reoccupies():
    state = HubState()
    t1, t2, t3 = NIGHT.replace(minute=0), NIGHT.replace(minute=10), NIGHT.replace(minute=20)
    rule = NightActivityUnusualRule()
    state.set_zone_occupied("cam_salon", "cocina", True, t1)
    assert rule.evaluate("cam_salon", "cocina", state, _config(), t1) is not None
    state.set_zone_occupied("cam_salon", "cocina", False, t2)
    rule.evaluate("cam_salon", "cocina", state, _config(), t2)
    state.set_zone_occupied("cam_salon", "cocina", True, t3)
    assert rule.evaluate("cam_salon", "cocina", state, _config(), t3) is not None


def test_none_when_camera_unavailable():
    state = HubState()
    state.set_zone_occupied("cam_salon", "cocina", True, NIGHT)
    state.set_camera_available("cam_salon", False)
    event = NightActivityUnusualRule().evaluate("cam_salon", "cocina", state, _config(), NIGHT)
    assert event is None
