import numpy as np
import pytest

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


def _face_with_sim(sim):
    """Cara cuyo embedding tiene similitud `sim` con MARIA y 0 con PEPE."""
    emb = np.array([sim, 0.0, (1 - sim**2) ** 0.5], dtype=np.float32)
    return FaceObservation((130, 20, 170, 70), emb)


def test_provisional_track_keeps_extraction_running():
    ident = _identifier([[FACE]])
    m0 = _matches(Tracker(), [PERSON], 0.0)
    m0[0].track.identity = TrackIdentity("maria", 0.7, provisional=True)
    ident.identify(None, "cam-a", m0, 0.0)
    assert ident._engine.calls == 1  # type: ignore[attr-defined]


def test_provisional_relabelled_by_any_valid_match_of_other_person():
    face = _face_with_sim(0.45)  # válida (>0.4) pero menor que la confianza 0.9
    ident = _identifier([[face]])
    m0 = _matches(Tracker(), [PERSON], 0.0)
    m0[0].track.identity = TrackIdentity("pepe", 0.9, provisional=True)
    ident.identify(None, "cam-a", m0, 0.0)
    got = m0[0].track.identity
    assert got is not None
    assert got.person_id == "maria"
    assert got.confidence == pytest.approx(0.45, abs=1e-5)
    assert got.provisional is False


def test_non_provisional_keeps_identity_against_weaker_match_of_other_person():
    face = _face_with_sim(0.45)
    ident = _identifier([[face]])
    m0 = _matches(Tracker(), [PERSON], 0.0)
    m0[0].track.identity = TrackIdentity("pepe", 0.9)
    ident.identify(None, "cam-a", m0, 0.0)  # sin provisional no se extrae
    assert m0[0].track.identity == TrackIdentity("pepe", 0.9)
    # y aunque se aplique directamente, la regla sim > confidence lo mantiene
    ident._apply("cam-a", m0[0].track, "maria", 0.45)
    assert m0[0].track.identity == TrackIdentity("pepe", 0.9)


def test_provisional_confirmed_by_same_person():
    face = _face_with_sim(0.5)
    ident = _identifier([[face]])
    m0 = _matches(Tracker(), [PERSON], 0.0)
    m0[0].track.identity = TrackIdentity("maria", 0.7, provisional=True)
    ident.identify(None, "cam-a", m0, 0.0)
    assert m0[0].track.identity == TrackIdentity("maria", 0.7, provisional=False)


def test_provisional_respects_attempt_cadence():
    stranger = FaceObservation((130, 20, 170, 70), _unit([0, 0, 1]))  # no matchea a nadie
    ident = _identifier([[stranger], [stranger], [stranger]])
    m0 = _matches(Tracker(), [PERSON], 0.0)
    provisional = TrackIdentity("pepe", 0.7, provisional=True)
    m0[0].track.identity = provisional
    ident.identify(None, "cam-a", m0, 0.0)
    assert ident._engine.calls == 1  # type: ignore[attr-defined]
    assert m0[0].track.identity == provisional
    ident.identify(None, "cam-a", m0, 0.4)  # dentro de la ventana: no extrae
    assert ident._engine.calls == 1  # type: ignore[attr-defined]
    assert m0[0].track.identity == provisional
    ident.identify(None, "cam-a", m0, ATTEMPT_EVERY_S)
    assert ident._engine.calls == 2  # type: ignore[attr-defined]
    assert m0[0].track.identity == provisional
