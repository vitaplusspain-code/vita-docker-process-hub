from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone

from event_engine.edge_sync_stub import EdgeSyncStub
from event_engine.event_store import EventStore
from event_engine.mqtt_client import FrigateMqttClient
from event_engine.rules.home_exit_entry import HomeExitEntryRule
from event_engine.rules.inactivity import InactivityProlongedRule
from event_engine.rules.night_activity import NightActivityUnusualRule
from event_engine.rules.presence_zone import PresenceZoneRule
from event_engine.rules_config import RulesConfig, load_rules_config
from event_engine.state import HubState

logger = logging.getLogger("event_engine")

DEBOUNCE_SECONDS = 3.0
TICK_SECONDS = 30.0


class EventEngine:
    def __init__(
        self,
        config: RulesConfig,
        store: EventStore,
        debounce_seconds: float = DEBOUNCE_SECONDS,
        tick_seconds: float = TICK_SECONDS,
    ) -> None:
        self.config = config
        self.store = store
        self.state = HubState()
        self._debounce_seconds = debounce_seconds
        self._tick_seconds = tick_seconds
        self.presence_rule = PresenceZoneRule()
        self.door_rule = HomeExitEntryRule()
        self.inactivity_rule = InactivityProlongedRule()
        self.night_rule = NightActivityUnusualRule()

    def bootstrap_last_motion(self) -> None:
        for camera_id in self.config.cameras:
            last = self.store.last_motion_camera(camera_id)
            if last:
                self.state.seed_last_motion(camera_id, datetime.fromisoformat(last))

    async def on_frigate_event(self, payload: dict) -> None:
        # Frigate's own JSON is already guaranteed valid by mqtt_client (Task
        # 10), but not its business shape: "after" or "current_zones" could
        # be present with an unexpected type (e.g. not a dict/list). This
        # method runs inside mqtt_client.run_forever(), which is awaited
        # directly in main()'s asyncio.gather -- an uncaught exception here
        # would crash the whole event-engine process, the same "nunca se
        # lanza el proceso" constraint the _persist_and_log deviation above
        # protects. Malformed business content is logged and dropped instead.
        try:
            after = payload.get("after") or {}
            camera_id = after.get("camera")
            if camera_id is None or camera_id not in self.config.cameras:
                return
            now = datetime.now(timezone.utc)
            current_zones = set(after.get("current_zones") or [])
        except (AttributeError, TypeError) as exc:
            logger.warning("malformed frigate event payload, dropping: %s (%s)", payload, exc)
            return

        self.state.touch_motion(camera_id, now)

        camera_cfg = self.config.cameras[camera_id]
        for zone_id, zone_cfg in camera_cfg.zones.items():
            occupied = zone_id in current_zones
            _, changed = self.state.set_zone_occupied(camera_id, zone_id, occupied, now)
            if not changed:
                continue
            if zone_cfg.type == "door" or (zone_cfg.type == "room" and occupied):
                asyncio.create_task(self._debounced_zone_check(camera_id, zone_id))

    async def _debounced_zone_check(self, camera_id: str, zone_id: str) -> None:
        await asyncio.sleep(self._debounce_seconds)
        zone_cfg = self.config.cameras[camera_id].zones[zone_id]
        now = datetime.now(timezone.utc)
        if zone_cfg.type == "door":
            event = self.door_rule.evaluate(camera_id, zone_id, self.state, self.config, now)
        else:
            if not self.state.camera(camera_id).zone(zone_id).occupied:
                return
            event = self.presence_rule.evaluate(camera_id, zone_id, self.state, self.config, now)
        if event is not None:
            self._persist_and_log(event)

    async def on_frigate_availability(self, available: bool) -> None:
        for camera_id in self.config.cameras:
            self.state.set_camera_available(camera_id, available)

    def _persist_and_log(self, event) -> None:
        # Deviation from the brief: EventStore.save_event (Task 5) retries
        # write to SQLite 3 times, logs critical, then raises RuntimeError on
        # persistent failure -- correct at that layer. But this method is
        # called from run_tick_once, which itself runs directly inside
        # asyncio.gather(...) in main() via run_tick_loop, and also from
        # _debounced_zone_check (an asyncio.create_task callback). Letting a
        # RuntimeError propagate out of here would crash run_tick_loop and
        # take down the whole asyncio.gather, killing the event-engine
        # process -- violating the plan's global constraint that a write
        # failure "nunca se lanza el proceso". We catch it here, log
        # critical (so the loss is visible, not silent), and continue.
        try:
            self.store.save_event(event)
        except RuntimeError:
            logger.critical("event lost, could not persist: %s", event.to_dict())
            return
        logger.info("event generated: %s", event.to_dict())

    async def run_tick_loop(self) -> None:
        while True:
            await asyncio.sleep(self._tick_seconds)
            self.run_tick_once()

    def run_tick_once(self, now: datetime | None = None) -> None:
        now = now or datetime.now(timezone.utc)
        for camera_id, camera_cfg in self.config.cameras.items():
            event = self.inactivity_rule.evaluate(camera_id, None, self.state, self.config, now)
            if event is not None:
                self._persist_and_log(event)
            for zone_id in camera_cfg.zones:
                event = self.night_rule.evaluate(camera_id, zone_id, self.state, self.config, now)
                if event is not None:
                    self._persist_and_log(event)


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    config_path = os.environ.get("RULES_CONFIG_PATH", "/config/hub/rules.yaml")
    db_path = os.environ.get("EVENT_DB_PATH", "/data/events.db")
    mqtt_host = os.environ.get("MQTT_HOST", "mosquitto")
    mqtt_port = int(os.environ.get("MQTT_PORT", "1883"))

    config = load_rules_config(config_path)
    store = EventStore(db_path)
    engine = EventEngine(config, store)
    engine.bootstrap_last_motion()

    sync_stub = EdgeSyncStub(store)
    mqtt_client = FrigateMqttClient(
        mqtt_host, mqtt_port, engine.on_frigate_event, engine.on_frigate_availability
    )

    await asyncio.gather(
        mqtt_client.run_forever(),
        engine.run_tick_loop(),
        sync_stub.run_forever(),
    )


if __name__ == "__main__":
    asyncio.run(main())
