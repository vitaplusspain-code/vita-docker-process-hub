from __future__ import annotations

from datetime import datetime

from vitahub.live.token import SessionClaims
from vitahub.models import Event


def live_view_started(
    hub_id: str, claims: SessionClaims, camera_id: str, camera_name: str, now: datetime
) -> Event:
    """Rastro de auditoría del lado hub (spec §8), independiente del audit_log de la API."""
    return Event(
        hub_id=hub_id,
        camera_id=camera_id,
        camera_name=camera_name,
        type="live_view_started",
        severity="info",
        timestamp=now.isoformat(),
        payload={"sid": claims.sid, "account_id": claims.sub, "camera_id": camera_id},
    )


def live_view_ended(
    hub_id: str,
    claims: SessionClaims,
    camera_id: str,
    camera_name: str,
    reason: str,
    seconds: float,
    now: datetime,
) -> Event:
    return Event(
        hub_id=hub_id,
        camera_id=camera_id,
        camera_name=camera_name,
        type="live_view_ended",
        severity="info",
        timestamp=now.isoformat(),
        payload={
            "sid": claims.sid,
            "account_id": claims.sub,
            "reason": reason,
            "seconds": int(seconds),
        },
    )
