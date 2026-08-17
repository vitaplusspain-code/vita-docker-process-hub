# Rescan de cámaras en caliente — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Que el hub dé de alta cámaras nuevas y reabsorba cambios de IP sin reiniciar el contenedor, por un bucle periódico y por un `POST /rescan` protegido con token que la app de un técnico en la LAN pueda invocar.

**Architecture:** Se parte `app.run()` en tres piezas testeables sin red: un `CameraSupervisor` que posee los hilos de cámara y converge hacia el estado deseado (cada worker con su propio `Event` de parada), un `RescanService` que encadena descubrimiento → reconciliación → persistencia → `apply`, y un `control.py` con un `ThreadingHTTPServer` de la stdlib. El timer periódico y el HTTP llaman al mismo `RescanService.run_once()`, serializado con un lock no bloqueante.

**Tech Stack:** Python 3.12, stdlib (`threading`, `http.server`, `hmac`, `json`), PyYAML. Pytest, ruff, mypy strict. **Cero dependencias nuevas.**

**Spec:** `docs/superpowers/specs/2026-08-17-rescan-camaras-design.md`

## Global Constraints

- **Cero dependencias nuevas.** Todo con la stdlib. No añadir nada a `pyproject.toml`.
- **mypy strict** (`[tool.mypy] strict = true`, `packages = ["vitahub"]`): todo el código de `src/` va tipado por completo, incluidos los retornos `-> None`.
- **ruff** con `line-length = 100`. El código de los tests de este plan está escrito para
  leerse; si alguna línea pasa de 100, pártela — no cambies el contenido del test.
- Todo módulo nuevo empieza con `from __future__ import annotations`.
- **Logs en español**, por `get_logger(<nombre>)` de `vitahub.logging_setup`. stdout es solo para eventos; los logs van a stderr.
- Ningún test puede depender de la LAN, de multicast ni de una cámara. El descubrimiento entra **inyectado** como callable.
- Los nombres de cámara editados a mano en el YAML se preservan: nunca sobrescribir `Camera.name`.
- Al terminar cada tarea, `pytest`, `ruff check src tests` y `mypy` deben quedar limpios.

## File Structure

| Fichero | Responsabilidad |
|---|---|
| `src/vitahub/supervisor.py` (nuevo) | Posee los hilos de cámara. Converge el conjunto de workers hacia `(cameras, uris)`. No sabe de descubrimiento ni de config. |
| `src/vitahub/rescan.py` (nuevo) | Orquesta un ciclo de rescan completo y devuelve un `RescanResult`. No sabe de HTTP. |
| `src/vitahub/control.py` (nuevo) | Servidor HTTP mínimo: autentica y traduce `RescanResult` a códigos y JSON. No sabe de descubrimiento. |
| `src/vitahub/app.py` (modificar) | Cableado: construye las piezas, rescan inicial, hilo periódico, heartbeat, apagado ordenado. |
| `src/vitahub/config.py` (modificar) | Añadir guardarraíl de `interval_seconds`. |
| `docker-compose.yml` (modificar) | Pasar `VITAHUB_ADMIN_TOKEN` y `VITAHUB_ADMIN_PORT`. |
| `tests/test_supervisor.py`, `tests/test_rescan.py`, `tests/test_control.py` (nuevos) | Uno por componente. |

---

### Task 1: `CameraSupervisor` — hilos de cámara dinámicos

**Files:**
- Create: `src/vitahub/supervisor.py`
- Test: `tests/test_supervisor.py`

**Interfaces:**
- Consumes: `vitahub.models.Camera` (campos `id`, `name`, `last_ip`, `enabled`).
- Produces:
  - `WorkerFn = Callable[[Camera, str, threading.Event], None]` — la firma que debe cumplir el bucle de cámara: recibe la cámara, su URI RTSP y **su propio** event de parada.
  - `SupervisorChange(started: list[str], restarted: list[str])`
  - `CameraSupervisor(worker_fn: WorkerFn, join_timeout: float = 15.0)` con `apply(cameras: list[Camera], uris: dict[str, str]) -> SupervisorChange` y `stop_all() -> None`.

- [ ] **Step 1: Escribir los tests que fallan**

Crear `tests/test_supervisor.py`:

```python
import threading
import time

from vitahub.models import Camera
from vitahub.supervisor import CameraSupervisor


def _cam(id_, enabled=True):
    return Camera(id=id_, name=f"n-{id_}", last_ip="10.0.0.1", enabled=enabled)


def _recording_worker(starts):
    """Worker falso: apunta la URI con la que arrancó y espera su señal de parada."""
    def worker(camera, uri, stop):
        starts.append((camera.id, uri))
        stop.wait()
    return worker


def _eventually(predicate, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def test_new_camera_is_started():
    starts = []
    sup = CameraSupervisor(_recording_worker(starts))
    change = sup.apply([_cam("a")], {"a": "rtsp://old"})
    assert change.started == ["a"]
    assert _eventually(lambda: starts == [("a", "rtsp://old")])
    sup.stop_all()


def test_apply_is_idempotent():
    starts = []
    sup = CameraSupervisor(_recording_worker(starts))
    sup.apply([_cam("a")], {"a": "rtsp://old"})
    change = sup.apply([_cam("a")], {"a": "rtsp://old"})
    assert change.started == []
    assert change.restarted == []
    sup.stop_all()


def test_changed_uri_restarts_worker():
    starts = []
    sup = CameraSupervisor(_recording_worker(starts), join_timeout=2.0)
    sup.apply([_cam("a")], {"a": "rtsp://old"})
    assert _eventually(lambda: len(starts) == 1)
    change = sup.apply([_cam("a")], {"a": "rtsp://new"})
    assert change.restarted == ["a"]
    assert _eventually(lambda: starts == [("a", "rtsp://old"), ("a", "rtsp://new")])
    sup.stop_all()


def test_absent_camera_is_left_alone():
    starts = []
    sup = CameraSupervisor(_recording_worker(starts))
    sup.apply([_cam("a")], {"a": "rtsp://old"})
    change = sup.apply([_cam("a")], {})  # no descubierta en este ciclo
    assert change.started == []
    assert change.restarted == []
    assert len(starts) == 1  # su worker sigue vivo, no se relanzó ni se paró
    sup.stop_all()


def test_camera_without_uri_is_not_started():
    starts = []
    sup = CameraSupervisor(_recording_worker(starts))
    change = sup.apply([_cam("a")], {})
    assert change.started == []
    assert starts == []


def test_disabled_camera_is_not_started():
    starts = []
    sup = CameraSupervisor(_recording_worker(starts))
    change = sup.apply([_cam("a", enabled=False)], {"a": "rtsp://old"})
    assert change.started == []
    assert starts == []


def test_disabled_camera_stops_running_worker():
    starts = []
    sup = CameraSupervisor(_recording_worker(starts), join_timeout=2.0)
    sup.apply([_cam("a")], {"a": "rtsp://old"})
    assert _eventually(lambda: len(starts) == 1)
    sup.apply([_cam("a", enabled=False)], {"a": "rtsp://old"})
    change = sup.apply([_cam("a", enabled=False)], {"a": "rtsp://old"})
    assert change.started == []  # sigue parada


def test_stop_all_stops_every_worker():
    alive = []

    def worker(camera, uri, stop):
        alive.append(camera.id)
        stop.wait()
        alive.remove(camera.id)

    sup = CameraSupervisor(worker, join_timeout=2.0)
    sup.apply([_cam("a"), _cam("b")], {"a": "rtsp://a", "b": "rtsp://b"})
    assert _eventually(lambda: len(alive) == 2)
    sup.stop_all()
    assert alive == []


def test_replacement_waits_for_previous_worker_to_die():
    """El caso delicado: dos workers de la misma cámara a la vez duplicarían eventos."""
    starts = []
    release = threading.Event()

    def stubborn_worker(camera, uri, stop):
        starts.append(uri)
        stop.wait()
        release.wait(timeout=5.0)  # tarda en morir DESPUÉS de recibir la señal

    sup = CameraSupervisor(stubborn_worker, join_timeout=0.2)
    sup.apply([_cam("a")], {"a": "rtsp://old"})
    assert _eventually(lambda: starts == ["rtsp://old"])

    change = sup.apply([_cam("a")], {"a": "rtsp://new"})
    assert change.restarted == []          # no se reporta el reinicio
    assert starts == ["rtsp://old"]        # y sobre todo: el nuevo NO arrancó

    release.set()                          # ahora el viejo muere de verdad

    # Se reintenta el apply hasta que el reemplazo arranca. No se asierta sobre
    # `started` vs `restarted`: cuál de los dos sea depende de si el hilo viejo
    # ya había muerto al entrar, y eso es una carrera. Lo que importa —y es
    # determinista— es que acabe habiendo exactamente un worker con la URI nueva.
    def _replacement_started():
        sup.apply([_cam("a")], {"a": "rtsp://new"})
        return starts == ["rtsp://old", "rtsp://new"]

    assert _eventually(_replacement_started)
    sup.stop_all()
```

- [ ] **Step 2: Correr los tests para verificar que fallan**

Run: `pytest tests/test_supervisor.py -v`
Expected: FAIL con `ModuleNotFoundError: No module named 'vitahub.supervisor'`

- [ ] **Step 3: Implementar el supervisor**

Crear `src/vitahub/supervisor.py`:

```python
from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass, field

from vitahub.logging_setup import get_logger
from vitahub.models import Camera

_log = get_logger("supervisor")

# Firma del bucle de cámara: recibe su cámara, su URI y su PROPIO event de parada.
WorkerFn = Callable[[Camera, str, threading.Event], None]


@dataclass
class SupervisorChange:
    started: list[str] = field(default_factory=list)
    restarted: list[str] = field(default_factory=list)


@dataclass
class _WorkerHandle:
    thread: threading.Thread
    stop: threading.Event
    uri: str


class CameraSupervisor:
    """Dueño de los hilos de cámara. `apply` converge hacia el estado deseado.

    Es idempotente: llamarlo dos veces con la misma entrada no hace nada. Eso
    permite usar el mismo camino para el rescan inicial y para los periódicos.
    """

    def __init__(self, worker_fn: WorkerFn, join_timeout: float = 15.0) -> None:
        self._worker_fn = worker_fn
        self._join_timeout = join_timeout
        self._workers: dict[str, _WorkerHandle] = {}
        self._lock = threading.Lock()

    def apply(self, cameras: list[Camera], uris: dict[str, str]) -> SupervisorChange:
        change = SupervisorChange()
        with self._lock:
            for camera in cameras:
                self._apply_one(camera, uris.get(camera.id), change)
        return change

    def _apply_one(
        self, camera: Camera, uri: str | None, change: SupervisorChange
    ) -> None:
        handle = self._workers.get(camera.id)
        # Un worker que murió por su cuenta se olvida, para poder relanzarlo.
        if handle is not None and not handle.thread.is_alive():
            del self._workers[camera.id]
            handle = None

        if not camera.enabled:
            if handle is not None:
                self._stop(camera.id, handle)
            return

        # Sin URI (conocida pero no descubierta en este ciclo) no se toca nada:
        # un probe ONVIF perdido no debe apagar una cámara que funciona.
        if not uri:
            return

        if handle is None:
            self._start(camera, uri)
            change.started.append(camera.id)
            return

        if handle.uri != uri:
            # Nunca se arranca el reemplazo antes de confirmar que el anterior
            # murió: dos workers de la misma cámara duplicarían eventos.
            if not self._stop(camera.id, handle):
                _log.warning(
                    "cam %s: el worker anterior no termina, reintento en el próximo ciclo",
                    camera.id,
                )
                return
            self._start(camera, uri)
            change.restarted.append(camera.id)

    def _start(self, camera: Camera, uri: str) -> None:
        stop = threading.Event()
        thread = threading.Thread(
            target=self._worker_fn,
            args=(camera, uri, stop),
            name=f"cam-{camera.id}",
            daemon=True,
        )
        self._workers[camera.id] = _WorkerHandle(thread=thread, stop=stop, uri=uri)
        thread.start()

    def _stop(self, camera_id: str, handle: _WorkerHandle) -> bool:
        """Señala y espera. Devuelve False si el hilo sigue vivo tras el timeout."""
        handle.stop.set()
        handle.thread.join(timeout=self._join_timeout)
        if handle.thread.is_alive():
            return False
        del self._workers[camera_id]
        return True

    def stop_all(self) -> None:
        with self._lock:
            for handle in self._workers.values():
                handle.stop.set()
            for camera_id, handle in self._workers.items():
                handle.thread.join(timeout=self._join_timeout)
                if handle.thread.is_alive():
                    _log.warning(
                        "cam %s no terminó en %.0fs", camera_id, self._join_timeout
                    )
            self._workers.clear()
```

- [ ] **Step 4: Correr los tests y verificar que pasan**

Run: `pytest tests/test_supervisor.py -v`
Expected: PASS (9 tests)

- [ ] **Step 5: Puertas de calidad**

Run: `ruff check src tests && mypy`
Expected: sin errores

- [ ] **Step 6: Commit**

```bash
git add src/vitahub/supervisor.py tests/test_supervisor.py
git commit -m "feat: supervisor de cámaras con arranque y reemplazo en caliente"
```

---

### Task 2: `RescanService` — un ciclo de rescan completo

**Files:**
- Create: `src/vitahub/rescan.py`
- Test: `tests/test_rescan.py`

**Interfaces:**
- Consumes: `CameraSupervisor.apply(cameras, uris) -> SupervisorChange` (Task 1); `vitahub.registry.reconcile`, `vitahub.registry.DiscoveredCamera`, `vitahub.config.{HubConfig, Credentials, save_cameras}`, `vitahub.ingest.rtsp.with_credentials`.
- Produces:
  - `DiscoverFn = Callable[[Credentials], list[DiscoveredCamera]]`
  - `RescanResult(status: str, found: int, added: list[dict[str, str]], ip_changed: list[str], started: list[str], cameras: int)` — `status` es `"ok"`, `"busy"` o `"error"`.
  - `RescanService(cfg: HubConfig, config_path: Path, supervisor: CameraSupervisor, discover_fn: DiscoverFn)` con `run_once() -> RescanResult`.

- [ ] **Step 1: Escribir los tests que fallan**

Crear `tests/test_rescan.py`:

```python
import threading
from pathlib import Path

from vitahub.config import (
    Credentials,
    DiscoveryConfig,
    HubConfig,
    InferenceConfig,
    load_config,
)
from vitahub.models import Camera
from vitahub.registry import DiscoveredCamera
from vitahub.rescan import RescanService


class _FakeSupervisor:
    def __init__(self):
        self.calls = []

    def apply(self, cameras, uris):
        self.calls.append((list(cameras), dict(uris)))
        from vitahub.supervisor import SupervisorChange

        return SupervisorChange(started=[c.id for c in cameras if uris.get(c.id)])

    def stop_all(self):
        pass


def _cfg(cameras=None):
    return HubConfig(
        hub_id="hub-test",
        discovery=DiscoveryConfig(interval_seconds=60),
        inference=InferenceConfig(detector="stub", stream="substream"),
        cameras=cameras if cameras is not None else [],
        credentials=Credentials(onvif_user="admin", onvif_password="clave"),
    )


def _config_file(tmp_path: Path) -> Path:
    path = tmp_path / "hub.yaml"
    path.write_text("hub_id: hub-test\ncameras: []\n")
    return path


def _disc(id_, ip):
    return DiscoveredCamera(
        id=id_,
        ip=ip,
        rtsp_main=f"rtsp://{ip}:554/Streaming/Channels/1",
        rtsp_sub=f"rtsp://{ip}:554/Streaming/Channels/2",
    )


def test_new_camera_is_added_and_persisted(tmp_path):
    path = _config_file(tmp_path)
    cfg = _cfg()
    sup = _FakeSupervisor()
    service = RescanService(cfg, path, sup, lambda creds: [_disc("onvif-a", "10.0.0.5")])

    result = service.run_once()

    assert result.status == "ok"
    assert result.found == 1
    assert result.added == [{"id": "onvif-a", "name": "camera-1", "ip": "10.0.0.5"}]
    assert result.started == ["onvif-a"]
    assert result.cameras == 1
    # persistido en el YAML
    reloaded = load_config(path, {"VITAHUB_ONVIF_USER": "u", "VITAHUB_ONVIF_PASSWORD": "p"})
    assert [c.id for c in reloaded.cameras] == ["onvif-a"]


def test_credentials_are_injected_in_the_uri(tmp_path):
    path = _config_file(tmp_path)
    sup = _FakeSupervisor()
    service = RescanService(_cfg(), path, sup, lambda creds: [_disc("onvif-a", "10.0.0.5")])

    service.run_once()

    _, uris = sup.calls[0]
    assert uris["onvif-a"] == "rtsp://admin:clave@10.0.0.5:554/Streaming/Channels/2"


def test_main_stream_is_used_when_configured(tmp_path):
    path = _config_file(tmp_path)
    cfg = _cfg()
    cfg.inference.stream = "main"
    sup = _FakeSupervisor()
    service = RescanService(cfg, path, sup, lambda creds: [_disc("onvif-a", "10.0.0.5")])

    service.run_once()

    _, uris = sup.calls[0]
    assert uris["onvif-a"].endswith("/Streaming/Channels/1")


def test_no_changes_does_not_write_to_disk(tmp_path, monkeypatch):
    """Blinda que un rescan cada 60s no escriba el YAML sin motivo."""
    path = _config_file(tmp_path)
    cfg = _cfg(cameras=[Camera(id="onvif-a", name="salon", last_ip="10.0.0.5")])
    saves = []
    monkeypatch.setattr(
        "vitahub.rescan.save_cameras", lambda p, c: saves.append(p)
    )
    service = RescanService(cfg, path, _FakeSupervisor(), lambda creds: [_disc("onvif-a", "10.0.0.5")])

    result = service.run_once()

    assert result.status == "ok"
    assert saves == []


def test_ip_change_is_reported_and_persisted(tmp_path):
    path = _config_file(tmp_path)
    cfg = _cfg(cameras=[Camera(id="onvif-a", name="salon", last_ip="10.0.0.5")])
    service = RescanService(cfg, path, _FakeSupervisor(), lambda creds: [_disc("onvif-a", "10.0.0.9")])

    result = service.run_once()

    assert result.ip_changed == ["onvif-a"]
    assert result.added == []
    reloaded = load_config(path, {"VITAHUB_ONVIF_USER": "u", "VITAHUB_ONVIF_PASSWORD": "p"})
    assert reloaded.cameras[0].last_ip == "10.0.0.9"


def test_discovery_failure_returns_error_without_raising(tmp_path):
    path = _config_file(tmp_path)

    def _boom(creds):
        raise OSError("la red se cayó")

    service = RescanService(_cfg(), path, _FakeSupervisor(), _boom)

    result = service.run_once()

    assert result.status == "error"
    assert result.found == 0


def test_save_failure_does_not_block_workers(tmp_path, monkeypatch):
    """Mejor las cámaras emitiendo aunque el registro no haya persistido."""
    path = _config_file(tmp_path)

    def _boom(p, c):
        raise OSError("disco lleno")

    monkeypatch.setattr("vitahub.rescan.save_cameras", _boom)
    sup = _FakeSupervisor()
    service = RescanService(_cfg(), path, sup, lambda creds: [_disc("onvif-a", "10.0.0.5")])

    result = service.run_once()

    assert result.status == "ok"
    assert result.started == ["onvif-a"]
    assert len(sup.calls) == 1


def test_concurrent_rescan_returns_busy(tmp_path):
    path = _config_file(tmp_path)
    entered = threading.Event()
    release = threading.Event()
    second: list[str] = []

    def _slow_discover(creds):
        entered.set()
        release.wait(timeout=5.0)
        return []

    service = RescanService(_cfg(), path, _FakeSupervisor(), _slow_discover)
    worker = threading.Thread(target=service.run_once, daemon=True)
    worker.start()
    assert entered.wait(timeout=5.0)

    second.append(service.run_once().status)

    release.set()
    worker.join(timeout=5.0)
    assert second == ["busy"]


def test_unseen_camera_is_kept_and_not_started(tmp_path):
    path = _config_file(tmp_path)
    cfg = _cfg(cameras=[Camera(id="onvif-a", name="salon", last_ip="10.0.0.5")])
    sup = _FakeSupervisor()
    service = RescanService(cfg, path, sup, lambda creds: [])

    result = service.run_once()

    assert result.cameras == 1  # sigue registrada
    assert result.started == []
    cameras, uris = sup.calls[0]
    assert [c.id for c in cameras] == ["onvif-a"]
    assert uris == {}  # sin URI: el supervisor no la tocará
```

- [ ] **Step 2: Correr los tests para verificar que fallan**

Run: `pytest tests/test_rescan.py -v`
Expected: FAIL con `ModuleNotFoundError: No module named 'vitahub.rescan'`

- [ ] **Step 3: Implementar el servicio**

Crear `src/vitahub/rescan.py`:

```python
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
```

- [ ] **Step 4: Correr los tests y verificar que pasan**

Run: `pytest tests/test_rescan.py -v`
Expected: PASS (9 tests)

- [ ] **Step 5: Puertas de calidad**

Run: `ruff check src tests && mypy`
Expected: sin errores

- [ ] **Step 6: Commit**

```bash
git add src/vitahub/rescan.py tests/test_rescan.py
git commit -m "feat: RescanService (descubrir, reconciliar, persistir, converger)"
```

---

### Task 3: `control.py` — endpoint `POST /rescan` con token

**Files:**
- Create: `src/vitahub/control.py`
- Test: `tests/test_control.py`

**Interfaces:**
- Consumes: `RescanService.run_once() -> RescanResult` (Task 2) — el servidor solo necesita un objeto con ese método, así que en tests entra un doble.
- Produces:
  - `admin_port(env: Mapping[str, str]) -> int` — lee `VITAHUB_ADMIN_PORT`, cae a `8787` si falta o no es válido.
  - `start_control_server(service, token: str, port: int, host: str = "0.0.0.0") -> ThreadingHTTPServer | None` — devuelve `None` (y no abre puerto) si `token` está vacío.

- [ ] **Step 1: Escribir los tests que fallan**

Crear `tests/test_control.py`:

```python
import json
import urllib.error
import urllib.request

import pytest

from vitahub.control import admin_port, start_control_server
from vitahub.rescan import RescanResult


class _FakeService:
    def __init__(self, result):
        self.result = result
        self.calls = 0

    def run_once(self):
        self.calls += 1
        return self.result


def _ok_result():
    return RescanResult(
        status="ok",
        found=2,
        added=[{"id": "onvif-a", "name": "camera-2", "ip": "10.0.0.9"}],
        ip_changed=[],
        started=["onvif-a"],
        cameras=2,
    )


@pytest.fixture
def server_factory():
    servers = []

    def _start(service, token="secreto"):
        httpd = start_control_server(service, token, port=0, host="127.0.0.1")
        assert httpd is not None
        servers.append(httpd)
        return f"http://127.0.0.1:{httpd.server_address[1]}"

    yield _start
    for httpd in servers:
        httpd.shutdown()
        httpd.server_close()


def _post(url, token=None):
    request = urllib.request.Request(url, method="POST", data=b"")
    if token is not None:
        request.add_header("Authorization", f"Bearer {token}")
    return urllib.request.urlopen(request, timeout=5)


def test_valid_token_runs_rescan_and_returns_json(server_factory):
    service = _FakeService(_ok_result())
    base = server_factory(service)

    with _post(f"{base}/rescan", token="secreto") as response:
        assert response.status == 200
        body = json.loads(response.read())

    assert service.calls == 1
    assert body == {
        "found": 2,
        "added": [{"id": "onvif-a", "name": "camera-2", "ip": "10.0.0.9"}],
        "ip_changed": [],
        "started": ["onvif-a"],
        "cameras": 2,
    }


def test_wrong_token_is_rejected(server_factory):
    service = _FakeService(_ok_result())
    base = server_factory(service)

    with pytest.raises(urllib.error.HTTPError) as exc:
        _post(f"{base}/rescan", token="incorrecto")

    assert exc.value.code == 401
    assert service.calls == 0  # no llegó a escanear


def test_missing_token_is_rejected(server_factory):
    service = _FakeService(_ok_result())
    base = server_factory(service)

    with pytest.raises(urllib.error.HTTPError) as exc:
        _post(f"{base}/rescan")

    assert exc.value.code == 401
    assert service.calls == 0


def test_unknown_path_is_404(server_factory):
    base = server_factory(_FakeService(_ok_result()))

    with pytest.raises(urllib.error.HTTPError) as exc:
        _post(f"{base}/otra-cosa", token="secreto")

    assert exc.value.code == 404


def test_get_is_404(server_factory):
    base = server_factory(_FakeService(_ok_result()))

    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(f"{base}/rescan", timeout=5)

    assert exc.value.code == 404


def test_busy_returns_409(server_factory):
    base = server_factory(_FakeService(RescanResult(status="busy")))

    with pytest.raises(urllib.error.HTTPError) as exc:
        _post(f"{base}/rescan", token="secreto")

    assert exc.value.code == 409


def test_discovery_error_returns_500(server_factory):
    base = server_factory(_FakeService(RescanResult(status="error")))

    with pytest.raises(urllib.error.HTTPError) as exc:
        _post(f"{base}/rescan", token="secreto")

    assert exc.value.code == 500


def test_no_token_configured_does_not_open_a_port():
    assert start_control_server(_FakeService(_ok_result()), "", port=0) is None


def test_token_is_registered_for_log_redaction(monkeypatch, server_factory):
    registered = []
    monkeypatch.setattr("vitahub.control.register_secret", registered.append)
    server_factory(_FakeService(_ok_result()), token="secreto")
    assert registered == ["secreto"]


@pytest.mark.parametrize(
    "env,expected",
    [
        ({}, 8787),
        ({"VITAHUB_ADMIN_PORT": "9000"}, 9000),
        ({"VITAHUB_ADMIN_PORT": "no-es-un-puerto"}, 8787),
        ({"VITAHUB_ADMIN_PORT": "0"}, 8787),
        ({"VITAHUB_ADMIN_PORT": "70000"}, 8787),
    ],
)
def test_admin_port_parsing(env, expected):
    assert admin_port(env) == expected
```

- [ ] **Step 2: Correr los tests para verificar que fallan**

Run: `pytest tests/test_control.py -v`
Expected: FAIL con `ModuleNotFoundError: No module named 'vitahub.control'`

- [ ] **Step 3: Implementar el servidor**

Crear `src/vitahub/control.py`:

```python
from __future__ import annotations

import hmac
import json
import threading
from collections.abc import Mapping
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Protocol

from vitahub.logging_setup import get_logger, register_secret
from vitahub.rescan import RescanResult

_log = get_logger("control")

_DEFAULT_PORT = 8787


class _Rescannable(Protocol):
    def run_once(self) -> RescanResult: ...


def admin_port(env: Mapping[str, str]) -> int:
    raw = env.get("VITAHUB_ADMIN_PORT")
    if not raw:
        return _DEFAULT_PORT
    try:
        port = int(raw)
    except ValueError:
        _log.warning("VITAHUB_ADMIN_PORT no es numérico (%s), uso %d", raw, _DEFAULT_PORT)
        return _DEFAULT_PORT
    if not 1 <= port <= 65535:
        _log.warning("VITAHUB_ADMIN_PORT fuera de rango (%d), uso %d", port, _DEFAULT_PORT)
        return _DEFAULT_PORT
    return port


def _build_handler(service: _Rescannable, token: str) -> type[BaseHTTPRequestHandler]:
    class _Handler(BaseHTTPRequestHandler):
        # Sin body ni parámetros: no hay nada que parsear, luego no hay
        # superficie de inyección.
        def do_POST(self) -> None:  # noqa: N802 — nombre impuesto por BaseHTTPRequestHandler
            if self.path != "/rescan":
                self._respond(404)
                return
            if not self._authorized():
                _log.warning("control: petición rechazada desde %s", self.client_address[0])
                self._respond(401)
                return
            result = service.run_once()
            if result.status == "busy":
                self._respond(409)
                return
            if result.status == "error":
                self._respond(500)
                return
            self._respond(
                200,
                {
                    "found": result.found,
                    "added": result.added,
                    "ip_changed": result.ip_changed,
                    "started": result.started,
                    "cameras": result.cameras,
                },
            )

        def do_GET(self) -> None:  # noqa: N802 — nombre impuesto por BaseHTTPRequestHandler
            self._respond(404)

        def _authorized(self) -> bool:
            header = self.headers.get("Authorization", "")
            prefix = "Bearer "
            if not header.startswith(prefix):
                return False
            # compare_digest evita filtrar el token por tiempo de comparación.
            return hmac.compare_digest(header[len(prefix) :], token)

        def _respond(self, code: int, payload: dict[str, object] | None = None) -> None:
            body = b"" if payload is None else json.dumps(payload, ensure_ascii=False).encode()
            self.send_response(code)
            if payload is not None:
                self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if body:
                self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            # Por defecto BaseHTTPRequestHandler escribe a stderr crudo; se
            # redirige al logger JSON para no ensuciar el formato.
            _log.info("control %s", format % args)

    return _Handler


def start_control_server(
    service: _Rescannable, token: str, port: int, host: str = "0.0.0.0"
) -> ThreadingHTTPServer | None:
    """Arranca el servidor de control. Sin token no se abre ningún puerto."""
    if not token:
        _log.warning("control HTTP deshabilitado: define VITAHUB_ADMIN_TOKEN")
        return None
    register_secret(token)
    httpd = ThreadingHTTPServer((host, port), _build_handler(service, token))
    threading.Thread(target=httpd.serve_forever, name="control-http", daemon=True).start()
    _log.info("control HTTP escuchando en %s:%d", host, httpd.server_address[1])
    return httpd
```

- [ ] **Step 4: Correr los tests y verificar que pasan**

Run: `pytest tests/test_control.py -v`
Expected: PASS (14 tests, contando los 5 parametrizados)

> Si mypy se queja del override de `log_message` (la firma de la clase base usa
> `*args: Any`), copiar la firma de la base en vez de pelearse con ella:
> `def log_message(self, format: str, *args: Any) -> None:` con
> `from typing import Any`. Es el único punto del módulo donde la stdlib impone la firma.

- [ ] **Step 5: Puertas de calidad**

Run: `ruff check src tests && mypy`
Expected: sin errores

- [ ] **Step 6: Commit**

```bash
git add src/vitahub/control.py tests/test_control.py
git commit -m "feat: endpoint POST /rescan protegido por token"
```

---

### Task 4: Cablear en `app.py`, guardarraíl de config y compose

**Files:**
- Modify: `src/vitahub/app.py:30-89` (`run`) y `src/vitahub/app.py:91` (firma de `_camera_loop`)
- Modify: `src/vitahub/config.py:96-101` (validación de `interval_seconds`)
- Modify: `docker-compose.yml:7-10`
- Test: `tests/test_app_loop.py` (nuevo), `tests/test_config.py` (añadir un caso)

**Interfaces:**
- Consumes: `CameraSupervisor` (Task 1), `RescanService`/`RescanResult` (Task 2), `start_control_server`/`admin_port` (Task 3), `vitahub.discovery.onvif.discover(creds, timeout=3.0)`.
- Produces: `_rescan_loop(service, interval_seconds: float, stop: threading.Event) -> None`.

- [ ] **Step 1: Escribir los tests que fallan**

Crear `tests/test_app_loop.py`:

```python
import threading

from vitahub.app import _rescan_loop
from vitahub.rescan import RescanResult


class _CountingService:
    def __init__(self, stop):
        self.calls = 0
        self._stop = stop

    def run_once(self):
        self.calls += 1
        self._stop.set()  # un solo ciclo y salimos
        return RescanResult(status="ok")


def test_rescan_loop_runs_until_stopped():
    stop = threading.Event()
    service = _CountingService(stop)
    _rescan_loop(service, 0.01, stop)
    assert service.calls == 1


def test_rescan_loop_exits_immediately_when_already_stopped():
    """Espera sobre el event, no con sleep: si durmiera, este test colgaría."""
    stop = threading.Event()
    stop.set()
    service = _CountingService(stop)
    _rescan_loop(service, 3600.0, stop)
    assert service.calls == 0
```

Añadir a `tests/test_config.py`:

```python
def test_non_positive_interval_is_rejected(tmp_path):
    path = tmp_path / "hub.yaml"
    path.write_text("hub_id: hub-x\ndiscovery:\n  interval_seconds: 0\n")
    with pytest.raises(ConfigError, match="interval_seconds"):
        load_config(path, {"VITAHUB_ONVIF_USER": "u", "VITAHUB_ONVIF_PASSWORD": "p"})
```

(Si `pytest`, `ConfigError` o `load_config` no estuvieran ya importados en ese fichero, añadir los imports.)

- [ ] **Step 2: Correr los tests para verificar que fallan**

Run: `pytest tests/test_app_loop.py tests/test_config.py -v`
Expected: FAIL — `ImportError: cannot import name '_rescan_loop'` y el caso de config sin lanzar `ConfigError`

- [ ] **Step 3: Añadir el guardarraíl en `config.py`**

En `src/vitahub/config.py`, justo después del bloque `try/except` que calcula `interval_seconds`:

```python
    if interval_seconds <= 0:
        raise ConfigError("Campo 'discovery.interval_seconds' debe ser mayor que 0")
```

- [ ] **Step 4: Reescribir el cableado de `app.py`**

Sustituir los imports y la función `run` de `src/vitahub/app.py` (el cuerpo de `_camera_loop`, `_touch_heartbeat` y `main` **no cambian**; de `_camera_loop` solo cambia el significado del parámetro `stop`, que pasa a ser el event propio de esa cámara):

```python
from vitahub.analytics.event_engine import EventEngine
from vitahub.config import ConfigError, load_config
from vitahub.control import admin_port, start_control_server
from vitahub.discovery.onvif import discover
from vitahub.factory import build_detector
from vitahub.ingest.rtsp import (
    backoff_delay,
    is_stalled,
    open_capture,
    should_sample,
)
from vitahub.logging_setup import configure_logging, get_logger, register_secret
from vitahub.models import Camera
from vitahub.rescan import RescanService
from vitahub.sinks.stdout_json import StdoutJsonSink
from vitahub.supervisor import CameraSupervisor
from vitahub.worker import process_frame


def run(config_path: Path, weights_path: str, env: dict[str, str]) -> None:
    cfg = load_config(config_path, env)
    register_secret(cfg.credentials.onvif_password)
    _log.info("hub %s arrancando", cfg.hub_id)

    # Los manejadores de señal van lo primero, antes de descubrimiento y carga
    # del detector, para que una señal recibida durante el arranque se atienda.
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())

    detector = build_detector(cfg.inference, weights_path)
    engine = EventEngine(cfg.hub_id)
    sink = StdoutJsonSink()

    def worker(camera: Camera, rtsp_url: str, cam_stop: threading.Event) -> None:
        _camera_loop(camera, rtsp_url, detector, engine, sink, cfg, cam_stop)

    supervisor = CameraSupervisor(worker)
    service = RescanService(cfg, config_path, supervisor, discover)

    # El rescan inicial usa exactamente el mismo camino que los periódicos.
    result = service.run_once()
    if result.status == "ok" and result.found == 0:
        _log.warning("descubrimiento: 0 cámaras encontradas — revisa ONVIF/credencial/red")

    httpd = start_control_server(
        service, env.get("VITAHUB_ADMIN_TOKEN", ""), admin_port(env)
    )

    # En su propio hilo: si el rescan compartiera hilo con el heartbeat, un
    # discover() lento dejaría de latir y Docker reiniciaría un hub sano.
    threading.Thread(
        target=_rescan_loop,
        args=(service, cfg.discovery.interval_seconds, stop),
        name="rescan",
        daemon=True,
    ).start()

    while not stop.wait(timeout=5.0):
        _touch_heartbeat()

    _log.info("apagando (SIGTERM)")
    supervisor.stop_all()
    if httpd is not None:
        httpd.shutdown()
    sink.close()


def _rescan_loop(
    service: RescanService, interval_seconds: float, stop: threading.Event
) -> None:
    """Espera sobre el event de parada (no duerme): el apagado es inmediato."""
    while not stop.wait(timeout=interval_seconds):
        service.run_once()
```

> Va tipada por completo (mypy solo comprueba `src/`, así que el doble de los
> tests sigue valiendo por duck typing). `_camera_loop` mantiene su
> `# type: ignore[no-untyped-def]` porque maneja fotogramas de cv2, que no
> tienen stubs; aquí no hay nada que lo justifique.

Notas para quien implemente:
- `with_credentials` deja de usarse en `app.py` (ahora vive en `RescanService`): quitarlo del import o ruff marcará `F401`.
- `_camera_loop` conserva su firma actual `(camera, rtsp_url, detector, engine, sink, cfg, stop)`; lo único que cambia es que el `stop` que recibe ya no es el global sino el suyo. No hace falta tocar su cuerpo.
- El bloque que construía `uris` y la lista de `threads` en `run()` desaparece: ese trabajo es ahora del `RescanService` y del supervisor.

- [ ] **Step 5: Correr toda la suite**

Run: `pytest -v`
Expected: PASS — los 56 tests previos siguen verdes más los nuevos

- [ ] **Step 6: Puertas de calidad**

Run: `ruff check src tests && mypy`
Expected: sin errores

- [ ] **Step 7: Pasar el token por compose**

En `docker-compose.yml`, dentro de `environment:`, añadir tras `VITAHUB_LOG_LEVEL`:

```yaml
      # Sin token, el endpoint de control no abre puerto (queda solo el rescan periódico).
      VITAHUB_ADMIN_TOKEN: ${VITAHUB_ADMIN_TOKEN:-}
      VITAHUB_ADMIN_PORT: ${VITAHUB_ADMIN_PORT:-8787}
```

- [ ] **Step 8: Arranque en seco de verificación**

```bash
VITAHUB_CONFIG=./config/hub.example.yaml VITAHUB_ONVIF_USER=admin \
VITAHUB_ONVIF_PASSWORD=test VITAHUB_ADMIN_TOKEN=pruebas \
python -m vitahub.app
```

Expected en stderr: `hub ... arrancando`, el aviso de 0 cámaras si no hay ninguna en la LAN, y `control HTTP escuchando en 0.0.0.0:8787`. En otra terminal, `curl -s -o /dev/null -w '%{http_code}' -X POST -H 'Authorization: Bearer pruebas' http://127.0.0.1:8787/rescan` debe devolver `200`, y sin la cabecera, `401`. `Ctrl-C` debe salir limpio y sin colgarse.

> Si `config/hub.example.yaml` tiene `detector: person_yolo`, exportar también `VITAHUB_WEIGHTS=yolo11n.pt` o poner `detector: stub` para no cargar YOLO.

- [ ] **Step 9: Commit**

```bash
git add src/vitahub/app.py src/vitahub/config.py docker-compose.yml \
        tests/test_app_loop.py tests/test_config.py
git commit -m "feat: rescan periódico y control HTTP cableados en app"
```

---

### Task 5: Documentación

**Files:**
- Modify: `README.md:33-49`
- Modify: `docs/como-probar.md` (tabla de diagnóstico, sección nueva, checklist)
- Modify: `docs/como-funciona.md` (tabla de módulos, flujo, config, limitaciones)
- Modify: `docs/backlog.md:7-22`

**Interfaces:**
- Consumes: el comportamiento final de las tareas 1-4. Ninguna dependencia de código.

- [ ] **Step 1: Corregir el aviso obsoleto del README**

En `README.md`, sustituir el punto 5 de "Instalación en campo" (que dice que una cámara nueva exige `docker compose restart`) por:

```markdown
5. **Cámaras nuevas**: el hub re-descubre la LAN cada `discovery.interval_seconds`
   (60 s por defecto), así que una cámara añadida después se da de alta sola. Para
   no esperar, define `VITAHUB_ADMIN_TOKEN` (genera uno con `openssl rand -hex 32`)
   y dispara el escaneo desde la misma red:

   ```bash
   curl -X POST -H "Authorization: Bearer $VITAHUB_ADMIN_TOKEN" \
     http://<ip-del-jetson>:8787/rescan
   ```

   Responde con las cámaras encontradas y las dadas de alta. Sin ese token el
   endpoint no escucha en ningún puerto y solo funciona el escaneo periódico.
```

- [ ] **Step 2: Actualizar `docs/como-probar.md`**

1. En la tabla de diagnóstico, sustituir la fila `Una cámara nueva no aparece` por:

```markdown
| Una cámara nueva no aparece | Espera a `discovery.interval_seconds` (60 s) o dispara `POST /rescan`. Si sigue sin salir, el problema es de descubrimiento (ONVIF/red), no de registro. |
| `POST /rescan` da 401 | El token de la cabecera no coincide con `VITAHUB_ADMIN_TOKEN`. |
| `POST /rescan` no conecta | El hub arrancó sin `VITAHUB_ADMIN_TOKEN` (mira el log `control HTTP deshabilitado`), o el puerto 8787 está ocupado por otro servicio del Jetson. |
| `POST /rescan` da 409 | Ya hay un escaneo en curso; reintenta en unos segundos. |
```

2. Añadir una sección tras "3.3 Con Docker":

```markdown
### 3.4 Forzar un escaneo de cámaras

El hub re-descubre solo cada `discovery.interval_seconds`. Para dar de alta una cámara
recién conectada sin esperar:

```bash
export VITAHUB_ADMIN_TOKEN=$(openssl rand -hex 32)   # antes de arrancar el hub
curl -X POST -H "Authorization: Bearer $VITAHUB_ADMIN_TOKEN" \
  http://127.0.0.1:8787/rescan
```

Respuesta esperada:

```json
{"found":2,"added":[{"id":"onvif-b","name":"camera-2","ip":"192.168.1.191"}],
 "ip_changed":[],"started":["onvif-b"],"cameras":2}
```

`found` son las cámaras vistas en ese escaneo, `added` las nuevas en el registro y
`started` aquellas cuyo hilo se arrancó o relanzó. Códigos: `401` token incorrecto,
`409` escaneo ya en curso, `500` fallo del descubrimiento (el detalle va al log).
```

3. Actualizar el recuento de tests (`54` → el número real tras `pytest -q`) en el paso 1 y en el checklist final.

4. Añadir al checklist final:

```markdown
- [ ] Conectar una cámara con el hub ya corriendo: aparece sola en ≤60 s (o al instante con `POST /rescan`), sin reiniciar el contenedor.
```

- [ ] **Step 3: Actualizar `docs/como-funciona.md`**

1. En la tabla de módulos, añadir tres filas tras `app.py`:

```markdown
| `supervisor.py` | Posee los hilos de cámara: arranca, relanza y para workers en caliente. |
| `rescan.py` | Un ciclo de rescan: descubrir → reconciliar → persistir → converger workers. |
| `control.py` | Servidor HTTP local: `POST /rescan` autenticado con token. |
```

2. Sustituir la nota "el descubrimiento corre **solo al arranque**" del paso 2 por:

```markdown
> **Nota:** este ciclo (descubrir → reconciliar → persistir → converger workers) es el
> mismo al arrancar, cada `discovery.interval_seconds` y cuando llega un `POST /rescan`.
> El registro solo se reescribe si hubo cambios. Una cámara que deja de verse **no** se
> para: un probe multicast perdido no debe apagar una cámara que funciona.
```

3. En el paso 5 ("Salud y apagado"), añadir:

```markdown
- El rescan periódico corre en **su propio hilo**: así el latido nunca depende de lo que
  tarde un descubrimiento, y un `discover()` lento no provoca un reinicio en falso.
```

4. En el bloque YAML de configuración, cambiar el comentario de `interval_seconds` a
   `# cadencia del redescubrimiento (debe ser > 0)`, y añadir a la tabla de variables:

```markdown
| `VITAHUB_ADMIN_TOKEN` | — (vacío = deshabilitado) | Token de `POST /rescan`. Sin él no se abre puerto. |
| `VITAHUB_ADMIN_PORT` | `8787` | Puerto del endpoint de control. |
```

5. En "Limitaciones conocidas", borrar el bullet de "Descubrimiento solo al arranque" y poner en su lugar:

```markdown
- **Sin fallback por `last_ip`**: una cámara conocida solo se reconecta cuando el
  descubrimiento vuelve a verla (≤60 s). No se construye una URL RTSP a partir de la IP
  guardada, porque la ruta del stream varía según el fabricante.
- **Sin comando remoto**: el `POST /rescan` solo es alcanzable desde la LAN del hogar. El
  disparo desde la nube llegará con el uplink (ver [backlog.md](backlog.md)).
```

- [ ] **Step 4: Actualizar `docs/backlog.md`**

Sustituir el ítem "### 1. Re-descubrimiento periódico + fallback por `last_ip`" completo por:

```markdown
### 1. ~~Re-descubrimiento periódico~~ — HECHO (slice de 2026-08-17)

El hub re-descubre cada `discovery.interval_seconds` y admite un `POST /rescan` en LAN.
Ver `docs/superpowers/specs/2026-08-17-rescan-camaras-design.md`.

**Descartado con motivo — fallback RTSP por `last_ip`:** construir la URL del stream desde
la IP guardada obliga a adivinar la ruta, que cambia con cada fabricante (la misma
fragilidad del `:10000` hardcodeado del punto 2). Con rescan periódico, una cámara conocida
que vuelve se recupera sola por descubrimiento, que devuelve la URI real. Si algún día una
cámara resultara indescubrible pero alcanzable, se reabre.

**Pendiente relacionado — comando remoto:** el disparo de rescan desde la nube (app del
técnico fuera del hogar) necesita el uplink del punto 3. La costura está lista: el downlink
solo tiene que llamar a `RescanService.run_once()`.
```

- [ ] **Step 5: Verificar que la documentación no miente**

Run: `grep -rn "solo al arranque\|docker compose restart" README.md docs/*.md`
Expected: ninguna coincidencia que afirme que el descubrimiento es solo de arranque (si aparece alguna, corregirla).

Run: `pytest -q`
Expected: el número de tests coincide con el que se dejó escrito en `docs/como-probar.md`.

- [ ] **Step 6: Commit**

```bash
git add README.md docs/como-probar.md docs/como-funciona.md docs/backlog.md
git commit -m "docs: rescan periódico y comando POST /rescan"
```

---

## Verificación final (tras la Task 5)

- [ ] `pytest -q` — todo verde.
- [ ] `ruff check src tests` y `mypy` — limpios.
- [ ] Arranque en seco con `VITAHUB_ADMIN_TOKEN`: el log muestra el puerto de control; `curl` con token → `200`, sin token → `401`; `Ctrl-C` sale limpio.
- [ ] **Con la cámara piloto** (criterios §9 del spec, requieren hardware):
  - Conectar una cámara con el hub corriendo → aparece en ≤60 s sin reiniciar.
  - `POST /rescan` la da de alta en el momento y responde con `added` y `started`.
  - Cambiar la IP de una cámara registrada → su worker se relanza con la URI nueva y no se duplican eventos.
  - Apagar una cámara → su worker reintenta con backoff y **no** desaparece del registro.
