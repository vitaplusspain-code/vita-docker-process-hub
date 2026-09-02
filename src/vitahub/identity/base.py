from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

import numpy as np
import numpy.typing as npt


@dataclass(frozen=True)
class FaceObservation:
    """Una cara encontrada en una imagen: caja en píxeles y embedding L2-normalizado."""

    bbox: tuple[int, int, int, int]
    embedding: npt.NDArray[np.float32]


class FaceEngine(ABC):
    """Detección + embedding facial. Implementaciones: InsightFace y stub de tests."""

    @abstractmethod
    def extract(self, image: object) -> list[FaceObservation]: ...
