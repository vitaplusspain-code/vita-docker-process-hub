# Uplink de eventos a AWS — lado hub — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Que el hub publique sus eventos JSON en AWS IoT Core por MQTT con TLS mutuo, sin dejar de escribirlos por `stdout`.

**Architecture:** Un `FanoutSink` sustituye al `StdoutJsonSink` que hoy usa `app.py` y emite por dos sinks: el de `stdout` de siempre y un `AwsIotSink` nuevo. El cliente MQTT se inyecta en el sink, así que toda la suite corre sin tocar red. La sección `uplink` de `hub.yaml` decide si el segundo sink existe; por defecto no, de modo que un hub ya instalado se comporta exactamente igual que hoy.

**Tech Stack:** Python 3.12, `paho-mqtt`, pytest, ruff, mypy strict.

## Global Constraints

- **Spec:** `docs/superpowers/specs/2026-08-21-uplink-eventos-aws-design.md`. Ante cualquier duda, gana el spec.
- **El payload no cambia.** Se publica exactamente `Event.to_json()`. No se toca `src/vitahub/models.py` en ninguna tarea.
- **Topic:** `vita/hub/{hub_id}/events`. El `hub_id` es también el nombre del thing y el `clientId` MQTT.
- **QoS 0**, sin cola, sin reintento propio. Lo que no sale con el enlace caído se pierde a propósito.
- **Ningún fallo de cloud puede tumbar el hub.** Un `publish` que lanza se traga y se registra. La única excepción es la validación de config en el arranque (Tarea 3).
- **Cero red en los tests.** Todo con dobles. La suite corre en CI sin credenciales ni conectividad.
- **`mypy strict` y `ruff` limpios** al final de cada tarea. Los `except Exception` deliberados llevan `# noqa: BLE001` con el motivo en la misma línea, como el resto del repo.
- **Comentarios en castellano**, explicando el *por qué*, siguiendo el estilo denso que ya usan `config.py` y `app.py`.
- Comandos: `pytest` desde la raíz del repo. Entorno: `pip install -e ".[dev]"`.

---

### Task 1: `FanoutSink`

Emite cada evento por varios sinks aislando el fallo de cada uno. Sin dependencias nuevas y sin tocar nada existente: es la pieza que permite que el uplink conviva con `stdout` en vez de sustituirlo.

**Files:**
- Create: `src/vitahub/sinks/fanout.py`
- Test: `tests/test_fanout_sink.py`

**Interfaces:**
- Consumes: `EventSink` de `src/vitahub/sinks/base.py`, `Event` de `src/vitahub/models.py`.
- Produces: `FanoutSink(sinks: list[EventSink])` con `emit(event: Event) -> None` y `close() -> None`. La Tarea 4 la construye.

- [ ] **Step 1: Escribe el test que falla**

Crea `tests/test_fanout_sink.py`:

```python
from vitahub.models import Event
from vitahub.sinks.base import EventSink
from vitahub.sinks.fanout import FanoutSink


def _event() -> Event:
    return Event(
        hub_id="hub-1",
        camera_id="onvif-abc",
        camera_name="salon",
        type="person_detected",
        severity="info",
        timestamp="2026-08-21T10:00:00+00:00",
        payload={"person_count": 1},
    )


class _RecordingSink(EventSink):
    def __init__(self) -> None:
        self.emitted: list[Event] = []
        self.closed = False

    def emit(self, event: Event) -> None:
        self.emitted.append(event)

    def close(self) -> None:
        self.closed = True


class _BrokenSink(EventSink):
    def emit(self, event: Event) -> None:
        raise RuntimeError("sink roto")

    def close(self) -> None:
        raise RuntimeError("cierre roto")


def test_emits_through_every_sink():
    a, b = _RecordingSink(), _RecordingSink()
    FanoutSink([a, b]).emit(_event())
    assert len(a.emitted) == 1
    assert len(b.emitted) == 1


def test_a_failing_sink_does_not_starve_the_others():
    # El caso real: el uplink a AWS caído no puede dejar además sin traza a
    # `docker logs`, que es lo único que tiene un técnico delante del Jetson.
    good = _RecordingSink()
    FanoutSink([_BrokenSink(), good]).emit(_event())
    assert len(good.emitted) == 1


def test_emit_never_propagates():
    FanoutSink([_BrokenSink()]).emit(_event())


def test_close_closes_every_sink_even_if_one_fails():
    good = _RecordingSink()
    FanoutSink([_BrokenSink(), good]).close()
    assert good.closed
```

- [ ] **Step 2: Ejecuta el test y comprueba que falla**

Run: `pytest tests/test_fanout_sink.py -v`
Expected: FAIL con `ModuleNotFoundError: No module named 'vitahub.sinks.fanout'`

- [ ] **Step 3: Escribe la implementación mínima**

Crea `src/vitahub/sinks/fanout.py`:

```python
from __future__ import annotations

from vitahub.logging_setup import get_logger
from vitahub.models import Event
from vitahub.sinks.base import EventSink

_log = get_logger("sinks.fanout")


class FanoutSink(EventSink):
    """Emite cada evento por varios sinks, aislando el fallo de cada uno.

    Un sink que lanza no puede impedir que los demás reciban el evento. El
    caso concreto que esto protege: con el uplink a AWS caído, `stdout` tiene
    que seguir recibiendo, porque `docker logs` es la única herramienta de
    diagnóstico de un técnico delante del Jetson.
    """

    def __init__(self, sinks: list[EventSink]) -> None:
        self._sinks = sinks

    def emit(self, event: Event) -> None:
        for sink in self._sinks:
            try:
                sink.emit(event)
            except Exception:  # noqa: BLE001 — un sink roto no silencia a los demás
                _log.exception("fallo emitiendo por %s", type(sink).__name__)

    def close(self) -> None:
        for sink in self._sinks:
            try:
                sink.close()
            except Exception:  # noqa: BLE001 — cerrar uno no puede impedir cerrar el resto
                _log.exception("fallo cerrando %s", type(sink).__name__)
```

- [ ] **Step 4: Ejecuta los tests y comprueba que pasan**

Run: `pytest tests/test_fanout_sink.py -v`
Expected: 4 passed

- [ ] **Step 5: Comprueba estilo y tipos**

Run: `ruff check . && mypy`
Expected: sin errores

- [ ] **Step 6: Commit**

```bash
git add src/vitahub/sinks/fanout.py tests/test_fanout_sink.py
git commit -m "feat: FanoutSink para emitir por varios sinks aislando fallos"
```

---

### Task 2: `AwsIotSink` y `build_client`

El sink que publica en IoT Core y la función que cablea el TLS mutuo. El cliente se inyecta, así que los tests no tocan red.

**Files:**
- Create: `src/vitahub/sinks/aws_iot.py`
- Modify: `pyproject.toml` (dependencia `paho-mqtt`)
- Test: `tests/test_aws_iot_sink.py`

**Interfaces:**
- Consumes: `EventSink`, `Event`, `get_logger`.
- Produces:
  - `topic_for(prefix: str, hub_id: str) -> str`
  - `MqttClient` (Protocol con `publish(topic: str, payload: str, qos: int) -> object`, `loop_stop() -> None`, `disconnect() -> None`)
  - `AwsIotSink(client: MqttClient, topic: str)`
  - `build_client(hub_id: str, endpoint: str, ca: Path, cert: Path, key: Path) -> MqttClient`
  - La Tarea 4 usa `topic_for`, `AwsIotSink` y `build_client`.

- [ ] **Step 1: Añade la dependencia**

En `pyproject.toml`, en `[project].dependencies`, añade `"paho-mqtt>=2.1"` después de `"PyYAML>=6.0"`. Deja el resto igual.

Run: `pip install -e ".[dev]"`
Expected: instala `paho-mqtt`

- [ ] **Step 2: Escribe el test que falla**

Crea `tests/test_aws_iot_sink.py`:

```python
import json
import os
import stat

from vitahub.models import Event
from vitahub.sinks.aws_iot import AwsIotSink, topic_for, warn_if_key_is_exposed


def _event() -> Event:
    return Event(
        hub_id="hub-1",
        camera_id="onvif-abc",
        camera_name="salon",
        type="person_detected",
        severity="info",
        timestamp="2026-08-21T10:00:00+00:00",
        payload={"person_count": 1},
    )


class _FakeClient:
    def __init__(self, fail: bool = False) -> None:
        self.published: list[tuple[str, str, int]] = []
        self.stopped = False
        self.disconnected = False
        self._fail = fail

    def publish(self, topic: str, payload: str, qos: int) -> object:
        if self._fail:
            raise RuntimeError("broker caído")
        self.published.append((topic, payload, qos))
        return None

    def loop_stop(self) -> None:
        self.stopped = True

    def disconnect(self) -> None:
        self.disconnected = True


def test_topic_for_composes_the_contract_topic():
    assert topic_for("vita/hub", "hub-casa-lopez") == "vita/hub/hub-casa-lopez/events"


def test_emit_publishes_the_exact_event_json():
    client = _FakeClient()
    event = _event()
    AwsIotSink(client, "vita/hub/hub-1/events").emit(event)
    topic, payload, qos = client.published[0]
    assert topic == "vita/hub/hub-1/events"
    # El contrato del spec: por MQTT sale byte a byte lo mismo que por stdout.
    assert payload == event.to_json()
    assert json.loads(payload)["type"] == "person_detected"
    assert qos == 0


def test_a_publish_that_raises_does_not_propagate():
    # Un broker caído no puede tumbar el hilo de una cámara.
    AwsIotSink(_FakeClient(fail=True), "vita/hub/hub-1/events").emit(_event())


def test_close_stops_the_loop_and_disconnects():
    client = _FakeClient()
    AwsIotSink(client, "vita/hub/hub-1/events").close()
    assert client.stopped
    assert client.disconnected


def test_warns_when_the_private_key_is_readable_by_others(tmp_path, caplog):
    key = tmp_path / "private.pem.key"
    key.write_text("clave")
    os.chmod(key, stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP | stat.S_IROTH)
    warn_if_key_is_exposed(key)
    assert "legible por otros" in caplog.text


def test_does_not_warn_when_the_private_key_is_locked_down(tmp_path, caplog):
    key = tmp_path / "private.pem.key"
    key.write_text("clave")
    os.chmod(key, stat.S_IRUSR | stat.S_IWUSR)
    warn_if_key_is_exposed(key)
    assert caplog.text == ""
```

- [ ] **Step 3: Ejecuta el test y comprueba que falla**

Run: `pytest tests/test_aws_iot_sink.py -v`
Expected: FAIL con `ModuleNotFoundError: No module named 'vitahub.sinks.aws_iot'`

- [ ] **Step 4: Escribe la implementación**

Crea `src/vitahub/sinks/aws_iot.py`:

```python
from __future__ import annotations

import ssl
from pathlib import Path
from typing import Protocol

from vitahub.logging_setup import get_logger
from vitahub.models import Event
from vitahub.sinks.base import EventSink

_log = get_logger("sinks.aws_iot")

# QoS 0 a propósito: este slice no tiene cola (ver el spec, §2). Un QoS 1 sin
# cola persistente solo añadiría reintentos en memoria que un reinicio del
# contenedor —justo lo que provoca un corte de luz— se lleva igual.
_QOS = 0
_PORT = 8883
_KEEPALIVE_S = 60


class MqttClient(Protocol):
    """Lo mínimo que el sink usa de paho.

    Existe para que los tests inyecten un doble: sin esto, probar el sink
    exigiría un broker, y la suite corre en CI sin red.
    """

    def publish(self, topic: str, payload: str, qos: int) -> object: ...
    def loop_stop(self) -> None: ...
    def disconnect(self) -> None: ...


def topic_for(prefix: str, hub_id: str) -> str:
    return f"{prefix}/{hub_id}/events"


class AwsIotSink(EventSink):
    def __init__(self, client: MqttClient, topic: str) -> None:
        self._client = client
        self._topic = topic

    def emit(self, event: Event) -> None:
        try:
            self._client.publish(self._topic, event.to_json(), _QOS)
        except Exception:  # noqa: BLE001 — el uplink caído no puede tumbar la cámara
            _log.exception("no se pudo publicar en %s", self._topic)

    def close(self) -> None:
        try:
            self._client.loop_stop()
            self._client.disconnect()
        except Exception:  # noqa: BLE001 — el apagado no falla por el uplink
            _log.exception("fallo cerrando el uplink")


def warn_if_key_is_exposed(key: Path) -> None:
    """Avisa si la clave privada la puede leer alguien más que su dueño.

    Solo avisa, no aborta: bloquear el arranque por un `chmod` dejaría una
    vivienda entera sin vigilancia por un problema de permisos de fichero.
    """
    try:
        mode = key.stat().st_mode & 0o777
    except OSError:
        return
    if mode & 0o077:
        _log.warning(
            "la clave privada %s es legible por otros (modo %o) — arréglalo con: chmod 600 %s",
            key,
            mode,
            key,
        )


def build_client(hub_id: str, endpoint: str, ca: Path, cert: Path, key: Path) -> MqttClient:
    """Cliente MQTT con TLS mutuo, conectando en segundo plano.

    `connect_async` + `loop_start`: el arranque del hub NO se bloquea si el
    enlace está caído — un hogar sin internet tiene que seguir vigilando. La
    reconexión con backoff la lleva el propio bucle de paho.

    El import va dentro de la función y no arriba: los tests inyectan un doble
    y nunca llegan aquí, así que la suite no paga el import ni depende de que
    paho esté instalado para recolectar los tests.
    """
    import paho.mqtt.client as mqtt

    warn_if_key_is_exposed(key)
    client = mqtt.Client(
        mqtt.CallbackAPIVersion.VERSION2,
        # La policy de IoT exige clientId == nombre del thing == hub_id. Con
        # otro valor, el broker rechaza la conexión sin más explicación.
        client_id=hub_id,
        protocol=mqtt.MQTTv311,
    )
    client.tls_set(
        ca_certs=str(ca),
        certfile=str(cert),
        keyfile=str(key),
        tls_version=ssl.PROTOCOL_TLSv1_2,
    )
    client.reconnect_delay_set(min_delay=1, max_delay=120)
    client.on_connect = _on_connect
    client.on_disconnect = _on_disconnect
    client.connect_async(endpoint, _PORT, keepalive=_KEEPALIVE_S)
    client.loop_start()
    return client


def _on_connect(
    client: object,
    userdata: object,
    flags: object,
    reason_code: object,
    properties: object = None,
) -> None:
    # reason_code 0 (o Success en paho 2) es el único caso bueno; cualquier
    # otro suele ser certificado no adjunto al thing o policy mal acotada.
    if str(reason_code) in ("0", "Success"):
        _log.info("uplink conectado a AWS IoT")
    else:
        _log.warning("uplink rechazado por el broker: %s", reason_code)


def _on_disconnect(
    client: object,
    userdata: object,
    flags: object,
    reason_code: object,
    properties: object = None,
) -> None:
    _log.warning("uplink desconectado (%s), reintentando en segundo plano", reason_code)
```

- [ ] **Step 5: Ejecuta los tests y comprueba que pasan**

Run: `pytest tests/test_aws_iot_sink.py -v`
Expected: 6 passed

- [ ] **Step 6: Comprueba estilo y tipos**

Run: `ruff check . && mypy`
Expected: sin errores.

Si `mypy` se queja de que `paho.mqtt.client` no tiene stubs, añade este bloque al final de `pyproject.toml` (mismo mecanismo que ya usa el proyecto para `ultralytics` y `cv2`):

```toml
[[tool.mypy.overrides]]
module = ["paho.*"]
ignore_missing_imports = true
```

- [ ] **Step 7: Commit**

```bash
git add src/vitahub/sinks/aws_iot.py tests/test_aws_iot_sink.py pyproject.toml
git commit -m "feat: AwsIotSink publica eventos en IoT Core por MQTT/TLS mutuo"
```

---

### Task 3: Sección `uplink` en la configuración

Añade `uplink` a `hub.yaml` con validación que falla en el arranque si la instalación está incompleta, y actualiza el fichero de ejemplo.

**Files:**
- Modify: `src/vitahub/config.py`
- Modify: `config/hub.example.yaml`
- Test: `tests/test_config.py`

**Interfaces:**
- Consumes: nada de tareas anteriores.
- Produces: `UplinkConfig(enabled: bool, topic_prefix: str, endpoint: str, ca_path: str, cert_path: str, key_path: str)` y el campo `HubConfig.uplink: UplinkConfig`. La Tarea 4 los lee.

- [ ] **Step 1: Escribe los tests que fallan**

Añade al final de `tests/test_config.py`:

```python
UPLINK_YAML = VALID_YAML + """
uplink:
  enabled: true
  topic_prefix: vita/hub
"""


def _certs(tmp_path: Path) -> dict[str, str]:
    """Crea los tres ficheros de certificado y devuelve el entorno que los apunta."""
    env = dict(ENV)
    for var, name in (
        ("VITAHUB_IOT_CA", "AmazonRootCA1.pem"),
        ("VITAHUB_IOT_CERT", "certificate.pem.crt"),
        ("VITAHUB_IOT_KEY", "private.pem.key"),
    ):
        path = tmp_path / name
        path.write_text("x")
        env[var] = str(path)
    env["VITAHUB_IOT_ENDPOINT"] = "abc123-ats.iot.eu-west-1.amazonaws.com"
    return env


def test_config_without_uplink_section_keeps_it_disabled(tmp_path):
    # Un hub ya instalado, con un hub.yaml anterior a este slice, arranca igual.
    cfg = load_config(_write(tmp_path, VALID_YAML), ENV)
    assert cfg.uplink.enabled is False


def test_enabled_uplink_without_endpoint_raises(tmp_path):
    with pytest.raises(ConfigError, match="VITAHUB_IOT_ENDPOINT"):
        load_config(_write(tmp_path, UPLINK_YAML), ENV)


def test_enabled_uplink_without_certificate_file_raises(tmp_path):
    env = dict(ENV)
    env["VITAHUB_IOT_ENDPOINT"] = "abc123-ats.iot.eu-west-1.amazonaws.com"
    with pytest.raises(ConfigError, match="no existe el fichero de certificado"):
        load_config(_write(tmp_path, UPLINK_YAML), env)


def test_enabled_uplink_loads_endpoint_and_paths(tmp_path):
    env = _certs(tmp_path)
    cfg = load_config(_write(tmp_path, UPLINK_YAML), env)
    assert cfg.uplink.enabled is True
    assert cfg.uplink.topic_prefix == "vita/hub"
    assert cfg.uplink.endpoint == env["VITAHUB_IOT_ENDPOINT"]
    assert cfg.uplink.key_path == env["VITAHUB_IOT_KEY"]


def test_empty_topic_prefix_is_rejected(tmp_path):
    yaml_text = VALID_YAML + "\nuplink:\n  enabled: true\n  topic_prefix: ''\n"
    with pytest.raises(ConfigError, match="topic_prefix"):
        load_config(_write(tmp_path, yaml_text), _certs(tmp_path))


def test_save_cameras_preserves_the_uplink_section(tmp_path):
    # El hub reescribe hub.yaml en cada descubrimiento. Si save_cameras se
    # comiera la sección uplink, el hogar dejaría de reportar tras el primer
    # rescan y nadie relacionaría una cosa con la otra.
    path = _write(tmp_path, UPLINK_YAML)
    save_cameras(path, [])
    assert "uplink:" in path.read_text()
    assert "enabled: true" in path.read_text()
```

- [ ] **Step 2: Ejecuta los tests y comprueba que fallan**

Run: `pytest tests/test_config.py -v -k uplink`
Expected: FAIL con `AttributeError: 'HubConfig' object has no attribute 'uplink'`

- [ ] **Step 3: Implementa `UplinkConfig`**

En `src/vitahub/config.py`, añade el dataclass después de `InferenceConfig`:

```python
# Rutas por defecto de los tres ficheros que deja scripts/provision-hub.sh
# (repo vitaplus-aws-architecture) en el Jetson.
_DEFAULT_CERT_DIR = "/data/certs"


@dataclass
class UplinkConfig:
    enabled: bool = False
    topic_prefix: str = "vita/hub"
    endpoint: str = ""
    ca_path: str = ""
    cert_path: str = ""
    key_path: str = ""
```

Añade el campo a `HubConfig`, **al final y con default**, para no romper ninguna construcción existente:

```python
@dataclass
class HubConfig:
    hub_id: str
    discovery: DiscoveryConfig
    inference: InferenceConfig
    cameras: list[Camera]
    credentials: Credentials
    uplink: UplinkConfig = field(default_factory=UplinkConfig)
```

Añade `field` al import de `dataclasses`: `from dataclasses import dataclass, field`.

- [ ] **Step 4: Implementa la lectura y validación**

Añade esta función a `src/vitahub/config.py`, junto a `_cameras_from_raw`:

```python
def _uplink_from_raw(raw: Mapping[str, object], env: Mapping[str, str]) -> UplinkConfig:
    """Lee la sección `uplink` y verifica que la instalación está completa.

    Falla el arranque a propósito cuando `enabled` es true y falta algo: es un
    error de instalación, y el técnico que sembró el certificado ESTÁ delante.
    Mismo criterio que `_credentials_from_env` con la credencial ONVIF. Un
    uplink que arranca en silencio sin poder publicar es un hogar que parece
    instalado y no reporta nada.
    """
    enabled = bool(raw.get("enabled", False))
    topic_prefix = str(raw.get("topic_prefix", "vita/hub"))
    if not topic_prefix:
        raise ConfigError("Campo 'uplink.topic_prefix' no puede estar vacío")
    if not enabled:
        return UplinkConfig(enabled=False, topic_prefix=topic_prefix)

    endpoint = env.get("VITAHUB_IOT_ENDPOINT", "")
    if not endpoint:
        raise ConfigError(
            "uplink.enabled es true pero falta la variable de entorno "
            "VITAHUB_IOT_ENDPOINT (el endpoint ATS de la cuenta; lo imprime "
            "scripts/provision-hub.sh del repo de arquitectura)"
        )

    paths = {
        "ca_path": env.get("VITAHUB_IOT_CA", f"{_DEFAULT_CERT_DIR}/AmazonRootCA1.pem"),
        "cert_path": env.get("VITAHUB_IOT_CERT", f"{_DEFAULT_CERT_DIR}/certificate.pem.crt"),
        "key_path": env.get("VITAHUB_IOT_KEY", f"{_DEFAULT_CERT_DIR}/private.pem.key"),
    }
    for name, value in paths.items():
        if not Path(value).is_file():
            raise ConfigError(
                f"uplink.enabled es true pero no existe el fichero de certificado "
                f"{value} ({name}) — siémbralo con scripts/provision-hub.sh"
            )

    return UplinkConfig(
        enabled=True,
        topic_prefix=topic_prefix,
        endpoint=endpoint,
        ca_path=paths["ca_path"],
        cert_path=paths["cert_path"],
        key_path=paths["key_path"],
    )
```

En `load_config`, después del bloque que valida `inf_raw`, añade:

```python
    up_raw = raw.get("uplink") or {}
    if not isinstance(up_raw, dict):
        raise ConfigError("Sección 'uplink' debe ser un mapping YAML")
```

Y en el `return HubConfig(...)`, añade como último argumento:

```python
        uplink=_uplink_from_raw(up_raw, env),
```

- [ ] **Step 5: Ejecuta los tests y comprueba que pasan**

Run: `pytest tests/test_config.py -v`
Expected: todos pasan, incluidos los seis nuevos

- [ ] **Step 6: Actualiza el fichero de ejemplo**

En `config/hub.example.yaml`, añade antes de la línea `cameras: []`:

```yaml
uplink:
  enabled: false            # true para publicar los eventos en AWS IoT Core
  topic_prefix: vita/hub    # NO lo cambies por hogar: la policy de IoT lo lleva fijo,
                            # y con otro valor el broker descarta los publish en silencio
# Con enabled: true hacen falta además estas variables de entorno:
#   VITAHUB_IOT_ENDPOINT  (lo imprime provision-hub.sh del repo de arquitectura)
#   VITAHUB_IOT_CA / VITAHUB_IOT_CERT / VITAHUB_IOT_KEY  (por defecto en /data/certs/)
```

- [ ] **Step 7: Comprueba que el ejemplo sigue cargando**

Run: `pytest tests/test_smoke.py -v && ruff check . && mypy`
Expected: sin errores

- [ ] **Step 8: Commit**

```bash
git add src/vitahub/config.py tests/test_config.py config/hub.example.yaml
git commit -m "feat: sección uplink en la config, con validación de instalación"
```

---

### Task 4: `build_sink` y cableado en `app.py`

Une las tres piezas: la config decide, la factoría construye, `app.py` cambia una línea.

**Files:**
- Modify: `src/vitahub/factory.py`
- Modify: `src/vitahub/app.py`
- Test: `tests/test_factory.py`

**Interfaces:**
- Consumes: `FanoutSink` (Tarea 1); `AwsIotSink`, `build_client`, `topic_for` (Tarea 2); `HubConfig.uplink` (Tarea 3).
- Produces: `build_sink(cfg: HubConfig) -> EventSink`.

- [ ] **Step 1: Escribe los tests que fallan**

Añade a `tests/test_factory.py`:

```python
from vitahub.config import (
    Credentials,
    DiscoveryConfig,
    HubConfig,
    InferenceConfig,
    UplinkConfig,
)
from vitahub.factory import build_sink
from vitahub.sinks.aws_iot import AwsIotSink
from vitahub.sinks.fanout import FanoutSink
from vitahub.sinks.stdout_json import StdoutJsonSink


def _hub_config(uplink: UplinkConfig) -> HubConfig:
    return HubConfig(
        hub_id="hub-casa-lopez",
        discovery=DiscoveryConfig(),
        inference=InferenceConfig(),
        cameras=[],
        credentials=Credentials(onvif_user="admin", onvif_password="x"),
        uplink=uplink,
    )


def test_build_sink_without_uplink_is_stdout_only():
    sink = build_sink(_hub_config(UplinkConfig(enabled=False)))
    assert isinstance(sink, StdoutJsonSink)


def test_build_sink_with_uplink_fans_out_to_stdout_and_aws(monkeypatch):
    built: dict[str, object] = {}

    def _fake_build_client(hub_id, endpoint, ca, cert, key):
        built["hub_id"] = hub_id
        built["endpoint"] = endpoint
        return object()

    monkeypatch.setattr("vitahub.factory.build_client", _fake_build_client)
    sink = build_sink(
        _hub_config(
            UplinkConfig(
                enabled=True,
                topic_prefix="vita/hub",
                endpoint="abc-ats.iot.eu-west-1.amazonaws.com",
                ca_path="/data/certs/AmazonRootCA1.pem",
                cert_path="/data/certs/certificate.pem.crt",
                key_path="/data/certs/private.pem.key",
            )
        )
    )
    assert isinstance(sink, FanoutSink)
    kinds = [type(s) for s in sink._sinks]
    assert StdoutJsonSink in kinds
    assert AwsIotSink in kinds
    assert built["hub_id"] == "hub-casa-lopez"


def test_build_sink_uses_the_contract_topic(monkeypatch):
    monkeypatch.setattr("vitahub.factory.build_client", lambda **kw: object())
    sink = build_sink(
        _hub_config(
            UplinkConfig(
                enabled=True,
                topic_prefix="vita/hub",
                endpoint="abc-ats.iot.eu-west-1.amazonaws.com",
                ca_path="a",
                cert_path="b",
                key_path="c",
            )
        )
    )
    aws = [s for s in sink._sinks if isinstance(s, AwsIotSink)][0]
    assert aws._topic == "vita/hub/hub-casa-lopez/events"
```

- [ ] **Step 2: Ejecuta los tests y comprueba que fallan**

Run: `pytest tests/test_factory.py -v`
Expected: FAIL con `ImportError: cannot import name 'build_sink'`

- [ ] **Step 3: Implementa `build_sink`**

Sustituye el contenido de `src/vitahub/factory.py` por:

```python
from __future__ import annotations

from pathlib import Path

from vitahub.config import HubConfig, InferenceConfig
from vitahub.inference.base import Detector
from vitahub.inference.person_yolo import PersonDetector
from vitahub.inference.stub import StubDetector
from vitahub.logging_setup import get_logger
from vitahub.sinks.aws_iot import AwsIotSink, build_client, topic_for
from vitahub.sinks.base import EventSink
from vitahub.sinks.fanout import FanoutSink
from vitahub.sinks.stdout_json import StdoutJsonSink

_log = get_logger("factory")


def build_detector(cfg: InferenceConfig, weights_path: str) -> Detector:
    if cfg.detector == "stub":
        return StubDetector()
    if cfg.detector == "person_yolo":
        return PersonDetector.from_weights(weights_path, confidence=cfg.confidence)
    raise ValueError(f"Detector desconocido: {cfg.detector}")


def build_sink(cfg: HubConfig) -> EventSink:
    """El sink de stdout siempre; el de AWS solo si el uplink está habilitado.

    Con el uplink apagado devuelve el StdoutJsonSink pelado, sin envolverlo en
    un fanout de un solo elemento: el comportamiento de un hub sin uplink es
    exactamente el de antes de este slice, sin una capa de más en medio.
    """
    stdout: EventSink = StdoutJsonSink()
    if not cfg.uplink.enabled:
        return stdout

    client = build_client(
        hub_id=cfg.hub_id,
        endpoint=cfg.uplink.endpoint,
        ca=Path(cfg.uplink.ca_path),
        cert=Path(cfg.uplink.cert_path),
        key=Path(cfg.uplink.key_path),
    )
    topic = topic_for(cfg.uplink.topic_prefix, cfg.hub_id)
    _log.info("uplink habilitado hacia %s", topic)
    return FanoutSink([stdout, AwsIotSink(client, topic)])
```

- [ ] **Step 4: Cablea `app.py`**

En `src/vitahub/app.py`:

1. En los imports, cambia `from vitahub.factory import build_detector` por `from vitahub.factory import build_detector, build_sink`.
2. Elimina la línea `from vitahub.sinks.stdout_json import StdoutJsonSink`.
3. En `run()`, sustituye `sink = StdoutJsonSink()` por `sink = build_sink(cfg)`.
4. En la firma de `_shutdown`, cambia el tipo del parámetro `sink` de `StdoutJsonSink` a `EventSink` (ya está importado en el fichero).

- [ ] **Step 5: Ejecuta la suite entera**

Run: `pytest -v`
Expected: todos pasan

- [ ] **Step 6: Comprueba estilo y tipos**

Run: `ruff check . && mypy`
Expected: sin errores

- [ ] **Step 7: Comprueba a mano que un hub sin uplink arranca igual que antes**

Run:
```bash
VITAHUB_ONVIF_USER=admin VITAHUB_ONVIF_PASSWORD=x \
VITAHUB_CONFIG=./config/hub.example.yaml \
timeout 15 python -m vitahub.app
```
Expected: arranca, loguea `hub hub-CAMBIAME arrancando`, **no** aparece la línea `uplink habilitado`, y no peta por falta de certificados.

- [ ] **Step 8: Commit**

```bash
git add src/vitahub/factory.py src/vitahub/app.py tests/test_factory.py
git commit -m "feat: build_sink cablea el uplink cuando la config lo habilita"
```

---

### Task 5: Documentación

Lo que un técnico necesita leer para instalar un hogar con uplink, y el cierre del punto 3 del backlog.

**Files:**
- Modify: `README.md`
- Modify: `docs/como-funciona.md`
- Modify: `docs/como-probar.md`
- Modify: `docs/backlog.md`

**Interfaces:**
- Consumes: todo lo anterior. No produce nada que consuma otra tarea.

- [ ] **Step 1: Sección nueva en el `README.md`**

Añade después de la sección "Instalación en campo (por hogar)":

```markdown
## Uplink a AWS (opcional)

Con `uplink.enabled: true` en `hub.yaml`, el hub publica cada evento en AWS IoT Core
por MQTT con TLS mutuo, **además** de seguir escribiéndolos por stdout. El topic es
`vita/hub/<hub_id>/events` y el payload es el mismo JSON que ves en `docker logs`.

Antes de encenderlo hay que dar de alta el hogar en AWS. Desde el repo
`vitaplus-aws-architecture`:

```bash
./scripts/provision-hub.sh <hub_id>
```

Ese script crea el "thing", su certificado y su policy, y te imprime el endpoint. Copia
los tres ficheros que deja a `./data/certs/` del Jetson y ajusta permisos:

```bash
mkdir -p ./data/certs && chmod 700 ./data/certs && chmod 600 ./data/certs/private.pem.key
```

Después, en el arranque:

```bash
export VITAHUB_IOT_ENDPOINT=<el que imprimió provision-hub.sh>
docker compose up -d
```

Si algo falta, el hub **no arranca** y lo dice: es un error de instalación y estás
delante. Si el enlace se cae *después*, el hub sigue detectando y logueando con
normalidad; los eventos de ese rato **se pierden** (no hay cola — es una decisión
consciente, ver `docs/backlog.md`).

Certificado comprometido o Jetson perdido: se revoca ese hogar y solo ese, con
`aws iot update-certificate --new-status REVOKED --certificate-id <id>`.
```

- [ ] **Step 2: Actualiza `docs/como-funciona.md`**

Localiza la parte que describe qué se hace con los eventos una vez el `EventEngine` los
produce, y añade a continuación:

```markdown
### A dónde van los eventos

`build_sink` (en `factory.py`) decide el destino a partir de `uplink.enabled`:

- **Apagado** (por defecto): un `StdoutJsonSink` pelado, exactamente como antes de este
  slice.
- **Encendido**: un `FanoutSink` con dos destinos — el mismo `stdout` de siempre **y** un
  `AwsIotSink` que publica en `vita/hub/<hub_id>/events` de AWS IoT Core por MQTT con TLS
  mutuo.

Por MQTT sale byte a byte el mismo JSON que ves en `docker logs`: es `Event.to_json()` en
los dos casos, un solo esquema que mantener.

Ningún fallo del uplink tumba nada. Un `publish` que lanza se traga en el `AwsIotSink`, y
si aun así escapara, el `FanoutSink` lo aísla para que `stdout` reciba igual, y por encima
están el `try` de `_emit_all` y el del bucle de cámara. Un hogar sin internet sigue
detectando; lo único que pierde son los eventos de ese rato, porque no hay cola.
```

- [ ] **Step 3: Actualiza `docs/como-probar.md`**

Añade una sección con la prueba manual del uplink, y actualiza el recuento de tests y la
sección "Qué cubren y qué no":

```markdown
### Uplink a AWS

En CI no se prueba contra AWS: los tests del sink usan un cliente MQTT falso, así que
cubren el topic, el payload exacto, que un publish fallido no propaga y que el fanout
aísla fallos — **pero no** que el certificado sea válido, que la policy autorice el topic
ni que la regla escriba en DynamoDB. Eso solo se comprueba contra la cuenta real:

1. `uplink.enabled: true` con los certificados sembrados → en el log aparece
   `uplink habilitado hacia vita/hub/<hub_id>/events` y luego `uplink conectado a AWS IoT`.
2. Provoca un evento delante de una cámara → el ítem aparece en la tabla
   `vita-dev-hub-events` **y** la línea sigue saliendo en `docker logs`.
3. Desenchufa la red del Jetson → sigue detectando y logueando, no se reinicia; en el log
   sale `uplink desconectado`. Al volver la red, los eventos nuevos llegan otra vez.
```

- [ ] **Step 4: Cierra el punto 3 del backlog**

En `docs/backlog.md`, marca el punto 3 como hecho igual que se marcó el 1:

```markdown
### 3. ~~Uplink a AWS~~ — HECHO (slice de 2026-08-21)

Los eventos suben a AWS IoT Core por MQTT/TLS y se guardan en DynamoDB. Ver
`docs/superpowers/specs/2026-08-21-uplink-eventos-aws-design.md`.
```

Y añade a la sección "Robustez / calidad" los diferidos de la §8 del spec:

```markdown
### Diferidos del slice de uplink (2026-08-21)

- **Sin cola: lo que no sale, se pierde.** Decisión consciente del slice. Duele en la
  línea base personal, que es el núcleo del producto: un hueco de tres horas no es un dato
  perdido, es una rutina mal aprendida. Primer candidato del siguiente slice de uplink.
- **Downlink**: el rescan remoto necesita `Subscribe`/`Receive` en la policy de IoT, que
  hoy no se conceden. La costura sigue lista: `RescanService.run_once()`.
- **Eventos de presencia de IoT** (`$aws/events/presence/…`) para `hub_online` /
  `hub_offline`: es lo que cierra el punto 4 de este backlog (supervisión desde la nube de
  un hub colgado). Es configuración de cuenta, no un recurso del stack.
- **El estado del `ConnectionMonitor` sigue solo en memoria.** Este backlog ya avisaba de
  que pasa a importar "en cuanto exista el uplink a AWS" — y ya existe. Un episodio
  `camera_unreachable` que nunca se cierra ahora llega a una tabla que alguien consultará.
```

- [ ] **Step 5: Verifica la suite completa antes de cerrar**

Run: `pytest -v && ruff check . && mypy`
Expected: sin errores

- [ ] **Step 6: Commit**

```bash
git add README.md docs/como-funciona.md docs/como-probar.md docs/backlog.md
git commit -m "docs: instalación del uplink, prueba manual y cierre del backlog #3"
```
