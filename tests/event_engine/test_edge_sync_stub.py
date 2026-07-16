import asyncio
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
    event = _event()
    store.save_event(event)
    store.save_event(_event())
    stub = EdgeSyncStub(store)
    with caplog.at_level(logging.INFO):
        sent = stub.sync_once()
    assert sent == 2
    assert store.get_pending() == []
    assert "edge_sync_stub" in caplog.text
    # The event JSON must actually appear in the log line, not just be
    # passed via `extra=` (which the default formatter never renders) --
    # the manual verification checklist (Task 14) inspects this log to
    # confirm only structured event JSON leaves the containers.
    assert event.event_id in caplog.text
    assert '"type": "presence_zone"' in caplog.text


def test_sync_once_with_nothing_pending_returns_zero():
    store = EventStore(":memory:")
    stub = EdgeSyncStub(store)
    assert stub.sync_once() == 0


async def test_run_forever_survives_sync_once_failure(monkeypatch, caplog):
    store = EventStore(":memory:")
    stub = EdgeSyncStub(store, interval_seconds=0.01)

    calls = []

    def _failing_sync_once():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("boom")
        return 0

    monkeypatch.setattr(stub, "sync_once", _failing_sync_once)

    task = asyncio.create_task(stub.run_forever())
    with caplog.at_level(logging.ERROR):
        await asyncio.sleep(0.05)
    task.cancel()

    assert len(calls) >= 2
    assert "sync_once failed" in caplog.text
