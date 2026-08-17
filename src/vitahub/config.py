from __future__ import annotations

import os
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

import yaml

from vitahub.models import Camera


class ConfigError(Exception):
    pass


@dataclass
class DiscoveryConfig:
    interval_seconds: int = 60


@dataclass
class InferenceConfig:
    detector: str = "person_yolo"
    sample_fps: float = 2.0
    confidence: float = 0.4
    stream: str = "substream"


@dataclass
class Credentials:
    onvif_user: str
    onvif_password: str


@dataclass
class HubConfig:
    hub_id: str
    discovery: DiscoveryConfig
    inference: InferenceConfig
    cameras: list[Camera]
    credentials: Credentials


def _credentials_from_env(env: Mapping[str, str]) -> Credentials:
    user = env.get("VITAHUB_ONVIF_USER")
    password = env.get("VITAHUB_ONVIF_PASSWORD")
    if not user:
        raise ConfigError("Falta la variable de entorno VITAHUB_ONVIF_USER")
    if not password:
        raise ConfigError("Falta la variable de entorno VITAHUB_ONVIF_PASSWORD")
    return Credentials(onvif_user=user, onvif_password=password)


def _cameras_from_raw(raw: list[dict[str, object]]) -> list[Camera]:
    cameras: list[Camera] = []
    for entry in raw:
        try:
            cameras.append(
                Camera(
                    id=str(entry["id"]),
                    name=str(entry["name"]),
                    last_ip=str(entry["last_ip"]),
                    enabled=bool(entry.get("enabled", True)),
                )
            )
        except KeyError as exc:
            raise ConfigError(f"Cámara en config sin campo obligatorio {exc}") from exc
        except TypeError as exc:
            raise ConfigError("Cámara en config no es un mapping YAML") from exc
    return cameras


def load_config(path: Path, env: Mapping[str, str]) -> HubConfig:
    credentials = _credentials_from_env(env)
    try:
        raw = yaml.safe_load(path.read_text()) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigError(f"No se pudo leer/parsear la config {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError("La config raíz debe ser un mapping YAML")

    hub_id = raw.get("hub_id")
    if not hub_id:
        raise ConfigError("Falta 'hub_id' en la config")

    disc_raw = raw.get("discovery") or {}
    if not isinstance(disc_raw, dict):
        raise ConfigError("Sección 'discovery' debe ser un mapping YAML")

    inf_raw = raw.get("inference") or {}
    if not isinstance(inf_raw, dict):
        raise ConfigError("Sección 'inference' debe ser un mapping YAML")

    try:
        interval_seconds = int(disc_raw.get("interval_seconds", 60))
    except (ValueError, TypeError) as exc:
        raise ConfigError(
            "Campo 'discovery.interval_seconds' no es numérico"
        ) from exc
    if interval_seconds <= 0:
        raise ConfigError("Campo 'discovery.interval_seconds' debe ser mayor que 0")

    try:
        sample_fps = float(inf_raw.get("sample_fps", 2.0))
    except (ValueError, TypeError) as exc:
        raise ConfigError("Campo 'inference.sample_fps' no es numérico") from exc

    try:
        confidence = float(inf_raw.get("confidence", 0.4))
    except (ValueError, TypeError) as exc:
        raise ConfigError("Campo 'inference.confidence' no es numérico") from exc

    return HubConfig(
        hub_id=str(hub_id),
        discovery=DiscoveryConfig(interval_seconds=interval_seconds),
        inference=InferenceConfig(
            detector=str(inf_raw.get("detector", "person_yolo")),
            sample_fps=sample_fps,
            confidence=confidence,
            stream=str(inf_raw.get("stream", "substream")),
        ),
        cameras=_cameras_from_raw(raw.get("cameras") or []),
        credentials=credentials,
    )


def save_cameras(path: Path, cameras: list[Camera]) -> None:
    raw = yaml.safe_load(path.read_text()) or {}
    raw["cameras"] = [
        {"id": c.id, "name": c.name, "last_ip": c.last_ip, "enabled": c.enabled}
        for c in cameras
    ]
    fd, tmp = tempfile.mkstemp(
        dir=str(path.parent), prefix=".hub-", suffix=".yaml.tmp"
    )
    try:
        with os.fdopen(fd, "w") as f:
            f.write(yaml.safe_dump(raw, sort_keys=False, allow_unicode=True))
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
