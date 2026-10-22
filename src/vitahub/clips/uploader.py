"""Cola persistente de clips y subida a S3."""
from __future__ import annotations

import json
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

from vitahub.clips.assembler import assemble_mp4
from vitahub.clips.constants import PENDING_MAX_AGE_S, PENDING_MAX_MB
from vitahub.clips.events import clip_event
from vitahub.clips.trigger import ClipRequest
from vitahub.ingest.rtsp import backoff_delay, next_attempt
from vitahub.live.credentials import IotCredentialsProvider
from vitahub.logging_setup import get_logger
from vitahub.sinks.base import EventSink

_log = get_logger("clips.uploader")


def _utcnow() -> datetime:
    return datetime.now(UTC)


class UploadRejected(Exception):
    """S3 respondió 403: reintentar no ayuda hasta que cambien los permisos."""


@dataclass(frozen=True)
class PendingClip:
    mp4: Path
    key: str
    camera_id: str
    camera_name: str
    episode_ids: list[str]
    clip_start: str
    sha256: str
    duration_s: float
    created_at: float

    @property
    def meta(self) -> Path:
        return self.mp4.with_suffix(".json")


class PendingQueue:
    """Clips ya montados esperando a subirse, en disco para sobrevivir a un reinicio.

    Cada clip son dos ficheros (`<uuid>.mp4` y `<uuid>.json`). El JSON se
    escribe el último y con renombrado atómico, así que su presencia indica que
    el clip está completo; un `.part` o un fichero sin pareja es resto de un
    corte a medias y no se sube.
    """

    def __init__(
        self,
        root: Path,
        max_bytes: int = PENDING_MAX_MB * 1024 * 1024,
        max_age_s: float = PENDING_MAX_AGE_S,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._root = root
        self._max_bytes = max_bytes
        self._max_age_s = max_age_s
        self._clock = clock
        self._lock = threading.Lock()
        root.mkdir(parents=True, exist_ok=True)
        self._clips: list[PendingClip] = self._scan()

    def _scan(self) -> list[PendingClip]:
        for part in self._root.glob("*.part"):
            part.unlink(missing_ok=True)
        clips: list[PendingClip] = []
        for meta in sorted(self._root.glob("*.json")):
            mp4 = meta.with_suffix(".mp4")
            try:
                data = json.loads(meta.read_text())
                clip = PendingClip(mp4=mp4, **{**data, "episode_ids": list(data["episode_ids"])})
            except Exception:  # noqa: BLE001 — un JSON corrupto no debe impedir arrancar la cola
                _log.exception("metadatos de clip ilegibles, se ignoran: %s", meta)
                continue
            if not mp4.exists():
                _log.warning("metadatos sin MP4, se ignoran: %s", meta)
                continue
            clips.append(clip)
        for mp4 in self._root.glob("*.mp4"):
            if not mp4.with_suffix(".json").exists():
                _log.warning("MP4 sin metadatos, se ignora: %s", mp4)
        clips.sort(key=lambda c: c.created_at)
        return clips

    def add(
        self, request: ClipRequest, hub_id: str
    ) -> tuple[PendingClip | None, list[PendingClip]]:
        name = uuid.uuid4().hex
        part = self._root / f"{name}.mp4.part"
        mp4 = self._root / f"{name}.mp4"
        meta = self._root / f"{name}.json"
        meta_part = self._root / f"{name}.json.part"
        try:
            clip_file = assemble_mp4(request.snapshot, part)
            part.rename(mp4)
            clip = PendingClip(
                mp4=mp4,
                key=f"{hub_id}/{request.camera_id}/{request.episode_ids[0]}.mp4",
                camera_id=request.camera_id,
                camera_name=request.camera_name,
                episode_ids=list(request.episode_ids),
                clip_start=request.clip_start.isoformat(),
                sha256=clip_file.sha256,
                duration_s=clip_file.duration_s,
                created_at=self._clock(),
            )
            data = asdict(clip)
            del data["mp4"]
            meta_part.write_text(json.dumps(data))
            meta_part.rename(meta)
        except Exception:  # noqa: BLE001 — el llamador emite mux_error; aquí solo se limpia
            _log.exception("no se pudo montar el clip de %s", request.camera_id)
            for leftover in (part, mp4, meta_part, meta):
                leftover.unlink(missing_ok=True)
            return None, []
        with self._lock:
            self._clips.append(clip)
            evicted = self._evict(keep=clip)
        return clip, evicted

    def _evict(self, keep: PendingClip) -> list[PendingClip]:
        """Expulsa los más antiguos hasta caber; el recién añadido se conserva siempre.

        Es la evidencia más reciente: si él solo supera el límite se queda y se
        expulsa todo lo anterior.
        """
        evicted: list[PendingClip] = []
        total = sum(self._size(c) for c in self._clips)
        for clip in sorted(self._clips, key=lambda c: c.created_at):
            if total <= self._max_bytes:
                break
            if clip is keep:
                continue
            total -= self._size(clip)
            self._delete(clip)
            evicted.append(clip)
        return evicted

    @staticmethod
    def _size(clip: PendingClip) -> int:
        try:
            return clip.mp4.stat().st_size
        except OSError:
            return 0

    def _delete(self, clip: PendingClip) -> None:
        clip.meta.unlink(missing_ok=True)
        clip.mp4.unlink(missing_ok=True)
        if clip in self._clips:
            self._clips.remove(clip)

    def expire(self) -> list[PendingClip]:
        limit = self._clock() - self._max_age_s
        with self._lock:
            old = [c for c in self._clips if c.created_at < limit]
            for clip in old:
                self._delete(clip)
        return old

    def oldest(self) -> PendingClip | None:
        with self._lock:
            return min(self._clips, key=lambda c: c.created_at, default=None)

    def remove(self, clip: PendingClip) -> None:
        with self._lock:
            self._delete(clip)


class ClipUploader:
    """Sube la cola a S3 y emite el resultado de cada clip."""

    def __init__(
        self,
        queue: PendingQueue,
        hub_id: str,
        sink: EventSink,
        put: Callable[[str, Path, dict[str, str]], None],
        clock: Callable[[], datetime] = _utcnow,
    ) -> None:
        self._queue = queue
        self._hub_id = hub_id
        self._sink = sink
        self._put = put
        self._clock = clock
        # Un 403 no se reintenta en bucle ni se avisa en cada vuelta; tras un
        # reinicio puede avisarse una vez más, y se acepta.
        self._rejected: set[str] = set()

    def _emit(
        self, camera_id: str, camera_name: str, type: str, payload: dict[str, object]
    ) -> None:
        self._sink.emit(clip_event(self._hub_id, camera_id, camera_name, type, payload, self._clock))

    def _failed(self, clip: PendingClip, reason: str) -> None:
        self._emit(
            clip.camera_id,
            clip.camera_name,
            "fall_clip_failed",
            {"episode_ids": clip.episode_ids, "reason": reason},
        )

    def submit(self, request: ClipRequest) -> None:
        clip, evicted = self._queue.add(request, self._hub_id)
        if clip is None:
            self._emit(
                request.camera_id,
                request.camera_name,
                "fall_clip_failed",
                {"episode_ids": request.episode_ids, "reason": "mux_error"},
            )
        for old in evicted:
            self._failed(old, "queue_full")

    def run_once(self) -> bool:
        for expired in self._queue.expire():
            self._failed(expired, "expired")
        clip = self._queue.oldest()
        if clip is None or clip.key in self._rejected:
            return False
        metadata = {
            "episode-ids": ",".join(clip.episode_ids),
            "camera-id": clip.camera_id,
            "clip-start": clip.clip_start,
            "sha256": clip.sha256,
        }
        try:
            self._put(clip.key, clip.mp4, metadata)
        except UploadRejected:
            self._rejected.add(clip.key)
            self._failed(clip, "upload_rejected")
            return False
        self._queue.remove(clip)
        self._emit(
            clip.camera_id,
            clip.camera_name,
            "fall_clip_ready",
            {
                "episode_ids": clip.episode_ids,
                "s3_key": clip.key,
                "sha256": clip.sha256,
                "duration_s": clip.duration_s,
                "clip_start": clip.clip_start,
            },
        )
        return True

    def run(self, stop: threading.Event) -> None:
        attempt = 0
        while not stop.is_set():
            try:
                if self._queue.oldest() is None:
                    self.run_once()  # solo caduca; no hay nada que subir
                    stop.wait(1.0)
                    continue
                if self.run_once():
                    attempt = 0
                    continue
            except Exception:  # noqa: BLE001 — el hilo de subida no puede morir
                _log.exception("fallo subiendo clip, se reintenta")
            attempt = next_attempt(attempt)
            stop.wait(backoff_delay(attempt))


def s3_put(
    bucket: str, region: str, creds: IotCredentialsProvider
) -> Callable[[str, Path, dict[str, str]], None]:
    """`put` real contra S3. Runtime only: requiere la cuenta real."""

    def _put(key: str, path: Path, metadata: dict[str, str]) -> None:
        import botocore.session
        from botocore.exceptions import ClientError

        c = creds.get()  # en cada llamada: las credenciales rotan
        client = botocore.session.get_session().create_client(
            "s3",
            region_name=region,
            aws_access_key_id=c.access_key,
            aws_secret_access_key=c.secret_key,
            aws_session_token=c.token,
        )
        try:
            with path.open("rb") as body:
                client.put_object(
                    Bucket=bucket,
                    Key=key,
                    Body=body,
                    ContentType="video/mp4",
                    Metadata=metadata,
                    IfNoneMatch="*",
                )
        except ClientError as exc:
            status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
            if status == 412:
                return  # ya estaba subido (spec §3.1)
            if status == 403:
                raise UploadRejected(key) from exc
            raise

    return _put
