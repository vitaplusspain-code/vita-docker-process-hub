from __future__ import annotations

from collections.abc import Callable
from typing import Any

from vitahub.inference.base import Detector
from vitahub.inference.person_yolo import _PERSON_CLASS_ID, _to_bbox, _to_float
from vitahub.models import Detection, Keypoints


def _to_keypoints(value: object) -> Keypoints:
    tolist = getattr(value, "tolist", None)
    rows = tolist() if callable(tolist) else list(value)  # type: ignore[call-overload]
    return tuple((float(x), float(y), float(c)) for x, y, c in rows)


class PosePersonDetector(Detector):
    """Detector de persona con pose (yolo11n-pose).

    Devuelve exactamente las mismas cajas que PersonDetector (misma clase,
    mismo umbral) y además los 17 keypoints COCO de cada persona. Un solo
    modelo sirve a la presencia y a la analítica de caídas.
    """

    def __init__(self, model: Callable[..., Any], confidence: float = 0.4) -> None:
        self._model = model
        self._confidence = confidence

    def detect(self, frame: object) -> list[Detection]:
        results = self._model(frame, verbose=False)
        if not results:
            return []
        boxes = results[0].boxes
        # Un modelo sin cabeza de pose (p. ej. yolo11n.pt cargado por error)
        # devuelve keypoints=None: se degrada a cajas sin pose en vez de fallar.
        kp_obj = getattr(results[0], "keypoints", None)
        kp_rows = list(kp_obj.data) if kp_obj is not None else None
        detections: list[Detection] = []
        for idx, (cls, conf, xyxy) in enumerate(
            zip(boxes.cls, boxes.conf, boxes.xyxy, strict=True)
        ):
            if int(_to_float(cls)) != _PERSON_CLASS_ID:
                continue
            score = _to_float(conf)
            if score < self._confidence:
                continue
            keypoints = _to_keypoints(kp_rows[idx]) if kp_rows is not None else None
            detections.append(
                Detection(
                    label="person",
                    confidence=round(score, 3),
                    bbox=_to_bbox(xyxy),
                    keypoints=keypoints,
                )
            )
        return detections

    @classmethod
    def from_weights(cls, weights_path: str, confidence: float = 0.4) -> PosePersonDetector:
        from ultralytics import YOLO  # import perezoso: torch solo en runtime real

        return cls(model=YOLO(weights_path), confidence=confidence)
