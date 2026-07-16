import logging
from datetime import datetime, timezone

from event_engine.edge_sync_stub import EdgeSyncStub
from event_engine.event_store import EventStore
from event_engine.models import Event


def _event():
    return Event(
        hub_id="hub_dev_001",
        user_id="usr_dev_001",
        camera_id="cam_salon",
        type="presence_zone",
        zone="cocina",
        start_time=datetime(2026, 7, 10, 10, 0, 0, tzinfo=timezone.utc),
    )


def test_sync_once_marks_all_pending_as_synced(caplog):
    store = EventStore(":memory:")
    store.save_event(_event())
    store.save_event(_event())
    stub = EdgeSyncStub(store)
    with caplog.at_level(logging.INFO):
        sent = stub.sync_once()
    assert sent == 2
    assert store.get_pending() == []
    assert "edge_sync_stub" in caplog.text


def test_sync_once_with_nothing_pending_returns_zero():
    store = EventStore(":memory:")
    stub = EdgeSyncStub(store)
    assert stub.sync_once() == 0
