from datetime import datetime, timezone

from event_engine.state import HubState

T1 = datetime(2026, 7, 10, 10, 0, 0, tzinfo=timezone.utc)
T2 = datetime(2026, 7, 10, 10, 5, 0, tzinfo=timezone.utc)


def test_set_zone_occupied_reports_transition_to_true():
    state = HubState()
    previous, changed = state.set_zone_occupied("cam_salon", "cocina", True, T1)
    assert previous is False
    assert changed is True
    zone = state.camera("cam_salon").zone("cocina")
    assert zone.occupied is True
    assert zone.last_change == T1


def test_set_zone_occupied_same_value_reports_no_change():
    state = HubState()
    state.set_zone_occupied("cam_salon", "cocina", True, T1)
    previous, changed = state.set_zone_occupied("cam_salon", "cocina", True, T2)
    assert previous is True
    assert changed is False
    assert state.camera("cam_salon").zone("cocina").last_change == T1


def test_set_zone_occupied_updates_camera_last_motion():
    state = HubState()
    state.set_zone_occupied("cam_salon", "cocina", True, T1)
    assert state.camera("cam_salon").last_motion == T1


def test_touch_motion_updates_last_motion_without_changing_zones():
    state = HubState()
    state.touch_motion("cam_salon", T1)
    assert state.camera("cam_salon").last_motion == T1
    assert state.camera("cam_salon").zones == {}


def test_set_camera_available_toggle():
    state = HubState()
    assert state.camera("cam_salon").available is True
    state.set_camera_available("cam_salon", False)
    assert state.camera("cam_salon").available is False


def test_seed_last_motion_only_advances():
    state = HubState()
    state.seed_last_motion("cam_salon", T2)
    state.seed_last_motion("cam_salon", T1)
    assert state.camera("cam_salon").last_motion == T2
