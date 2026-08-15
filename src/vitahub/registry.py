from __future__ import annotations

from dataclasses import dataclass

from vitahub.models import Camera


@dataclass(frozen=True)
class DiscoveredCamera:
    id: str
    ip: str
    rtsp_main: str
    rtsp_sub: str


@dataclass(frozen=True)
class RegistryChange:
    camera_id: str
    kind: str  # "added" | "ip_changed"


def reconcile(
    existing: list[Camera], discovered: list[DiscoveredCamera]
) -> tuple[list[Camera], list[RegistryChange]]:
    by_id = {c.id: c for c in existing}
    changes: list[RegistryChange] = []
    next_index = len(existing) + 1

    for dc in discovered:
        known = by_id.get(dc.id)
        if known is None:
            by_id[dc.id] = Camera(
                id=dc.id, name=f"camera-{next_index}", last_ip=dc.ip, enabled=True
            )
            changes.append(RegistryChange(dc.id, "added"))
            next_index += 1
        elif known.last_ip != dc.ip:
            known.last_ip = dc.ip
            changes.append(RegistryChange(dc.id, "ip_changed"))

    return list(by_id.values()), changes
