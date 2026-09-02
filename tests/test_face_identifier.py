import numpy as np

from vitahub.analytics.tracker import Tracker, TrackIdentity
from vitahub.identity.base import FaceObservation
from vitahub.identity.face_id import ATTEMPT_EVERY_S, FaceIdentifier
from vitahub.identity.gallery import Gallery
from vitahub.identity.stub import StubFaceEngine
from vitahub.models import Detection


def _unit(v):
    arr = np.asarray(v, dtype=np.float32)
    return arr / np.linalg.norm(arr)


MARIA = _unit([1, 0, 0])
PEPE = _unit([0, 1, 0])
GALLERY = Gallery(people={"maria": MARIA, "pepe": PEPE})
# Persona con caja (100,0,200,300); su cara, dentro y de 50 px de alto.
PERSON = Detection("person", 0.9, (100, 0, 200, 300))
FACE = FaceObservation((130, 20, 170, 70), MARIA)


def _identifier(responses):
    return FaceIdentifier(StubFaceEngine(responses), GALLERY, match_threshold=0.4)


def _matches(tracker, dets, now):
    return tracker.observe("cam-a", dets, now).matches


def test_two_consistent_matches_label_the_track():
    ident = _identifier([[FACE], [FACE]])
    tracker = Tracker()
    m0 = _matches(tracker, [PERSON], 0.0)
    ident.identify(None, "cam-a", m0, 0.0)
    assert m0[0].track.identity is None  # una sola coincidencia no basta
    assert m0[0].track.pending_identity == TrackIdentity("maria", 1.0)
    m1 = _matches(tracker, [PERSON], ATTEMPT_EVERY_S)
    ident.identify(None, "cam-a", m1, ATTEMPT_EVERY_S)
    assert m1[0].track.identity == TrackIdentity("maria", 1.0)


def test_rate_limit_one_extraction_per_second_per_camera():
    ident = _identifier([[FACE], [FACE]])
    tracker = Tracker()
    ident.identify(None, "cam-a", _matches(tracker, [PERSON], 0.0), 0.0)
    ident.identify(None, "cam-a", _matches(tracker, [PERSON], 0.4), 0.4)  # dentro de la ventana
    assert ident._engine.calls == 1  # type: ignore[attr-defined]


def test_no_extraction_when_all_tracks_identified():
    ident = _identifier([[FACE]])
    tracker = Tracker()
    m0 = _matches(tracker, [PERSON], 0.0)
    m0[0].track.identity = TrackIdentity("maria", 0.9)
    ident.identify(None, "cam-a", m0, 0.0)
    assert ident._engine.calls == 0  # type: ignore[attr-defined]


def test_small_face_is_ignored():
    tiny = FaceObservation((130, 20, 160, 50), MARIA)  # 30 px < MIN_FACE_PX
    ident = _identifier([[tiny], [tiny]])
    tracker = Tracker()
    m0 = _matches(tracker, [PERSON], 0.0)
    ident.identify(None, "cam-a", m0, 0.0)
    assert m0[0].track.pending_identity is None


def test_face_outside_every_track_is_ignored():
    outside = FaceObservation((400, 400, 460, 460), MARIA)
    ident = _identifier([[outside]])
    tracker = Tracker()
    m0 = _matches(tracker, [PERSON], 0.0)
    ident.identify(None, "cam-a", m0, 0.0)
    assert m0[0].track.pending_identity is None


def test_unknown_face_never_labels():
    stranger = FaceObservation((130, 20, 170, 70), _unit([0, 0, 1]))
    ident = _identifier([[stranger], [stranger]])
    tracker = Tracker()
    m0 = _matches(tracker, [PERSON], 0.0)
    ident.identify(None, "cam-a", m0, 0.0)
    m1 = _matches(tracker, [PERSON], ATTEMPT_EVERY_S)
    ident.identify(None, "cam-a", m1, ATTEMPT_EVERY_S)
    assert m1[0].track.identity is None and m1[0].track.pending_identity is None


def test_conflicting_pending_is_replaced_not_confirmed():
    face_pepe = FaceObservation((130, 20, 170, 70), PEPE)
    ident = _identifier([[FACE], [face_pepe]])
    tracker = Tracker()
    ident.identify(None, "cam-a", _matches(tracker, [PERSON], 0.0), 0.0)
    m1 = _matches(tracker, [PERSON], ATTEMPT_EVERY_S)
    ident.identify(None, "cam-a", m1, ATTEMPT_EVERY_S)
    assert m1[0].track.identity is None
    assert m1[0].track.pending_identity == TrackIdentity("pepe", 1.0)


def test_correction_replaces_identity_with_better_match(caplog):
    # La pista quedó etiquetada como pepe con confianza baja (cruce de pistas);
    # aparece maria con similitud claramente mayor → se corrige.
    ident = _identifier([[FACE]])
    tracker = Tracker()
    far = Detection("person", 0.9, (500, 0, 600, 300))
    m0 = _matches(tracker, [PERSON, far], 0.0)
    labeled = next(m for m in m0 if m.detection is PERSON)
    labeled.track.identity = TrackIdentity("pepe", 0.45)
    ident.identify(None, "cam-a", m0, 0.0)  # far está anónima → sí extrae
    assert labeled.track.identity == TrackIdentity("maria", 1.0)
    assert any("identidad corregida" in r.message for r in caplog.records)


def test_face_assigned_to_smallest_containing_track():
    inner = Detection("person", 0.9, (120, 10, 180, 120))
    ident = _identifier([[FACE], [FACE]])
    tracker = Tracker()
    m0 = _matches(tracker, [PERSON, inner], 0.0)
    ident.identify(None, "cam-a", m0, 0.0)
    m1 = _matches(tracker, [PERSON, inner], ATTEMPT_EVERY_S)
    ident.identify(None, "cam-a", m1, ATTEMPT_EVERY_S)
    small = min(m1, key=lambda m: (m.track.bbox[2] - m.track.bbox[0])
                * (m.track.bbox[3] - m.track.bbox[1])).track
    assert small.identity == TrackIdentity("maria", 1.0)
