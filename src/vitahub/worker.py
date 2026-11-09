from __future__ import annotations

from vitahub.analytics.event_engine import EventEngine
from vitahub.analytics.fall_engine import FallEngine
from vitahub.analytics.inactivity_engine import InactivityEngine
from vitahub.analytics.tracker import Tracker
from vitahub.identity.face_id import FaceIdentifier
from vitahub.inference.base import Detector
from vitahub.logging_setup import get_logger
from vitahub.models import Camera, Event
from vitahub.sinks.base import EventSink

_log = get_logger("worker")

# Cámaras para las que ya se ha logueado un fallo de la analítica de caídas:
# un bug en el engine se loguea una vez, no en cada frame a 2 fps.
_fall_failure_logged: set[str] = set()

# Igual que _fall_failure_logged pero para la analítica de inactividad.
_inactivity_failure_logged: set[str] = set()

# Igual que _fall_failure_logged pero para el identificador: sin identidad
# hay person: null, nunca un hub caído, y el aviso no debe repetirse a 2 fps.
_identity_failure_logged: set[str] = set()


def process_frame(
    camera: Camera,
    frame: object,
    detector: Detector,
    engine: EventEngine,
    sink: EventSink,
    now: float,
    fall_engine: FallEngine | None = None,
    tracker: Tracker | None = None,
    identifier: FaceIdentifier | None = None,
    inactivity_engine: InactivityEngine | None = None,
) -> list[Event]:
    detections = detector.detect(frame)
    person_count = sum(1 for d in detections if d.label == "person")
    confidence = max((d.confidence for d in detections), default=0.0)
    events = engine.observe(camera, person_count, confidence, now)
    if tracker is not None:
        update = None
        try:
            update = tracker.observe(camera.id, detections, now)
            if identifier is not None:
                try:
                    identifier.identify(frame, camera.id, update.matches, now)
                except Exception:  # noqa: BLE001 — sin identidad hay person null, no un hub caído
                    if camera.id not in _identity_failure_logged:
                        _identity_failure_logged.add(camera.id)
                        _log.exception(
                            "cam %s: error en el identificador (se silencia a partir de ahora)",
                            camera.id,
                        )
        except Exception:  # noqa: BLE001 — el tracker no debe tumbar la presencia
            update = None
            if camera.id not in _fall_failure_logged:
                _fall_failure_logged.add(camera.id)
                _log.exception(
                    "cam %s: error en el seguimiento (se silencia a partir de ahora)",
                    camera.id,
                )
        # Cada motor en su propio try: el fallo de uno no salta al otro ni
        # a la presencia. Si el tracker falló no hay `update` y ninguno corre.
        if update is not None and fall_engine is not None:
            try:
                events = events + fall_engine.observe(camera, update, now)
            except Exception:  # noqa: BLE001 — la caída no debe tumbar la presencia
                if camera.id not in _fall_failure_logged:
                    _fall_failure_logged.add(camera.id)
                    _log.exception(
                        "cam %s: error en la analítica de caídas (se silencia a partir de ahora)",
                        camera.id,
                    )
        if update is not None and inactivity_engine is not None:
            try:
                events = events + inactivity_engine.observe(camera, update, now)
            except Exception:  # noqa: BLE001 — la inactividad no debe tumbar presencia ni caídas
                if camera.id not in _inactivity_failure_logged:
                    _inactivity_failure_logged.add(camera.id)
                    _log.exception(
                        "cam %s: error en la analítica de inactividad (se silencia a partir de ahora)",
                        camera.id,
                    )
    for event in events:
        sink.emit(event)
    return events
