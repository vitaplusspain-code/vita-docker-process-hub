from vitahub.analytics.tracker import TRACK_TTL_S, Tracker
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
