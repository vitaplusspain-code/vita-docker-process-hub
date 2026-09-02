"""Envoltorio de InsightFace (SCRFD + ArcFace) con los pesos embebidos en la imagen.

Fino a propósito y sin tests unitarios (instanciarlo carga los ONNX): se
verifica con scripts/replay_video.py sobre vídeo real, igual que los
detectores YOLO.
"""
from __future__ import annotations

from typing import Any

from vitahub.identity.base import FaceEngine, FaceObservation


class InsightFaceEngine(FaceEngine):
    def __init__(self, app: Any) -> None:
        self._app = app

    @classmethod
    def from_weights(cls, root: str) -> InsightFaceEngine:
        # Import aquí, no arriba: los tests y los hubs sin identidad no
        # necesitan tener insightface instalado.
        from insightface.app import FaceAnalysis

        app = FaceAnalysis(
            name="buffalo_s",
            root=root,
            allowed_modules=["detection", "recognition"],
        )
        # ctx_id=0 usa GPU si onnxruntime la ofrece (Orin) y cae a CPU si no (Mac).
        app.prepare(ctx_id=0, det_size=(640, 640))
        return cls(app)

    def extract(self, image: object) -> list[FaceObservation]:
        return [
            FaceObservation(
                bbox=(int(f.bbox[0]), int(f.bbox[1]), int(f.bbox[2]), int(f.bbox[3])),
                embedding=f.normed_embedding,
            )
            for f in self._app.get(image)
        ]
