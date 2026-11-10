"""Analítica de inactividad: episodios de quietud por pista identificada.

    candidate ──quieta min_minutes──► emitted ──movimiento confirmado──► inactivity_ended
        │                                │      cambio de identidad ───► inactivity_ended + candidate nuevo
        └──movimiento confirmado─────────┘
          (en candidate se descarta en silencio y arranca otro anclado en los pies actuales)

Un episodio es "la persona no sale de un radio alrededor de donde pisa".
Solo existe para pistas con identidad (incluida la provisional): una pista
anónima no genera episodios. El anclaje es el punto de pies (tobillos si son
fiables, base de la caja si no): levantarse sin moverse no lo desplaza, a
diferencia del centro de la caja. El radio es RADIUS_FACTOR por el mayor lado
de la caja más grande vista en el episodio, así que no depende de la
postura. Una salida del radio solo cuenta si dura MOVE_CONFIRM_S (un frame
ruidoso no cierra nada).

Las duraciones usan el `now` monótono del worker; los campos de reloj de
pared (started_at, ended_at) se derivan de `clock()` menos la distancia
monótona al instante descrito.
Todo el estado es en memoria: un reinicio del hub a mitad de episodio no
emite inactivity_ended (el motor de reglas lo absorbe, igual que el
fall_resolved de la caída; ver docs/backlog.md).
"""
from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Literal

from vitahub.analytics.fall_engine import _person_payload, _utcnow
from vitahub.analytics.tracker import TrackerUpdate
from vitahub.models import Camera, Detection, Event

# Radio de quietud como fracción del mayor lado de la caja más grande del episodio.
RADIUS_FACTOR = 0.5
# Tiempo continuo fuera del radio para dar el movimiento por real.
# Valor inicial: se calibra con grabaciones reales.
MOVE_CONFIRM_S = 2.0
# Confianza mínima de un tobillo para fiarse del punto de pies por keypoints.
# Valor inicial: se calibra con grabaciones reales.
ANKLE_MIN_CONF = 0.5
# Índices COCO de los tobillos.
L_ANKLE, R_ANKLE = 15, 16

# Por qué termina un episodio emitido. Viaja en el payload de inactivity_ended.
EndReason = Literal["moved", "track_lost", "identity_changed"]

_SEV_INFO = "info"


def feet_point(det: Detection) -> tuple[float, float]:
    """Punto de apoyo de la persona en la imagen.

    Punto medio de los tobillos si ambos son fiables; si no (o sin keypoints),
    centro de la base de la caja.
    """
    kps = det.keypoints
    if kps is not None:
        la, ra = kps[L_ANKLE], kps[R_ANKLE]
        if la[2] >= ANKLE_MIN_CONF and ra[2] >= ANKLE_MIN_CONF:
            return ((la[0] + ra[0]) / 2, (la[1] + ra[1]) / 2)
    x1, _, x2, y2 = det.bbox
    return ((x1 + x2) / 2, float(y2))


def _largest_side(bbox: tuple[int, int, int, int]) -> float:
    return float(max(bbox[2] - bbox[0], bbox[3] - bbox[1]))


@dataclass
class _Episode:
    person_id: str
    person: dict[str, object] | None
    anchor: tuple[float, float]
    start: float
    scale: float
    # Último instante visto dentro del radio (fin real si se mueve).
    last_inside: float
    # Desde cuándo está fuera del radio de forma continua; None = dentro.
    out_since: float | None = None
    # Solo con emitted == True:
    emitted: bool = False
    episode_id: str = ""


@dataclass
class _CameraState:
    episodes: dict[int, _Episode] = field(default_factory=dict)
    # Episodios emitidos en esta cámara: desempata los episode_id del mismo segundo.
    count: int = 0


class InactivityEngine:
    def __init__(
        self,
        hub_id: str,
        min_minutes: float = 5.0,
        clock: Callable[[], datetime] = _utcnow,
    ) -> None:
        self._hub_id = hub_id
        self._min_s = min_minutes * 60.0
        self._clock = clock
        self._cameras: dict[str, _CameraState] = {}

    def observe(self, camera: Camera, update: TrackerUpdate, now: float) -> list[Event]:
        cam = self._cameras.setdefault(camera.id, _CameraState())
        events: list[Event] = []

        # Ocultaciones, antes que las coincidencias y en este orden: con un hueco
        # largo entre frames una misma pista puede venir en expired, parked y
        # lost a la vez, y el primero que la cierra evita el doble fin.
        for parked in update.expired:
            ep = cam.episodes.pop(parked.track_id, None)
            if ep is not None and ep.emitted:
                events.append(self._ended(camera, ep, parked.parked_at, now, "track_lost"))

        parked_ids = {p.track_id for p in update.parked}
        for track in update.lost:
            if track.track_id in parked_ids:
                # Aparcada: el episodio sigue vivo con `last_inside` congelado;
                # la reanudación o la caducidad lo resuelven.
                continue
            ep = cam.episodes.pop(track.track_id, None)
            if ep is not None and ep.emitted:
                events.append(self._ended(camera, ep, track.last_seen, now, "track_lost"))

        for resume in update.resumed:
            ep = cam.episodes.pop(resume.previous_track_id, None)
            if ep is not None:
                # El tiempo oculto cuenta: mismo ancla, inicio y episode_id. Una
                # salida ruidosa previa a la ocultación no debe contar como
                # "fuera desde hace rato" al volver.
                ep.out_since = None
                cam.episodes[resume.track.track_id] = ep

        for match in update.matches:
            track = match.track
            identity = track.identity
            if identity is None:
                ep = cam.episodes.pop(track.track_id, None)
                if ep is not None and ep.emitted:
                    # La pista pierde su identidad con el episodio emitido: se
                    # cierra, no se deja huérfano.
                    events.append(self._ended(camera, ep, now, now, "identity_changed"))
                continue
            det = match.detection
            feet = feet_point(det)
            ep = cam.episodes.get(track.track_id)

            if ep is not None and ep.person_id != identity.person_id:
                # Se saca del estado antes de la guarda de abajo: si la nueva
                # persona ya tiene episodio en otra pista, el viejo no puede
                # quedarse y volver a cerrarse en cada frame.
                del cam.episodes[track.track_id]
                if ep.emitted:
                    events.append(self._ended(camera, ep, now, now, "identity_changed"))
                ep = None
            if ep is None:
                if any(
                    other.person_id == identity.person_id
                    for tid, other in cam.episodes.items()
                    if tid != track.track_id
                ):
                    # Una persona, un episodio por cámara (spec §3.4.6): la pista
                    # más nueva espera sin episodio a que acabe la anterior.
                    continue
                ep = _Episode(
                    person_id=identity.person_id,
                    person=_person_payload(identity),
                    anchor=feet,
                    start=now,
                    scale=_largest_side(det.bbox),
                    last_inside=now,
                )
                cam.episodes[track.track_id] = ep
            else:
                ep.scale = max(ep.scale, _largest_side(det.bbox))

            events += self._advance(
                camera, cam, ep, track.track_id, feet, _largest_side(det.bbox), now
            )
        return events

    # --- estado por episodio ----------------------------------------------

    def _advance(
        self,
        camera: Camera,
        cam: _CameraState,
        ep: _Episode,
        track_id: int,
        feet: tuple[float, float],
        side: float,
        now: float,
    ) -> list[Event]:
        dist = math.hypot(feet[0] - ep.anchor[0], feet[1] - ep.anchor[1])
        if dist <= RADIUS_FACTOR * ep.scale:
            ep.out_since = None
            ep.last_inside = now
        else:
            if ep.out_since is None:
                ep.out_since = now
            if now - ep.out_since < MOVE_CONFIRM_S:
                return []
            events: list[Event] = []
            if ep.emitted:
                events.append(self._ended(camera, ep, ep.last_inside, now, "moved"))
            # Candidato nuevo anclado donde está ahora y con la escala de la caja
            # actual: la de la posición anterior (p. ej. cerca de la cámara) no vale.
            cam.episodes[track_id] = _Episode(
                person_id=ep.person_id,
                person=ep.person,
                anchor=feet,
                start=now,
                scale=side,
                last_inside=now,
            )
            return events

        if not ep.emitted and now - ep.start >= self._min_s:
            cam.count += 1
            ep.emitted = True
            ep.episode_id = f"{camera.id}-{int(self._clock().timestamp())}-{cam.count}"
            return [
                self._event(
                    camera,
                    "inactivity_started",
                    {
                        "episode_id": ep.episode_id,
                        "started_at": self._wall(ep.start, now),
                        "still_s": round(now - ep.start, 3),
                        "person": ep.person,
                    },
                )
            ]
        return []

    # --- eventos ----------------------------------------------------------

    def _wall(self, instant: float, now: float) -> str:
        """Reloj de pared del instante monótono `instant` (ISO 8601 con offset)."""
        return (self._clock() - timedelta(seconds=now - instant)).isoformat()

    def _ended(
        self, camera: Camera, ep: _Episode, end: float, now: float, reason: EndReason
    ) -> Event:
        return self._event(
            camera,
            "inactivity_ended",
            {
                "episode_id": ep.episode_id,
                "started_at": self._wall(ep.start, now),
                "ended_at": self._wall(end, now),
                "duration_s": round(end - ep.start, 3),
                "reason": reason,
                "person": ep.person,
            },
        )

    def _event(self, camera: Camera, event_type: str, payload: dict[str, object]) -> Event:
        return Event(
            hub_id=self._hub_id,
            camera_id=camera.id,
            camera_name=camera.name,
            type=event_type,
            severity=_SEV_INFO,
            timestamp=self._clock().isoformat(),
            payload=payload,
        )
