from __future__ import annotations

from datetime import datetime, time

from event_engine.models import Event
from event_engine.rules_config import RulesConfig
from event_engine.state import HubState


def _within_window(now: datetime, start: time, end: time) -> bool:
    current = now.time()
    if start <= end:
        return start <= current <= end
    return current >= start or current <= end


class InactivityProlongedRule:
    event_type = "inactivity_prolonged"

    def __init__(self) -> None:
        self._fired_at: dict[str, datetime | None] = {}

    def evaluate(
        self,
        camera_id: str,
        zone_id: str | None,
        state: HubState,
        config: RulesConfig,
        now: datetime,
    ) -> Event | None:
        if camera_id not in config.cameras:
            return None
        camera = state.camera(camera_id)
        if not camera.available:
            return None
        window = config.schedules.expected_activity
        if not _within_window(now, window.start, window.end):
            self._fired_at[camera_id] = None
            return None
        if camera.last_motion is None:
            return None
        elapsed_minutes = (now - camera.last_motion).total_seconds() / 60
        if elapsed_minutes <= config.thresholds.inactivity_minutes:
            self._fired_at[camera_id] = None
            return None
        # If motion has changed since we last fired, reset the fired flag
        if self._fired_at.get(camera_id) != camera.last_motion:
            self._fired_at[camera_id] = camera.last_motion
            return Event(
                hub_id=config.hub_id,
                user_id=config.user_id,
                camera_id=camera_id,
                type=self.event_type,
                zone=None,
                start_time=camera.last_motion,
                metadata={"inactivity_minutes": round(elapsed_minutes, 1)},
            )
        return None
