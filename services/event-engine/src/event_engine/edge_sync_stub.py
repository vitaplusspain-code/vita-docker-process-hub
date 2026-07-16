from __future__ import annotations

import asyncio
import json
import logging

from event_engine.event_store import EventStore

logger = logging.getLogger("edge_sync_stub")


class EdgeSyncStub:
    def __init__(self, store: EventStore, interval_seconds: float = 5.0) -> None:
        self._store = store
        self._interval = interval_seconds

    def sync_once(self) -> int:
        pending = self._store.get_pending()
        for payload in pending:
            event_id = payload["event_id"]
            self._store.mark_syncing(event_id)
            logger.info("edge_sync_stub: sending event %s: %s", event_id, json.dumps(payload))
            self._store.mark_synced(event_id)
        return len(pending)

    async def run_forever(self) -> None:
        while True:
            try:
                self.sync_once()
            except Exception:
                logger.exception("edge_sync_stub: sync_once failed, will retry next interval")
            await asyncio.sleep(self._interval)
