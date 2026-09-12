from __future__ import annotations

import sys
from typing import TextIO

from vitahub.models import Event
from vitahub.sinks.base import EventSink


class StdoutJsonSink(EventSink):
    def __init__(self) -> None:
        # Capturado aquí, no resuelto en cada emit(): el engine de identidad
        # carga sus pesos de forma perezosa (primera foto subida, con los
        # hilos de cámara ya vivos) y redirige sys.stdout a stderr mientras
        # dura esa carga. Si emit() mirara sys.stdout en ese momento, los
        # eventos emitidos durante la carga se perderían por ese stderr.
        self._out: TextIO = sys.stdout

    def emit(self, event: Event) -> None:
        self._out.write(event.to_json() + "\n")
        self._out.flush()

    def close(self) -> None:
        self._out.flush()
