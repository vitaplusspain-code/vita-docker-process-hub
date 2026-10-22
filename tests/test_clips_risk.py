from __future__ import annotations

from datetime import UTC, datetime

import pytest

from vitahub.clips.events import clip_event
from vitahub.clips.risk import assess


@pytest.mark.parametrize(
    "drop, reason, expected",
    [
        (0.9, None, "risk"),
        (0.5, None, "risk"),
        (0.49, None, "slow"),
        (None, None, "risk"),
        (0.9, "track_lost", "risk"),
        (0.1, "track_lost", "slow"),
        (0.9, "upright", "got_up"),
        (None, "upright", "got_up"),
    ],
)
def test_assess(drop: float | None, reason: str | None, expected: str) -> None:
    assert assess(drop, reason) == expected


def test_clip_event_shape() -> None:
    ev = clip_event(
        "hub-1", "cam1", "Salón", "fall_clip_discarded", {"reason": "slow"},
        clock=lambda: datetime(2026, 10, 22, tzinfo=UTC),
    )
    assert (ev.type, ev.severity, ev.camera_id) == ("fall_clip_discarded", "info", "cam1")
    assert ev.timestamp == "2026-10-22T00:00:00+00:00"
    assert ev.hub_id == "hub-1"
    assert ev.camera_name == "Salón"
    assert ev.payload == {"reason": "slow"}
