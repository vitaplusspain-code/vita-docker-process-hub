"""Subconjunto curado de hub.yaml editable por el técnico.

Solo tres campos; el resto del fichero se preserva byte a byte en estructura.
La candidata pasa por el load_config real antes de escribir: si no valida,
el hub.yaml queda como estaba (nunca cementamos una config que impida arrancar).
"""
from __future__ import annotations

import os
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

import yaml

from vitahub.admin.enrollment import PersonSummary
from vitahub.config import ConfigError, load_config


@dataclass(frozen=True)
class AdminSettings:
    fall_enabled: bool
    identity_enabled: bool
    match_threshold: float


def read_settings(config_path: Path) -> AdminSettings:
    raw = yaml.safe_load(config_path.read_text()) or {}
    if not isinstance(raw, dict):
        raise ConfigError(f"{config_path} no es un mapping YAML")
    inference = raw.get("inference") or {}
    fall = inference.get("fall") or {}
    identity = inference.get("identity") or {}
    return AdminSettings(
        fall_enabled=bool(fall.get("enabled", False)),
        identity_enabled=bool(identity.get("enabled", False)),
        match_threshold=float(identity.get("match_threshold", 0.4)),
    )


def write_settings(
    config_path: Path, settings: AdminSettings, env: Mapping[str, str]
) -> None:
    raw = yaml.safe_load(config_path.read_text()) or {}
    # Mismo guardarraíl que save_cameras: sobre un fichero corrupto no se
    # escribe (con restart: unless-stopped sería un bucle de reinicio).
    if not isinstance(raw, dict) or not raw.get("hub_id"):
        raise ConfigError(
            f"No se guarda la config: {config_path} está vacío, corrupto o sin 'hub_id'"
        )
    inference = raw.setdefault("inference", {})
    inference.setdefault("fall", {})["enabled"] = settings.fall_enabled
    identity = inference.setdefault("identity", {})
    identity["enabled"] = settings.identity_enabled
    identity["match_threshold"] = settings.match_threshold

    fd, tmp = tempfile.mkstemp(
        dir=str(config_path.parent), prefix=".hub-", suffix=".yaml.tmp"
    )
    try:
        with os.fdopen(fd, "w") as f:
            f.write(yaml.safe_dump(raw, sort_keys=False, allow_unicode=True))
            # flush + fsync antes del replace, como en save_cameras: un corte
            # de luz no debe dejar un hub.yaml truncado en la eMMC.
            f.flush()
            os.fsync(f.fileno())
        # La validación corre sobre el MISMO fichero que se va a promocionar.
        load_config(Path(tmp), env)
        os.replace(tmp, config_path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def validate_apply(settings: AdminSettings, people: list[PersonSummary]) -> None:
    if not settings.identity_enabled:
        return
    if not any(p.photos for p in people):
        raise ConfigError(
            "identity.enabled requiere al menos una persona enrolada con una foto"
        )
