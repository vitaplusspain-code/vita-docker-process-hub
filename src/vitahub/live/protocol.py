from __future__ import annotations

import json
from dataclasses import dataclass

# Contrato con el frontend (spec §3.3). Cambiar algo aquí exige cambiar
# `LiveCloseReason` en shared/ de vita-web-family-app.
CLOSE_REASONS = frozenset(
    {"expired", "replaced", "limit", "invalid", "camera_unavailable", "shutdown"}
)


@dataclass(frozen=True)
class AuthMsg:
    token: str


@dataclass(frozen=True)
class SelectMsg:
    camera_id: str


ClientMessage = AuthMsg | SelectMsg


def parse_client_message(raw: str | bytes) -> ClientMessage | None:
    """Mensaje del viewer → tipo, o None si no es un mensaje válido del contrato."""
    try:
        data = json.loads(raw)
    except (ValueError, UnicodeDecodeError, RecursionError):
        # RecursionError: JSON anidado a propósito para agotar la pila del parser.
        return None
    if not isinstance(data, dict):
        return None
    kind = data.get("type")
    if kind == "auth":
        token = data.get("token")
        return AuthMsg(token) if isinstance(token, str) and token else None
    if kind == "select":
        camera_id = data.get("camera_id")
        return SelectMsg(camera_id) if isinstance(camera_id, str) and camera_id else None
    return None


def ready_message(cameras: list[tuple[str, str]]) -> str:
    return json.dumps(
        {"type": "ready", "cameras": [{"id": i, "name": n} for i, n in cameras]},
        ensure_ascii=False,
    )


def closed_message(reason: str) -> str:
    if reason not in CLOSE_REASONS:
        raise ValueError(f"reason fuera del contrato: {reason!r}")
    return json.dumps({"type": "closed", "reason": reason})
