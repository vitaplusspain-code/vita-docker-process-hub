from datetime import UTC, datetime, timedelta

from vitahub.analytics.inactivity_engine import (
    ANKLE_MIN_CONF,
    L_ANKLE,
    R_ANKLE,
    InactivityEngine,
    feet_point,
)
from vitahub.analytics.tracker import Match, Track, Tracker, TrackerUpdate, TrackIdentity
from vitahub.models import Camera, Detection

CAM = Camera(id="cam1", name="salon", last_ip="10.0.0.5")
T0 = datetime(2026, 8, 22, 10, 0, 0, tzinfo=UTC)
SIT = (100, 200, 200, 400)
ANA = TrackIdentity("ana", 0.7)


class _Clock:
    def __init__(self):
        self.t = T0

    def __call__(self):
        return self.t


def _wall(t):
    return (T0 + timedelta(seconds=t)).isoformat()


def _det(bbox, keypoints=None):
    return Detection("person", 0.9, bbox, keypoints)


def _setup(min_minutes=5.0):
    clock = _Clock()
    return InactivityEngine("hub-1", min_minutes=min_minutes, clock=clock), Tracker(), clock


def _run(engine, tracker, clock, frames, ident=lambda det: ANA):
    """frames: (now, detecciones) a 2 fps. Etiqueta las pistas nuevas con
    `ident(det)` antes de pasarlas al motor; el reloj sigue a `now`."""
    events = []
    for now, dets in frames:
        clock.t = T0 + timedelta(seconds=now)
        update = tracker.observe(CAM.id, dets, now)
        for m in update.matches:
            if m.track.identity is None:
                m.track.identity = ident(m.detection)
        events += engine.observe(CAM, update, now)
    return events


def _still(box, from_s, to_s):
    n = int((to_s - from_s) * 2)
    return [(from_s + i * 0.5, [_det(box)]) for i in range(n + 1)]


def _kps(ankles):
    kps = [(0.0, 0.0, 0.0)] * 17
    kps[L_ANKLE] = ankles[0]
    kps[R_ANKLE] = ankles[1]
    return tuple(kps)


def _ankles_at(x, bbox=SIT):
    """Misma caja, tobillos en x: mueve el punto de pies sin cambiar de pista."""
    return _det(bbox, _kps(((x - 10.0, 400.0, 0.9), (x + 10.0, 400.0, 0.9))))


def test_feet_point_uses_ankles_when_confident_else_box_bottom():
    good = ANKLE_MIN_CONF + 0.1
    bad = ANKLE_MIN_CONF - 0.1
    box = (100, 0, 200, 400)
    assert feet_point(_det(box, _kps(((120.0, 390.0, good), (140.0, 380.0, good))))) == (130.0, 385.0)
    # un tobillo dudoso: se cae a la base de la caja
    assert feet_point(_det(box, _kps(((120.0, 390.0, good), (140.0, 380.0, bad))))) == (150.0, 400)
    assert feet_point(_det(box)) == (150.0, 400)


def test_short_episode_emits_nothing():
    engine, tracker, clock = _setup()
    frames = _still(SIT, 0, 290) + _still((700, 200, 800, 400), 290.5, 300)
    assert _run(engine, tracker, clock, frames) == []


def test_started_at_min_minutes_with_real_start():
    engine, tracker, clock = _setup()
    events = _run(engine, tracker, clock, _still(SIT, 0, 400))
    assert [e.type for e in events] == ["inactivity_started"]
    e = events[0]
    assert e.severity == "info"
    assert e.camera_id == "cam1"
    assert e.payload["started_at"] == _wall(0)
    assert e.payload["still_s"] == 300.0
    assert e.payload["person"] == {"id": "ana", "confidence": 0.7}
    assert e.payload["episode_id"].startswith("cam1-")
    assert e.timestamp == _wall(300)


def test_standing_up_in_place_does_not_end():
    engine, tracker, clock = _setup()
    standing = (110, 0, 190, 400)
    frames = _still(SIT, 0, 360) + _still(standing, 360.5, 420)
    events = _run(engine, tracker, clock, frames)
    assert [e.type for e in events] == ["inactivity_started"]


def test_one_frame_jitter_does_not_end():
    engine, tracker, clock = _setup()
    # un único frame con los pies 300 px fuera (misma pista), luego vuelve
    frames = [(i * 0.5, [_ankles_at(150)]) for i in range(721)]
    frames[720] = (360.0, [_ankles_at(450)])
    frames += [(360.5 + i * 0.5, [_ankles_at(150)]) for i in range(60)]
    events = _run(engine, tracker, clock, frames)
    assert [e.type for e in events] == ["inactivity_started"]


def test_confirmed_move_ends_with_moved_and_last_inside_time():
    engine, tracker, clock = _setup()
    frames = [(i * 0.5, [_ankles_at(150)]) for i in range(720)]  # hasta 359.5
    frames += [(360 + i * 0.5, [_ankles_at(450)]) for i in range(10)]
    events = _run(engine, tracker, clock, frames)
    assert [e.type for e in events] == ["inactivity_started", "inactivity_ended"]
    start, end = events
    assert end.severity == "info"
    assert end.payload["reason"] == "moved"
    assert end.payload["ended_at"] == _wall(359.5)
    assert end.payload["duration_s"] == 359.5
    assert end.payload["started_at"] == _wall(0)
    assert end.payload["episode_id"] == start.payload["episode_id"]
    assert end.payload["person"] == {"id": "ana", "confidence": 0.7}
    # tras el fin arranca un candidato nuevo en la posición nueva, desde la
    # confirmación (360 + MOVE_CONFIRM_S), y emite 5 min después
    later = _run(engine, tracker, clock, [(365 + i * 0.5, [_ankles_at(450)]) for i in range(700)])
    assert [e.type for e in later] == ["inactivity_started"]
    assert later[0].payload["started_at"] == _wall(362)
    assert later[0].timestamp == _wall(662)


def test_identity_changed_ends_episode():
    engine, tracker, clock = _setup()
    events = _run(engine, tracker, clock, _still(SIT, 0, 310))
    assert [e.type for e in events] == ["inactivity_started"]
    tracker._cameras[CAM.id].tracks[0].identity = TrackIdentity("luis", 0.5)
    more = _run(engine, tracker, clock, _still(SIT, 310.5, 500))
    assert [e.type for e in more] == ["inactivity_ended"]
    end = more[0]
    assert end.payload["reason"] == "identity_changed"
    assert end.payload["person"] == {"id": "ana", "confidence": 0.7}
    assert end.payload["ended_at"] == _wall(310.5)
    assert end.payload["duration_s"] == 310.5
    # el episodio de luis arrancó en 310.5 y aún no llega a 5 min
    assert _run(engine, tracker, clock, _still(SIT, 501, 609)) == []
    late = _run(engine, tracker, clock, _still(SIT, 609.5, 611))
    assert [e.type for e in late] == ["inactivity_started"]
    assert late[0].payload["person"] == {"id": "luis", "confidence": 0.5}
    assert late[0].payload["started_at"] == _wall(310.5)
    assert late[0].payload["episode_id"] != events[0].payload["episode_id"]


def test_two_people_independent_episodes():
    engine, tracker, clock = _setup()
    a, b = (100, 200, 200, 400), (600, 200, 700, 400)

    def ident(det):
        return TrackIdentity("ana", 0.7) if det.bbox[0] < 300 else TrackIdentity("luis", 0.6)

    both = [_det(a), _det(b)]
    frames = [(i * 0.5, both) for i in range(720)]
    events = _run(engine, tracker, clock, frames, ident)
    assert sorted(e.payload["person"]["id"] for e in events) == ["ana", "luis"]
    assert len({e.payload["episode_id"] for e in events}) == 2
    # ana camina, luis sigue quieto: solo termina el episodio de ana
    walk = []
    for i in range(20):
        x = 100 + 40 * (i + 1)
        walk.append((360 + i * 0.5, [_det((x, 200, x + 100, 400)), _det(b)]))
    events = _run(engine, tracker, clock, walk, ident)
    assert [e.type for e in events] == ["inactivity_ended"]
    assert events[0].payload["person"]["id"] == "ana"


def test_anonymous_track_never_emits():
    engine, tracker, clock = _setup()
    assert _run(engine, tracker, clock, _still(SIT, 0, 600), ident=lambda d: None) == []


def test_person_yolo_boxes_without_keypoints():
    engine, tracker, clock = _setup()
    frames = [(i * 0.5, [Detection("person", 0.8, SIT, keypoints=None)]) for i in range(700)]
    events = _run(engine, tracker, clock, frames)
    assert [e.type for e in events] == ["inactivity_started"]


def _excursion(out_s):
    """Quieta 6 min, `out_s` segundos con los pies fuera, y de vuelta."""
    frames = [(i * 0.5, [_ankles_at(150)]) for i in range(720)]
    n_out = int(out_s * 2) + 1
    frames += [(360 + i * 0.5, [_ankles_at(450)]) for i in range(n_out)]
    back = 360 + n_out * 0.5
    frames += [(back + i * 0.5, [_ankles_at(150)]) for i in range(20)]
    return frames


def test_excursion_below_confirm_threshold_does_not_end():
    engine, tracker, clock = _setup()
    events = _run(engine, tracker, clock, _excursion(1.5))
    assert [e.type for e in events] == ["inactivity_started"]


def test_excursion_at_confirm_threshold_ends():
    engine, tracker, clock = _setup()
    events = _run(engine, tracker, clock, _excursion(2.0))
    assert [e.type for e in events] == ["inactivity_started", "inactivity_ended"]
    assert events[1].payload["reason"] == "moved"


def test_slow_drift_past_radius_ends_episode():
    engine, tracker, clock = _setup()
    # 10 px por muestra: cada paso cae dentro del radio del anterior, pero el
    # ancla es fija y el total (radio 100) acaba fuera
    frames = [(i * 0.5, [_ankles_at(150)]) for i in range(720)]
    frames += [(360 + i * 0.5, [_ankles_at(150 + 10 * (i + 1))]) for i in range(30)]
    events = _run(engine, tracker, clock, frames)
    assert [e.type for e in events] == ["inactivity_started", "inactivity_ended"]
    # última muestra dentro: desplazamiento 100 (i = 9 -> t = 364.5)
    assert events[1].payload["ended_at"] == _wall(364.5)


def _direct(engine, clock, frames):
    """Alimenta el motor con una pista fija e identificada, sin pasar por el
    Tracker (permite cajas que el emparejamiento por IoU no seguiría)."""
    track = Track(1, frames[0][1].bbox, 0.0, ANA)
    events = []
    for now, det in frames:
        clock.t = T0 + timedelta(seconds=now)
        track.bbox = det.bbox
        track.last_seen = now
        events += engine.observe(CAM, TrackerUpdate([Match(track, det, 0.5)], []), now)
    return events


def test_scale_grows_with_largest_box_of_episode():
    engine, _, clock = _setup()
    standing = (110, 0, 190, 400)
    # de pie y se desplaza 150 px: dentro del radio de la caja de pie (200),
    # fuera del de la sentada (100) -> no termina
    moved = (260, 0, 340, 400)
    frames = [(i * 0.5, _det(SIT)) for i in range(721)]
    frames += [(360.5 + i * 0.5, _det(standing)) for i in range(10)]
    frames += [(366 + i * 0.5, _det(moved)) for i in range(60)]
    events = _direct(engine, clock, frames)
    assert [e.type for e in events] == ["inactivity_started"]


def test_new_candidate_after_move_uses_current_box_scale():
    engine, _, clock = _setup(min_minutes=1.0)
    big = (0, 0, 400, 600)  # cerca de la cámara: radio 300
    small = (950, 450, 1050, 600)  # lejos: radio 75
    shifted = (1050, 450, 1150, 600)  # 100 px: fuera de 75, dentro de 300
    frames = [(i * 0.5, _det(big)) for i in range(20)]
    frames += [(10 + i * 0.5, _det(small)) for i in range(8)]  # movida confirmada en 12
    frames += [(14 + i * 0.5, _det(shifted)) for i in range(300)]
    events = _direct(engine, clock, frames)
    assert [e.type for e in events] == ["inactivity_started"]
    # candidato nuevo en 12; la caja pequeña da radio 75, así que el
    # desplazamiento de 100 lo reinicia en 16 (con escala 600 no habría reinicio)
    assert events[0].payload["started_at"] == _wall(16)


# --- ocultaciones: aparcado, reanudación y caducidad -------------------------


def _hidden(from_s, to_s):
    """Frames sin detecciones a 2 fps."""
    n = int((to_s - from_s) * 2)
    return [(from_s + i * 0.5, []) for i in range(n + 1)]


def test_four_second_occlusion_keeps_episode():
    engine, tracker, clock = _setup()
    frames = _still(SIT, 0, 360) + _hidden(360.5, 364) + _still(SIT, 364.5, 370)
    labelled = []

    def ident(det):
        labelled.append(det)
        return ANA

    events = _run(engine, tracker, clock, frames, ident=ident)
    assert [e.type for e in events] == ["inactivity_started"]
    # la pista reanudada hereda a "ana": nadie la etiqueta de nuevo
    assert len(labelled) == 1
    episode_id = events[0].payload["episode_id"]
    # se aleja a 30 px por frame (el IoU enlaza la pista); sale del radio 100 al 4.º
    away = [
        (370.5 + i * 0.5, [_det((100 + 30 * (i + 1), 200, 200 + 30 * (i + 1), 400))])
        for i in range(8)
    ]
    events = _run(engine, tracker, clock, away)
    assert [e.type for e in events] == ["inactivity_ended"]
    ended = events[0].payload
    assert ended["episode_id"] == episode_id
    assert ended["reason"] == "moved"
    # empieza en 0 y la última vez dentro del radio fue 371.5: incluye los 4 s ocultos
    assert ended["duration_s"] == 371.5


def test_occlusion_before_threshold_keeps_counting():
    engine, tracker, clock = _setup()
    frames = _still(SIT, 0, 180) + _hidden(180.5, 190) + _still(SIT, 190.5, 310)
    events = _run(engine, tracker, clock, frames)
    assert [e.type for e in events] == ["inactivity_started"]
    assert events[0].payload["started_at"] == _wall(0)


def test_parked_expiry_ends_with_track_lost():
    from vitahub.analytics.tracker import PARK_TTL_S

    engine, tracker, clock = _setup()
    frames = _still(SIT, 0, 400) + _hidden(400.5, 400 + PARK_TTL_S + 5)
    events = _run(engine, tracker, clock, frames)
    assert [e.type for e in events] == ["inactivity_started", "inactivity_ended"]
    ended = events[1].payload
    assert ended["reason"] == "track_lost"
    assert ended["ended_at"] == _wall(400)
    assert ended["duration_s"] == 400.0


def test_long_frame_gap_ends_with_last_seen():
    engine, tracker, clock = _setup()
    frames = _still(SIT, 0, 400) + [(1000.0, [_det(SIT)])]
    events = _run(engine, tracker, clock, frames)
    assert [e.type for e in events] == ["inactivity_started", "inactivity_ended"]
    ended = events[1].payload
    assert ended["reason"] == "track_lost"
    assert ended["ended_at"] == _wall(400)
    # la pista nueva empieza un candidato silencioso propio
    more = _run(engine, tracker, clock, [(1000.5 + i * 0.5, [_det(SIT)]) for i in range(20)])
    assert more == []


def test_moving_track_lost_ends_immediately():
    engine, tracker, clock = _setup(min_minutes=1.0)
    frames = _still(SIT, 0, 70)
    # se mueve 10 px por frame (80 px < radio 100): sigue dentro del radio pero
    # la pista no llega a estar quieta 4 s, así que no se aparca al perderse
    frames += [(70.5 + i * 0.5, [_det((100 + 10 * (i + 1), 200, 200 + 10 * (i + 1), 400))]) for i in range(8)]
    events = _run(engine, tracker, clock, frames)
    assert [e.type for e in events] == ["inactivity_started"]
    # la pista se pierde a los 3 s (TRACK_TTL_S): el fin llega en el update de lost
    events = _run(engine, tracker, clock, _hidden(74.5, 78))
    assert [e.type for e in events] == ["inactivity_ended"]
    ended = events[0].payload
    assert ended["reason"] == "track_lost"
    assert ended["ended_at"] == _wall(74)


# --- reanudación: ruido, personas duplicadas e identidad perdida ---


def test_stale_out_since_does_not_survive_resume():
    engine, tracker, clock = _setup(min_minutes=1.0)
    frames = [(i * 0.5, [_ankles_at(150)]) for i in range(141)]  # 0..70 s quieta
    frames.append((70.5, [_ankles_at(300)]))  # un frame ruidoso fuera
    frames += [(71.0 + i * 0.5, []) for i in range(14)]  # 7 s oculta: aparcada
    frames.append((78.0, [_ankles_at(300)]))  # reanuda con otro frame ruidoso
    frames += [(78.5 + i * 0.5, [_ankles_at(150)]) for i in range(10)]
    events = _run(engine, tracker, clock, frames)
    assert [e.type for e in events] == ["inactivity_started"]


def test_resume_clearly_outside_radius_ends_with_moved():
    engine, tracker, clock = _setup(min_minutes=1.0)
    frames = [(i * 0.5, [_ankles_at(150)]) for i in range(141)]
    frames += [(70.5 + i * 0.5, []) for i in range(14)]  # oculta y aparcada
    # vuelve claramente fuera del radio (50 px) y se queda ahí
    frames += [(78.0 + i * 0.5, [_ankles_at(300)]) for i in range(10)]
    events = _run(engine, tracker, clock, frames)
    assert [e.type for e in events] == ["inactivity_started", "inactivity_ended"]
    assert events[1].payload["reason"] == "moved"


def test_second_track_of_same_person_gets_no_second_episode():
    engine, tracker, clock = _setup(min_minutes=1.0)
    other = (400, 200, 500, 400)
    frames = _still(SIT, 0, 70)
    # A se pierde y se aparca; B aparece en otra caja y es "ana" por cara
    frames += [(70.5 + i * 0.5, [_det(other)]) for i in range(8)]
    # C aparece en la caja aparcada: "ana" ya vive en B, no hereda
    frames += [(74.5 + i * 0.5, [_det(other), _det(SIT)]) for i in range(200)]
    events = _run(engine, tracker, clock, frames)
    started = [e for e in events if e.type == "inactivity_started"]
    assert len(started) == 1
    assert len({e.payload["episode_id"] for e in started}) == 1


def test_engine_does_not_open_second_episode_for_same_person_in_camera():
    # Orden inverso: la pista nueva con la misma persona llega con identidad
    # propia (sin pasar por el aparcado) mientras la antigua sigue viva.
    engine, tracker, clock = _setup(min_minutes=1.0)
    frames = [(i * 0.5, [_det(SIT), _det((400, 200, 500, 400))]) for i in range(300)]
    events = _run(engine, tracker, clock, frames)
    started = [e for e in events if e.type == "inactivity_started"]
    assert len(started) == 1


def test_identity_dropped_while_emitted_ends_with_identity_changed():
    engine, tracker, clock = _setup()
    events = _run(engine, tracker, clock, _still(SIT, 0, 310))
    assert [e.type for e in events] == ["inactivity_started"]
    tracker._cameras[CAM.id].tracks[0].identity = None
    more = _run(engine, tracker, clock, _still(SIT, 310.5, 312), ident=lambda det: None)
    assert [e.type for e in more] == ["inactivity_ended"]
    assert more[0].payload["reason"] == "identity_changed"
    assert more[0].payload["ended_at"] == _wall(310.5)
