"""Descarga los pesos YOLO en build (para embeberlos en la imagen)."""
from __future__ import annotations

import sys

from ultralytics import YOLO

if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else "yolo11n.pt"
    YOLO(target)  # descarga a la cache; se copia en el Dockerfile
    print(f"modelo {target} descargado")
