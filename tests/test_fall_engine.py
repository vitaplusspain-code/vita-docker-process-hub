from datetime import UTC, datetime, timedelta

from vitahub.analytics.fall_engine import FallEngine
from vitahub.models import Camera, Detection

CAM = Camera(id="onvif-abc", name="salon", last_ip="10.0.0.5")
NOSE, L_SH, R_SH, L_HIP, R_HIP = 0, 5, 6, 11, 12


def _pose(nose, shoulders, hips, conf=0.9):
    kps = [(0.0, 0.0, 0.0)] * 17
    kps[NOSE] = (*nose, conf)
    kps[L_SH] = (shoulders[0] - 10, shoulders[1], conf)
    kps[R_SH] = (shoulders[0] + 10, shoulders[1], conf)
    kps[L_HIP] = (hips[0] - 10, hips[1], conf)
    kps[R_HIP] = (hips[0] + 10, hips[1], conf)
    return tuple(kps)


# Geometría elegida para que (a) la caja de pie y la tumbada solapen con
# IoU ≥ 0.3 (misma pista) y (b) la bajada de caderas 50 → 115 en 0.5 s sea
# brusca (> 1 altura de caja por segundo).
def standing(x=50):
    return Detection("person", 0.9, (x - 20, 0, x + 20, 120),
                     _pose(nose=(x, 10), shoulders=(x, 30), hips=(x, 50)))


def lying(x=50):
    return Detection("person", 0.9, (x - 50, 50, x + 50, 120),
                     _pose(nose=(x - 40, 115), shoulders=(x - 20, 115), hips=(x + 40, 115)))


class _Clock:
    def __init__(self):
        self.t = datetime(2026, 8, 22, 10, 0, 0, tzinfo=UTC)

    def __call__(self):
        return self.t

    def advance(self, s):
        self.t += timedelta(seconds=s)


def _engine(min_score=0.3):
    clock = _Clock()
    return FallEngine(hub_id="hub-1", min_score=min_score, clock=clock), clock


def _feed(engine, clock, seq, step=0.5, start=0.0):
    """seq: lista de listas de detecciones, una por frame. Devuelve todos los eventos."""
    events = []
    now = start
    for dets in seq:
        events += engine.observe(CAM, dets, now)
        now += step
        clock.advance(step)
    return events


def test_sudden_fall_emits_detected_after_two_seconds_on_floor():
    eng, clock = _engine()
    # 1 s de pie, caída en 0.5 s, luego en el suelo
    seq = [[standing()]] * 2 + [[lying()]] * 6
    events = _feed(eng, clock, seq)
    assert [e.type for e in events] == ["fall_detected"]
    ev = events[0]
    assert ev.severity == "high"
    assert ev.camera_id == "onvif-abc"
    assert ev.payload["score"] >= 0.7
    assert ev.payload["signals"]["floor_time_s"] >= 2.0
    assert ev.payload["signals"]["drop_speed"] is not None
    assert ev.payload["person_count"] == 1
    assert ev.payload["episode_id"].startswith("onvif-abc-")


def test_no_event_before_two_seconds_on_floor():
    eng, clock = _engine()
    seq = [[standing()]] * 2 + [[lying()]] * 4  # 1.5 s en el suelo (frames a 0, .5, 1, 1.5)
    assert _feed(eng, clock, seq) == []


def test_slow_lie_down_scores_low_and_respects_min_score():
    eng, clock = _engine(min_score=0.5)
    # Sin transición brusca: aparece ya tumbada y no se mueve (drop_speed = 0),
    # así que el score es 0.35 + 0.05·s de suelo. La permanencia sola acaba
    # superando 0.5 a los 3 s (por diseño: seguir en el suelo es cada vez más
    # sospechoso), así que esta rama se limita a 2.5 s de suelo (score 0.475).
    seq_short = [[lying()]] * 6
    assert _feed(eng, clock, seq_short) == []
    seq = [[lying()]] * 8
    eng2, clock2 = _engine(min_score=0.3)
    events = _feed(eng2, clock2, seq)
    assert [e.type for e in events] == ["fall_detected"]
    assert events[0].payload["score"] < 0.5


def test_crouch_and_stand_up_emits_nothing():
    eng, clock = _engine()
    seq = [[standing()]] * 2 + [[lying()]] * 3 + [[standing()]] * 8
    assert _feed(eng, clock, seq) == []


def test_updates_every_ten_seconds_while_on_floor():
    eng, clock = _engine()
    seq = [[standing()]] * 2 + [[lying()]] * 60  # 30 s en el suelo
    events = _feed(eng, clock, seq)
    types = [e.type for e in events]
    assert types[0] == "fall_detected"
    assert types.count("fall_update") == 2  # a +10 s y +20 s del detected
    assert all(e.payload["episode_id"] == events[0].payload["episode_id"] for e in events)
    assert all(e.severity == "high" for e in events)


def test_resolved_when_person_stands_up():
    eng, clock = _engine()
    seq = [[standing()]] * 2 + [[lying()]] * 6 + [[standing()]] * 5
    events = _feed(eng, clock, seq)
    assert [e.type for e in events] == ["fall_detected", "fall_resolved"]
    res = events[1]
    assert res.severity == "info"
    assert res.payload["episode_id"] == events[0].payload["episode_id"]
    assert res.payload["max_score"] >= events[0].payload["score"]
    assert res.payload["duration_s"] > 0


def test_resolved_when_track_disappears():
    eng, clock = _engine()
    seq = [[standing()]] * 2 + [[lying()]] * 6 + [[]] * 7  # 3 s sin verla
    events = _feed(eng, clock, seq)
    assert [e.type for e in events] == ["fall_detected", "fall_resolved"]


def test_missing_keypoints_uses_bbox_and_never_raises():
    # Sin pose solo puntúan brusquedad y permanencia: el score es bajo; el
    # umbral se baja para comprobar el camino de caja, no la calibración.
    eng, clock = _engine(min_score=0.1)
    tall = Detection("person", 0.9, (30, 0, 70, 120), None)
    wide = Detection("person", 0.9, (0, 50, 100, 120), None)
    seq = [[tall]] * 2 + [[wide]] * 6
    events = _feed(eng, clock, seq)
    assert [e.type for e in events] == ["fall_detected"]
    assert events[0].payload["signals"]["torso_angle"] is None
    assert events[0].payload["signals"]["keypoint_conf"] is None


def test_degenerate_detections_do_not_raise():
    eng, clock = _engine()
    bad = Detection("person", 0.9, (10, 10, 10, 10), ())
    assert _feed(eng, clock, [[bad]] * 10) == []


def test_two_people_are_tracked_separately():
    eng, clock = _engine()
    # Persona A de pie a la izquierda; persona B cae a la derecha
    seq = [[standing(60), standing(300)]] * 2 + [[standing(60), lying(300)]] * 6
    events = _feed(eng, clock, seq)
    assert [e.type for e in events] == ["fall_detected"]
    assert events[0].payload["person_count"] == 2


def test_per_camera_state_is_independent():
    eng, _clock = _engine()
    other = Camera(id="onvif-xyz", name="cocina", last_ip="10.0.0.6")
    now = 0.0
    for _ in range(2):
        eng.observe(CAM, [standing()], now)
        eng.observe(other, [standing()], now)
        now += 0.5
    events = []
    for _ in range(6):
        events += eng.observe(CAM, [lying()], now)
        events += eng.observe(other, [standing()], now)
        now += 0.5
    assert [(e.type, e.camera_id) for e in events] == [("fall_detected", "onvif-abc")]


def test_sampling_gap_does_not_inflate_floor_time():
    # El tiempo en el suelo es tiempo *observado*: un hueco de muestreo de
    # 2.5 s solo suma MAX_STEP_S (1 s), no los 2.5 s de reloj.
    eng, clock = _engine()
    for now in (0.0, 0.5):
        assert eng.observe(CAM, [standing()], now) == []
        clock.advance(0.5)
    assert eng.observe(CAM, [lying()], 1.0) == []  # floor_time 0
    clock.advance(2.5)
    assert eng.observe(CAM, [lying()], 3.5) == []  # hueco: +1.0 (tope), no +2.5
    clock.advance(0.5)
    assert eng.observe(CAM, [lying()], 4.0) == []  # 1.5
    clock.advance(0.5)
    events = eng.observe(CAM, [lying()], 4.5)  # 2.0
    assert [e.type for e in events] == ["fall_detected"]
    assert events[0].payload["signals"]["floor_time_s"] == 2.0


def test_gap_then_reappear_resolves_and_starts_new_track():
    # Sin llamadas a observe durante 5 s la pista caduca: al reaparecer, ese
    # mismo frame resuelve el episodio y abre pista nueva (nada más).
    eng, clock = _engine()
    events = _feed(eng, clock, [[standing()]] * 2 + [[lying()]] * 6)
    assert [e.type for e in events] == ["fall_detected"]
    clock.advance(5.0)
    late = eng.observe(CAM, [lying()], 8.5)
    assert [e.type for e in late] == ["fall_resolved"]
    assert late[0].payload["episode_id"] == events[0].payload["episode_id"]
    clock.advance(0.5)
    assert eng.observe(CAM, [lying()], 9.0) == []  # pista nueva: sigue en candidate


def test_forward_fall_without_box_overlap_keeps_track():
    # Caída hacia delante: la caja tumbada no solapa con la de pie (IoU = 0),
    # pero los centros están a ~87 px (≤ 120 = lado mayor) → misma pista.
    eng, clock = _engine()
    away = Detection("person", 0.9, (80, 60, 180, 130),
                     _pose(nose=(90, 115), shoulders=(100, 115), hips=(150, 115)))
    events = _feed(eng, clock, [[standing()]] * 2 + [[away]] * 6)
    assert [e.type for e in events] == ["fall_detected"]
    assert events[0].payload["signals"]["drop_speed"] is not None
    assert events[0].payload["score"] >= 0.7
