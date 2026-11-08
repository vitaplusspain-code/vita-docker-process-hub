"""Topics y payloads del named shadow `face-gallery` (AWS IoT)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Literal

from vitahub.admin.enrollment import PhotoReason

SHADOW_NAME = "face-gallery"

PhotoStatus = tuple[Literal["accepted"], None] | tuple[Literal["rejected"], PhotoReason]


@dataclass(frozen=True)
class ShadowTopics:
    get: str
    get_accepted: str
    get_rejected: str
    update: str
    update_delta: str
    update_rejected: str


def shadow_topics(hub_id: str) -> ShadowTopics:
    base = f"$aws/things/{hub_id}/shadow/name/{SHADOW_NAME}"
    return ShadowTopics(
        get=f"{base}/get",
        get_accepted=f"{base}/get/accepted",
        get_rejected=f"{base}/get/rejected",
        update=f"{base}/update",
        update_delta=f"{base}/update/delta",
        update_rejected=f"{base}/update/rejected",
    )


@dataclass(frozen=True)
class ShadowSnapshot:
    desired_version: int | None
    reported_version: int | None
    reported_identity_enabled: bool | None
    reported_photo_ids: frozenset[str]


def _as_int(value: Any) -> int | None:
    # bool es subclase de int: no vale como versión.
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def parse_get_accepted(raw: bytes) -> ShadowSnapshot:
    """Lee `get/accepted`. Lanza `ValueError` si el JSON está roto."""
    body = _as_dict(json.loads(raw))
    state = _as_dict(body.get("state"))
    desired = _as_dict(state.get("desired"))
    reported = _as_dict(state.get("reported"))
    enabled = reported.get("identity_enabled")
    return ShadowSnapshot(
        desired_version=_as_int(desired.get("version")),
        reported_version=_as_int(reported.get("version")),
        reported_identity_enabled=enabled if isinstance(enabled, bool) else None,
        reported_photo_ids=frozenset(_as_dict(reported.get("photos"))),
    )


def parse_delta_version(raw: bytes) -> int | None:
    """`state.version` del delta, o None si falta o no es entero."""
    state = _as_dict(_as_dict(json.loads(raw)).get("state"))
    return _as_int(state.get("version"))


def parse_error_code(raw: bytes) -> int | None:
    """`code` de un `*/rejected`, o None si falta o el JSON está roto.

    Solo se lee el código: el `message` no se loguea."""
    try:
        return _as_int(_as_dict(json.loads(raw)).get("code"))
    except ValueError:
        return None


def reported_payload(
    version: int,
    identity_enabled: bool,
    statuses: dict[str, PhotoStatus],
    previous_ids: frozenset[str],
) -> str:
    """El shadow fusiona JSON: lo que ya no está se borra mandando null."""
    photos: dict[str, dict[str, str] | None] = {}
    for photo_id, (status, reason) in statuses.items():
        photos[photo_id] = {"status": status} if reason is None else {"status": status, "reason": reason}
    for photo_id in previous_ids - statuses.keys():
        photos[photo_id] = None
    return json.dumps(
        {"state": {"reported": {"version": version, "identity_enabled": identity_enabled, "photos": photos}}}
    )


def identity_only_payload(identity_enabled: bool) -> str:
    return json.dumps({"state": {"reported": {"identity_enabled": identity_enabled}}})
