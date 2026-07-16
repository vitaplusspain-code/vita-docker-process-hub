from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class ZoneState:
    occupied: bool = False
    last_change: datetime | None = None


@dataclass
class CameraState:
    zones: dict[str, ZoneState] = field(default_factory=dict)
    last_motion: datetime | None = None
    available: bool = True

    def zone(self, zone_id: str) -> ZoneState:
        return self.zones.setdefault(zone_id, ZoneState())


class HubState:
    def __init__(self) -> None:
        self._cameras: dict[str, CameraState] = {}

    def camera(self, camera_id: str) -> CameraState:
        return self._cameras.setdefault(camera_id, CameraState())

    def set_zone_occupied(
        self, camera_id: str, zone_id: str, occupied: bool, at: datetime
    ) -> tuple[bool, bool]:
        cam = self.camera(camera_id)
        zone = cam.zone(zone_id)
        previous = zone.occupied
        changed = previous != occupied
        if changed:
            zone.occupied = occupied
            zone.last_change = at
        cam.last_motion = at
        return previous, changed

    def touch_motion(self, camera_id: str, at: datetime) -> None:
        self.camera(camera_id).last_motion = at

    def set_camera_available(self, camera_id: str, available: bool) -> None:
        self.camera(camera_id).available = available

    def seed_last_motion(self, camera_id: str, at: datetime) -> None:
        cam = self.camera(camera_id)
        if cam.last_motion is None or at > cam.last_motion:
            cam.last_motion = at
