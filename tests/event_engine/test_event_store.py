import logging
import sqlite3
from datetime import datetime, timezone

import pytest

from event_engine.event_store import EventStore
from event_engine.models import Event


def _event(start_time=datetime(2026, 7, 10, 10, 0, 0, tzinfo=timezone.utc)):
    return Event(
        hub_id="hub_dev_001",
        user_id="usr_dev_001",
        camera_id="cam_salon",
        type="presence_zone",
        zone="cocina",
        start_time=start_time,
    )


def test_save_and_get_pending():
    store = EventStore(":memory:")
    event = _event()
    store.save_event(event)
    pending = store.get_pending()
    assert len(pending) == 1
    assert pending[0]["event_id"] == event.event_id


def test_save_event_is_idempotent_by_event_id():
    store = EventStore(":memory:")
    event = _event()
    store.save_event(event)
    store.save_event(event)
    assert len(store.get_pending()) == 1


def test_mark_synced_removes_event_from_pending():
    store = EventStore(":memory:")
    event = _event()
    store.save_event(event)
    store.mark_syncing(event.event_id)
    store.mark_synced(event.event_id)
    assert store.get_pending() == []


def test_get_pending_includes_events_stuck_in_syncing():
    store = EventStore(":memory:")
    event = _event()
    store.save_event(event)
    store.mark_syncing(event.event_id)
    pending = store.get_pending()
    assert len(pending) == 1
    assert pending[0]["event_id"] == event.event_id


def test_last_motion_camera_returns_latest_start_time():
    store = EventStore(":memory:")
    store.save_event(_event(start_time=datetime(2026, 7, 10, 9, 0, 0, tzinfo=timezone.utc)))
    store.save_event(_event(start_time=datetime(2026, 7, 10, 11, 0, 0, tzinfo=timezone.utc)))
    assert store.last_motion_camera("cam_salon") == "2026-07-10T11:00:00Z"


def test_last_motion_camera_none_when_no_events():
    store = EventStore(":memory:")
    assert store.last_motion_camera("cam_salon") is None


def test_save_event_retries_then_raises_and_logs_critical(monkeypatch, caplog):
    store = EventStore(":memory:")

    class _AlwaysFailingConn:
        def execute(self, *args, **kwargs):
            raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(store, "_conn", _AlwaysFailingConn())

    with caplog.at_level(logging.CRITICAL):
        with pytest.raises(RuntimeError):
            store.save_event(_event())

    assert "failed after 3 attempts" in caplog.text
