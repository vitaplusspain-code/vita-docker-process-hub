"""Analítica de caídas: máquina de estados de episodio por pista.

    upright ──tumbado──► candidate ──≥ 2 s en el suelo, con bajada brusca──► reported
       ▲                    │  │          y score ≥ min                         │
       │                    │  │                de pie 2 s, o pista perdida ────┘ → fall_resolved
       │                    │  └──≥ 2 s en el suelo, sin bajada o score bajo──► lying
       └──de pie 2 s────────┘                                                    │
       ▲                                                                         │
       └──fuera del suelo 2 s, o pista perdida ──────────────────────────────────┘ → lying_resolved

Un episodio `lying` (tumbado sin caída) nunca escala a `reported`.

El emparejamiento entre frames (qué pista es qué persona) lo hace el
`Tracker` (ver vitahub.analytics.tracker); este motor solo consume sus
`Match`/pistas perdidas y lleva el estado del episodio (postura, score,
umbrales, cadencia de eventos) por track_id.
Todo el estado es en memoria; un reinicio del hub a mitad de episodio
pierde el fall_resolved o lying_resolved (igual que el ConnectionMonitor, ver docs/backlog.md).
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal

from vitahub.analytics.fall_signals import (
    MIN_FALL_DROP,
    Sample,
    Signals,
    compute_signals,
    is_horizontal,
    is_upright,
    reference_y,
    score,
)
from vitahub.analytics.frame_edges import touches_edge
from vitahub.analytics.tracker import Match, Track, TrackerUpdate, TrackIdentity
from vitahub.models import Camera, Event

# Historial de alturas que se conserva por pista (cubre la ventana de drop_speed).
HISTORY_S = 3.0
# Tiempo mínimo en el suelo antes de emitir: evita disparar por un frame ruidoso.
CONFIRM_FLOOR_S = 2.0
# Espera extra (más allá de CONFIRM_FLOOR_S) a que el score de una bajada brusca
# llegue a min_score antes de darla por tumbado: la permanencia en el suelo sube
# el score con el tiempo. Valor calibrado: con 3 s la caída típica con score
# justo por debajo del mínimo a los 2 s lo alcanza, sin retrasar el aviso de tumbado.
FALL_SCORE_WAIT_S = 3.0
# Tiempo de pie necesario para cerrar un episodio (o volver de candidate).
CONFIRM_UPRIGHT_S = 2.0
# Cadencia máxima de fall_update mientras sigue en el suelo.
UPDATE_EVERY_S = 10.0
# Cadencia de lying_update: un tumbado puede durar horas, no hace falta más.
LYING_UPDATE_S = 60.0
# Tiempo máximo que suma una sola muestra a los acumuladores: el doble del
# periodo de muestreo a 2 fps. Un hueco mayor (frames perdidos, worker
# atascado) no cuenta como tiempo "visto" en el suelo ni de pie.
MAX_STEP_S = 1.0

# Por que se cierra un episodio: de pie CONFIRM_UPRIGHT_S ("upright") o pista
# sin observacion TRACK_TTL_S ("track_lost"). Viaja en el payload de fall_resolved.
ResolvedReason = Literal["upright", "track_lost"]
# Igual para lying_resolved: "upright" = dejó de estar en el suelo (de pie o
# sentada) CONFIRM_UPRIGHT_S; "track_lost" = pista sin observación.
LyingReason = Literal["upright", "track_lost"]

_SEV_HIGH = "high"
_SEV_INFO = "info"


def _person_payload(identity: TrackIdentity | None) -> dict[str, object] | None:
    """Extrae identidad de la pista para el payload del evento.

    None = pista anónima. El hub etiqueta, no filtra: quien decide por
    identidad es el consumidor en AWS; del hogar solo sale el alias.
    """
    if identity is None:
        return None
    return {"id": identity.person_id, "confidence": round(identity.confidence, 3)}


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass
class _EpisodeState:
    history: list[Sample] = field(default_factory=list)
    state: str = "upright"  # upright | candidate | reported | lying
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
    # Con state == "lying": tiempo seguido fuera del suelo (de pie u otra
    # postura, p. ej. sentada en la cama); cierra el episodio.
    not_floor_time: float = 0.0
    # Solo con state == "reported" o "lying":
    episode_id: str = ""
    episode_start: float = 0.0
    last_event_at: float = 0.0
    max_score: float = 0.0  # solo "reported"


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

    def observe(
        self,
        camera: Camera,
        update: TrackerUpdate,
        now: float,
        frame_size: tuple[int, int] | None = None,
    ) -> list[Event]:
        cam = self._cameras.setdefault(camera.id, _CameraState())
        events: list[Event] = []

        # El tracker ya caducó estas pistas; aquí solo queda cerrar el episodio.
        for track in update.lost:
            state = cam.states.pop(track.track_id, None)
            if state is not None and state.state == "reported":
                events.append(self._resolved(camera, state, track, now, "track_lost"))
            elif state is not None and state.state == "lying":
                events.append(self._lying_resolved(camera, state, track, now, "track_lost"))

        for match in update.matches:
            state = cam.states.setdefault(match.track.track_id, _EpisodeState())
            events += self._update_state(
                camera, cam, state, match, now, len(update.matches), frame_size
            )
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
        frame_size: tuple[int, int] | None = None,
    ) -> list[Event]:
        det = match.detection
        track = match.track
        # Tiempo observado desde la muestra anterior (0 en una pista nueva).
        step = min(match.step_s, MAX_STEP_S)
        h = max(0, det.bbox[3] - det.bbox[1])
        state.history.append((now, reference_y(det.keypoints, det.bbox), float(h)))
        state.history = [s for s in state.history if s[0] >= now - HISTORY_S]

        signals = compute_signals(det.keypoints, det.bbox, state.history, now, state.floor_time)

        # Caja cortada por el borde y sin torso fiable: las proporciones de la
        # caja truncada no dicen nada de la postura. Muestra neutra en todo
        # estado: ni suma ni reinicia contadores, la postura anterior se
        # conserva y el episodio ni se cierra ni se estanca. La muestra sí queda
        # en el historial, pero su punto de referencia puede ser el centro de una
        # caja truncada, no la altura real: pendiente de calibración con vídeo real.
        if signals.torso_angle is None and touches_edge(det.bbox, frame_size):
            return []

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
            if not (horizontal and floor_time >= CONFIRM_FLOOR_S):
                return []
            sharp = state.peak_drop is not None and state.peak_drop >= MIN_FALL_DROP
            # Bajada brusca con score aún corto: la permanencia lo sube; se
            # espera hasta FALL_SCORE_WAIT_S antes de rendirse a tumbado.
            if (
                sharp
                and current < self._min_score
                and floor_time < CONFIRM_FLOOR_S + FALL_SCORE_WAIT_S
            ):
                return []
            cam.episodes += 1
            state.episode_id = f"{camera.id}-{int(self._clock().timestamp())}-{cam.episodes}"
            state.episode_start = now
            state.last_event_at = now
            if sharp and current >= self._min_score:
                state.state = "reported"
                state.max_score = current
                return [
                    self._fall_event(
                        camera, state, track, "fall_detected", current, signals, person_count
                    )
                ]
            # Sin bajada brusca, o con ella pero sin llegar al score mínimo a
            # tiempo: tumbado, no caída.
            state.state = "lying"
            state.not_floor_time = 0.0
            return [self._lying_detected(camera, state, track, signals, person_count)]

        if state.state == "lying":
            state.not_floor_time = state.not_floor_time + step if state.posture != "floor" else 0.0
            if state.not_floor_time >= CONFIRM_UPRIGHT_S:
                ev = self._lying_resolved(camera, state, track, now, "upright")
                state.state = "upright"
                state.peak_drop = None
                return [ev]
            if now - state.last_event_at >= LYING_UPDATE_S:
                state.last_event_at = now
                return [self._lying_update(camera, state, track)]
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
                "person": _person_payload(track.identity),
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
                "person": _person_payload(track.identity),
            },
        )

    def _lying_detected(
        self,
        camera: Camera,
        state: _EpisodeState,
        track: Track,
        signals: Signals,
        person_count: int,
    ) -> Event:
        return Event(
            hub_id=self._hub_id,
            camera_id=camera.id,
            camera_name=camera.name,
            type="lying_detected",
            severity=_SEV_HIGH,
            timestamp=self._clock().isoformat(),
            payload={
                "episode_id": state.episode_id,
                "signals": signals.to_payload(),
                "person_count": person_count,
                "person": _person_payload(track.identity),
            },
        )

    def _lying_update(self, camera: Camera, state: _EpisodeState, track: Track) -> Event:
        return Event(
            hub_id=self._hub_id,
            camera_id=camera.id,
            camera_name=camera.name,
            type="lying_update",
            severity=_SEV_INFO,
            timestamp=self._clock().isoformat(),
            payload={
                "episode_id": state.episode_id,
                "person": _person_payload(track.identity),
            },
        )

    def _lying_resolved(
        self, camera: Camera, state: _EpisodeState, track: Track, now: float, reason: LyingReason
    ) -> Event:
        return Event(
            hub_id=self._hub_id,
            camera_id=camera.id,
            camera_name=camera.name,
            type="lying_resolved",
            severity=_SEV_INFO,
            timestamp=self._clock().isoformat(),
            payload={
                "episode_id": state.episode_id,
                "duration_s": round(now - state.episode_start, 3),
                "reason": reason,
                "person": _person_payload(track.identity),
            },
        )
