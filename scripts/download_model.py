"""Descarga los pesos YOLO en build (para embeberlos en la imagen)."""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

from ultralytics import YOLO


def main() -> int:
    dest = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("yolo11n.pt")
    # El nombre del fichero destino es el nombre del modelo en ultralytics
    # (yolo11n.pt, yolo11n-pose.pt...): un solo script para todos los pesos.
    model = YOLO(dest.name)  # descarga a la cache de ultralytics
    src = Path(getattr(model, "ckpt_path", "") or dest.name)
    if not src.exists():
        print(f"error: no se encontró el peso descargado ({src})", file=sys.stderr)
        return 1
    dest.parent.mkdir(parents=True, exist_ok=True)
    if src.resolve() != dest.resolve():
        shutil.copy(src, dest)
    print(f"modelo en {dest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
