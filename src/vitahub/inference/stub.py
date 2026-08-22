from __future__ import annotations

from vitahub.inference.base import Detector
from vitahub.models import Detection, Keypoints


class StubDetector(Detector):
    """Detector sin modelo: devuelve N personas fijas. Para pruebas y config sin GPU."""

    def __init__(
        self,
        person_count: int = 0,
        confidence: float = 0.99,
        keypoints: Keypoints | None = None,
        bbox: tuple[int, int, int, int] = (0, 0, 1, 1),
    ) -> None:
        self._person_count = person_count
        self._confidence = confidence
        self._keypoints = keypoints
        self._bbox = bbox

    def detect(self, frame: object) -> list[Detection]:
        return [
            Detection(
                label="person",
                confidence=self._confidence,
                bbox=self._bbox,
                keypoints=self._keypoints,
            )
            for _ in range(self._person_count)
        ]
