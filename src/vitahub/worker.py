from __future__ import annotations

from vitahub.analytics.event_engine import EventEngine
from vitahub.analytics.fall_engine import FallEngine
from vitahub.inference.base import Detector
from vitahub.logging_setup import get_logger
from vitahub.models import Camera, Event
from vitahub.sinks.base import EventSink

_log = get_logger("worker")

# Cámaras para las que ya se ha logueado un fallo de la analítica de caídas:
# un bug en el engine se loguea una vez, no en cada frame a 2 fps.
_fall_failure_logged: set[str] = set()


def process_frame(
    camera: Camera,
    frame: object,
    detector: Detector,
    engine: EventEngine,
    sink: EventSink,
    now: float,
    fall_engine: FallEngine | None = None,
) -> list[Event]:
    detections = detector.detect(frame)
    person_count = sum(1 for d in detections if d.label == "person")
    confidence = max((d.confidence for d in detections), default=0.0)
    events = engine.observe(camera, person_count, confidence, now)
    if fall_engine is not None:
        try:
            events = events + fall_engine.observe(camera, detections, now)
        except Exception:  # noqa: BLE001 — la caída no debe tumbar la presencia
            if camera.id not in _fall_failure_logged:
                _fall_failure_logged.add(camera.id)
                _log.exception(
                    "cam %s: error en la analítica de caídas (se silencia a partir de ahora)",
                    camera.id,
                )
    for event in events:
        sink.emit(event)
    return events
