"""Envoltorio de InsightFace (SCRFD + ArcFace) con los pesos embebidos en la imagen.

Fino a propósito: instanciarlo de verdad carga los ONNX, así que se verifica
con scripts/replay_video.py sobre vídeo real, igual que los detectores YOLO.
El único unit test (ver tests/test_insightface_engine.py) stubea
insightface.app.FaceAnalysis para comprobar que from_weights no filtra sus
prints a stdout.
"""
from __future__ import annotations

import contextlib
import sys
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

        # InsightFace imprime a stdout al cargar los ONNX ("find model:",
        # "Applied providers:", "set det-size:") y stdout es el flujo de
        # eventos JSON-lines. from_weights puede correr de forma perezosa
        # (primera foto subida en el admin, con hilos de cámara ya vivos), así
        # que esta redirección SÍ cambia sys.stdout para todo el proceso
        # mientras dura la carga. Que eso no descarrile eventos depende de
        # StdoutJsonSink: captura su stream en __init__, no en cada emit(), así
        # que el redirect de aquí no puede desviarlos.
        with contextlib.redirect_stdout(sys.stderr):
            app = FaceAnalysis(
                name="buffalo_s",
                root=root,
                allowed_modules=["detection", "recognition"],
            )
            # ctx_id=0 usa GPU si onnxruntime la ofrece. El paquete onnxruntime
            # de PyPI es CPU-only: en el Orin hace falta la wheel de NVIDIA
            # (onnxruntime-gpu para Jetson) para que ctx_id=0 use GPU de verdad.
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
