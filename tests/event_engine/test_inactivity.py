from datetime import datetime, timedelta, timezone
from pathlib import Path

from event_engine.rules.inactivity import InactivityProlongedRule
from event_engine.rules_config import load_rules_config
from event_engine.state import HubState

FIXTURES = Path(__file__).parent / "fixtures"
NOON = datetime(2026, 7, 10, 12, 0, 0, tzinfo=timezone.utc)
NIGHT = datetime(2026, 7, 10, 3, 0, 0, tzinfo=timezone.utc)


def _config():
    return load_rules_config(FIXTURES / "valid_rules.yaml")


def test_none_when_no_motion_recorded():
    state = HubState()
    rule = InactivityProlongedRule()
    assert rule.evaluate("cam_salon", None, state, _config(), NOON) is None


def test_none_when_within_threshold():
    state = HubState()
    state.touch_motion("cam_salon", NOON - timedelta(minutes=30))
    rule = InactivityProlongedRule()
    assert rule.evaluate("cam_salon", None, state, _config(), NOON) is None


def test_fires_once_when_threshold_exceeded():
    state = HubState()
    state.touch_motion("cam_salon", NOON - timedelta(minutes=241))
    rule = InactivityProlongedRule()
    first = rule.evaluate("cam_salon", None, state, _config(), NOON)
    assert first is not None
    assert first.type == "inactivity_prolonged"
    second = rule.evaluate("cam_salon", None, state, _config(), NOON + timedelta(seconds=1))
    assert second is None


def test_fires_again_after_motion_resumes():
    state = HubState()
    state.touch_motion("cam_salon", NOON - timedelta(minutes=241))
    rule = InactivityProlongedRule()
    assert rule.evaluate("cam_salon", None, state, _config(), NOON) is not None
    state.touch_motion("cam_salon", NOON)
    later = NOON + timedelta(minutes=241)
    assert rule.evaluate("cam_salon", None, state, _config(), later) is not None


def test_none_outside_expected_activity_window():
    state = HubState()
    state.touch_motion("cam_salon", NIGHT - timedelta(minutes=500))
    rule = InactivityProlongedRule()
    assert rule.evaluate("cam_salon", None, state, _config(), NIGHT) is None


def test_none_when_camera_unavailable():
    state = HubState()
    state.touch_motion("cam_salon", NOON - timedelta(minutes=300))
    state.set_camera_available("cam_salon", False)
    rule = InactivityProlongedRule()
    assert rule.evaluate("cam_salon", None, state, _config(), NOON) is None
