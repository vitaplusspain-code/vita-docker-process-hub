from __future__ import annotations

from abc import ABC, abstractmethod

from vitahub.models import Detection


class Detector(ABC):
    @abstractmethod
    def detect(self, frame: object) -> list[Detection]: ...
