from __future__ import annotations

from datetime import datetime

from event_engine.models import Event
from event_engine.rules_config import RulesConfig
from event_engine.state import HubState

_ENTRY = "home_entry"
_EXIT = "home_exit"


class HomeExitEntryRule:
    # No fixed event_type: the emitted Event.type depends on the transition
    # direction (entry vs exit), unlike single-outcome rules.
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
        if camera_cfg.zones[zone_id].type != "door":
            return None
        zone = state.camera(camera_id).zone(zone_id)
        event_type = _ENTRY if zone.occupied else _EXIT
        return Event(
            hub_id=config.hub_id,
            user_id=config.user_id,
            camera_id=camera_id,
            type=event_type,
            zone=zone_id,
            start_time=zone.last_change or now,
        )
