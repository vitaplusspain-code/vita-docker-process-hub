import math

import pytest

from vitahub.analytics.fall_signals import (
    Signals,
    compute_signals,
    drop_speed,
    head_low,
    is_horizontal,
    is_upright,
    keypoint_conf,
    reference_y,
    score,
    torso_angle,
)

NOSE, L_SH, R_SH, L_HIP, R_HIP = 0, 5, 6, 11, 12


def _pose(nose, shoulders, hips, conf=0.9):
    """Pose sintética: solo nariz, hombros y caderas; el resto a conf 0."""
    kps = [(0.0, 0.0, 0.0)] * 17
    kps[NOSE] = (*nose, conf)
    kps[L_SH] = (shoulders[0] - 10, shoulders[1], conf)
    kps[R_SH] = (shoulders[0] + 10, shoulders[1], conf)
    kps[L_HIP] = (hips[0] - 10, hips[1], conf)
    kps[R_HIP] = (hips[0] + 10, hips[1], conf)
    return tuple(kps)


STANDING = _pose(nose=(50, 10), shoulders=(50, 30), hips=(50, 80))   # torso vertical
LYING = _pose(nose=(10, 90), shoulders=(30, 90), hips=(90, 90))      # torso horizontal
BOX_STANDING = (30, 0, 70, 120)
BOX_LYING = (0, 80, 120, 110)


def test_torso_angle_vertical_and_horizontal():
    assert torso_angle(STANDING) == pytest.approx(0.0)
    assert torso_angle(LYING) == pytest.approx(90.0)


def test_torso_angle_diagonal():
    diag = _pose(nose=(0, 0), shoulders=(0, 0), hips=(50, 50))
    assert torso_angle(diag) == pytest.approx(45.0)


def test_torso_angle_none_without_confident_points():
    low = _pose(nose=(50, 10), shoulders=(50, 30), hips=(50, 80), conf=0.1)
    assert torso_angle(low) is None
    assert torso_angle(None) is None


def test_head_low_uses_hips_when_available():
    assert head_low(STANDING, BOX_STANDING) is False
    assert head_low(LYING, BOX_LYING) is False  # nariz a la misma altura que cadera
    below = _pose(nose=(50, 100), shoulders=(50, 30), hips=(50, 80))
    assert head_low(below, BOX_STANDING) is True


def test_head_low_falls_back_to_box_thirds_without_hips():
    only_nose = [(0.0, 0.0, 0.0)] * 17
    only_nose[NOSE] = (50.0, 100.0, 0.9)
    assert head_low(tuple(only_nose), (0, 0, 100, 120)) is True  # 100 > 80 (2/3 de 120)
    only_nose[NOSE] = (50.0, 10.0, 0.9)
    assert head_low(tuple(only_nose), (0, 0, 100, 120)) is False


def test_head_low_none_without_nose():
    assert head_low(None, BOX_STANDING) is None
    no_nose = list(STANDING)
    no_nose[NOSE] = (0.0, 0.0, 0.0)
    assert head_low(tuple(no_nose), BOX_STANDING) is None


def test_keypoint_conf_is_mean_of_used_points():
    assert keypoint_conf(STANDING) == pytest.approx(0.9)
    assert keypoint_conf(None) is None


def test_reference_y_prefers_hips_then_box_center():
    assert reference_y(STANDING, BOX_STANDING) == pytest.approx(80.0)
    assert reference_y(None, (0, 0, 100, 120)) == pytest.approx(60.0)


def test_drop_speed_max_within_window():
    # caderas bajan de 50 a 100 en 0.5 s con caja de 100 de alto -> 1.0 alturas/s
    history = [(0.0, 50.0, 100.0), (0.5, 100.0, 100.0), (1.0, 100.0, 100.0)]
    assert drop_speed(history, now=1.0) == pytest.approx(1.0)


def test_drop_speed_ignores_samples_outside_window_and_rising():
    history = [(0.0, 50.0, 100.0), (0.5, 100.0, 100.0), (3.0, 90.0, 100.0), (3.5, 80.0, 100.0)]
    assert drop_speed(history, now=3.5) == pytest.approx(0.0)  # solo sube en la ventana


def test_drop_speed_none_with_fewer_than_two_samples():
    assert drop_speed([], now=0.0) is None
    assert drop_speed([(0.0, 50.0, 100.0)], now=0.0) is None


def test_drop_speed_tolerates_degenerate_box_and_dt():
    history = [(0.0, 50.0, 0.0), (0.0, 90.0, 0.0)]
    assert drop_speed(history, now=0.0) is None


def test_is_horizontal_and_upright_from_angle():
    lying = Signals(90.0, 0.5, None, 0.0, None, None)
    standing = Signals(10.0, 3.0, None, 0.0, None, None)
    assert is_horizontal(lying) and not is_upright(lying)
    assert is_upright(standing) and not is_horizontal(standing)


def test_is_horizontal_and_upright_fall_back_to_bbox_ratio():
    wide = Signals(None, 1.5, None, 0.0, None, None)
    tall = Signals(None, 0.5, None, 0.0, None, None)
    assert is_horizontal(wide) and not is_upright(wide)
    assert is_upright(tall) and not is_horizontal(tall)


def test_score_full_fall_is_high():
    s = Signals(torso_angle=85.0, bbox_ratio=2.0, drop_speed=1.2, floor_time_s=6.0,
                head_low=True, keypoint_conf=0.8)
    assert score(s) == pytest.approx(1.0)


def test_score_slow_lie_down_is_low():
    s = Signals(torso_angle=85.0, bbox_ratio=2.0, drop_speed=0.1, floor_time_s=0.0,
                head_low=False, keypoint_conf=0.8)
    assert score(s) == pytest.approx(0.35)


def test_score_renormalizes_when_signals_missing():
    # Solo permanencia (0.25) y horizontalidad (0.35) disponibles, ambas al máximo -> 1.0
    s = Signals(torso_angle=85.0, bbox_ratio=2.0, drop_speed=None, floor_time_s=6.0,
                head_low=None, keypoint_conf=None)
    assert score(s) == pytest.approx(1.0)


def test_score_linear_mapping_midpoints():
    s = Signals(torso_angle=62.5, bbox_ratio=1.0, drop_speed=0.65, floor_time_s=2.5,
                head_low=False, keypoint_conf=0.5)
    # todos los términos a 0.5 salvo head_low=0: 0.35*0.5 + 0.30*0.5 + 0.25*0.5 + 0.10*0 = 0.45
    assert score(s) == pytest.approx(0.45)


def test_compute_signals_integrates_everything():
    history = [(0.0, 50.0, 100.0), (0.5, 100.0, 100.0)]
    s = compute_signals(LYING, BOX_LYING, history, now=0.5, floor_time_s=1.0)
    assert s.torso_angle == pytest.approx(90.0)
    assert s.bbox_ratio == pytest.approx(4.0)
    assert s.drop_speed == pytest.approx(1.0)
    assert s.floor_time_s == 1.0
    assert s.head_low is False
    assert s.keypoint_conf == pytest.approx(0.9)


def test_compute_signals_degenerate_box_does_not_raise():
    s = compute_signals(None, (10, 10, 10, 10), [], now=0.0, floor_time_s=0.0)
    assert s.bbox_ratio == 0.0
    assert math.isfinite(score(s))


def test_to_payload_rounds_and_keeps_nulls():
    s = Signals(torso_angle=74.123456, bbox_ratio=1.9, drop_speed=None, floor_time_s=2.5,
                head_low=True, keypoint_conf=0.6149)
    assert s.to_payload() == {
        "torso_angle": 74.123, "bbox_ratio": 1.9, "drop_speed": None,
        "floor_time_s": 2.5, "head_low": True, "keypoint_conf": 0.615,
    }
