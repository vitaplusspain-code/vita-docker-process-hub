import numpy as np

import vitahub.worker as worker_module
from vitahub.analytics.event_engine import EventEngine
from vitahub.analytics.fall_engine import FallEngine
from vitahub.analytics.tracker import Tracker
from vitahub.identity.base import FaceObservation
from vitahub.identity.face_id import FaceIdentifier
from vitahub.identity.gallery import Gallery
from vitahub.identity.stub import StubFaceEngine
from vitahub.inference.stub import StubDetector
from vitahub.models import Camera, Event
from vitahub.worker import process_frame


class _RecordingSink:
    def __init__(self):
        self.events: list[Event] = []

    def emit(self, event: Event) -> None:
        self.events.append(event)

    def close(self) -> None:
        pass


CAM = Camera(id="onvif-a", name="salon", last_ip="10.0.0.5")


def test_process_frame_emits_person_detected_after_threshold():
    engine = EventEngine(hub_id="hub-1", present_after_s=2.0, absent_after_s=5.0)
    sink = _RecordingSink()
    detector = StubDetector(person_count=1)

    assert process_frame(CAM, None, detector, engine, sink, now=0.0) == []
    emitted = process_frame(CAM, None, detector, engine, sink, now=2.0)

    assert len(emitted) == 1
    assert emitted[0].type == "person_detected"
    assert sink.events == emitted


NOSE, L_SH, R_SH, L_HIP, R_HIP = 0, 5, 6, 11, 12


def _pose(nose, shoulders, hips, conf=0.9):
    kps = [(0.0, 0.0, 0.0)] * 17
    kps[NOSE] = (*nose, conf)
    kps[L_SH] = (shoulders[0] - 10, shoulders[1], conf)
    kps[R_SH] = (shoulders[0] + 10, shoulders[1], conf)
    kps[L_HIP] = (hips[0] - 10, hips[1], conf)
    kps[R_HIP] = (hips[0] + 10, hips[1], conf)
    return tuple(kps)


LYING_KPS = _pose(nose=(10, 110), shoulders=(30, 110), hips=(90, 110))
LYING_BOX = (0, 90, 120, 120)


def test_process_frame_without_fall_engine_is_unchanged():
    engine = EventEngine(hub_id="hub-1", present_after_s=2.0, absent_after_s=5.0)
    sink = _RecordingSink()
    detector = StubDetector(person_count=1, keypoints=LYING_KPS, bbox=LYING_BOX)
    process_frame(CAM, None, detector, engine, sink, now=0.0, fall_engine=None)
    emitted = process_frame(CAM, None, detector, engine, sink, now=2.0, fall_engine=None)
    assert [e.type for e in emitted] == ["person_detected"]


def test_process_frame_emits_presence_and_fall_through_same_sink():
    engine = EventEngine(hub_id="hub-1", present_after_s=2.0, absent_after_s=5.0)
    fall = FallEngine(hub_id="hub-1", min_score=0.3)
    tracker = Tracker()
    sink = _RecordingSink()
    detector = StubDetector(person_count=1, keypoints=LYING_KPS, bbox=LYING_BOX)
    all_events = []
    now = 0.0
    for _ in range(8):
        all_events += process_frame(
            CAM, None, detector, engine, sink, now, fall_engine=fall, tracker=tracker
        )
        now += 0.5
    types = [e.type for e in all_events]
    assert "person_detected" in types
    assert "fall_detected" in types
    assert sink.events == all_events


def test_fall_engine_exception_does_not_break_presence(caplog, monkeypatch):
    class _Boom:
        def observe(self, camera, update, now):
            raise RuntimeError("boom")

    # El registro de "ya avisado" es global al módulo: se aísla para que el
    # test no dependa de si otro test ya quemó esta cámara.
    monkeypatch.setattr(worker_module, "_fall_failure_logged", set())
    engine = EventEngine(hub_id="hub-1", present_after_s=2.0, absent_after_s=5.0)
    tracker = Tracker()
    sink = _RecordingSink()
    detector = StubDetector(person_count=1)
    process_frame(CAM, None, detector, engine, sink, now=0.0, fall_engine=_Boom(), tracker=tracker)
    emitted = process_frame(
        CAM, None, detector, engine, sink, now=2.0, fall_engine=_Boom(), tracker=tracker
    )
    assert [e.type for e in emitted] == ["person_detected"]
    assert sink.events == emitted
    assert sum("analítica de caídas" in r.message for r in caplog.records) == 1


def test_identifier_exception_does_not_break_falls(caplog, monkeypatch):
    class _BoomIdentifier:
        def identify(self, frame, camera_id, matches, now):
            raise RuntimeError("boom")

    monkeypatch.setattr(worker_module, "_identity_failure_logged", set())
    engine = EventEngine(hub_id="hub-1", present_after_s=2.0, absent_after_s=5.0)
    fall = FallEngine(hub_id="hub-1", min_score=0.3)
    tracker = Tracker()  # uno solo: la pista debe persistir entre frames
    sink = _RecordingSink()
    detector = StubDetector(person_count=1, keypoints=LYING_KPS, bbox=LYING_BOX)
    all_events = []
    now = 0.0
    for _ in range(8):
        all_events += process_frame(
            CAM,
            None,
            detector,
            engine,
            sink,
            now,
            fall_engine=fall,
            tracker=tracker,
            identifier=_BoomIdentifier(),
        )
        now += 0.5
    # Degradación silenciosa: sin identidad, pero la caída sale igual (person null).
    fall_events = [e for e in all_events if e.type == "fall_detected"]
    assert len(fall_events) == 1
    assert fall_events[0].payload["person"] is None
    assert sum("identificador" in r.message for r in caplog.records) == 1


# Cara dentro de LYING_BOX (0, 90, 120, 120): centro (60, 105), 50 px de alto
# (≥ MIN_FACE_PX). El embedding es el mismo vector unitario que la galería
# de "maria", así que cada extracción coincide.
MARIA = np.asarray([1.0, 0.0, 0.0], dtype=np.float32)
FACE = FaceObservation((40, 80, 80, 130), MARIA)


def test_identified_track_labels_fall_detected_person():
    engine = EventEngine(hub_id="hub-1", present_after_s=2.0, absent_after_s=5.0)
    fall = FallEngine(hub_id="hub-1", min_score=0.3)
    tracker = Tracker()  # uno solo: la identidad viaja colgada de la pista
    sink = _RecordingSink()
    detector = StubDetector(person_count=1, keypoints=LYING_KPS, bbox=LYING_BOX)
    gallery = Gallery(people={"maria": MARIA})
    # identify() extrae como mucho 1 vez/s por cámara (ATTEMPT_EVERY_S): con
    # frames cada 0.5 s solo se consume en el 1º y el 3º (t=0.0 y t=1.0), que
    # son las dos coincidencias consistentes que exige la regla de 2.
    face_engine = StubFaceEngine([[FACE], [FACE]])
    identifier = FaceIdentifier(face_engine, gallery, match_threshold=0.4)
    all_events = []
    now = 0.0
    for _ in range(8):
        all_events += process_frame(
            CAM,
            None,
            detector,
            engine,
            sink,
            now,
            fall_engine=fall,
            tracker=tracker,
            identifier=identifier,
        )
        now += 0.5

    fall_events = [e for e in all_events if e.type == "fall_detected"]
    assert len(fall_events) == 1
    assert fall_events[0].payload["person"] == {"id": "maria", "confidence": 1.0}
    assert face_engine.calls == 2
