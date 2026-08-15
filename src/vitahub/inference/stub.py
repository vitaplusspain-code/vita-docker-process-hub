from __future__ import annotations

from vitahub.inference.base import Detector
from vitahub.models import Detection


class StubDetector(Detector):
    def __init__(self, person_count: int = 0, confidence: float = 0.99) -> None:
        self._person_count = person_count
        self._confidence = confidence

    def detect(self, frame: object) -> list[Detection]:
        return [
            Detection(label="person", confidence=self._confidence, bbox=(0, 0, 1, 1))
            for _ in range(self._person_count)
        ]
