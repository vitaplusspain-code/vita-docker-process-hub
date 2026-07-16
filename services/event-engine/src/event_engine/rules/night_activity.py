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


class NightActivityUnusualRule:
    event_type = "night_activity_unusual"

    def __init__(self) -> None:
        self._fired: dict[tuple[str, str], bool] = {}

    def evaluate(
        self,
        camera_id: str,
        zone_id: str,
        state: HubState,
        config: RulesConfig,
        now: datetime,
    ) -> Event | None:
        camera_cfg = config.cameras.get(camera_id)
        if camera_cfg is None or zone_id not in camera_cfg.zones:
            return None
        camera = state.camera(camera_id)
        if not camera.available:
            return None
        key = (camera_id, zone_id)
        zone = camera.zone(zone_id)
        if not zone.occupied:
            self._fired[key] = False
            return None
        window = config.schedules.sleep_window
        if not _within_window(now, window.start, window.end):
            return None
        if self._fired.get(key):
            return None
        self._fired[key] = True
        return Event(
            hub_id=config.hub_id,
            user_id=config.user_id,
            camera_id=camera_id,
            type=self.event_type,
            zone=zone_id,
            start_time=zone.last_change or now,
        )
