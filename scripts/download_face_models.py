"""Descarga el pack buffalo_s de InsightFace a la ruta de pesos embebidos.

Uso (build de la imagen y desarrollo local):

    python scripts/download_face_models.py [/app/models/insightface]
"""
from __future__ import annotations

import sys


def main() -> int:
    root = sys.argv[1] if len(sys.argv) > 1 else "/app/models/insightface"
    from insightface.app import FaceAnalysis

    # Instanciar descarga el pack si falta; prepare() valida que los ONNX cargan.
    app = FaceAnalysis(name="buffalo_s", root=root,
                       allowed_modules=["detection", "recognition"])
    app.prepare(ctx_id=0, det_size=(640, 640))
    print(f"modelos buffalo_s listos en {root}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
