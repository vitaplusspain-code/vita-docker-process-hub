"""Señales geométricas de caída a partir de una pose y su historial reciente.

Funciones puras, sin estado: todo lo temporal (historial, tiempo en el
suelo) entra como argumento. El FallEngine se encarga del estado.

Pesos y umbrales son constantes a propósito (no config): se calibran una
vez con scripts/replay_video.py sobre vídeos reales. Si en campo hiciera
falta ajustarlos por hogar, se pasarán a config entonces.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from itertools import pairwise

from vitahub.models import Keypoints

# Índices COCO-17 que usa la analítica.
_NOSE, _L_SHOULDER, _R_SHOULDER, _L_HIP, _R_HIP = 0, 5, 6, 11, 12
_USED_POINTS = (_NOSE, _L_SHOULDER, _R_SHOULDER, _L_HIP, _R_HIP)

# Un keypoint por debajo de esta confianza se trata como ausente.
KEYPOINT_MIN_CONF = 0.3
# Torso a ≥ 60° de la vertical = tumbado; < 45° = de pie. El hueco entre
# ambos evita oscilar en posturas intermedias (sentado, agachado).
HORIZONTAL_ANGLE = 60.0
UPRIGHT_ANGLE = 45.0
# Sin keypoints: una caja más ancha que alta (×1.2) cuenta como tumbado.
HORIZONTAL_RATIO = 1.2
# Ventana sobre la que se busca la bajada más rápida de las caderas.
DROP_WINDOW_S = 1.5

# Score: pesos y mapeos lineales (valor en el que el término vale 0 → 1).
_W_HORIZONTAL, _ANGLE_0, _ANGLE_1 = 0.35, 45.0, 80.0
_W_DROP, _DROP_0, _DROP_1 = 0.30, 0.3, 1.0
_W_FLOOR, _FLOOR_0, _FLOOR_1 = 0.25, 0.0, 5.0
_W_HEAD = 0.10

# (t, ref_y, box_h): instante, altura de referencia (caderas o centro de
# caja) y alto de la caja en ese instante.
Sample = tuple[float, float, float]


def _point(kps: Keypoints | None, idx: int) -> tuple[float, float] | None:
    if kps is None or idx >= len(kps):
        return None
    x, y, conf = kps[idx]
    if conf < KEYPOINT_MIN_CONF:
        return None
    return (x, y)


def _mid(kps: Keypoints | None, a: int, b: int) -> tuple[float, float] | None:
    pa, pb = _point(kps, a), _point(kps, b)
    if pa is None or pb is None:
        return None
    return ((pa[0] + pb[0]) / 2.0, (pa[1] + pb[1]) / 2.0)


def torso_angle(kps: Keypoints | None) -> float | None:
    """Ángulo en grados del torso (hombros→caderas) respecto a la vertical."""
    shoulders = _mid(kps, _L_SHOULDER, _R_SHOULDER)
    hips = _mid(kps, _L_HIP, _R_HIP)
    if shoulders is None or hips is None:
        return None
    dx = abs(hips[0] - shoulders[0])
    dy = abs(hips[1] - shoulders[1])
    if dx == 0.0 and dy == 0.0:
        return None
    return math.degrees(math.atan2(dx, dy))


def head_low(kps: Keypoints | None, bbox: tuple[int, int, int, int]) -> bool | None:
    """Nariz por debajo de las caderas; sin caderas, en el tercio inferior de la caja."""
    nose = _point(kps, _NOSE)
    if nose is None:
        return None
    hips = _mid(kps, _L_HIP, _R_HIP)
    if hips is not None:
        return nose[1] > hips[1]
    _, y1, _, y2 = bbox
    return nose[1] > y1 + (y2 - y1) * 2.0 / 3.0


def keypoint_conf(kps: Keypoints | None) -> float | None:
    """Confianza media de los puntos que usa la analítica (nariz, hombros, caderas)."""
    if kps is None:
        return None
    confs = [kps[i][2] for i in _USED_POINTS if i < len(kps)]
    if not confs:
        return None
    return sum(confs) / len(confs)


def reference_y(kps: Keypoints | None, bbox: tuple[int, int, int, int]) -> float:
    """Altura que se sigue en el tiempo: medio de caderas, o centro de la caja."""
    hips = _mid(kps, _L_HIP, _R_HIP)
    if hips is not None:
        return hips[1]
    return (bbox[1] + bbox[3]) / 2.0


def drop_speed(history: list[Sample], now: float) -> float | None:
    """Máxima velocidad de bajada en la ventana, en alturas de caja por segundo.

    Positivo = baja (y crece). Subidas cuentan como 0. None si no hay dos
    muestras válidas en la ventana.
    """
    window = [s for s in history if s[0] >= now - DROP_WINDOW_S]
    best: float | None = None
    for (t0, y0, h0), (t1, y1, _) in pairwise(window):
        dt = t1 - t0
        if dt <= 0.0 or h0 <= 0.0:
            continue
        speed = max(0.0, (y1 - y0) / h0 / dt)
        best = speed if best is None else max(best, speed)
    return best


@dataclass(frozen=True)
class Signals:
    torso_angle: float | None
    bbox_ratio: float
    drop_speed: float | None
    floor_time_s: float
    head_low: bool | None
    keypoint_conf: float | None

    def to_payload(self) -> dict[str, object]:
        def r(v: float | None) -> float | None:
            return None if v is None else round(v, 3)

        return {
            "torso_angle": r(self.torso_angle),
            "bbox_ratio": round(self.bbox_ratio, 3),
            "drop_speed": r(self.drop_speed),
            "floor_time_s": round(self.floor_time_s, 3),
            "head_low": self.head_low,
            "keypoint_conf": r(self.keypoint_conf),
        }


def compute_signals(
    kps: Keypoints | None,
    bbox: tuple[int, int, int, int],
    history: list[Sample],
    now: float,
    floor_time_s: float,
) -> Signals:
    x1, y1, x2, y2 = bbox
    w, h = max(0, x2 - x1), max(0, y2 - y1)
    return Signals(
        torso_angle=torso_angle(kps),
        bbox_ratio=(w / h) if h > 0 else 0.0,
        drop_speed=drop_speed(history, now),
        floor_time_s=floor_time_s,
        head_low=head_low(kps, bbox),
        keypoint_conf=keypoint_conf(kps),
    )


def is_horizontal(s: Signals) -> bool:
    if s.torso_angle is not None:
        return s.torso_angle >= HORIZONTAL_ANGLE
    return s.bbox_ratio >= HORIZONTAL_RATIO


def is_upright(s: Signals) -> bool:
    if s.torso_angle is not None:
        return s.torso_angle < UPRIGHT_ANGLE
    return s.bbox_ratio < HORIZONTAL_RATIO


def _ramp(value: float, at_zero: float, at_one: float) -> float:
    if at_one == at_zero:
        return 1.0 if value >= at_one else 0.0
    return min(1.0, max(0.0, (value - at_zero) / (at_one - at_zero)))


def score(s: Signals) -> float:
    """Suma ponderada en [0, 1]; los términos sin señal no cuentan y el resto se renormaliza."""
    terms: list[tuple[float, float]] = []
    if s.torso_angle is not None:
        terms.append((_W_HORIZONTAL, _ramp(s.torso_angle, _ANGLE_0, _ANGLE_1)))
    if s.drop_speed is not None:
        terms.append((_W_DROP, _ramp(s.drop_speed, _DROP_0, _DROP_1)))
    terms.append((_W_FLOOR, _ramp(s.floor_time_s, _FLOOR_0, _FLOOR_1)))
    if s.head_low is not None:
        terms.append((_W_HEAD, 1.0 if s.head_low else 0.0))
    total_weight = sum(w for w, _ in terms)
    if total_weight <= 0.0:
        return 0.0
    return sum(w * v for w, v in terms) / total_weight
