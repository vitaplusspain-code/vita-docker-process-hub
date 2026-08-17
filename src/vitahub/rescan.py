from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from vitahub.config import Credentials, HubConfig, save_cameras
from vitahub.ingest.rtsp import with_credentials
from vitahub.logging_setup import get_logger
from vitahub.registry import DiscoveredCamera, reconcile
from vitahub.supervisor import CameraSupervisor

_log = get_logger("rescan")

DiscoverFn = Callable[[Credentials], list[DiscoveredCamera]]


@dataclass
class RescanResult:
    status: str  # "ok" | "busy" | "error"
    found: int = 0
    added: list[dict[str, str]] = field(default_factory=list)
    ip_changed: list[str] = field(default_factory=list)
    started: list[str] = field(default_factory=list)
    cameras: int = 0


class RescanService:
    """Un ciclo completo: descubrir, reconciliar, persistir y converger workers.

    El timer periódico, el arranque y el endpoint HTTP entran todos por aquí.
    """

    def __init__(
        self,
        cfg: HubConfig,
        config_path: Path,
        supervisor: CameraSupervisor,
        discover_fn: DiscoverFn,
    ) -> None:
        self._cfg = cfg
        self._config_path = config_path
        self._supervisor = supervisor
        self._discover_fn = discover_fn
        self._lock = threading.Lock()

    def run_once(self) -> RescanResult:
        # No bloqueante: si ya hay un rescan en vuelo, el segundo no se encola.
        if not self._lock.acquire(blocking=False):
            _log.info("rescan ya en curso, se ignora la petición")
            return RescanResult(status="busy")
        try:
            return self._rescan()
        finally:
            self._lock.release()

    def _rescan(self) -> RescanResult:
        try:
            discovered = self._discover_fn(self._cfg.credentials)
        except Exception:  # noqa: BLE001 — un fallo de red no debe tumbar el hub
            _log.exception("rescan: fallo en el descubrimiento")
            return RescanResult(status="error")

        cameras, changes = reconcile(self._cfg.cameras, discovered)
        self._cfg.cameras = cameras
        for change in changes:
            _log.info("registro: %s %s", change.kind, change.camera_id)

        if changes:
            try:
                save_cameras(self._config_path, cameras)
            except Exception:  # noqa: BLE001 — persistir no debe frenar los workers
                _log.exception("rescan: no se pudo persistir el registro")

        uris = {
            dc.id: with_credentials(
                dc.rtsp_sub if self._cfg.inference.stream == "substream" else dc.rtsp_main,
                self._cfg.credentials.onvif_user,
                self._cfg.credentials.onvif_password,
            )
            for dc in discovered
        }
        applied = self._supervisor.apply(cameras, uris)

        by_id = {c.id: c for c in cameras}
        added = [
            {"id": c.camera_id, "name": by_id[c.camera_id].name, "ip": by_id[c.camera_id].last_ip}
            for c in changes
            if c.kind == "added"
        ]
        started = applied.started + applied.restarted
        _log.info(
            "rescan: %d encontradas, %d altas, %d workers arrancados",
            len(discovered),
            len(added),
            len(started),
        )
        return RescanResult(
            status="ok",
            found=len(discovered),
            added=added,
            ip_changed=[c.camera_id for c in changes if c.kind == "ip_changed"],
            started=started,
            cameras=len(cameras),
        )
