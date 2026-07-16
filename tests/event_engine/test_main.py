import asyncio
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

from event_engine.event_store import EventStore
from event_engine.main import EventEngine
from event_engine.models import Event
from event_engine.rules_config import load_rules_config

FIXTURES = Path(__file__).parent / "fixtures"


def _engine(debounce_seconds=0.01):
    config = load_rules_config(FIXTURES / "valid_rules.yaml")
    store = EventStore(":memory:")
    return EventEngine(config, store, debounce_seconds=debounce_seconds), store


async def test_frigate_event_persists_presence_zone_after_debounce():
    engine, store = _engine()
    payload = {"after": {"camera": "cam_salon", "current_zones": ["cocina"]}}
    await engine.on_frigate_event(payload)
    await asyncio.sleep(0.05)
    pending = store.get_pending()
    assert len(pending) == 1
    assert pending[0]["type"] == "presence_zone"
    assert pending[0]["zone"] == "cocina"


async def test_room_zone_exit_produces_no_extra_event():
    engine, store = _engine()
    enter = {"after": {"camera": "cam_salon", "current_zones": ["cocina"]}}
    exit_ = {"after": {"camera": "cam_salon", "current_zones": []}}
    await engine.on_frigate_event(enter)
    await asyncio.sleep(0.05)
    await engine.on_frigate_event(exit_)
    await asyncio.sleep(0.05)
    types = [e["type"] for e in store.get_pending()]
    assert types == ["presence_zone"]


async def test_door_zone_entry_produces_home_entry():
    engine, store = _engine()
    payload = {"after": {"camera": "cam_salon", "current_zones": ["puerta"]}}
    await engine.on_frigate_event(payload)
    await asyncio.sleep(0.05)
    pending = store.get_pending()
    assert len(pending) == 1
    assert pending[0]["type"] == "home_entry"


async def test_door_zone_exit_produces_home_exit():
    engine, store = _engine()
    enter = {"after": {"camera": "cam_salon", "current_zones": ["puerta"]}}
    exit_ = {"after": {"camera": "cam_salon", "current_zones": []}}
    await engine.on_frigate_event(enter)
    await asyncio.sleep(0.05)
    await engine.on_frigate_event(exit_)
    await asyncio.sleep(0.05)
    types = [e["type"] for e in store.get_pending()]
    assert types == ["home_entry", "home_exit"]


async def test_malformed_frigate_payload_is_dropped_not_raised(caplog):
    """mqtt_client (Task 10) only guarantees valid JSON, not valid business
    shape. on_frigate_event runs inside mqtt_client.run_forever(), which is
    awaited directly in main()'s asyncio.gather -- an uncaught exception here
    would crash the whole event-engine process, violating "nunca se lanza el
    proceso". A malformed but valid-JSON payload must be logged and dropped.
    """
    engine, store = _engine()
    malformed_payloads = [
        {"after": "not-a-dict"},
        {"after": {"camera": "cam_salon", "current_zones": 42}},
    ]
    with caplog.at_level(logging.WARNING, logger="event_engine"):
        for payload in malformed_payloads:
            await engine.on_frigate_event(payload)  # must not raise

    assert store.get_pending() == []
    assert any("malformed frigate event payload" in record.message for record in caplog.records)


async def test_availability_false_marks_all_configured_cameras_unavailable():
    engine, _ = _engine()
    await engine.on_frigate_availability(False)
    assert engine.state.camera("cam_salon").available is False


def test_run_tick_once_fires_inactivity_when_threshold_exceeded():
    engine, store = _engine()
    now = datetime(2026, 7, 10, 12, 0, 0, tzinfo=timezone.utc)
    engine.state.touch_motion("cam_salon", now - timedelta(minutes=241))
    engine.run_tick_once(now=now)
    types = [e["type"] for e in store.get_pending()]
    assert "inactivity_prolonged" in types


def test_run_tick_once_fires_night_activity_when_zone_occupied_at_night():
    engine, store = _engine()
    now = datetime(2026, 7, 10, 2, 0, 0, tzinfo=timezone.utc)
    engine.state.set_zone_occupied("cam_salon", "cocina", True, now)
    engine.run_tick_once(now=now)
    types = [e["type"] for e in store.get_pending()]
    assert "night_activity_unusual" in types


def test_bootstrap_last_motion_seeds_state_from_store():
    config = load_rules_config(FIXTURES / "valid_rules.yaml")
    store = EventStore(":memory:")
    store.save_event(
        Event(
            hub_id="h",
            user_id="u",
            camera_id="cam_salon",
            type="presence_zone",
            zone="cocina",
            start_time=datetime(2026, 7, 10, 9, 0, 0, tzinfo=timezone.utc),
        )
    )
    engine = EventEngine(config, store)
    engine.bootstrap_last_motion()
    assert engine.state.camera("cam_salon").last_motion == datetime(
        2026, 7, 10, 9, 0, 0, tzinfo=timezone.utc
    )


def test_persist_and_log_swallows_save_event_runtime_error_and_logs_critical(
    monkeypatch, caplog
):
    """Deviation from the brief: EventStore.save_event retries 3x then raises
    RuntimeError on persistent failure (Task 5). _persist_and_log is invoked
    from run_tick_once (which runs directly inside asyncio.gather in main()),
    so an uncaught RuntimeError there would crash the whole event-engine
    process -- violating the plan's "nunca se lanza el proceso" constraint.
    This test asserts the error is caught, logged as critical, and never
    propagates.
    """
    engine, store = _engine()
    now = datetime(2026, 7, 10, 12, 0, 0, tzinfo=timezone.utc)
    engine.state.touch_motion("cam_salon", now - timedelta(minutes=241))

    def _raise(event):
        raise RuntimeError(f"failed to persist event {event.event_id}")

    monkeypatch.setattr(store, "save_event", _raise)

    with caplog.at_level(logging.CRITICAL, logger="event_engine"):
        engine.run_tick_once(now=now)  # must not raise

    assert any(
        record.levelno == logging.CRITICAL and "event lost" in record.message
        for record in caplog.records
    )
