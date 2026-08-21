from __future__ import annotations

from pathlib import Path

from vitahub.config import HubConfig, InferenceConfig
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

    client = build_client(
        hub_id=cfg.hub_id,
        endpoint=cfg.uplink.endpoint,
        ca=Path(cfg.uplink.ca_path),
        cert=Path(cfg.uplink.cert_path),
        key=Path(cfg.uplink.key_path),
    )
    topic = topic_for(cfg.uplink.topic_prefix, cfg.hub_id)
    _log.info("uplink habilitado hacia %s", topic)
    return FanoutSink([stdout, AwsIotSink(client, topic)])
