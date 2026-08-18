# URL RTSP persistida y telemetría de conexión — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Que el hub reconecte con una cámara ya conocida aunque ONVIF esté apagado — recordando la URI que ONVIF resolvió en su día — y que una cámara inalcanzable deje de ser invisible.

**Architecture:** El registro persistido (`hub.yaml`) gana las dos URIs RTSP de cada cámara, que se rellenan con lo que devolvió el descubrimiento y se limpian de credenciales antes de escribirse. Al construir el mapa de URIs para el supervisor, lo descubierto gana y lo recordado actúa de red de seguridad. En paralelo, un `ConnectionMonitor` puro convierte los éxitos y fallos de conexión que le reporta el bucle de cámara en eventos `camera_unreachable` / `camera_reachable`.

**Tech Stack:** Python 3.12, stdlib, PyYAML. Pytest, ruff, mypy strict. **Cero dependencias nuevas.**

**Spec:** `docs/superpowers/specs/2026-08-18-url-rtsp-persistida-design.md`

## Global Constraints

- **Cero dependencias nuevas.** No añadir nada a `pyproject.toml`.
- **mypy strict** (`strict = true`, `packages = ["vitahub"]`): todo `src/` tipado por completo, incluidos los `-> None`.
- **ruff** con `line-length = 100`.
- Todo módulo nuevo empieza con `from __future__ import annotations`.
- **Logs y mensajes de error en español**, vía `get_logger(...)`. stdout es solo para eventos; los logs van a stderr.
- **El fichero de config no contiene secretos, nunca.** Las credenciales se inyectan en memoria con `with_credentials()`. Ninguna URI persistida puede llevar `usuario:clave@`.
- **Retrocompatibilidad obligatoria**: un `hub.yaml` de la versión anterior (sin los campos nuevos) tiene que cargar sin error.
- Ningún test puede depender de red, multicast ni cámaras.
- Al terminar cada tarea: `pytest`, `ruff check src tests` y `mypy` limpios.
- El entorno virtual está en `.venv` (`.venv/bin/python -m pytest`, `.venv/bin/ruff`, `.venv/bin/mypy`).

## File Structure

| Fichero | Responsabilidad |
|---|---|
| `src/vitahub/models.py` (modificar) | `Camera` gana `rtsp_main` y `rtsp_sub` opcionales. |
| `src/vitahub/ingest/rtsp.py` (modificar) | `strip_credentials()`, inverso de `with_credentials()`. |
| `src/vitahub/config.py` (modificar) | Leer y escribir los campos nuevos, tolerando su ausencia. |
| `src/vitahub/registry.py` (modificar) | `reconcile` guarda y refresca las URIs; nuevo tipo de cambio. |
| `src/vitahub/rescan.py` (modificar) | Mapa de URIs con precedencia descubierto > recordado. |
| `src/vitahub/analytics/connection_monitor.py` (nuevo) | Éxitos/fallos de conexión → eventos. Puro, sin red ni hilos. |
| `src/vitahub/app.py` (modificar) | Cablear el monitor en el bucle de cámara. |

---

### Task 1: `Camera` recuerda sus URIs, y el YAML las guarda sin credenciales

**Files:**
- Modify: `src/vitahub/models.py` (dataclass `Camera`)
- Modify: `src/vitahub/ingest/rtsp.py` (añadir `strip_credentials`)
- Modify: `src/vitahub/config.py` (`_cameras_from_raw`, `save_cameras`)
- Test: `tests/test_models.py`, `tests/test_rtsp_logic.py`, `tests/test_config.py`

**Interfaces:**
- Produces:
  - `Camera(id, name, last_ip, enabled=True, rtsp_main="", rtsp_sub="")` — los dos campos nuevos son `str` y por defecto vacíos ("aún no se ha descubierto").
  - `strip_credentials(rtsp_url: str) -> str` en `vitahub.ingest.rtsp`.

- [ ] **Step 1: Escribir los tests que fallan**

Añadir a `tests/test_rtsp_logic.py`:

```python
from vitahub.ingest.rtsp import strip_credentials


def test_strip_credentials_removes_userinfo():
    url = "rtsp://admin:cl%40ve@10.0.0.5:554/V_ENC_000"
    assert strip_credentials(url) == "rtsp://10.0.0.5:554/V_ENC_000"


def test_strip_credentials_leaves_clean_url_untouched():
    url = "rtsp://10.0.0.5:554/V_ENC_000"
    assert strip_credentials(url) == url


def test_strip_credentials_keeps_query_and_port():
    url = "rtsp://user:pass@10.0.0.5:8554/cam?channel=1"
    assert strip_credentials(url) == "rtsp://10.0.0.5:8554/cam?channel=1"


def test_strip_then_with_credentials_roundtrip():
    original = "rtsp://admin:secreto@10.0.0.5:554/V_ENC_000"
    limpia = strip_credentials(original)
    assert "secreto" not in limpia
    from vitahub.ingest.rtsp import with_credentials
    assert with_credentials(limpia, "admin", "secreto") == original
```

Añadir a `tests/test_models.py`:

```python
def test_camera_uris_default_to_empty():
    from vitahub.models import Camera
    cam = Camera(id="a", name="camera-1", last_ip="10.0.0.5")
    assert cam.rtsp_main == ""
    assert cam.rtsp_sub == ""
```

Añadir a `tests/test_config.py`:

```python
def test_config_without_uri_fields_still_loads(tmp_path):
    """Retrocompatibilidad: un hub.yaml de la versión anterior debe cargar."""
    path = tmp_path / "hub.yaml"
    path.write_text(
        "hub_id: hub-x\n"
        "cameras:\n"
        "- id: onvif-a\n"
        "  name: camera-1\n"
        "  last_ip: 10.0.0.5\n"
    )
    cfg = load_config(path, {"VITAHUB_ONVIF_USER": "u", "VITAHUB_ONVIF_PASSWORD": "p"})
    assert cfg.cameras[0].rtsp_main == ""
    assert cfg.cameras[0].rtsp_sub == ""


def test_config_reads_uri_fields(tmp_path):
    path = tmp_path / "hub.yaml"
    path.write_text(
        "hub_id: hub-x\n"
        "cameras:\n"
        "- id: onvif-a\n"
        "  name: camera-1\n"
        "  last_ip: 10.0.0.5\n"
        "  rtsp_main: rtsp://10.0.0.5:554/V_ENC_000\n"
        "  rtsp_sub: rtsp://10.0.0.5:554/V_ENC_001\n"
    )
    cfg = load_config(path, {"VITAHUB_ONVIF_USER": "u", "VITAHUB_ONVIF_PASSWORD": "p"})
    assert cfg.cameras[0].rtsp_main == "rtsp://10.0.0.5:554/V_ENC_000"
    assert cfg.cameras[0].rtsp_sub == "rtsp://10.0.0.5:554/V_ENC_001"


def test_save_cameras_persists_uri_fields(tmp_path):
    from vitahub.config import save_cameras
    from vitahub.models import Camera
    path = tmp_path / "hub.yaml"
    path.write_text("hub_id: hub-x\ncameras: []\n")
    save_cameras(path, [Camera(
        id="onvif-a", name="camera-1", last_ip="10.0.0.5",
        rtsp_main="rtsp://10.0.0.5:554/V_ENC_000",
        rtsp_sub="rtsp://10.0.0.5:554/V_ENC_001",
    )])
    reloaded = load_config(path, {"VITAHUB_ONVIF_USER": "u", "VITAHUB_ONVIF_PASSWORD": "p"})
    assert reloaded.cameras[0].rtsp_main == "rtsp://10.0.0.5:554/V_ENC_000"
    assert reloaded.cameras[0].rtsp_sub == "rtsp://10.0.0.5:554/V_ENC_001"


def test_saved_yaml_never_contains_credentials(tmp_path):
    """Invariante del proyecto: el fichero de config no guarda secretos."""
    from vitahub.config import save_cameras
    from vitahub.ingest.rtsp import strip_credentials
    from vitahub.models import Camera
    path = tmp_path / "hub.yaml"
    path.write_text("hub_id: hub-x\ncameras: []\n")
    sucia = "rtsp://admin:secreto@10.0.0.5:554/V_ENC_000"
    save_cameras(path, [Camera(
        id="onvif-a", name="camera-1", last_ip="10.0.0.5",
        rtsp_main=strip_credentials(sucia), rtsp_sub="",
    )])
    assert "secreto" not in path.read_text()
```

(Si `load_config` o `Camera` no estuvieran ya importados en esos ficheros, añadir los imports.)

- [ ] **Step 2: Correr los tests para verificar que fallan**

Run: `.venv/bin/python -m pytest tests/test_rtsp_logic.py tests/test_models.py tests/test_config.py -v`
Expected: FAIL — `ImportError: cannot import name 'strip_credentials'` y `TypeError` por los campos inexistentes de `Camera`

- [ ] **Step 3: Añadir los campos a `Camera`**

En `src/vitahub/models.py`, la dataclass `Camera` pasa a:

```python
@dataclass
class Camera:
    id: str
    name: str
    last_ip: str
    enabled: bool = True
    # URIs que ONVIF resolvió en su día, sin credenciales. Permiten reconectar
    # cuando el descubrimiento no encuentra la cámara (firmware que apaga ONVIF
    # al reiniciar, multicast que no llega). Vacías = aún no descubierta.
    rtsp_main: str = ""
    rtsp_sub: str = ""
```

- [ ] **Step 4: Añadir `strip_credentials`**

En `src/vitahub/ingest/rtsp.py`, junto a `with_credentials`:

```python
def strip_credentials(rtsp_url: str) -> str:
    """Quita el userinfo (usuario:clave@) de una URL RTSP. Inverso de with_credentials.

    El fichero de config no contiene secretos: las credenciales se inyectan en
    memoria al conectar. Si una URI llegara de ONVIF con userinfo y se
    persistiera tal cual, la contraseña del hogar acabaría escrita en el YAML.
    """
    parts = urlsplit(rtsp_url)
    if "@" not in parts.netloc:
        return rtsp_url
    host = parts.netloc.rsplit("@", 1)[1]
    return urlunsplit((parts.scheme, host, parts.path, parts.query, parts.fragment))
```

- [ ] **Step 5: Leer y escribir los campos en la config**

En `src/vitahub/config.py`, dentro de `_cameras_from_raw`, la construcción de `Camera` pasa a:

```python
                Camera(
                    id=str(entry["id"]),
                    name=str(entry["name"]),
                    last_ip=str(entry["last_ip"]),
                    enabled=bool(entry.get("enabled", True)),
                    rtsp_main=str(entry.get("rtsp_main", "")),
                    rtsp_sub=str(entry.get("rtsp_sub", "")),
                )
```

Y en `save_cameras`, la construcción de `raw["cameras"]`:

```python
    raw["cameras"] = [
        {
            "id": c.id,
            "name": c.name,
            "last_ip": c.last_ip,
            "rtsp_main": c.rtsp_main,
            "rtsp_sub": c.rtsp_sub,
            "enabled": c.enabled,
        }
        for c in cameras
    ]
```

- [ ] **Step 6: Correr la suite completa**

Run: `.venv/bin/python -m pytest -q`
Expected: PASS — los 111 previos siguen verdes más los nuevos

- [ ] **Step 7: Puertas de calidad**

Run: `.venv/bin/ruff check src tests && .venv/bin/mypy`
Expected: sin errores

- [ ] **Step 8: Commit**

```bash
git add src/vitahub/models.py src/vitahub/ingest/rtsp.py src/vitahub/config.py tests/
git commit -m "feat: el registro guarda las URIs RTSP, siempre sin credenciales"
```

---

### Task 2: `reconcile` guarda y refresca las URIs

**Files:**
- Modify: `src/vitahub/registry.py`
- Test: `tests/test_registry.py`

**Interfaces:**
- Consumes: `Camera(..., rtsp_main, rtsp_sub)` y `strip_credentials` (Task 1).
- Produces: `RegistryChange.kind` admite un valor más, `"uri_changed"`, además de `"added"` e `"ip_changed"`.

- [ ] **Step 1: Escribir los tests que fallan**

Añadir a `tests/test_registry.py`:

```python
def test_new_camera_stores_its_uris():
    merged, _ = reconcile([], [_disc("onvif-a", "10.0.0.5")])
    assert merged[0].rtsp_main == "rtsp://10.0.0.5:554/Streaming/Channels/1"
    assert merged[0].rtsp_sub == "rtsp://10.0.0.5:554/Streaming/Channels/2"


def test_changed_uri_is_updated_and_reported():
    existing = [Camera(
        id="onvif-a", name="salon", last_ip="10.0.0.5",
        rtsp_main="rtsp://10.0.0.5:554/vieja",
        rtsp_sub="rtsp://10.0.0.5:554/vieja-sub",
    )]
    merged, changes = reconcile(existing, [_disc("onvif-a", "10.0.0.5")])
    assert merged[0].rtsp_main == "rtsp://10.0.0.5:554/Streaming/Channels/1"
    assert "uri_changed" in [ch.kind for ch in changes]


def test_same_uris_produce_no_change():
    """Sin esto se reescribiría el YAML en cada rescan periódico."""
    existing = [Camera(
        id="onvif-a", name="salon", last_ip="10.0.0.5",
        rtsp_main="rtsp://10.0.0.5:554/Streaming/Channels/1",
        rtsp_sub="rtsp://10.0.0.5:554/Streaming/Channels/2",
    )]
    _, changes = reconcile(existing, [_disc("onvif-a", "10.0.0.5")])
    assert changes == []


def test_uris_are_stored_without_credentials():
    sucia = DiscoveredCamera(
        id="onvif-a", ip="10.0.0.5",
        rtsp_main="rtsp://admin:secreto@10.0.0.5:554/main",
        rtsp_sub="rtsp://admin:secreto@10.0.0.5:554/sub",
    )
    merged, _ = reconcile([], [sucia])
    assert "secreto" not in merged[0].rtsp_main
    assert "secreto" not in merged[0].rtsp_sub
    assert merged[0].rtsp_main == "rtsp://10.0.0.5:554/main"


def test_unseen_camera_keeps_its_uris():
    """La red de seguridad: una cámara que deja de verse conserva cómo conectar."""
    existing = [Camera(
        id="onvif-a", name="salon", last_ip="10.0.0.5",
        rtsp_main="rtsp://10.0.0.5:554/main", rtsp_sub="rtsp://10.0.0.5:554/sub",
    )]
    merged, changes = reconcile(existing, [])
    assert merged[0].rtsp_main == "rtsp://10.0.0.5:554/main"
    assert changes == []
```

(`_disc` y `Camera` ya existen en ese fichero; añadir el import de `DiscoveredCamera` si falta.)

- [ ] **Step 2: Correr los tests para verificar que fallan**

Run: `.venv/bin/python -m pytest tests/test_registry.py -v`
Expected: FAIL — las URIs no se guardan (cadenas vacías) y no existe `uri_changed`

- [ ] **Step 3: Implementar**

En `src/vitahub/registry.py`, añadir el import y reescribir el bucle de `reconcile`:

```python
from vitahub.ingest.rtsp import strip_credentials
```

```python
    for dc in discovered:
        # Nunca se persiste userinfo: el YAML no contiene secretos.
        main = strip_credentials(dc.rtsp_main)
        sub = strip_credentials(dc.rtsp_sub)
        known = by_id.get(dc.id)
        if known is None:
            by_id[dc.id] = Camera(
                id=dc.id,
                name=f"camera-{next_index}",
                last_ip=dc.ip,
                enabled=True,
                rtsp_main=main,
                rtsp_sub=sub,
            )
            changes.append(RegistryChange(dc.id, "added"))
            next_index += 1
            continue

        if known.last_ip != dc.ip:
            known.last_ip = dc.ip
            changes.append(RegistryChange(dc.id, "ip_changed"))
        if (known.rtsp_main, known.rtsp_sub) != (main, sub):
            known.rtsp_main = main
            known.rtsp_sub = sub
            changes.append(RegistryChange(dc.id, "uri_changed"))
```

Y actualizar el comentario del campo `kind`:

```python
    kind: str  # "added" | "ip_changed" | "uri_changed"
```

- [ ] **Step 4: Correr la suite completa**

Run: `.venv/bin/python -m pytest -q`
Expected: PASS

> **Se va a romper un test existente, y está previsto.**
> `tests/test_rescan.py::test_no_changes_does_not_write_to_disk` construye su cámara
> como `Camera(id="onvif-a", name="salon", last_ip="10.0.0.5")` — sin URIs — mientras que
> el `_disc(...)` que le pasa sí las trae. A partir de esta tarea eso **es** un cambio real
> (`uri_changed`), así que el rescan guardará y el test fallará.
>
> La corrección correcta es poblar las URIs de esa cámara para que de verdad no haya nada
> que cambiar, que es lo que el test quiere comprobar:
>
> ```python
>     cfg = _cfg(cameras=[Camera(
>         id="onvif-a", name="salon", last_ip="10.0.0.5",
>         rtsp_main="rtsp://10.0.0.5:554/Streaming/Channels/1",
>         rtsp_sub="rtsp://10.0.0.5:554/Streaming/Channels/2",
>     )])
> ```
>
> **No** lo arregles dejando de reportar `uri_changed`: ese reporte es el que hace que la
> URI se persista, o sea, el slice entero. El resto de tests de `test_rescan.py` deben
> seguir pasando sin tocarlos (`test_ip_change_is_reported_and_persisted` filtra por
> `kind`, así que el `uri_changed` adicional no le afecta).

- [ ] **Step 5: Puertas de calidad**

Run: `.venv/bin/ruff check src tests && .venv/bin/mypy`
Expected: sin errores

- [ ] **Step 6: Commit**

```bash
git add src/vitahub/registry.py tests/test_registry.py
git commit -m "feat: reconcile guarda y refresca las URIs RTSP descubiertas"
```

---

### Task 3: conectar con la URI recordada cuando el descubrimiento no ve la cámara

**Files:**
- Modify: `src/vitahub/rescan.py` (construcción del mapa de URIs dentro de `_rescan`)
- Test: `tests/test_rescan.py`

**Interfaces:**
- Consumes: `Camera.rtsp_main` / `Camera.rtsp_sub` (Task 1), `reconcile` (Task 2).
- Produces: el mapa `dict[camera_id, uri]` que recibe `CameraSupervisor.apply(cameras, uris)` pasa a incluir también cámaras no descubiertas que tengan URI recordada.

- [ ] **Step 1: Escribir los tests que fallan**

Añadir a `tests/test_rescan.py`:

```python
def test_remembered_uri_is_used_when_camera_is_not_discovered(tmp_path):
    """El caso del corte de luz: la cámara vuelve sin ONVIF, pero sirve vídeo."""
    path = _config_file(tmp_path)
    cfg = _cfg(cameras=[Camera(
        id="onvif-a", name="salon", last_ip="10.0.0.5",
        rtsp_main="rtsp://10.0.0.5:554/main", rtsp_sub="rtsp://10.0.0.5:554/sub",
    )])
    sup = _FakeSupervisor()
    service = RescanService(cfg, path, sup, lambda creds: [])

    result = service.run_once()

    _, uris = sup.calls[0]
    assert uris["onvif-a"] == "rtsp://admin:clave@10.0.0.5:554/sub"
    assert result.found == 0        # no se descubrió nada
    assert result.started == ["onvif-a"]  # y aun así hay worker


def test_discovered_uri_wins_over_remembered(tmp_path):
    path = _config_file(tmp_path)
    cfg = _cfg(cameras=[Camera(
        id="onvif-a", name="salon", last_ip="10.0.0.9",
        rtsp_main="rtsp://10.0.0.9:554/vieja", rtsp_sub="rtsp://10.0.0.9:554/vieja-sub",
    )])
    sup = _FakeSupervisor()
    service = RescanService(cfg, path, sup, lambda creds: [_disc("onvif-a", "10.0.0.5")])

    service.run_once()

    _, uris = sup.calls[0]
    assert uris["onvif-a"] == "rtsp://admin:clave@10.0.0.5:554/Streaming/Channels/2"


def test_camera_without_any_uri_is_not_in_the_map(tmp_path):
    path = _config_file(tmp_path)
    cfg = _cfg(cameras=[Camera(id="onvif-a", name="salon", last_ip="10.0.0.5")])
    sup = _FakeSupervisor()
    service = RescanService(cfg, path, sup, lambda creds: [])

    service.run_once()

    _, uris = sup.calls[0]
    assert uris == {}


def test_remembered_main_stream_is_used_when_configured(tmp_path):
    path = _config_file(tmp_path)
    cfg = _cfg(cameras=[Camera(
        id="onvif-a", name="salon", last_ip="10.0.0.5",
        rtsp_main="rtsp://10.0.0.5:554/main", rtsp_sub="rtsp://10.0.0.5:554/sub",
    )])
    cfg.inference.stream = "main"
    sup = _FakeSupervisor()
    service = RescanService(cfg, path, sup, lambda creds: [])

    service.run_once()

    _, uris = sup.calls[0]
    assert uris["onvif-a"].endswith("/main")
```

(`Camera` ya se importa en ese fichero.)

- [ ] **Step 2: Correr los tests para verificar que fallan**

Run: `.venv/bin/python -m pytest tests/test_rescan.py -v`
Expected: FAIL — hoy `uris` se construye solo a partir de `discovered`, así que sale `{}`

- [ ] **Step 3: Implementar**

En `src/vitahub/rescan.py`, sustituir el bloque que construye `uris` dentro de `_rescan` por una llamada a un método nuevo:

```python
        uris = self._uris_for(cameras, discovered)
```

Y añadir el método a la clase:

```python
    def _uris_for(
        self, cameras: list[Camera], discovered: list[DiscoveredCamera]
    ) -> dict[str, str]:
        """URI de conexión por cámara. Lo descubierto gana; lo recordado es la red de seguridad.

        Una cámara conocida que no aparece en este ciclo se conecta con la URI
        que ONVIF resolvió en su día: es lo que permite reconectar tras un corte
        de luz cuando el firmware de la cámara vuelve con ONVIF apagado.
        """
        substream = self._cfg.inference.stream == "substream"
        by_disc = {dc.id: dc for dc in discovered}
        uris: dict[str, str] = {}
        for camera in cameras:
            dc = by_disc.get(camera.id)
            if dc is not None:
                raw = dc.rtsp_sub if substream else dc.rtsp_main
            else:
                raw = camera.rtsp_sub if substream else camera.rtsp_main
            if not raw:
                continue
            uris[camera.id] = with_credentials(
                raw,
                self._cfg.credentials.onvif_user,
                self._cfg.credentials.onvif_password,
            )
        return uris
```

Añadir `Camera` al import de `vitahub.models` en ese fichero si no está.

- [ ] **Step 4: Correr la suite completa**

Run: `.venv/bin/python -m pytest -q`
Expected: PASS

- [ ] **Step 5: Puertas de calidad**

Run: `.venv/bin/ruff check src tests && .venv/bin/mypy`
Expected: sin errores

- [ ] **Step 6: Commit**

```bash
git add src/vitahub/rescan.py tests/test_rescan.py
git commit -m "feat: reconectar con la URI recordada si el descubrimiento no ve la cámara"
```

---

### Task 4: `ConnectionMonitor` — que una cámara perdida se note

**Files:**
- Create: `src/vitahub/analytics/connection_monitor.py`
- Test: `tests/test_connection_monitor.py`

**Interfaces:**
- Consumes: `vitahub.models.Camera` y `vitahub.models.Event`.
- Produces:
  - `ConnectionMonitor(hub_id: str, unreachable_after_s: float = 300.0, clock: Callable[[], datetime] = _utcnow)`
  - `on_connected(camera: Camera, now: float) -> list[Event]`
  - `on_failed(camera: Camera, now: float) -> list[Event]`
  - `now` es un reloj **monotónico** (segundos), igual que en `EventEngine.observe`; `clock` solo produce el `timestamp` del evento.

- [ ] **Step 1: Escribir los tests que fallan**

Crear `tests/test_connection_monitor.py`:

```python
from datetime import UTC, datetime

from vitahub.analytics.connection_monitor import ConnectionMonitor
from vitahub.models import Camera


def _clock():
    return datetime(2026, 8, 18, 10, 0, 0, tzinfo=UTC)


def _cam(id_="onvif-a"):
    return Camera(id=id_, name=f"n-{id_}", last_ip="10.0.0.5")


def _monitor():
    return ConnectionMonitor("hub-test", unreachable_after_s=300.0, clock=_clock)


def test_no_event_before_the_threshold():
    m = _monitor()
    cam = _cam()
    assert m.on_failed(cam, 0.0) == []
    assert m.on_failed(cam, 299.0) == []


def test_unreachable_is_emitted_once_the_threshold_passes():
    m = _monitor()
    cam = _cam()
    m.on_failed(cam, 0.0)
    events = m.on_failed(cam, 301.0)
    assert len(events) == 1
    assert events[0].type == "camera_unreachable"
    assert events[0].severity == "medium"
    assert events[0].camera_id == "onvif-a"
    assert events[0].payload["last_ip"] == "10.0.0.5"
    assert events[0].payload["minutes_down"] == 5.0


def test_unreachable_is_not_repeated_while_still_down():
    m = _monitor()
    cam = _cam()
    m.on_failed(cam, 0.0)
    m.on_failed(cam, 301.0)
    assert m.on_failed(cam, 900.0) == []


def test_reachable_only_after_an_unreachable():
    m = _monitor()
    cam = _cam()
    m.on_failed(cam, 0.0)
    assert m.on_connected(cam, 10.0) == []        # nunca se reportó caída
    m.on_failed(cam, 20.0)
    m.on_failed(cam, 400.0)                        # aquí sí se reporta
    events = m.on_connected(cam, 500.0)
    assert len(events) == 1
    assert events[0].type == "camera_reachable"
    assert events[0].severity == "info"


def test_a_new_outage_emits_again():
    m = _monitor()
    cam = _cam()
    m.on_failed(cam, 0.0)
    m.on_failed(cam, 301.0)
    m.on_connected(cam, 310.0)
    m.on_failed(cam, 320.0)
    events = m.on_failed(cam, 700.0)
    assert [e.type for e in events] == ["camera_unreachable"]


def test_camera_that_never_connects_still_reports():
    """El escenario del corte de luz: vuelve sin ONVIF y con otra IP, nunca conecta."""
    m = _monitor()
    cam = _cam()
    m.on_failed(cam, 0.0)
    events = m.on_failed(cam, 400.0)
    assert [e.type for e in events] == ["camera_unreachable"]


def test_cameras_do_not_contaminate_each_other():
    m = _monitor()
    a, b = _cam("onvif-a"), _cam("onvif-b")
    m.on_failed(a, 0.0)
    m.on_failed(b, 0.0)
    m.on_connected(b, 100.0)
    events = m.on_failed(a, 400.0)
    assert [e.camera_id for e in events] == ["onvif-a"]
    assert m.on_failed(b, 401.0) == []
```

- [ ] **Step 2: Correr los tests para verificar que fallan**

Run: `.venv/bin/python -m pytest tests/test_connection_monitor.py -v`
Expected: FAIL con `ModuleNotFoundError: No module named 'vitahub.analytics.connection_monitor'`

- [ ] **Step 3: Implementar**

Crear `src/vitahub/analytics/connection_monitor.py`:

```python
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from vitahub.models import Camera, Event


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass
class _ConnState:
    last_ok: float
    reported_down: bool = False


class ConnectionMonitor:
    """Convierte éxitos y fallos de conexión en eventos.

    El umbral es holgado a propósito: un tirón de cable tarda ~30s solo en que
    el watchdog de FFmpeg lo detecte, más el backoff de reconexión. Por debajo
    de eso saldrían avisos falsos cada vez que alguien desenchufa algo.
    """

    def __init__(
        self,
        hub_id: str,
        unreachable_after_s: float = 300.0,
        clock: Callable[[], datetime] = _utcnow,
    ) -> None:
        self._hub_id = hub_id
        self._unreachable_after_s = unreachable_after_s
        self._clock = clock
        self._states: dict[str, _ConnState] = {}

    def on_connected(self, camera: Camera, now: float) -> list[Event]:
        state = self._states.setdefault(camera.id, _ConnState(last_ok=now))
        was_down = state.reported_down
        state.last_ok = now
        state.reported_down = False
        if not was_down:
            return []
        return [self._event(camera, "camera_reachable", "info", {"last_ip": camera.last_ip})]

    def on_failed(self, camera: Camera, now: float) -> list[Event]:
        # El reloj arranca en el primer fallo, que ocurre a los pocos segundos
        # del arranque: así una cámara que NUNCA llega a conectar también acaba
        # reportándose.
        state = self._states.setdefault(camera.id, _ConnState(last_ok=now))
        if state.reported_down:
            return []
        down_for = now - state.last_ok
        if down_for < self._unreachable_after_s:
            return []
        state.reported_down = True
        return [
            self._event(
                camera,
                "camera_unreachable",
                "medium",
                {"last_ip": camera.last_ip, "minutes_down": round(down_for / 60.0, 1)},
            )
        ]

    def _event(
        self, camera: Camera, event_type: str, severity: str, payload: dict[str, object]
    ) -> Event:
        return Event(
            hub_id=self._hub_id,
            camera_id=camera.id,
            camera_name=camera.name,
            type=event_type,
            severity=severity,
            timestamp=self._clock().isoformat(),
            payload=payload,
        )
```

- [ ] **Step 4: Correr los tests y verificar que pasan**

Run: `.venv/bin/python -m pytest tests/test_connection_monitor.py -v`
Expected: PASS (7 tests)

- [ ] **Step 5: Puertas de calidad**

Run: `.venv/bin/ruff check src tests && .venv/bin/mypy`
Expected: sin errores

- [ ] **Step 6: Commit**

```bash
git add src/vitahub/analytics/connection_monitor.py tests/test_connection_monitor.py
git commit -m "feat: monitor de conexión (camera_unreachable / camera_reachable)"
```

---

### Task 5: cablear el monitor en el bucle de cámara

**Files:**
- Modify: `src/vitahub/app.py` (`run`, `_camera_loop`)
- Test: `tests/test_app_loop.py`

**Interfaces:**
- Consumes: `ConnectionMonitor.on_connected(camera, now) -> list[Event]` y `.on_failed(camera, now) -> list[Event]` (Task 4); `StdoutJsonSink.emit(event)`.
- Produces: `_emit_all(sink, events)` — helper que vuelca una lista de eventos por el sink sin que un fallo de emisión tumbe el worker.

- [ ] **Step 1: Escribir el test que falla**

Añadir a `tests/test_app_loop.py`:

```python
def test_emit_all_survives_a_failing_sink():
    """Un fallo al emitir no puede tumbar el hilo de la cámara."""
    from vitahub.app import _emit_all

    class _BoomSink:
        def __init__(self):
            self.seen = []

        def emit(self, event):
            self.seen.append(event)
            raise OSError("stdout roto")

    sink = _BoomSink()
    _emit_all(sink, ["evento-1", "evento-2"])
    assert len(sink.seen) == 2  # siguió con el segundo pese al fallo del primero
```

- [ ] **Step 2: Correr el test para verificar que falla**

Run: `.venv/bin/python -m pytest tests/test_app_loop.py -v`
Expected: FAIL — `ImportError: cannot import name '_emit_all'`

- [ ] **Step 3: Implementar el helper y cablear el monitor**

En `src/vitahub/app.py`, añadir el import:

```python
from vitahub.analytics.connection_monitor import ConnectionMonitor
```

Añadir el helper a nivel de módulo:

```python
def _emit_all(sink, events) -> None:  # type: ignore[no-untyped-def]
    """Vuelca eventos por el sink. Un fallo de emisión no tumba el worker."""
    for event in events:
        try:
            sink.emit(event)
        except Exception:  # noqa: BLE001 — emitir no debe matar el hilo de cámara
            _log.exception("no se pudo emitir un evento")
```

En `run()`, construir el monitor junto al motor de eventos y pasárselo al worker:

```python
    engine = EventEngine(cfg.hub_id)
    monitor = ConnectionMonitor(cfg.hub_id)
    sink = StdoutJsonSink()

    def worker(camera: Camera, rtsp_url: str, cam_stop: threading.Event) -> None:
        _camera_loop(  # type: ignore[no-untyped-call]
            camera, rtsp_url, detector, engine, sink, cfg, cam_stop, monitor
        )
```

En `_camera_loop`, añadir `monitor` al final de la firma y reportarle los dos hechos.
La firma pasa a:

```python
def _camera_loop(camera, rtsp_url, detector, engine, sink, cfg, stop, monitor):  # type: ignore[no-untyped-def]
```

Dentro del bucle, en la rama de "no abre" (justo después del `_log.warning("cam %s no abre...")`):

```python
                _emit_all(sink, monitor.on_failed(camera, time.monotonic()))
```

Justo después del `_log.info("cam %s conectada", camera.id)`:

```python
            _emit_all(sink, monitor.on_connected(camera, time.monotonic()))
```

Y en el `except Exception` de fallo de conexión, después del `_log.exception("cam %s error de conexión, reconecto")`:

```python
            _emit_all(sink, monitor.on_failed(camera, time.monotonic()))
```

- [ ] **Step 4: Correr la suite completa**

Run: `.venv/bin/python -m pytest -q`
Expected: PASS

- [ ] **Step 5: Puertas de calidad**

Run: `.venv/bin/ruff check src tests && .venv/bin/mypy`
Expected: sin errores

- [ ] **Step 6: Arranque en seco de verificación**

```bash
VITAHUB_CONFIG=./config/hub.example.yaml VITAHUB_ONVIF_USER=admin VITAHUB_ONVIF_PASSWORD=test .venv/bin/python -m vitahub.app
```

Expected: arranca, avisa de 0 cámaras y late sin caerse; `Ctrl-C` sale limpio. (Poner `detector: stub` en esa config para no cargar YOLO.)

- [ ] **Step 7: Commit**

```bash
git add src/vitahub/app.py tests/test_app_loop.py
git commit -m "feat: emitir eventos de conexión desde el bucle de cámara"
```

---

### Task 6: Documentación

**Files:**
- Modify: `docs/como-funciona.md`, `docs/como-probar.md`, `docs/backlog.md`

**Interfaces:**
- Consumes: el comportamiento final de las tareas 1-5.

- [ ] **Step 1: `docs/como-funciona.md`**

1. En la tabla de módulos, añadir tras `analytics/event_engine.py`:

```markdown
| `analytics/connection_monitor.py` | Éxitos y fallos de conexión → eventos `camera_unreachable` / `camera_reachable`. |
```

2. En el paso "Descubrimiento y registro", tras la nota del ciclo de rescan, añadir:

```markdown
> **El registro guarda también cómo reconectar.** Junto al `last_ip` se persisten las dos URIs RTSP
> que ONVIF resolvió (sin credenciales: se inyectan en memoria al conectar). Si en un rescan
> posterior el descubrimiento no encuentra a esa cámara, el hub se conecta con la URI recordada.
> Esto importa porque hay firmware —el de la cámara piloto, sin ir más lejos— que **desactiva ONVIF
> en cada reinicio**: tras un corte de luz la cámara vuelve sirviendo vídeo pero sin responder al
> descubrimiento, y sin la URI recordada el hub no tendría forma de reconectar.
>
> Lo descubierto gana siempre sobre lo recordado. Y esas URIs se pueden escribir a mano en
> `hub.yaml`, que es la vía para cámaras sin ONVIF o para entornos donde el multicast no llega.
```

3. En la sección del envelope de evento, tras la lista de tipos de presencia, añadir:

```markdown
Además de los eventos de presencia, el hub emite **eventos de conexión**:

- `camera_unreachable` (severidad `medium`) — la cámara lleva 5 minutos sin conseguir abrir el
  stream. El payload trae `last_ip` y `minutes_down`. Se emite **una vez** por episodio.
- `camera_reachable` (severidad `info`) — vuelve a conectar, y solo si antes se avisó de la caída.

El umbral es holgado a propósito: un tirón de cable tarda ~30 s solo en que el watchdog de FFmpeg lo
detecte, más el backoff. Por debajo de eso saldrían avisos falsos cada vez que alguien desenchufa
algo.
```

- [ ] **Step 2: `docs/como-probar.md`**

1. En el aviso sobre macOS, sustituir la frase final ("Si algún día hiciera falta descubrimiento
   real en Docker sobre Mac...") por:

```markdown
> Hay una salida sin cambiar de motor de contenedores: **declarar la cámara a mano** en
> `./data/hub.yaml` con su URI RTSP (ver §3.5). El hub conecta sin descubrimiento alguno, así que
> el multicast deja de hacer falta. Si aun así quisieras descubrimiento real en Docker sobre Mac, la
> única vía es un motor con red *bridged* (Colima/Lima con `socket_vmnet`), que pone la VM en la LAN
> con su propia IP.
```

2. Añadir una sección tras §3.4:

```markdown
### 3.5 Declarar una cámara a mano

Sirve para cámaras sin ONVIF, para firmware que lo desactiva al reiniciar, y para entornos donde el
multicast no llega (Docker sobre macOS, WiFi que aísla clientes). Añade a `hub.yaml`:

```yaml
cameras:
- id: camara-salon            # identificador estable, lo eliges tú
  name: salon
  last_ip: 192.168.1.190
  rtsp_main: rtsp://192.168.1.190:554/V_ENC_000
  rtsp_sub: rtsp://192.168.1.190:554/V_ENC_001
  enabled: true
```

**Sin credenciales en la URI**: el hub las inyecta desde `VITAHUB_ONVIF_USER` /
`VITAHUB_ONVIF_PASSWORD` al conectar. Para averiguar la ruta correcta de tu cámara, consulta su
ficha técnica; si no la tienes, un `DESCRIBE` por RTSP distingue una ruta que existe (`401
Unauthorized`) de una que no (`404 Not Found`).

Con `id` elegido a mano pierdes la identidad estable por número de serie: si un día esa cámara se
descubre por ONVIF, entrará como una cámara **distinta**, con su `onvif-<serie>`. Úsalo para pruebas
y para cámaras que nunca vayan a hablar ONVIF.
```

3. En la tabla de diagnóstico, añadir:

```markdown
| Sale `camera_unreachable` | La cámara lleva 5 min sin conectar: comprueba que está encendida, que su IP no ha cambiado y que la URI del registro sigue siendo válida. |
```

- [ ] **Step 3: `docs/backlog.md`**

Añadir al final de la sección "Próximo slice (mayor impacto)":

```markdown
### 5. Reserva DHCP por MAC en el protocolo de instalación
El hub ya recuerda la URI RTSP de cada cámara, pero esa URI lleva la IP dentro. Si el router cambia
la IP de una cámara **y** su ONVIF está apagado (firmware que lo desactiva al reiniciar), la URI
recordada apunta a una IP muerta y solo queda el evento `camera_unreachable`. La defensa real no es
código: es **fijar una reserva DHCP por MAC** en el router de cada hogar durante la instalación.
Cinco minutos por casa y el problema desaparece de raíz.

**Nota de compra, no de software:** la cámara piloto (Tuya) **desactiva ONVIF en cada reinicio**
—medido el 2026-08-17: tras un ciclo de corriente, puerto 10000 cerrado y sonda multicast 0 de 6,
con el RTSP intacto—. Si el modelo definitivo va a ser este, cada apagón de cada hogar dependerá de
que el hub recuerde la URI. Una cámara con ONVIF estable elimina la clase entera de problemas.
```

- [ ] **Step 4: Verificar que la documentación no miente**

Run: `.venv/bin/python -m pytest -q`
Expected: el recuento de tests coincide con el que quede escrito en `docs/como-probar.md` (actualízalo si cambió).

Run: `grep -rn "solo al arranque" README.md docs/*.md`
Expected: ninguna coincidencia que afirme que el descubrimiento es solo de arranque.

- [ ] **Step 5: Commit**

```bash
git add docs/
git commit -m "docs: URI recordada, cámaras declaradas a mano y eventos de conexión"
```

---

## Verificación final

- [ ] `.venv/bin/python -m pytest -q` — todo verde.
- [ ] `.venv/bin/ruff check src tests` y `.venv/bin/mypy` — limpios.
- [ ] `grep -n "rtsp" data/hub.yaml` tras un arranque con cámara real: las URIs están persistidas y **no** contienen credenciales.
- [ ] **Con la cámara piloto** (criterios §8 del spec, requieren hardware):
  - Descubrir la cámara, parar el hub, **desactivar ONVIF**, arrancar de nuevo → conecta igualmente con la URI recordada. Es el escenario del corte de luz, y hoy falla.
  - Un `hub.yaml` de la versión anterior (sin los campos nuevos) arranca sin error.
  - Declarando la cámara a mano, el hub conecta sin descubrimiento — verificable incluso en Docker sobre macOS.
  - Cámara apagada más de 5 minutos → `camera_unreachable` por stdout; al encenderla → `camera_reachable`.
