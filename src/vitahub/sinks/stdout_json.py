from __future__ import annotations

import sys

from vitahub.models import Event
from vitahub.sinks.base import EventSink


class StdoutJsonSink(EventSink):
    def emit(self, event: Event) -> None:
        sys.stdout.write(event.to_json() + "\n")
        sys.stdout.flush()

    def close(self) -> None:
        sys.stdout.flush()
