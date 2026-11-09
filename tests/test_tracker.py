from vitahub.analytics.tracker import (
    PARK_TTL_S,
    TRACK_TTL_S,
    Resume,
    Tracker,
    TrackIdentity,
)
from vitahub.models import Detection


def _person(bbox):
    return Detection("person", 0.9, bbox)


def test_overlapping_detection_keeps_track_id():
    tracker = Tracker()
    u0 = tracker.observe("cam-a", [_person((10, 0, 50, 120))], 0.0)
    u1 = tracker.observe("cam-a", [_person((12, 0, 52, 120))], 0.5)
    assert len(u0.matches) == 1 and len(u1.matches) == 1
    assert u1.matches[0].track.track_id == u0.matches[0].track.track_id
    assert u1.matches[0].step_s == 0.5
    assert u0.matches[0].step_s == 0.0  # pista nueva


def test_center_fallback_rescues_forward_fall():
    # Caída hacia delante: caja de pie (30,0,70,120) y tumbada (80,60,180,130)
    # no solapan (IoU = 0) pero los centros quedan a menos de un lado mayor.
    tracker = Tracker()
    u0 = tracker.observe("cam-a", [_person((30, 0, 70, 120))], 0.0)
    u1 = tracker.observe("cam-a", [_person((80, 60, 180, 130))], 0.5)
    assert u1.matches[0].track.track_id == u0.matches[0].track.track_id


def test_far_detection_opens_new_track():
    tracker = Tracker()
    u0 = tracker.observe("cam-a", [_person((0, 0, 40, 120))], 0.0)
    u1 = tracker.observe("cam-a", [_person((500, 0, 540, 120))], 0.5)
    assert u1.matches[0].track.track_id != u0.matches[0].track.track_id


def test_expired_track_is_reported_lost_before_matching():
    tracker = Tracker()
    u0 = tracker.observe("cam-a", [_person((10, 0, 50, 120))], 0.0)
    old_id = u0.matches[0].track.track_id
    # TRACK_TTL_S sin verse: aunque la caja nueva solape, es pista nueva.
    u1 = tracker.observe("cam-a", [_person((10, 0, 50, 120))], TRACK_TTL_S + 0.5)
    assert [t.track_id for t in u1.lost] == [old_id]
    assert u1.matches[0].track.track_id != old_id


def test_empty_frame_reports_lost_after_ttl():
    tracker = Tracker()
    u0 = tracker.observe("cam-a", [_person((10, 0, 50, 120))], 0.0)
    assert tracker.observe("cam-a", [], 1.0).lost == []
    u2 = tracker.observe("cam-a", [], TRACK_TTL_S + 0.5)
    assert [t.track_id for t in u2.lost] == [u0.matches[0].track.track_id]
    # Perdida una vez, no se repite.
    assert tracker.observe("cam-a", [], TRACK_TTL_S + 1.0).lost == []


def test_two_people_keep_separate_tracks():
    tracker = Tracker()
    a, b = _person((0, 0, 40, 120)), _person((300, 0, 340, 120))
    u0 = tracker.observe("cam-a", [a, b], 0.0)
    u1 = tracker.observe("cam-a", [a, b], 0.5)
    ids0 = sorted(m.track.track_id for m in u0.matches)
    ids1 = sorted(m.track.track_id for m in u1.matches)
    assert ids0 == ids1 and len(set(ids0)) == 2


def test_cameras_do_not_share_tracks():
    tracker = Tracker()
    u_a = tracker.observe("cam-a", [_person((10, 0, 50, 120))], 0.0)
    u_b = tracker.observe("cam-b", [_person((10, 0, 50, 120))], 0.0)
    assert u_a.matches[0].track.track_id != u_b.matches[0].track.track_id


def test_non_person_detections_are_ignored():
    tracker = Tracker()
    update = tracker.observe("cam-a", [Detection("dog", 0.9, (0, 0, 40, 40))], 0.0)
    assert update.matches == [] and update.lost == []


def test_degenerate_bbox_never_raises():
    tracker = Tracker()
    bad = _person((10, 10, 10, 10))
    for i in range(10):
        tracker.observe("cam-a", [bad], i * 0.5)


def test_duplicate_detection_object_still_opens_two_tracks():
    # El dedup de detecciones sin pareja debe ser por índice en `persons`, no
    # por id(det): si el detector repite el mismo objeto Detection (dos
    # personas con caja idéntica), el greedy solo puede emparejar una con la
    # pista existente — la otra debe abrir pista nueva, no desaparecer.
    tracker = Tracker()
    tracker.observe("cam-a", [_person((10, 0, 50, 120))], 0.0)
    same = _person((10, 0, 50, 120))
    update = tracker.observe("cam-a", [same, same], 0.5)
    assert len(update.matches) == 2
    assert len({m.track.track_id for m in update.matches}) == 2


BOX = (10, 0, 50, 120)


def _still_ana(tracker, bbox=BOX, seconds=5.0, who="ana"):
    """Pista identificada y quieta `seconds` s; devuelve (pista, last_seen)."""
    u = tracker.observe("cam-a", [_person(bbox)], 0.0)
    track = u.matches[0].track
    track.identity = TrackIdentity(who, 0.7)
    t = 0.0
    while t < seconds:
        t += 1.0
        tracker.observe("cam-a", [_person(bbox)], t)
    return track, t


def _park(tracker, bbox=BOX, who="ana"):
    track, last = _still_ana(tracker, bbox, who=who)
    u = tracker.observe("cam-a", [], last + TRACK_TTL_S)
    return track, last, u


def test_still_identified_track_is_parked_when_lost():
    tracker = Tracker()
    track, last, u = _park(tracker)
    assert [t.track_id for t in u.lost] == [track.track_id]
    assert len(u.parked) == 1
    p = u.parked[0]
    assert p.track_id == track.track_id
    assert p.identity.person_id == "ana"
    assert p.parked_at == last
    assert p.bbox == BOX


def test_moving_track_is_not_parked():
    tracker = Tracker()
    u = tracker.observe("cam-a", [_person((0, 0, 40, 120))], 0.0)
    u.matches[0].track.identity = TrackIdentity("ana", 0.7)
    for i in range(1, 8):  # desplazamiento de media caja: IoU < STILL_IOU
        x = i * 20
        tracker.observe("cam-a", [_person((x, 0, x + 40, 120))], float(i))
    u = tracker.observe("cam-a", [], 7.0 + TRACK_TTL_S)
    assert len(u.lost) == 1
    assert u.parked == []


def test_anonymous_track_is_not_parked():
    tracker = Tracker()
    tracker.observe("cam-a", [_person(BOX)], 0.0)
    for t in range(1, 6):
        tracker.observe("cam-a", [_person(BOX)], float(t))
    u = tracker.observe("cam-a", [], 5.0 + TRACK_TTL_S)
    assert len(u.lost) == 1
    assert u.parked == []


def test_new_track_in_same_box_resumes_parked_identity():
    tracker = Tracker()
    track, last, _ = _park(tracker)
    u = tracker.observe("cam-a", [_person(BOX)], last + 4.0)
    new = u.matches[0].track
    assert new.track_id != track.track_id
    assert new.identity == TrackIdentity("ana", 0.7, provisional=True)
    assert u.resumed == [Resume(track=new, previous_track_id=track.track_id)]
    # Consumida: otra pista nueva en la misma caja no la hereda otra vez.
    tracker.observe("cam-a", [], last + 4.0 + TRACK_TTL_S)
    u2 = tracker.observe("cam-a", [_person(BOX)], last + 20.0)
    assert u2.matches[0].track.identity is None
    assert u2.resumed == []


def test_new_track_elsewhere_does_not_resume():
    tracker = Tracker()
    _, last, _ = _park(tracker)
    u = tracker.observe("cam-a", [_person((500, 0, 540, 120))], last + 4.0)
    assert u.matches[0].track.identity is None
    assert u.resumed == []


def test_best_iou_parked_wins():
    tracker = Tracker()
    ana_box, luis_box = (0, 0, 100, 200), (20, 0, 120, 200)
    u = tracker.observe("cam-a", [_person(ana_box), _person(luis_box)], 0.0)
    for m in u.matches:
        m.track.identity = TrackIdentity(
            "ana" if m.detection.bbox == ana_box else "luis", 0.7
        )
    for t in range(1, 6):
        tracker.observe("cam-a", [_person(ana_box), _person(luis_box)], float(t))
    u = tracker.observe("cam-a", [], 5.0 + TRACK_TTL_S)
    assert len(u.parked) == 2
    u = tracker.observe("cam-a", [_person((15, 0, 115, 200))], 10.0)
    assert u.matches[0].track.identity is not None
    assert u.matches[0].track.identity.person_id == "luis"


def test_parked_identity_expires_after_ttl():
    tracker = Tracker()
    _, last, u = _park(tracker)
    parked = u.parked[0]
    assert tracker.observe("cam-a", [], last + PARK_TTL_S - 1.0).expired == []
    u2 = tracker.observe("cam-a", [], last + PARK_TTL_S)
    assert u2.expired == [parked]
    assert tracker.observe("cam-a", [], last + PARK_TTL_S + 1.0).expired == []


def test_frames_resume_after_long_gap_expire_before_resume():
    tracker = Tracker()
    track, last = _still_ana(tracker)
    u = tracker.observe("cam-a", [_person(BOX)], last + 600.0)
    assert [t.track_id for t in u.lost] == [track.track_id]
    assert len(u.parked) == 1
    assert u.expired == u.parked
    assert u.matches[0].track.identity is None
    assert u.resumed == []


def test_parked_identity_of_person_already_on_live_track_is_not_resumed():
    tracker = Tracker()
    _, last, _ = _park(tracker)  # "ana" aparcada en BOX
    other = (400, 0, 440, 120)
    # "ana" aparece ya identificada en otra caja (p. ej. por cara) y sigue viva.
    u = tracker.observe("cam-a", [_person(other)], last + 4.0)
    u.matches[0].track.identity = TrackIdentity("ana", 0.9)
    # Una pista anónima en la caja aparcada no hereda: "ana" ya está en pantalla.
    u2 = tracker.observe("cam-a", [_person(other), _person(BOX)], last + 4.5)
    anon = next(m.track for m in u2.matches if m.detection.bbox == BOX)
    assert anon.identity is None
    assert u2.resumed == []
