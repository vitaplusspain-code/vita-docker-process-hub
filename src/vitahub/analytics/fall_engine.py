"""Analítica de caídas: pistas por persona y máquina de estados de episodio.

    upright ──tumbado──► candidate ──score ≥ min y ≥ 2 s en el suelo──► reported
       ▲                    │                                             │
       └──de pie 2 s────────┘         de pie 2 s, o pista perdida 3 s ────┘ → fall_resolved

Una pista es una persona seguida entre frames por solapamiento de cajas
(IoU). Todo el estado es en memoria; un reinicio del hub a mitad de episodio
pierde el fall_resolved (igual que el ConnectionMonitor, ver docs/backlog.md).
"""
from __future__ import annotations

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


@dataclass
class _Track:
    bbox: tuple[int, int, int, int]
    last_seen: float
    history: list[Sample] = field(default_factory=list)
    state: str = "upright"  # upright | candidate | reported
    floor_since: float | None = None
    upright_since: float | None = None
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

        matched = self._match(cam.tracks, persons)
        for track, det in matched:
            events += self._update_track(camera, track, det, now, len(persons))

        # Pistas no vistas: caducan a los TRACK_TTL_S; si estaban en episodio, se resuelve.
        alive: list[_Track] = []
        for track in cam.tracks:
            if now - track.last_seen < TRACK_TTL_S:
                alive.append(track)
            elif track.state == "reported":
                events.append(self._resolved(camera, track, now))
        cam.tracks = alive
        return events

    # --- emparejamiento -------------------------------------------------

    def _match(
        self, tracks: list[_Track], persons: list[Detection]
    ) -> list[tuple[_Track, Detection]]:
        """Greedy por mayor IoU; las detecciones sin pista abren una nueva."""
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
        track.bbox = det.bbox
        track.last_seen = now
        h = max(0, det.bbox[3] - det.bbox[1])
        track.history.append((now, reference_y(det.keypoints, det.bbox), float(h)))
        track.history = [s for s in track.history if s[0] >= now - HISTORY_S]

        floor_time = 0.0 if track.floor_since is None else now - track.floor_since
        signals = compute_signals(det.keypoints, det.bbox, track.history, now, floor_time)

        if is_horizontal(signals):
            if track.floor_since is None:
                track.floor_since = now
            track.upright_since = None
        else:
            track.floor_since = None
            if is_upright(signals):
                if track.upright_since is None:
                    track.upright_since = now
            else:
                track.upright_since = None
        if track.state == "upright" and track.floor_since is None:
            track.peak_drop = None
        elif signals.drop_speed is not None:
            track.peak_drop = max(track.peak_drop or 0.0, signals.drop_speed)

        # Recalcular con el floor_since recién fijado (primer frame tumbado = 0 s)
        # y con el pico de bajada en lugar del valor instantáneo.
        floor_time = 0.0 if track.floor_since is None else now - track.floor_since
        signals = Signals(
            signals.torso_angle, signals.bbox_ratio, track.peak_drop,
            floor_time, signals.head_low, signals.keypoint_conf,
        )
        upright_for = 0.0 if track.upright_since is None else now - track.upright_since

        if track.state == "upright":
            if track.floor_since is not None:
                track.state = "candidate"
            return []

        if track.state == "candidate":
            if upright_for >= CONFIRM_UPRIGHT_S:
                track.state = "upright"
                track.peak_drop = None
                return []
            current = score(signals)
            confirmed = track.floor_since is not None and floor_time >= CONFIRM_FLOOR_S
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
