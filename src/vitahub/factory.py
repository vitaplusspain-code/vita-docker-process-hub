from __future__ import annotations

from pathlib import Path

from vitahub.config import ConfigError, HubConfig, InferenceConfig
from vitahub.inference.base import Detector
from vitahub.inference.person_yolo import PersonDetector
from vitahub.inference.stub import StubDetector
from vitahub.logging_setup import get_logger
from vitahub.sinks.aws_iot import AwsIotSink, build_client, topic_for
from vitahub.sinks.base import EventSink
from vitahub.sinks.fanout import FanoutSink
from vitahub.sinks.stdout_json import StdoutJsonSink

_log = get_logger("factory")


def build_detector(cfg: InferenceConfig, weights_path: str) -> Detector:
    if cfg.detector == "stub":
        return StubDetector()
    if cfg.detector == "person_yolo":
        return PersonDetector.from_weights(weights_path, confidence=cfg.confidence)
    raise ValueError(f"Detector desconocido: {cfg.detector}")


def build_sink(cfg: HubConfig) -> EventSink:
    """El sink de stdout siempre; el de AWS solo si el uplink está habilitado.

    Con el uplink apagado devuelve el StdoutJsonSink pelado, sin envolverlo en
    un fanout de un solo elemento: el comportamiento de un hub sin uplink es
    exactamente el de antes de este slice, sin una capa de más en medio.
    """
    stdout: EventSink = StdoutJsonSink()
    if not cfg.uplink.enabled:
        return stdout

    # `load_config` solo comprueba que los tres ficheros existen
    # (`Path.is_file()`); no que sean PEM válidos. Un fichero truncado o
    # ilegible pasa esa validación y revienta aquí, en `client.tls_set()`,
    # con un `ssl.SSLError` que de otro modo escaparía de `build_sink` → `run`
    # → el `except Exception` de `main`: "fallo no controlado" + `exit(1)` +
    # bucle de reinicio, un traceback en vez de un mensaje de instalación. Se
    # relanza rápido — según el spec §4.4 es lo correcto, el técnico está
    # delante — pero como `ConfigError`, con la ruta y qué revisar.
    try:
        client = build_client(
            hub_id=cfg.hub_id,
            endpoint=cfg.uplink.endpoint,
            ca=Path(cfg.uplink.ca_path),
            cert=Path(cfg.uplink.cert_path),
            key=Path(cfg.uplink.key_path),
        )
    except Exception as exc:  # se traduce a ConfigError abajo (con la causa encadenada)
        raise ConfigError(
            "uplink.enabled es true pero no se pudo construir el cliente MQTT "
            f"({exc}) — revisa que los certificados en {cfg.uplink.ca_path}, "
            f"{cfg.uplink.cert_path} y {cfg.uplink.key_path} sean PEM válidos "
            "y correspondan al mismo hogar dado de alta con scripts/provision-hub.sh"
        ) from exc
    topic = topic_for(cfg.uplink.topic_prefix, cfg.hub_id)
    _log.info("uplink habilitado hacia %s", topic)
    return FanoutSink([stdout, AwsIotSink(client, topic)])
