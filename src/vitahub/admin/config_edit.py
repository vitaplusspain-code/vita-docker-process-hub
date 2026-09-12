"""Subconjunto curado de hub.yaml editable por el técnico.

Solo tres campos; el resto del fichero sobrevive con su misma estructura de
claves y valores (y su orden), pero NO byte a byte: al pasar por
yaml.safe_load + yaml.safe_dump se pierden los comentarios y el formato
original (indentación, estilo de listas, etc.).
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
    # GET /api/config y POST /api/apply corren en un hilo de petición HTTP:
    # un hub.yaml borrado o con YAML roto no debe tirar ese hilo con un
    # traceback, sino contestar un 400 legible.
    try:
        raw = yaml.safe_load(config_path.read_text()) or {}
    except OSError as exc:
        raise ConfigError(f"no se puede leer {config_path}: {exc}") from exc
    except yaml.YAMLError as exc:
        raise ConfigError(f"{config_path} no es YAML válido: {exc}") from exc
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
    # load_gallery revienta al arrancar con CUALQUIER carpeta de persona sin
    # fotos válidas (gallery.py), no solo cuando no hay ninguna: si se borra
    # la última foto de una persona, la carpeta vacía queda ahí y el hub
    # entra en bucle de reinicio con restart: unless-stopped.
    if any(not p.photos for p in people):
        raise ConfigError(
            "identity.enabled: hay una persona enrolada sin fotos — bórrala o "
            "añádele al menos una foto antes de aplicar"
        )
