from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

Severity = Literal["info", "warning", "critical"]


@dataclass
class Evidence:
    clip_ref: str | None = None
    uploaded: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {"clip_ref": self.clip_ref, "uploaded": self.uploaded}


def _new_event_id() -> str:
    return f"evt_{uuid.uuid4().hex}"


def _iso_z(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


@dataclass
class Event:
    hub_id: str
    user_id: str
    camera_id: str
    type: str
    zone: str | None
    start_time: datetime
    severity: Severity = "info"
    confidence: float = 1.0
    end_time: datetime | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    evidence: Evidence = field(default_factory=Evidence)
    schema_version: str = "1.0"
    event_id: str = field(default_factory=_new_event_id)

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "hub_id": self.hub_id,
            "user_id": self.user_id,
            "camera_id": self.camera_id,
            "type": self.type,
            "severity": self.severity,
            "confidence": self.confidence,
            "zone": self.zone,
            "start_time": _iso_z(self.start_time),
            "end_time": _iso_z(self.end_time) if self.end_time else None,
            "metadata": self.metadata,
            "evidence": self.evidence.to_dict(),
            "schema_version": self.schema_version,
        }
