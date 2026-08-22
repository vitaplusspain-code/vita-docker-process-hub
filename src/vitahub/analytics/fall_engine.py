"""Analítica de caídas: pistas por persona y máquina de estados de episodio.

    upright ──tumbado──► candidate ──score ≥ min y ≥ 2 s en el suelo──► reported
       ▲                    │                                             │
       └──de pie 2 s────────┘         de pie 2 s, o pista perdida 3 s ────┘ → fall_resolved

Una pista es una persona seguida entre frames por solapamiento de cajas
(IoU) y, cuando no hay solape, por cercanía de centros (una caída hacia
delante mueve la caja entera). Las pistas caducan antes de emparejar: quien
reaparece tras un hueco largo abre pista nueva, no hereda la anterior.
Todo el estado es en memoria; un reinicio del hub a mitad de episodio
pierde el fall_resolved (igual que el ConnectionMonitor, ver docs/backlog.md).
"""
from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

from vitahub.analytics.fall_signals import (
    Sample,
    Signals,
    compute_signals,
    is_horizontal,
    is_upright,
    reference_y,
    score,
)
from vitahub.models import Camera, Detection, Event

# Solapamiento mínimo para decir "es la misma persona que en el frame anterior".
IOU_MATCH = 0.3
# Respaldo cuando no hay solape: distancia entre centros normalizada por el
# lado mayor de las dos cajas. Una caída hacia delante desplaza la caja hasta
# una altura de cuerpo, y a 2 fps eso puede dejar IoU = 0 entre frames
# consecutivos. Puede cruzar personas en escenas concurridas: el tracker real
# está en el backlog.
CENTER_MATCH = 1.0
# Una pista sin observación durante este tiempo se da por perdida.
TRACK_TTL_S = 3.0
# Historial de alturas que se conserva por pista (cubre la ventana de drop_speed).
HISTORY_S = 3.0
# Tiempo mínimo en el suelo antes de emitir: evita disparar por un frame ruidoso.
CONFIRM_FLOOR_S = 2.0
# Tiempo de pie necesario para cerrar un episodio (o volver de candidate).
CONFIRM_UPRIGHT_S = 2.0
# Cadencia máxima de fall_update mientras sigue en el suelo.
UPDATE_EVERY_S = 10.0
# Tiempo máximo que suma una sola muestra a los acumuladores: el doble del
# periodo de muestreo a 2 fps. Un hueco mayor (frames perdidos, worker
# atascado) no cuenta como tiempo "visto" en el suelo ni de pie.
MAX_STEP_S = 1.0

_SEV_HIGH = "high"
_SEV_INFO = "info"


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _iou(a: tuple[int, int, int, int], b: tuple[int, int, int, int]) -> float:
    ix1, iy1 = max(a[0], b[0]), max(a[1], b[1])
    ix2, iy2 = min(a[2], b[2]), min(a[3], b[3])
    inter = max(0, ix2 - ix1) * max(0, iy2 - iy1)
    area_a = max(0, a[2] - a[0]) * max(0, a[3] - a[1])
    area_b = max(0, b[2] - b[0]) * max(0, b[3] - b[1])
    union = area_a + area_b - inter
    return inter / union if union > 0 else 0.0


def _center_distance(a: tuple[int, int, int, int], b: tuple[int, int, int, int]) -> float:
    """Distancia entre centros normalizada por el lado mayor de ambas cajas."""
    ax, ay = (a[0] + a[2]) / 2.0, (a[1] + a[3]) / 2.0
    bx, by = (b[0] + b[2]) / 2.0, (b[1] + b[3]) / 2.0
    scale = max(a[2] - a[0], a[3] - a[1], b[2] - b[0], b[3] - b[1])
    if scale <= 0:
        return float("inf")
    return math.hypot(ax - bx, ay - by) / scale


@dataclass
class _Track:
    bbox: tuple[int, int, int, int]
    last_seen: float
    history: list[Sample] = field(default_factory=list)
    state: str = "upright"  # upright | candidate | reported
    # Postura observada en el frame anterior: "" (pista nueva) | floor | upright | other.
    posture: str = ""
    # Tiempo *observado* en la postura actual: suma de pasos entre muestras
    # (cada uno con tope MAX_STEP_S), no diferencia de reloj. Un hueco de
    # muestreo no puede inflar el tiempo en el suelo.
    floor_time: float = 0.0
    upright_time: float = 0.0
    # Pico de drop_speed desde que dejó de estar de pie. La ventana de
    # drop_speed (1.5 s) es más corta que la confirmación (2 s): sin
    # conservar el pico, la brusquedad de la caída nunca llegaría al score.
    peak_drop: float | None = None
    # Solo con state == "reported":
    episode_id: str = ""
    episode_start: float = 0.0
    last_event_at: float = 0.0
    max_score: float = 0.0


@dataclass
class _CameraState:
    tracks: list[_Track] = field(default_factory=list)


class FallEngine:
    def __init__(
        self,
        hub_id: str,
        min_score: float = 0.3,
        clock: Callable[[], datetime] = _utcnow,
    ) -> None:
        self._hub_id = hub_id
        self._min_score = min_score
        self._clock = clock
        self._cameras: dict[str, _CameraState] = {}

    def observe(self, camera: Camera, detections: list[Detection], now: float) -> list[Event]:
        cam = self._cameras.setdefault(camera.id, _CameraState())
        persons = [d for d in detections if d.label == "person"]
        events: list[Event] = []

        # Caducar ANTES de emparejar: si la pista lleva TRACK_TTL_S sin verse,
        # la persona que aparece ahora no es "la misma de antes" aunque su caja
        # solape. Se resuelve el episodio y la detección abre pista nueva.
        alive: list[_Track] = []
        for track in cam.tracks:
            if now - track.last_seen < TRACK_TTL_S:
                alive.append(track)
            elif track.state == "reported":
                events.append(self._resolved(camera, track, now))
        cam.tracks = alive

        for track, det in self._match(cam.tracks, persons):
            events += self._update_track(camera, track, det, now, len(persons))
        return events

    # --- emparejamiento -------------------------------------------------

    def _match(
        self, tracks: list[_Track], persons: list[Detection]
    ) -> list[tuple[_Track, Detection]]:
        """Greedy por mayor IoU, luego por cercanía de centros; el resto abre pista nueva."""
        pairs = sorted(
            (
                (_iou(t.bbox, d.bbox), ti, di)
                for ti, t in enumerate(tracks)
                for di, d in enumerate(persons)
            ),
            reverse=True,
        )
        used_t: set[int] = set()
        used_d: set[int] = set()
        result: list[tuple[_Track, Detection]] = []
        for iou, ti, di in pairs:
            if iou < IOU_MATCH or ti in used_t or di in used_d:
                continue
            used_t.add(ti)
            used_d.add(di)
            result.append((tracks[ti], persons[di]))

        # Segunda pasada: pistas y detecciones aún sueltas, por cercanía de
        # centros (menor distancia primero). Rescata la caída sin solape.
        near = sorted(
            (
                (_center_distance(t.bbox, d.bbox), ti, di)
                for ti, t in enumerate(tracks)
                if ti not in used_t
                for di, d in enumerate(persons)
                if di not in used_d
            ),
        )
        for dist, ti, di in near:
            if dist > CENTER_MATCH or ti in used_t or di in used_d:
                continue
            used_t.add(ti)
            used_d.add(di)
            result.append((tracks[ti], persons[di]))

        for di, d in enumerate(persons):
            if di not in used_d:
                track = _Track(bbox=d.bbox, last_seen=-1.0)
                tracks.append(track)
                result.append((track, d))
        return result

    # --- estado por pista -----------------------------------------------

    def _update_track(
        self, camera: Camera, track: _Track, det: Detection, now: float, person_count: int
    ) -> list[Event]:
        # Tiempo observado desde la muestra anterior (0 en una pista nueva).
        step = 0.0 if track.last_seen < 0 else min(now - track.last_seen, MAX_STEP_S)
        track.bbox = det.bbox
        track.last_seen = now
        h = max(0, det.bbox[3] - det.bbox[1])
        track.history.append((now, reference_y(det.keypoints, det.bbox), float(h)))
        track.history = [s for s in track.history if s[0] >= now - HISTORY_S]

        signals = compute_signals(det.keypoints, det.bbox, track.history, now, track.floor_time)

        # El frame de la transición arranca el contador en 0; a partir de ahí
        # cada muestra suma lo que se ha visto realmente.
        horizontal = is_horizontal(signals)
        if horizontal:
            track.floor_time = track.floor_time + step if track.posture == "floor" else 0.0
            track.upright_time = 0.0
            track.posture = "floor"
        elif is_upright(signals):
            track.upright_time = track.upright_time + step if track.posture == "upright" else 0.0
            track.floor_time = 0.0
            track.posture = "upright"
        else:
            track.floor_time = 0.0
            track.upright_time = 0.0
            track.posture = "other"

        if track.state == "upright" and not horizontal:
            track.peak_drop = None
        elif signals.drop_speed is not None:
            track.peak_drop = max(track.peak_drop or 0.0, signals.drop_speed)

        # Rehacer las señales con el tiempo en el suelo recién acumulado y con
        # el pico de bajada en lugar del valor instantáneo.
        signals = Signals(
            signals.torso_angle, signals.bbox_ratio, track.peak_drop,
            track.floor_time, signals.head_low, signals.keypoint_conf,
        )
        floor_time = track.floor_time
        upright_for = track.upright_time

        if track.state == "upright":
            if horizontal:
                track.state = "candidate"
            return []

        if track.state == "candidate":
            if upright_for >= CONFIRM_UPRIGHT_S:
                track.state = "upright"
                track.peak_drop = None
                return []
            current = score(signals)
            confirmed = horizontal and floor_time >= CONFIRM_FLOOR_S
            if confirmed and current >= self._min_score:
                track.state = "reported"
                track.episode_id = f"{camera.id}-{int(self._clock().timestamp())}"
                track.episode_start = now
                track.last_event_at = now
                track.max_score = current
                return [
                    self._fall_event(camera, track, "fall_detected", current, signals, person_count)
                ]
            return []

        # reported
        if upright_for >= CONFIRM_UPRIGHT_S:
            ev = self._resolved(camera, track, now)
            track.state = "upright"
            track.peak_drop = None
            return [ev]
        if now - track.last_event_at >= UPDATE_EVERY_S:
            current = score(signals)
            track.last_event_at = now
            track.max_score = max(track.max_score, current)
            return [self._fall_event(camera, track, "fall_update", current, signals, person_count)]
        return []

    # --- eventos ----------------------------------------------------------

    def _fall_event(
        self,
        camera: Camera,
        track: _Track,
        event_type: str,
        current: float,
        signals: Signals,
        person_count: int,
    ) -> Event:
        return Event(
            hub_id=self._hub_id,
            camera_id=camera.id,
            camera_name=camera.name,
            type=event_type,
            severity=_SEV_HIGH,
            timestamp=self._clock().isoformat(),
            payload={
                "episode_id": track.episode_id,
                "score": round(current, 3),
                "signals": signals.to_payload(),
                "person_count": person_count,
            },
        )

    def _resolved(self, camera: Camera, track: _Track, now: float) -> Event:
        return Event(
            hub_id=self._hub_id,
            camera_id=camera.id,
            camera_name=camera.name,
            type="fall_resolved",
            severity=_SEV_INFO,
            timestamp=self._clock().isoformat(),
            payload={
                "episode_id": track.episode_id,
                "duration_s": round(now - track.episode_start, 3),
                "max_score": round(track.max_score, 3),
            },
        )
