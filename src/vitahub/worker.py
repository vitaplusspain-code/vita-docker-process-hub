from __future__ import annotations

from vitahub.analytics.event_engine import EventEngine
from vitahub.inference.base import Detector
from vitahub.models import Camera, Event
from vitahub.sinks.base import EventSink


def process_frame(
    camera: Camera,
    frame: object,
    detector: Detector,
    engine: EventEngine,
    sink: EventSink,
    now: float,
) -> list[Event]:
    detections = detector.detect(frame)
    person_count = sum(1 for d in detections if d.label == "person")
    confidence = max((d.confidence for d in detections), default=0.0)
    events = engine.observe(camera, person_count, confidence, now)
    for event in events:
        sink.emit(event)
    return events
