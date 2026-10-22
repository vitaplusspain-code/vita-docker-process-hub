"""Disparador de clips: decora el sink de eventos y decide qué caídas se graban."""
from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from vitahub.clips.buffer import PacketBuffer, Snapshot
from vitahub.clips.constants import MAX_POST_S, POST_S, PRE_S
from vitahub.clips.events import clip_event
from vitahub.clips.risk import assess
from vitahub.logging_setup import get_logger
from vitahub.models import Event
from vitahub.sinks.base import EventSink

_log = get_logger("clips.trigger")


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class ClipRequest:
    camera_id: str
    camera_name: str
    episode_ids: list[str]
    clip_start: datetime
    snapshot: Snapshot


@dataclass
class _Window:
    camera_id: str
    camera_name: str
    t0: float
    deadline: float
    episode_ids: list[str] = field(default_factory=list)
    drop_speeds: dict[str, float | None] = field(default_factory=dict)
    reasons: dict[str, str] = field(default_factory=dict)


def _drop_speed(payload: object) -> float | None:
    """Lee signals.drop_speed; cualquier forma inesperada cuenta como no medida."""
    if not isinstance(payload, dict):
        return None
    signals = payload.get("signals")
    if not isinstance(signals, dict):
        return None
    value = signals.get("drop_speed")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


class ClipTriggerSink(EventSink):
    """Reenvía todo al sink interno y, ante una caída, abre una ventana de clip.

    Las ventanas se miden con reloj monótono (un salto del reloj de pared no
    debe moverlas); el de pared solo sirve para fechar el inicio del clip. El
    cierre lo hace `tick()`, llamado periódicamente desde fuera. Montar el MP4
    (`on_clip`) y emitir eventos propios se hace siempre fuera del lock.
    """

    def __init__(
        self,
        inner: EventSink,
        hub_id: str,
        cameras: set[str],
        buffers: Callable[[str], PacketBuffer | None],
        on_clip: Callable[[ClipRequest], None],
        monotonic: Callable[[], float] = time.monotonic,
        wall: Callable[[], datetime] = _utcnow,
    ) -> None:
        self._inner = inner
        self._hub_id = hub_id
        self._cameras = cameras
        self._buffers = buffers
        self._on_clip = on_clip
        self._monotonic = monotonic
        self._wall = wall
        self._lock = threading.Lock()
        self._windows: dict[str, _Window] = {}
        # Tras close() un hilo de cámara rezagado aún puede emitir: el evento
        # sigue llegando al sink interno, pero no abre ventanas que ya nadie
        # cerraría (ni dispararía un clip con el sink interno ya cerrado).
        self._closed = False

    def emit(self, event: Event) -> None:
        # Primero el sink interno: una excepción suya se propaga como siempre.
        self._inner.emit(event)
        try:
            self._track(event)
        except Exception:  # noqa: BLE001 — el disparador nunca debe romper al hilo de cámara
            _log.exception("fallo propio del disparador de clips")

    def _track(self, event: Event) -> None:
        if event.camera_id not in self._cameras:
            return
        payload = event.payload
        episode_id = payload.get("episode_id")
        if not isinstance(episode_id, str):
            return
        now = self._monotonic()
        with self._lock:
            if self._closed:
                return
            win = self._windows.get(event.camera_id)
            if event.type == "fall_detected":
                speed = _drop_speed(payload)
                if win is None:
                    self._windows[event.camera_id] = _Window(
                        camera_id=event.camera_id,
                        camera_name=event.camera_name,
                        t0=now,
                        deadline=now + POST_S,
                        episode_ids=[episode_id],
                        drop_speeds={episode_id: speed},
                    )
                else:
                    if episode_id not in win.episode_ids:
                        win.episode_ids.append(episode_id)
                        win.drop_speeds[episode_id] = speed
                    win.deadline = min(now + POST_S, win.t0 + MAX_POST_S)
            elif event.type == "fall_resolved":
                reason = payload.get("reason")
                if (
                    win is not None
                    and now <= win.deadline
                    and episode_id in win.episode_ids
                    and isinstance(reason, str)
                ):
                    win.reasons[episode_id] = reason

    def tick(self) -> None:
        now = self._monotonic()
        with self._lock:
            if self._closed:
                return
            due = [w for w in self._windows.values() if w.deadline <= now]
            for w in due:
                del self._windows[w.camera_id]
        for w in due:
            try:
                self._close_window(w, now)
            except Exception:  # noqa: BLE001 — una ventana mala no debe frenar a las demás
                _log.exception("fallo cerrando la ventana de clip de %s", w.camera_id)

    def _close_window(self, w: _Window, now: float) -> None:
        verdicts = [
            assess(w.drop_speeds.get(ep), w.reasons.get(ep)) for ep in w.episode_ids
        ]
        if "risk" not in verdicts:
            reason = "got_up" if all(v == "got_up" for v in verdicts) else "slow"
            self._emit_own(w, "fall_clip_discarded", {
                "episode_ids": list(w.episode_ids),
                "drop_speed": w.drop_speeds.get(w.episode_ids[0]),
                "reason": reason,
            })
            return
        start = w.t0 - PRE_S
        buf = self._buffers(w.camera_id)
        snap = buf.snapshot(start, w.deadline) if buf is not None else None
        if snap is None:
            self._emit_own(w, "fall_clip_failed", {
                "episode_ids": list(w.episode_ids),
                "reason": "no_video",
            })
            return
        clip_start = self._wall() - timedelta(seconds=now - start)
        self._on_clip(ClipRequest(
            camera_id=w.camera_id,
            camera_name=w.camera_name,
            episode_ids=list(w.episode_ids),
            clip_start=clip_start,
            snapshot=snap,
        ))

    def _emit_own(self, w: _Window, type: str, payload: dict[str, object]) -> None:
        self._inner.emit(
            clip_event(self._hub_id, w.camera_id, w.camera_name, type, payload, self._wall)
        )

    def close(self) -> None:
        with self._lock:
            self._closed = True
            pending = list(self._windows.values())
            self._windows.clear()
        for w in pending:
            try:
                self._emit_own(w, "fall_clip_failed", {
                    "episode_ids": list(w.episode_ids),
                    "reason": "shutdown",
                })
            except Exception:  # noqa: BLE001 — el cierre debe llegar siempre a inner.close()
                _log.exception("fallo emitiendo fall_clip_failed en el cierre")
        self._inner.close()
