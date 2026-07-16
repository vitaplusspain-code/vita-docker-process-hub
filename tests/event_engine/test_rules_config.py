from pathlib import Path

import pytest
from pydantic import ValidationError

from event_engine.rules_config import load_rules_config

FIXTURES = Path(__file__).parent / "fixtures"


def test_load_valid_rules_config():
    config = load_rules_config(FIXTURES / "valid_rules.yaml")
    assert config.hub_id == "hub_test_001"
    assert config.user_id == "usr_test_001"
    assert config.cameras["cam_salon"].zones["cocina"].type == "room"
    assert config.cameras["cam_salon"].zones["puerta"].type == "door"
    assert config.thresholds.inactivity_minutes == 240
    assert config.schedules.expected_activity.start.isoformat() == "07:00:00"


def test_invalid_polygon_is_rejected():
    with pytest.raises(ValidationError):
        load_rules_config(FIXTURES / "invalid_polygon.yaml")


def test_incoherent_schedule_is_rejected():
    with pytest.raises(ValidationError):
        load_rules_config(FIXTURES / "invalid_schedule.yaml")


def test_invalid_zone_type_is_rejected():
    # A typo'd zone type (e.g. "rom" instead of "room") used to pass
    # validation silently since Zone.type was a plain str -- every consumer
    # (main.py, all four rules) would then skip that zone forever with no
    # error. rules.yaml is the sole config surface for zones/schedules/
    # thresholds, so this must fail loudly at load time instead.
    with pytest.raises(ValidationError):
        load_rules_config(FIXTURES / "invalid_zone_type.yaml")
