from __future__ import annotations

import json
import logging
import sqlite3
import time
from pathlib import Path

from event_engine.models import Event

logger = logging.getLogger("event_store")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    event_id TEXT PRIMARY KEY,
    camera_id TEXT NOT NULL,
    type TEXT NOT NULL,
    payload TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL
);
"""

_MAX_ATTEMPTS = 3


class EventStore:
    def __init__(self, db_path: str | Path) -> None:
        self._conn = sqlite3.connect(str(db_path))
        self._conn.execute(_SCHEMA)
        self._conn.commit()

    def save_event(self, event: Event) -> None:
        payload = json.dumps(event.to_dict())
        last_error: Exception | None = None
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            try:
                self._conn.execute(
                    "INSERT OR IGNORE INTO events "
                    "(event_id, camera_id, type, payload, status, created_at) "
                    "VALUES (?, ?, ?, ?, 'pending', ?)",
                    (
                        event.event_id,
                        event.camera_id,
                        event.type,
                        payload,
                        event.start_time.isoformat(),
                    ),
                )
                self._conn.commit()
                return
            except sqlite3.OperationalError as exc:
                last_error = exc
                logger.warning(
                    "save_event attempt %d/%d failed for %s: %s",
                    attempt,
                    _MAX_ATTEMPTS,
                    event.event_id,
                    exc,
                )
                time.sleep(0.1 * attempt)
        logger.critical(
            "save_event failed after %d attempts for %s: %s",
            _MAX_ATTEMPTS,
            event.event_id,
            last_error,
        )
        raise RuntimeError(f"failed to persist event {event.event_id}") from last_error

    def get_pending(self) -> list[dict]:
        rows = self._conn.execute(
            "SELECT payload FROM events WHERE status = 'pending' ORDER BY created_at"
        ).fetchall()
        return [json.loads(row[0]) for row in rows]

    def mark_syncing(self, event_id: str) -> None:
        self._conn.execute("UPDATE events SET status = 'syncing' WHERE event_id = ?", (event_id,))
        self._conn.commit()

    def mark_synced(self, event_id: str) -> None:
        self._conn.execute("UPDATE events SET status = 'synced' WHERE event_id = ?", (event_id,))
        self._conn.commit()

    def last_motion_camera(self, camera_id: str) -> str | None:
        row = self._conn.execute(
            "SELECT payload FROM events WHERE camera_id = ? ORDER BY created_at DESC LIMIT 1",
            (camera_id,),
        ).fetchone()
        if row is None:
            return None
        return json.loads(row[0])["start_time"]

    def close(self) -> None:
        self._conn.close()
