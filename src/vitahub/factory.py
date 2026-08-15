from __future__ import annotations

from vitahub.config import InferenceConfig
from vitahub.inference.base import Detector
from vitahub.inference.person_yolo import PersonDetector
from vitahub.inference.stub import StubDetector


def build_detector(cfg: InferenceConfig, weights_path: str) -> Detector:
    if cfg.detector == "stub":
        return StubDetector()
    if cfg.detector == "person_yolo":
        return PersonDetector.from_weights(weights_path, confidence=cfg.confidence)
    raise ValueError(f"Detector desconocido: {cfg.detector}")
