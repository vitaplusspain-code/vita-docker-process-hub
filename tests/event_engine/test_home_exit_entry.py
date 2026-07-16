from datetime import datetime, timezone
from pathlib import Path

from event_engine.rules.home_exit_entry import HomeExitEntryRule
from event_engine.rules_config import load_rules_config
from event_engine.state import HubState

FIXTURES = Path(__file__).parent / "fixtures"


def _config():
    return load_rules_config(FIXTURES / "valid_rules.yaml")


def test_home_entry_when_door_zone_becomes_occupied():
    config = _config()
    state = HubState()
    at = datetime(2026, 7, 10, 10, 0, 0, tzinfo=timezone.utc)
    state.set_zone_occupied("cam_salon", "puerta", True, at)
    event = HomeExitEntryRule().evaluate("cam_salon", "puerta", state, config, at)
    assert event is not None
    assert event.type == "home_entry"
    assert event.zone == "puerta"


def test_home_exit_when_door_zone_becomes_unoccupied():
    config = _config()
    state = HubState()
    t1 = datetime(2026, 7, 10, 10, 0, 0, tzinfo=timezone.utc)
    t2 = datetime(2026, 7, 10, 10, 0, 5, tzinfo=timezone.utc)
    state.set_zone_occupied("cam_salon", "puerta", True, t1)
    state.set_zone_occupied("cam_salon", "puerta", False, t2)
    event = HomeExitEntryRule().evaluate("cam_salon", "puerta", state, config, t2)
    assert event is not None
    assert event.type == "home_exit"


def test_none_for_room_zones():
    config = _config()
    state = HubState()
    at = datetime(2026, 7, 10, 10, 0, 0, tzinfo=timezone.utc)
    state.set_zone_occupied("cam_salon", "cocina", True, at)
    event = HomeExitEntryRule().evaluate("cam_salon", "cocina", state, config, at)
    assert event is None
