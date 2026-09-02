"""Analítica de caídas: máquina de estados de episodio por pista.

    upright ──tumbado──► candidate ──score ≥ min y ≥ 2 s en el suelo──► reported
       ▲                    │                                             │
       └──de pie 2 s────────┘         de pie 2 s, o pista perdida 3 s ────┘ → fall_resolved

El emparejamiento entre frames (qué pista es qué persona) lo hace el
`Tracker` (ver vitahub.analytics.tracker); este motor solo consume sus
`Match`/pistas perdidas y lleva el estado del episodio (postura, score,
umbrales, cadencia de eventos) por track_id.
Todo el estado es en memoria; un reinicio del hub a mitad de episodio
pierde el fall_resolved (igual que el ConnectionMonitor, ver docs/backlog.md).
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal

from vitahub.analytics.fall_signals import (
    Sample,
    Signals,
    compute_signals,
    is_horizontal,
    is_upright,
    reference_y,
    score,
)
from vitahub.analytics.tracker import Match, Track, TrackerUpdate
from vitahub.models import Camera, Event

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

# Por que se cierra un episodio: de pie CONFIRM_UPRIGHT_S ("upright") o pista
# sin observacion TRACK_TTL_S ("track_lost"). Viaja en el payload de fall_resolved.
ResolvedReason = Literal["upright", "track_lost"]

_SEV_HIGH = "high"
_SEV_INFO = "info"


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass
class _EpisodeState:
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
    states: dict[int, _EpisodeState] = field(default_factory=dict)
    # Episodios confirmados en esta cámara: desempata los episode_id de dos
    # caídas que se confirman en el mismo segundo de reloj.
    episodes: int = 0


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

    def observe(self, camera: Camera, update: TrackerUpdate, now: float) -> list[Event]:
        cam = self._cameras.setdefault(camera.id, _CameraState())
        events: list[Event] = []

        # El tracker ya caducó estas pistas; aquí solo queda cerrar el episodio.
        for track in update.lost:
            state = cam.states.pop(track.track_id, None)
            if state is not None and state.state == "reported":
                events.append(self._resolved(camera, state, track, now, "track_lost"))

        for match in update.matches:
            state = cam.states.setdefault(match.track.track_id, _EpisodeState())
            events += self._update_state(camera, cam, state, match, now, len(update.matches))
        return events

    # --- estado por pista -----------------------------------------------

    def _update_state(
        self,
        camera: Camera,
        cam: _CameraState,
        state: _EpisodeState,
        match: Match,
        now: float,
        person_count: int,
    ) -> list[Event]:
        det = match.detection
        track = match.track
        # Tiempo observado desde la muestra anterior (0 en una pista nueva).
        step = min(match.step_s, MAX_STEP_S)
        h = max(0, det.bbox[3] - det.bbox[1])
        state.history.append((now, reference_y(det.keypoints, det.bbox), float(h)))
        state.history = [s for s in state.history if s[0] >= now - HISTORY_S]

        signals = compute_signals(det.keypoints, det.bbox, state.history, now, state.floor_time)

        # El frame de la transición arranca el contador en 0; a partir de ahí
        # cada muestra suma lo que se ha visto realmente.
        horizontal = is_horizontal(signals)
        if horizontal:
            state.floor_time = state.floor_time + step if state.posture == "floor" else 0.0
            state.upright_time = 0.0
            state.posture = "floor"
        elif is_upright(signals):
            state.upright_time = state.upright_time + step if state.posture == "upright" else 0.0
            state.floor_time = 0.0
            state.posture = "upright"
        else:
            state.floor_time = 0.0
            state.upright_time = 0.0
            state.posture = "other"

        if state.state == "upright" and not horizontal:
            state.peak_drop = None
        elif signals.drop_speed is not None:
            state.peak_drop = max(state.peak_drop or 0.0, signals.drop_speed)

        # Rehacer las señales con el tiempo en el suelo recién acumulado y con
        # el pico de bajada en lugar del valor instantáneo.
        signals = Signals(
            signals.torso_angle, signals.bbox_ratio, state.peak_drop,
            state.floor_time, signals.head_low, signals.keypoint_conf,
        )
        floor_time = state.floor_time
        upright_for = state.upright_time

        if state.state == "upright":
            if horizontal:
                state.state = "candidate"
            return []

        if state.state == "candidate":
            if upright_for >= CONFIRM_UPRIGHT_S:
                state.state = "upright"
                state.peak_drop = None
                return []
            current = score(signals)
            confirmed = horizontal and floor_time >= CONFIRM_FLOOR_S
            if confirmed and current >= self._min_score:
                cam.episodes += 1
                state.state = "reported"
                state.episode_id = f"{camera.id}-{int(self._clock().timestamp())}-{cam.episodes}"
                state.episode_start = now
                state.last_event_at = now
                state.max_score = current
                return [
                    self._fall_event(
                        camera, state, track, "fall_detected", current, signals, person_count
                    )
                ]
            return []

        # reported: el score se recalcula cada frame (max_score es el pico real
        # del episodio); la cadencia de 10 s solo gobierna la emisión.
        current = score(signals)
        state.max_score = max(state.max_score, current)
        if upright_for >= CONFIRM_UPRIGHT_S:
            ev = self._resolved(camera, state, track, now, "upright")
            state.state = "upright"
            state.peak_drop = None
            return [ev]
        if now - state.last_event_at >= UPDATE_EVERY_S:
            state.last_event_at = now
            return [
                self._fall_event(
                    camera, state, track, "fall_update", current, signals, person_count
                )
            ]
        return []

    # --- eventos ----------------------------------------------------------

    def _fall_event(
        self,
        camera: Camera,
        state: _EpisodeState,
        track: Track,
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
                "episode_id": state.episode_id,
                "score": round(current, 3),
                "signals": signals.to_payload(),
                "person_count": person_count,
            },
        )

    def _resolved(
        self, camera: Camera, state: _EpisodeState, track: Track, now: float, reason: ResolvedReason
    ) -> Event:
        # `reason` distingue "se levanto" de "dejamos de verla": para el motor de
        # reglas no es lo mismo, una oclusion de 3 s no debe cerrar una alerta.
        return Event(
            hub_id=self._hub_id,
            camera_id=camera.id,
            camera_name=camera.name,
            type="fall_resolved",
            severity=_SEV_INFO,
            timestamp=self._clock().isoformat(),
            payload={
                "episode_id": state.episode_id,
                "duration_s": round(now - state.episode_start, 3),
                "max_score": round(state.max_score, 3),
                "reason": reason,
            },
        )
