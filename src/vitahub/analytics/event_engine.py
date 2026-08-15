from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from vitahub.models import Camera, Event


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass
class _CameraState:
    reported_count: int = 0
    pending_count: int | None = None
    pending_since: float = 0.0


class EventEngine:
    def __init__(
        self,
        hub_id: str,
        present_after_s: float = 2.0,
        absent_after_s: float = 5.0,
        clock: Callable[[], datetime] = _utcnow,
    ) -> None:
        self._hub_id = hub_id
        self._present_after_s = present_after_s
        self._absent_after_s = absent_after_s
        self._clock = clock
        self._states: dict[str, _CameraState] = {}

    def observe(
        self, camera: Camera, person_count: int, confidence: float, now: float
    ) -> list[Event]:
        state = self._states.setdefault(camera.id, _CameraState())

        if person_count == state.reported_count:
            state.pending_count = None
            return []

        if state.pending_count != person_count:
            state.pending_count = person_count
            state.pending_since = now
            return []

        required = self._required_hold(state.reported_count, person_count)
        if now - state.pending_since < required:
            return []

        event_type = self._event_type(state.reported_count, person_count)
        state.reported_count = person_count
        state.pending_count = None
        return [
            Event(
                hub_id=self._hub_id,
                camera_id=camera.id,
                camera_name=camera.name,
                type=event_type,
                severity="info",
                timestamp=self._clock().isoformat(),
                payload={
                    "person_count": person_count,
                    "confidence": round(confidence, 3),
                },
            )
        ]

    def _required_hold(self, old: int, new: int) -> float:
        if new == 0:
            return self._absent_after_s
        return self._present_after_s

    def _event_type(self, old: int, new: int) -> str:
        if old == 0 and new > 0:
            return "person_detected"
        if new == 0:
            return "person_absent"
        return "person_count_changed"
