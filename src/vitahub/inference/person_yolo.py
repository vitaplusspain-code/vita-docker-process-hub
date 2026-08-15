from __future__ import annotations

from collections.abc import Callable
from typing import Any

from vitahub.inference.base import Detector
from vitahub.models import Detection

_PERSON_CLASS_ID = 0


def _to_float(value: object) -> float:
    item = getattr(value, "item", None)  # soporta tensores torch
    return float(item()) if callable(item) else float(value)  # type: ignore[arg-type]


def _to_bbox(value: object) -> tuple[int, int, int, int]:
    tolist = getattr(value, "tolist", None)
    coords = tolist() if callable(tolist) else list(value)  # type: ignore[call-overload]
    x1, y1, x2, y2 = (round(float(c)) for c in coords)
    return (x1, y1, x2, y2)


class PersonDetector(Detector):
    def __init__(self, model: Callable[..., Any], confidence: float = 0.4) -> None:
        self._model = model
        self._confidence = confidence

    def detect(self, frame: object) -> list[Detection]:
        results = self._model(frame, verbose=False)
        if not results:
            return []
        boxes = results[0].boxes
        detections: list[Detection] = []
        for cls, conf, xyxy in zip(boxes.cls, boxes.conf, boxes.xyxy, strict=True):
            if int(_to_float(cls)) != _PERSON_CLASS_ID:
                continue
            score = _to_float(conf)
            if score < self._confidence:
                continue
            detections.append(
                Detection(label="person", confidence=round(score, 3), bbox=_to_bbox(xyxy))
            )
        return detections

    @classmethod
    def from_weights(cls, weights_path: str, confidence: float = 0.4) -> PersonDetector:
        from ultralytics import YOLO  # import perezoso: torch solo en runtime real

        return cls(model=YOLO(weights_path), confidence=confidence)
