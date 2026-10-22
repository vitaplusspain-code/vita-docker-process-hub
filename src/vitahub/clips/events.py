"""Construcción de los eventos `fall_clip_*` (siempre severidad info)."""
from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from vitahub.models import Event


def clip_event(
    hub_id: str,
    camera_id: str,
    camera_name: str,
    type: str,
    payload: dict[str, object],
    clock: Callable[[], datetime],
) -> Event:
    return Event(
        hub_id=hub_id,
        camera_id=camera_id,
        camera_name=camera_name,
        type=type,
        severity="info",
        timestamp=clock().isoformat(),
        payload=payload,
    )
