from __future__ import annotations

from vitahub.logging_setup import get_logger
from vitahub.models import Event
from vitahub.sinks.base import EventSink

_log = get_logger("sinks.fanout")


class FanoutSink(EventSink):
    """Emite cada evento por varios sinks, aislando el fallo de cada uno.

    Un sink que lanza no puede impedir que los demás reciban el evento. El
    caso concreto que esto protege: con el uplink a AWS caído, `stdout` tiene
    que seguir recibiendo, porque `docker logs` es la única herramienta de
    diagnóstico de un técnico delante del Jetson.
    """

    def __init__(self, sinks: list[EventSink]) -> None:
        self._sinks = sinks

    def emit(self, event: Event) -> None:
        for sink in self._sinks:
            try:
                sink.emit(event)
            except Exception:  # noqa: BLE001 — un sink roto no silencia a los demás
                _log.exception("fallo emitiendo por %s", type(sink).__name__)

    def close(self) -> None:
        for sink in self._sinks:
            try:
                sink.close()
            except Exception:  # noqa: BLE001 — cerrar uno no puede impedir cerrar el resto
                _log.exception("fallo cerrando %s", type(sink).__name__)
