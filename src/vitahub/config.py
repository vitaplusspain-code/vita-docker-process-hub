from __future__ import annotations

import os
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from vitahub.ingest.rtsp import strip_credentials
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


# Rutas por defecto de los tres ficheros que deja scripts/provision-hub.sh
# (repo vitaplus-aws-architecture) en el Jetson.
_DEFAULT_CERT_DIR = "/data/certs"


@dataclass
class UplinkConfig:
    enabled: bool = False
    topic_prefix: str = "vita/hub"
    endpoint: str = ""
    ca_path: str = ""
    cert_path: str = ""
    key_path: str = ""


@dataclass
class HubConfig:
    hub_id: str
    discovery: DiscoveryConfig
    inference: InferenceConfig
    cameras: list[Camera]
    credentials: Credentials
    uplink: UplinkConfig = field(default_factory=UplinkConfig)


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
            # `strip_credentials` también aquí, no solo en `registry.py`: un
            # hub.yaml editado a mano (el propio proyecto lo documenta e
            # invita a hacerlo) puede traer `usuario:clave@` en la URI, y sin
            # limpiar al leer, la siguiente `save_cameras` cementaría la
            # contraseña en disco indefinidamente.
            cameras.append(
                Camera(
                    id=str(entry["id"]),
                    name=str(entry["name"]),
                    last_ip=str(entry["last_ip"]),
                    enabled=bool(entry.get("enabled", True)),
                    rtsp_main=strip_credentials(str(entry.get("rtsp_main") or "")),
                    rtsp_sub=strip_credentials(str(entry.get("rtsp_sub") or "")),
                )
            )
        except KeyError as exc:
            raise ConfigError(f"Cámara en config sin campo obligatorio {exc}") from exc
        except TypeError as exc:
            raise ConfigError("Cámara en config no es un mapping YAML") from exc
    return cameras


def _uplink_from_raw(raw: Mapping[str, object], env: Mapping[str, str]) -> UplinkConfig:
    """Lee la sección `uplink` y verifica que la instalación está completa.

    Falla el arranque a propósito cuando `enabled` es true y falta algo: es un
    error de instalación, y el técnico que sembró el certificado ESTÁ delante.
    Mismo criterio que `_credentials_from_env` con la credencial ONVIF. Un
    uplink que arranca en silencio sin poder publicar es un hogar que parece
    instalado y no reporta nada.
    """
    enabled = bool(raw.get("enabled", False))
    topic_prefix = str(raw.get("topic_prefix", "vita/hub"))
    if not topic_prefix:
        raise ConfigError("Campo 'uplink.topic_prefix' no puede estar vacío")
    if not enabled:
        return UplinkConfig(enabled=False, topic_prefix=topic_prefix)

    endpoint = env.get("VITAHUB_IOT_ENDPOINT", "")
    if not endpoint:
        raise ConfigError(
            "uplink.enabled es true pero falta la variable de entorno "
            "VITAHUB_IOT_ENDPOINT (el endpoint ATS de la cuenta; lo imprime "
            "scripts/provision-hub.sh del repo de arquitectura)"
        )

    paths = {
        "ca_path": env.get("VITAHUB_IOT_CA", f"{_DEFAULT_CERT_DIR}/AmazonRootCA1.pem"),
        "cert_path": env.get("VITAHUB_IOT_CERT", f"{_DEFAULT_CERT_DIR}/certificate.pem.crt"),
        "key_path": env.get("VITAHUB_IOT_KEY", f"{_DEFAULT_CERT_DIR}/private.pem.key"),
    }
    for name, value in paths.items():
        if not Path(value).is_file():
            raise ConfigError(
                f"uplink.enabled es true pero no existe el fichero de certificado "
                f"{value} ({name}) — siémbralo con scripts/provision-hub.sh"
            )

    return UplinkConfig(
        enabled=True,
        topic_prefix=topic_prefix,
        endpoint=endpoint,
        ca_path=paths["ca_path"],
        cert_path=paths["cert_path"],
        key_path=paths["key_path"],
    )


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

    up_raw = raw.get("uplink") or {}
    if not isinstance(up_raw, dict):
        raise ConfigError("Sección 'uplink' debe ser un mapping YAML")

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

    stream = str(inf_raw.get("stream", "substream"))
    if stream not in ("main", "substream"):
        # Cualquier valor no reconocido cae en el stream principal en
        # rescan.py (RescanService._rescan compara contra "substream"), sin
        # ni una línea de log: una errata en config/hub.example.yaml
        # ("sub_stream", "Substream") dobla la carga de decodificación e
        # inferencia en el Jetson en silencio. Se rechaza en vez de adivinar.
        raise ConfigError(
            "Campo 'inference.stream' debe ser 'main' o 'substream' "
            f"(recibido: {stream!r})"
        )

    return HubConfig(
        hub_id=str(hub_id),
        discovery=DiscoveryConfig(interval_seconds=interval_seconds),
        inference=InferenceConfig(
            detector=str(inf_raw.get("detector", "person_yolo")),
            sample_fps=sample_fps,
            confidence=confidence,
            stream=stream,
        ),
        cameras=_cameras_from_raw(raw.get("cameras") or []),
        credentials=credentials,
        uplink=_uplink_from_raw(up_raw, env),
    )


def save_cameras(path: Path, cameras: list[Camera]) -> None:
    raw = yaml.safe_load(path.read_text()) or {}
    # Nos negamos a guardar si lo releído no es una config válida (fichero
    # vacío, corrupto, o sin 'hub_id'): escribir aquí cementaría un hub.yaml
    # inválido permanente (load_config fallaría en cada arranque siguiente,
    # con restart: unless-stopped eso es un bucle de reinicio infinito).
    if not isinstance(raw, dict) or not raw.get("hub_id"):
        raise ConfigError(
            f"No se guarda el registro de cámaras: {path} está vacío, corrupto "
            "o sin 'hub_id' — se descarta el guardado para no cementar una "
            "config inválida"
        )
    raw["cameras"] = [
        {
            "id": c.id,
            "name": c.name,
            "last_ip": c.last_ip,
            # Última línea de defensa de la invariante "el YAML no contiene
            # secretos": aunque llegara una Camera con credenciales sin
            # limpiar, aquí no se escriben.
            "rtsp_main": strip_credentials(c.rtsp_main),
            "rtsp_sub": strip_credentials(c.rtsp_sub),
            "enabled": c.enabled,
        }
        for c in cameras
    ]
    fd, tmp = tempfile.mkstemp(
        dir=str(path.parent), prefix=".hub-", suffix=".yaml.tmp"
    )
    try:
        with os.fdopen(fd, "w") as f:
            f.write(yaml.safe_dump(raw, sort_keys=False, allow_unicode=True))
            # flush + fsync antes del replace: sin esto, un corte de luz justo
            # después de os.replace puede dejar el contenido del fichero
            # temporal solo en el buffer de la libc/el kernel, no en la eMMC,
            # y el hub.yaml resultante queda truncado tras el reinicio.
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
