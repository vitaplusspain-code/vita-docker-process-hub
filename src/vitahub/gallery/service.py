"""Sincroniza /data/faces con el named shadow `face-gallery`.

Los callbacks MQTT llegan en el hilo de red de paho y solo anotan estado y
despiertan al hilo propio: listar S3, descargar y validar con el motor facial
es lento y bloquearía el uplink. Todo el trabajo se hace en `step`, que también
existe para testear sin hilos.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from pathlib import Path

from vitahub.gallery.apply import apply_manifest
from vitahub.gallery.manifest import Manifest, parse_manifest
from vitahub.gallery.s3 import gallery_key
from vitahub.gallery.shadow import (
    identity_only_payload,
    parse_delta_version,
    parse_get_accepted,
    reported_payload,
    shadow_topics,
)
from vitahub.identity.base import FaceEngine
from vitahub.sinks.aws_iot import MqttLink

logger = logging.getLogger(__name__)

GET_RETRY_S = 30.0
BACKOFF_FIRST_S = 60.0
BACKOFF_MAX_S = 900.0
# Un fallo persistente (permiso mal puesto) se repite durante horas: se loguea
# el primero de cada episodio y uno de cada LOG_EVERY.
LOG_EVERY = 10
# El bucle no puede esperar a `stop` y a la señal a la vez: espera a la señal
# con este tope para enterarse de `stop` sin tardar.
STOP_POLL_S = 1.0


class _ApplyFailed(Exception):
    """Aplicación abortada sin tocar el disco; se reintenta con backoff."""


class CloudGalleryService:
    def __init__(
        self,
        link: MqttLink,
        hub_id: str,
        faces_dir: Path,
        list_keys: Callable[[str], frozenset[str]],
        get_object: Callable[[str], bytes],
        engine: Callable[[], FaceEngine],
        identity_enabled: bool,
        request_restart: Callable[[], None],
        clock: Callable[[], float] = time.monotonic,
        debounce_s: float = 30.0,
    ) -> None:
        self._link = link
        self._hub_id = hub_id
        self._faces_dir = faces_dir
        self._list_keys = list_keys
        self._get_object = get_object
        self._engine = engine
        self._identity_enabled = identity_enabled
        self._request_restart = request_restart
        self._clock = clock
        self._debounce_s = debounce_s
        self._topics = shadow_topics(hub_id)

        self._lock = threading.Lock()
        self._wake = threading.Event()
        # Siguiente `get` pendiente; None cuando ya llegó la respuesta.
        self._get_at: float | None = None
        # Sin respuesta al `get` no se conocen los photoId reportados: aplicar
        # antes dejaría sin borrar (null) los que ya no están.
        self._synced = False
        self._desired: int | None = None
        self._applied = 0
        self._previous_ids: frozenset[str] = frozenset()
        self._apply_at: float | None = None
        self._failures = 0

    # --- ciclo de vida ------------------------------------------------------

    def start(self, stop: threading.Event) -> None:
        self._link.subscribe(self._topics.get_accepted, self._on_get_accepted)
        self._link.subscribe(self._topics.get_rejected, self._on_get_rejected)
        self._link.subscribe(self._topics.update_delta, self._on_delta)
        # Después de suscribir: si ya hay conexión el listener corre ya y su
        # `get` necesita la suscripción a la respuesta.
        self._link.add_connect_listener(self._on_connected)
        threading.Thread(target=self._run, args=(stop,), name="face-gallery", daemon=True).start()

    def _run(self, stop: threading.Event) -> None:
        while not stop.is_set():
            # Se limpia antes de `step`: una señal que llegue durante `step`
            # hace que la espera siguiente vuelva enseguida.
            self._wake.clear()
            try:
                timeout = self.step(self._clock())
            except Exception:  # el hilo de la galería no puede morir
                logger.exception("face-gallery: fallo inesperado en el bucle")
                timeout = BACKOFF_FIRST_S
            self._wake.wait(STOP_POLL_S if timeout is None else min(timeout, STOP_POLL_S))

    # --- callbacks MQTT (hilo de paho) ---------------------------------------

    def _on_connected(self) -> None:
        with self._lock:
            self._get_at = self._clock()
        self._wake.set()

    def _on_get_accepted(self, raw: bytes) -> None:
        try:
            snap = parse_get_accepted(raw)
        except ValueError:
            logger.warning("face-gallery: get/accepted ilegible, se ignora")
            return
        now = self._clock()
        republish = False
        with self._lock:
            self._get_at = None
            self._synced = True
            self._previous_ids = snap.reported_photo_ids
            self._applied = snap.reported_version or 0
            self._desired = _max(self._desired, snap.desired_version)
            if self._desired is not None and self._desired > self._applied:
                self._apply_at = now + self._debounce_s
            else:
                # Galería al día pero `identity_enabled` cambió en `hub.yaml`
                # desde el último reported: la web lo vería mal sin esto.
                republish = (
                    snap.desired_version is not None
                    and snap.reported_version is not None
                    and snap.reported_identity_enabled is not None
                    and snap.reported_identity_enabled != self._identity_enabled
                )
        if republish:
            self._link.publish(self._topics.update, identity_only_payload(self._identity_enabled))
        self._wake.set()

    def _on_get_rejected(self, raw: bytes) -> None:
        # Normalmente 404: el shadow aún no existe. No hay nada que aplicar;
        # cuando la web escriba `desired` llegará un delta.
        with self._lock:
            self._get_at = None
            self._synced = True
        self._wake.set()

    def _on_delta(self, raw: bytes) -> None:
        try:
            version = parse_delta_version(raw)
        except ValueError:
            logger.warning("face-gallery: delta ilegible, se ignora")
            return
        if version is None:
            return
        now = self._clock()
        with self._lock:
            self._desired = _max(self._desired, version)
            # Cada delta reinicia la espera: la web sube fotos en ráfaga.
            self._apply_at = now + self._debounce_s
        self._wake.set()

    # --- trabajo (hilo propio) -------------------------------------------------

    def step(self, now: float) -> float | None:
        """Hace lo pendiente y devuelve los segundos hasta la próxima acción."""
        with self._lock:
            send_get = self._get_at is not None and now >= self._get_at
            if send_get:
                self._get_at = now + GET_RETRY_S
            apply_due = (
                self._synced
                and self._desired is not None
                and self._apply_at is not None
                and now >= self._apply_at
            )
            if apply_due:
                # Un delta durante la aplicación vuelve a fijar `_apply_at`.
                self._apply_at = None
            desired = self._desired
            previous_ids = self._previous_ids
        if send_get:
            self._link.publish(self._topics.get, "{}")
        if apply_due and desired is not None:
            self._attempt(now, desired, previous_ids)
        with self._lock:
            # Sin respuesta al `get` la aplicación vencida no puede correr: contarla
            # devolvería 0 y el bucle giraría sin parar hasta la respuesta.
            apply_at = self._apply_at if self._synced else None
            pending = [t for t in (self._get_at, apply_at) if t is not None]
        return max(0.0, min(pending) - now) if pending else None

    def _attempt(self, now: float, desired: int, previous_ids: frozenset[str]) -> None:
        try:
            self._apply(now, desired, previous_ids)
        except Exception as exc:  # noqa: BLE001 — 403, manifiesto roto o motor sin cargar: reintento
            with self._lock:
                self._failures += 1
                failures = self._failures
                # Exponente acotado: tras ~1000 fallos (días con un permiso
                # mal puesto) 2**n ya no cabe en un float.
                delay = min(BACKOFF_FIRST_S * 2 ** min(failures - 1, 10), BACKOFF_MAX_S)
                # Un delta llegado durante el intento manda: la web cambió algo.
                if self._apply_at is None:
                    self._apply_at = now + delay
            if (failures - 1) % LOG_EVERY == 0:
                logger.warning(
                    "face-gallery: no se pudo aplicar la versión %d (intento %d, reintento en %.0f s): %s: %s",
                    desired,
                    failures,
                    delay,
                    type(exc).__name__,
                    exc,
                )

    def _apply(self, now: float, desired: int, previous_ids: frozenset[str]) -> None:
        listed = self._list_keys(gallery_key(self._hub_id))
        manifest_key = gallery_key(self._hub_id, "manifest.json")
        if manifest_key in listed:
            manifest = parse_manifest(self._get_object(manifest_key))
        else:
            # Ausente del listado = la web borró todo. Un 403 al listar lanza
            # antes de llegar aquí y nunca se confunde con galería vacía.
            manifest = Manifest(version=desired, persons={})
        if manifest.version < desired:
            raise _ApplyFailed(f"el manifiesto es la versión {manifest.version}, se espera {desired}")

        def fetch(person_key: str, photo_id: str) -> bytes | None:
            key = gallery_key(self._hub_id, person_key, f"{photo_id}.jpg")
            # Solo lo listado: un GetObject de una clave inexistente da 403.
            return self._get_object(key) if key in listed else None

        result = apply_manifest(self._faces_dir, manifest, fetch, self._engine())
        self._link.publish(
            self._topics.update,
            reported_payload(manifest.version, self._identity_enabled, result.statuses, previous_ids),
        )
        with self._lock:
            self._failures = 0
            self._previous_ids = frozenset(result.statuses)
            self._applied = max(self._applied, manifest.version)
            # Un delta durante la aplicación ya dejó su propia espera; esto solo
            # cubre que `desired` subiera sin espera fijada, para no quedarse atrás.
            if self._desired is not None and self._desired > self._applied and self._apply_at is None:
                self._apply_at = now + self._debounce_s
        # Después de publicar: el reinicio corta el proceso y el reported se perdería.
        if result.changed and self._identity_enabled:
            self._request_restart()


def _max(a: int | None, b: int | None) -> int | None:
    if a is None:
        return b
    if b is None:
        return a
    return max(a, b)
