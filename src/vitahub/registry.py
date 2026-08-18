from __future__ import annotations

from dataclasses import dataclass

from vitahub.ingest.rtsp import strip_credentials
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
    kind: str  # "added" | "ip_changed" | "uri_changed"


def reconcile(
    existing: list[Camera], discovered: list[DiscoveredCamera]
) -> tuple[list[Camera], list[RegistryChange]]:
    by_id = {c.id: c for c in existing}
    changes: list[RegistryChange] = []
    next_index = len(existing) + 1

    for dc in discovered:
        # Nunca se persiste userinfo: el YAML no contiene secretos.
        main = strip_credentials(dc.rtsp_main)
        sub = strip_credentials(dc.rtsp_sub)
        known = by_id.get(dc.id)
        if known is None:
            by_id[dc.id] = Camera(
                id=dc.id,
                name=f"camera-{next_index}",
                last_ip=dc.ip,
                enabled=True,
                rtsp_main=main,
                rtsp_sub=sub,
            )
            changes.append(RegistryChange(dc.id, "added"))
            next_index += 1
            continue

        if known.last_ip != dc.ip:
            known.last_ip = dc.ip
            changes.append(RegistryChange(dc.id, "ip_changed"))
        if (known.rtsp_main, known.rtsp_sub) != (main, sub):
            known.rtsp_main = main
            known.rtsp_sub = sub
            changes.append(RegistryChange(dc.id, "uri_changed"))

    return list(by_id.values()), changes
