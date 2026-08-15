from __future__ import annotations

from abc import ABC, abstractmethod

from vitahub.models import Event


class EventSink(ABC):
    @abstractmethod
    def emit(self, event: Event) -> None: ...

    @abstractmethod
    def close(self) -> None: ...
