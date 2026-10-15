# Vídeo en directo — lado hub — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Que el hub sirva vídeo en directo de sus cámaras a familiares y cuidadores por KVS WebRTC, verificando un token firmado por la nube y aplicando él mismo las reglas de sesión (una conexión por cuenta, tope de visores, caducidad).

**Architecture:** Un subsistema nuevo `src/vitahub/live/` corre en un hilo propio con su loop `asyncio`. Actúa como *master* del canal de señalización KVS de su hogar, con credenciales AWS temporales obtenidas del IoT credentials provider con el certificado del uplink. Cada viewer es una `RTCPeerConnection` de `aiortc` con un DataChannel `control`. La lógica de decisión (token, sesiones, protocolo, eventos) es pura y se prueba sin red; el resto se prueba en proceso con dobles. Con `live.enabled: false` (el default), el hub se comporta exactamente igual que hoy.

**Tech Stack:** Python 3.12, `aiortc` (WebRTC), `PyJWT[crypto]` (ES256), `botocore` (KVS y SigV4), `websockets` (señalización), pytest, ruff, mypy strict.

**Spec:** `docs/superpowers/specs/2026-10-15-video-en-directo-design.md`

## Global Constraints

- **Ante cualquier duda, gana el spec.** Este plan cubre §3 (contrato, lado hub), §4 y la parte del hub de §9.
- **Aclaración del spec (acordada con los planes de infra y app):** el nombre del canal es `<VITAHUB_LIVE_CHANNEL_PREFIX>-<hub_id>`, con el prefijo `vita-<env>-live`. El hub resuelve el ARN del canal por nombre con `DescribeSignalingChannel`, porque el ARN lleva un sufijo aleatorio; el rol `hub-live` incluye ese permiso.
- **Token:** JWT ES256, cabecera con `kid`; claims obligatorios `sid`, `sub`, `hub`, `iat`, `exp`. Válido = firma correcta con un `kid` conocido, `exp > now`, `hub == hub_id`.
- **DataChannel `control`:** mensajes JSON de una línea, exactamente los de spec §3.3. `reason` ∈ `expired | replaced | limit | invalid | camera_unavailable | shutdown`. `camera_unavailable` no cierra la conexión.
- **Plazos:** `auth` en 5 s desde que abre el DataChannel; un peer que no abre canal en 30 s se descarta.
- **Config:** `live.enabled` (false), `live.max_viewers` (3, rango 1–10), `live.max_fps` (15, rango 1–30), `live.region` (`eu-west-1`), `live.session_keys` (`kid` → PEM P-256). Entorno: `VITAHUB_IOT_CREDENTIALS_ENDPOINT`, `VITAHUB_LIVE_ROLE_ALIAS`, `VITAHUB_LIVE_CHANNEL_PREFIX`. Con `live.enabled: true` exige `uplink.enabled: true`.
- **Eventos:** `live_view_started` y `live_view_ended`, severidad `info`, por el sink normal. Payloads exactos de spec §4.5.
- **Aislamiento:** ningún fallo de `live/` puede afectar a la inferencia, los workers ni el sink. Fallos de red se reintentan con `backoff_delay`/`next_attempt` de `ingest/rtsp.py`.
- **El vídeo nunca se escribe a disco.**
- **Cero red externa en los tests.** Se permiten sockets locales en proceso (WebRTC entre dos `RTCPeerConnection` locales y un servidor `websockets` en `127.0.0.1`).
- **`mypy strict` y `ruff` limpios** al final de cada tarea. Los `except Exception` deliberados llevan `# noqa: BLE001` con el motivo en la misma línea.
- **Comentarios en castellano**, explicando el *por qué*, con el estilo de `config.py` y `app.py`.
- Comandos: `pytest`, `ruff check src tests`, `mypy` desde la raíz. Entorno: `pip install -e ".[dev]"`.

## Review Focus

1. **Un viewer que conecta y nunca envía `auth`** (o un cliente malicioso que solo abre WebRTC). Debe cerrarse a los 5 s sin haber recibido un solo frame de cámara. Lo fija `test_no_auth_closes_invalid_without_camera_frames` (Tarea 10).
2. **La misma cuenta renovando por la misma conexión** no debe contarse como sustitución ni chocar con el tope de visores. Lo fijan `test_renewal_on_same_connection_is_not_replacement` y `test_renewal_does_not_hit_limit` (Tarea 5).
3. **Una cámara que no abre el substream** (apagada, slot RTSP agotado) no debe tumbar la sesión ni el servicio: se responde `camera_unavailable` y el viewer puede elegir otra. Lo fija `test_select_unavailable_camera_keeps_connection` (Tarea 10).
4. **Señalización caída** (WSS cortado, credenciales caducadas, `GO_AWAY`): las sesiones P2P ya establecidas siguen vivas y el servicio reconecta con backoff. Lo fija `test_signaling_drop_keeps_existing_viewer` (Tarea 10).
5. **Apagado del hub con viewers conectados:** todos reciben `closed:shutdown`, se emiten sus `live_view_ended` **antes** de cerrar el sink y `stop()` no excede su plazo. Lo fijan `test_stop_sends_shutdown_and_emits_ended` (Tarea 10) y `test_shutdown_stops_live_before_sink` (Tarea 11).

## File Structure

| Fichero | Responsabilidad |
|---|---|
| `src/vitahub/live/__init__.py` | Paquete vacío. |
| `src/vitahub/live/token.py` | Carga de claves públicas y verificación del JWT. Pura. |
| `src/vitahub/live/protocol.py` | Parseo y serialización de mensajes del DataChannel. Pura. |
| `src/vitahub/live/events.py` | Construcción de `live_view_started`/`live_view_ended`. Pura. |
| `src/vitahub/live/sessions.py` | `SessionBook`: admisión, renovación, sustitución, tope y caducidad. Pura. |
| `src/vitahub/live/credentials.py` | Credenciales AWS temporales vía IoT credentials provider, con caché. |
| `src/vitahub/live/signaling.py` | Códec de mensajes KVS, URL prefirmada y cliente WSS como master. |
| `src/vitahub/live/source.py` | Destinos en directo por cámara, pool de fuentes RTSP compartidas, limitador de fps y pista en reposo. |
| `src/vitahub/live/peer.py` | `ViewerPeer`: una `RTCPeerConnection` por viewer con su DataChannel. |
| `src/vitahub/live/service.py` | `LiveService`: orquesta todo; `start()`/`stop()`. |
| `src/vitahub/config.py` | Gana `LiveConfig` y `_live_from_raw`. |
| `src/vitahub/factory.py` | Gana `build_live_service`. |
| `src/vitahub/app.py` | Arranca y para el servicio en orden. |
| `scripts/spike_live_cpu.py` | Spike de CPU (Tarea 1). Se borra al cerrar la tarea. |

---

### Task 1: Spike de CPU de `aiortc` en el Jetson

Decide la librería WebRTC (spec §4.3). **Es throwaway**: lo único que queda en el repo es el resultado anotado en el spec. Requiere el Jetson del piloto con una cámara real; lo ejecuta una persona, no un subagente sin hardware.

**Files:**
- Create (temporal): `scripts/spike_live_cpu.py`
- Modify: `docs/superpowers/specs/2026-10-15-video-en-directo-design.md` (§4.3, resultado)

**Interfaces:**
- Consumes: nada.
- Produces: la decisión `aiortc` / `webrtcbin`, anotada en el spec. Si sale `webrtcbin`, **parar** y replanificar las Tareas 8–10 antes de seguir.

- [ ] **Step 1: Escribe el script del spike**

```python
"""Spike throwaway: CPU de aiortc recodificando el substream para N viewers locales.

Uso (dentro del contenedor del hub en el Jetson, con la inferencia corriendo):
    pip install aiortc
    python scripts/spike_live_cpu.py 'rtsp://user:pass@192.168.1.190:8554/sub' 3 60
"""
from __future__ import annotations

import asyncio
import sys
import time

from aiortc import RTCPeerConnection
from aiortc.contrib.media import MediaPlayer, MediaRelay


async def _pair(track) -> RTCPeerConnection:  # type: ignore[no-untyped-def]
    sender, receiver = RTCPeerConnection(), RTCPeerConnection()
    sender.addTrack(track)
    frames = 0

    @receiver.on("track")
    def _on_track(t):  # type: ignore[no-untyped-def]
        async def _drain() -> None:
            nonlocal frames
            while True:
                await t.recv()
                frames += 1

        asyncio.ensure_future(_drain())

    await sender.setLocalDescription(await sender.createOffer())
    await receiver.setRemoteDescription(sender.localDescription)
    await receiver.setLocalDescription(await receiver.createAnswer())
    await sender.setRemoteDescription(receiver.localDescription)
    return sender


async def main(url: str, viewers: int, seconds: int) -> None:
    player = MediaPlayer(url, format="rtsp", options={"rtsp_transport": "tcp"})
    relay = MediaRelay()
    pcs = [await _pair(relay.subscribe(player.video, buffered=False)) for _ in range(viewers)]
    cpu0, wall0 = time.process_time(), time.monotonic()
    await asyncio.sleep(seconds)
    cpu, wall = time.process_time() - cpu0, time.monotonic() - wall0
    print(f"viewers={viewers} cpu={100 * cpu / wall:.0f}% de un núcleo")
    for pc in pcs:
        await pc.close()


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1], int(sys.argv[2]), int(sys.argv[3])))
```

- [ ] **Step 2: Ejecútalo en el Jetson con la inferencia activa**

Con el hub corriendo (`docker compose up -d`), dentro del contenedor:

```bash
docker compose exec hub sh -c "pip install aiortc && python scripts/spike_live_cpu.py '<url substream con credenciales>' 1 60"
docker compose exec hub python scripts/spike_live_cpu.py '<url>' 2 60
docker compose exec hub python scripts/spike_live_cpu.py '<url>' 3 60
```

Anota además `tegrastats` durante cada ejecución (CPU total del sistema).
Criterio: **con 3 viewers, la CPU total del sistema por debajo del 80 %** y el heartbeat del hub sin saltos. Si se cumple, `aiortc`. Si no, `webrtcbin`.

- [ ] **Step 3: Anota el resultado en el spec y borra el script**

Añade al final de §4.3 del spec un párrafo `**Resultado del spike (fecha):**` con las tres mediciones y la decisión.

```bash
rm scripts/spike_live_cpu.py
git add docs/superpowers/specs/2026-10-15-video-en-directo-design.md
git commit -m "docs: resultado del spike de CPU de aiortc en el Jetson"
```

---

### Task 2: Verificación del token de sesión

**Files:**
- Modify: `pyproject.toml` (dependencias y overrides de mypy)
- Create: `src/vitahub/live/__init__.py`, `src/vitahub/live/token.py`
- Test: `tests/test_live_token.py`

**Interfaces:**
- Consumes: nada.
- Produces:
  - `SessionKeyError(ValueError)`
  - `load_session_keys(raw: Mapping[str, str]) -> dict[str, ec.EllipticCurvePublicKey]`
  - `SessionClaims(sid: str, sub: str, hub: str, exp: float)` (frozen)
  - `TokenRejected(reason: str)` (frozen); `reason` ∈ `malformed | unknown_kid | bad_signature | expired | wrong_hub`
  - `verify_token(token: str, keys: Mapping[str, ec.EllipticCurvePublicKey], hub_id: str, now: float) -> SessionClaims | TokenRejected`

- [ ] **Step 1: Añade las dependencias de todo el slice**

En `pyproject.toml`, añade a `dependencies`:

```toml
    "aiortc>=1.9",
    "PyJWT[crypto]>=2.8",
    "botocore>=1.34",
    "websockets>=13",
```

Y una sección de overrides de mypy para las librerías sin tipos (detrás de la existente):

```toml
[[tool.mypy.overrides]]
module = ["aiortc.*", "av.*", "botocore.*", "aioice.*"]
ignore_missing_imports = true
follow_imports = "skip"
```

Run: `pip install -e ".[dev]"`
Expected: instala sin errores.

- [ ] **Step 2: Escribe el test que falla**

Crea `src/vitahub/live/__init__.py` vacío y `tests/test_live_token.py`:

```python
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa

from vitahub.live.token import (
    SessionClaims,
    SessionKeyError,
    TokenRejected,
    load_session_keys,
    verify_token,
)

_PRIV = ec.generate_private_key(ec.SECP256R1())
_OTHER = ec.generate_private_key(ec.SECP256R1())


def _pem(key: ec.EllipticCurvePrivateKey) -> str:
    return (
        key.public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        .decode()
    )


_KEYS = load_session_keys({"k1": _pem(_PRIV)})


def _token(key: ec.EllipticCurvePrivateKey = _PRIV, kid: str = "k1", **over: object) -> str:
    claims: dict[str, object] = {
        "sid": "s-1", "sub": "acc-1", "hub": "hub-1", "iat": 1000, "exp": 1600,
    }
    claims.update(over)
    return jwt.encode(claims, key, algorithm="ES256", headers={"kid": kid})


def test_valid_token_returns_claims():
    assert verify_token(_token(), _KEYS, "hub-1", now=1100) == SessionClaims(
        sid="s-1", sub="acc-1", hub="hub-1", exp=1600.0
    )


def test_unknown_kid_is_rejected():
    assert verify_token(_token(kid="k9"), _KEYS, "hub-1", 1100) == TokenRejected("unknown_kid")


def test_signature_from_another_key_is_rejected():
    # Mismo kid, otra clave: el caso de un token fabricado por quien no tiene KMS.
    assert verify_token(_token(key=_OTHER), _KEYS, "hub-1", 1100) == TokenRejected(
        "bad_signature"
    )


def test_expired_token_is_rejected():
    assert verify_token(_token(), _KEYS, "hub-1", now=1600) == TokenRejected("expired")


def test_token_for_another_hub_is_rejected():
    # Un token válido de otra casa no abre esta: credenciales de viewer y token
    # van acotados al mismo hub, pero el hub no se fía y lo comprueba.
    assert verify_token(_token(), _KEYS, "hub-2", 1100) == TokenRejected("wrong_hub")


@pytest.mark.parametrize("raw", ["", "abc", "a.b.c"])
def test_garbage_is_malformed(raw: str):
    assert verify_token(raw, _KEYS, "hub-1", 1100) == TokenRejected("malformed")


def test_missing_claim_is_malformed():
    token = jwt.encode(
        {"sub": "acc-1", "hub": "hub-1", "iat": 1000, "exp": 1600},
        _PRIV, algorithm="ES256", headers={"kid": "k1"},
    )
    assert verify_token(token, _KEYS, "hub-1", 1100) == TokenRejected("malformed")


def test_hs256_with_known_kid_is_malformed():
    # Confusión de algoritmo: un HS256 firmado con el PEM público como secreto
    # no debe pasar nunca. algorithms=["ES256"] lo cierra.
    token = jwt.encode(
        {"sid": "s", "sub": "a", "hub": "hub-1", "iat": 1, "exp": 9999},
        "secreto", algorithm="HS256", headers={"kid": "k1"},
    )
    assert verify_token(token, _KEYS, "hub-1", 1100) == TokenRejected("malformed")


def test_load_rejects_unreadable_pem():
    with pytest.raises(SessionKeyError, match="k1"):
        load_session_keys({"k1": "-----BEGIN PUBLIC KEY-----\nbasura\n-----END PUBLIC KEY-----"})


def test_load_rejects_non_p256_key():
    rsa_pem = (
        rsa.generate_private_key(public_exponent=65537, key_size=2048)
        .public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        .decode()
    )
    with pytest.raises(SessionKeyError, match="P-256"):
        load_session_keys({"k1": rsa_pem})
```

- [ ] **Step 3: Ejecuta el test y comprueba que falla**

Run: `pytest tests/test_live_token.py -v`
Expected: FAIL con `ModuleNotFoundError: No module named 'vitahub.live.token'`.

- [ ] **Step 4: Implementa `token.py`**

```python
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

import jwt
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import load_pem_public_key

_REQUIRED = ["sid", "sub", "hub", "iat", "exp"]


class SessionKeyError(ValueError):
    pass


@dataclass(frozen=True)
class SessionClaims:
    sid: str
    sub: str
    hub: str
    exp: float


@dataclass(frozen=True)
class TokenRejected:
    # malformed | unknown_kid | bad_signature | expired | wrong_hub. Solo para
    # logs: al viewer le llega siempre `closed:invalid`, sin pistas de por qué.
    reason: str


def load_session_keys(raw: Mapping[str, str]) -> dict[str, ec.EllipticCurvePublicKey]:
    """PEM por `kid` → claves P-256. Falla con el `kid` en el mensaje.

    Se llama desde `load_config`: una clave mal pegada en hub.yaml durante la
    instalación debe parar el arranque con un mensaje que diga cuál, no
    descubrirse cuando el primer familiar intente ver y reciba `invalid`.
    """
    keys: dict[str, ec.EllipticCurvePublicKey] = {}
    for kid, pem in raw.items():
        try:
            key = load_pem_public_key(pem.encode())
        except ValueError as exc:
            raise SessionKeyError(f"clave de sesión {kid!r}: PEM ilegible ({exc})") from exc
        if not isinstance(key, ec.EllipticCurvePublicKey) or not isinstance(
            key.curve, ec.SECP256R1
        ):
            raise SessionKeyError(f"clave de sesión {kid!r}: no es una clave pública P-256")
        keys[kid] = key
    return keys


def verify_token(
    token: str,
    keys: Mapping[str, ec.EllipticCurvePublicKey],
    hub_id: str,
    now: float,
) -> SessionClaims | TokenRejected:
    try:
        header = jwt.get_unverified_header(token)
    except jwt.InvalidTokenError:
        return TokenRejected("malformed")
    key = keys.get(str(header.get("kid", "")))
    if key is None:
        return TokenRejected("unknown_kid")
    try:
        # exp se comprueba a mano contra `now` inyectado: PyJWT usaría el reloj
        # del sistema y el test de caducidad dependería de la hora real.
        payload = jwt.decode(
            token,
            key,
            algorithms=["ES256"],
            options={"verify_exp": False, "verify_iat": False, "require": _REQUIRED},
        )
    except jwt.InvalidSignatureError:
        return TokenRejected("bad_signature")
    except jwt.InvalidTokenError:
        return TokenRejected("malformed")
    if payload["hub"] != hub_id:
        return TokenRejected("wrong_hub")
    exp = float(payload["exp"])
    if exp <= now:
        return TokenRejected("expired")
    return SessionClaims(
        sid=str(payload["sid"]), sub=str(payload["sub"]), hub=str(payload["hub"]), exp=exp
    )
```

- [ ] **Step 5: Ejecuta los tests y comprueba que pasan**

Run: `pytest tests/test_live_token.py -v && ruff check src tests && mypy`
Expected: PASS, sin errores de ruff ni mypy.

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml src/vitahub/live/__init__.py src/vitahub/live/token.py tests/test_live_token.py
git commit -m "feat: verificación del token de sesión de vídeo en directo (ES256)"
```

---

### Task 3: Sección `live` de la config

**Files:**
- Modify: `src/vitahub/config.py`
- Modify: `config/hub.example.yaml`
- Test: `tests/test_config.py`

**Interfaces:**
- Consumes: `load_session_keys`, `SessionKeyError` de `vitahub.live.token`.
- Produces: `LiveConfig` con `enabled: bool`, `max_viewers: int`, `max_fps: float`, `region: str`, `session_keys: dict[str, str]` (PEM ya validados), `credentials_endpoint: str`, `role_alias: str`, `channel_prefix: str`. `HubConfig.live: LiveConfig`.

- [ ] **Step 1: Escribe los tests que fallan**

Añade al final de `tests/test_config.py` (reutiliza `VALID_YAML`, `ENV` y `_certs`, que ya existen):

```python
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

_LIVE_PEM = (
    ec.generate_private_key(ec.SECP256R1())
    .public_key()
    .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
    .decode()
)


def _live_yaml(live_block: str, uplink: bool = True) -> str:
    up = "uplink:\n  enabled: true\n" if uplink else ""
    return VALID_YAML + up + live_block


def _live_block(**over: str) -> str:
    pem = "\n".join("      " + line for line in _LIVE_PEM.strip().splitlines())
    fields = {"enabled": "true", "max_viewers": "3", "max_fps": "15"} | over
    body = "".join(f"  {k}: {v}\n" for k, v in fields.items())
    return f"live:\n{body}  session_keys:\n    k1: |\n{pem}\n"


def _live_env(tmp_path: Path) -> dict[str, str]:
    env = _certs(tmp_path)
    env["VITAHUB_IOT_CREDENTIALS_ENDPOINT"] = "abc.credentials.iot.eu-west-1.amazonaws.com"
    env["VITAHUB_LIVE_ROLE_ALIAS"] = "vita-dev-hub-live"
    env["VITAHUB_LIVE_CHANNEL_PREFIX"] = "vita-dev-live"
    return env


def test_live_disabled_by_default(tmp_path):
    cfg = load_config(_write(tmp_path, VALID_YAML), ENV)
    assert cfg.live.enabled is False


def test_live_enabled_loads(tmp_path):
    cfg = load_config(_write(tmp_path, _live_yaml(_live_block())), _live_env(tmp_path))
    assert cfg.live.enabled is True
    assert cfg.live.max_viewers == 3
    assert cfg.live.max_fps == 15.0
    assert cfg.live.region == "eu-west-1"
    assert list(cfg.live.session_keys) == ["k1"]
    assert cfg.live.channel_prefix == "vita-dev-live"


def test_live_requires_uplink(tmp_path):
    # El hub saca las credenciales AWS del certificado del uplink: sin uplink
    # no hay certificado sembrado y el directo no podría autenticarse.
    with pytest.raises(ConfigError, match="uplink"):
        load_config(_write(tmp_path, _live_yaml(_live_block(), uplink=False)), _live_env(tmp_path))


@pytest.mark.parametrize(
    "var",
    ["VITAHUB_IOT_CREDENTIALS_ENDPOINT", "VITAHUB_LIVE_ROLE_ALIAS", "VITAHUB_LIVE_CHANNEL_PREFIX"],
)
def test_live_requires_env(tmp_path, var):
    env = _live_env(tmp_path)
    del env[var]
    with pytest.raises(ConfigError, match=var):
        load_config(_write(tmp_path, _live_yaml(_live_block())), env)


def test_live_requires_session_keys(tmp_path):
    block = "live:\n  enabled: true\n"
    with pytest.raises(ConfigError, match="session_keys"):
        load_config(_write(tmp_path, _live_yaml(block)), _live_env(tmp_path))


def test_live_rejects_bad_pem(tmp_path):
    block = "live:\n  enabled: true\n  session_keys:\n    k1: basura\n"
    with pytest.raises(ConfigError, match="k1"):
        load_config(_write(tmp_path, _live_yaml(block)), _live_env(tmp_path))


@pytest.mark.parametrize(
    "field,value", [("max_viewers", "0"), ("max_viewers", "11"), ("max_fps", "0"), ("max_fps", "31"),
                    ("max_viewers", "tres")],
)
def test_live_rejects_out_of_range(tmp_path, field, value):
    with pytest.raises(ConfigError, match=field):
        load_config(_write(tmp_path, _live_yaml(_live_block(**{field: value}))), _live_env(tmp_path))


def test_live_section_must_be_mapping(tmp_path):
    with pytest.raises(ConfigError, match="live"):
        load_config(_write(tmp_path, VALID_YAML + "live: [1]\n"), ENV)
```

- [ ] **Step 2: Ejecuta y comprueba que fallan**

Run: `pytest tests/test_config.py -k live -v`
Expected: FAIL con `AttributeError: 'HubConfig' object has no attribute 'live'`.

- [ ] **Step 3: Implementa `LiveConfig`**

En `src/vitahub/config.py`, importa `from vitahub.live.token import SessionKeyError, load_session_keys` junto a los imports existentes. Detrás de `UplinkConfig`:

```python
@dataclass
class LiveConfig:
    enabled: bool = False
    max_viewers: int = 3
    max_fps: float = 15.0
    region: str = "eu-west-1"
    # kid -> PEM. Se guardan los PEM (no los objetos clave) para que la config
    # siga siendo datos planos; `load_config` ya los ha validado.
    session_keys: dict[str, str] = field(default_factory=dict)
    credentials_endpoint: str = ""
    role_alias: str = ""
    channel_prefix: str = ""
```

Añade `live: LiveConfig = field(default_factory=LiveConfig)` como último campo de `HubConfig`.

Detrás de `_uplink_from_raw`:

```python
# KVS admite hasta 10 viewers por canal; por encima de eso la señalización los
# rechaza igualmente, pero con un error opaco en vez de `closed:limit`.
_KVS_MAX_VIEWERS = 10


def _live_from_raw(raw: object, env: Mapping[str, str], uplink: UplinkConfig) -> LiveConfig:
    """Lee la sección `live`. Mismo criterio fail-fast que `_uplink_from_raw`.

    El técnico que activa el directo está delante del Jetson: una clave mal
    pegada o una variable que falta debe parar el arranque con un mensaje que
    diga qué arreglar, no dejar un hogar donde el botón "Ver cámaras" falla
    siempre sin explicación.
    """
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ConfigError("Sección 'live' debe ser un mapping YAML")
    enabled = bool(raw.get("enabled", False))
    try:
        max_viewers = int(raw.get("max_viewers", 3))
    except (ValueError, TypeError) as exc:
        raise ConfigError("Campo 'live.max_viewers' no es un entero") from exc
    if not 1 <= max_viewers <= _KVS_MAX_VIEWERS:
        raise ConfigError(f"Campo 'live.max_viewers' debe estar entre 1 y {_KVS_MAX_VIEWERS}")
    try:
        max_fps = float(raw.get("max_fps", 15))
    except (ValueError, TypeError) as exc:
        raise ConfigError("Campo 'live.max_fps' no es numérico") from exc
    if not 1 <= max_fps <= 30:
        raise ConfigError("Campo 'live.max_fps' debe estar entre 1 y 30")
    region = raw.get("region", "eu-west-1")
    if not isinstance(region, str) or not region:
        raise ConfigError("Campo 'live.region' debe ser una cadena no vacía")
    if not enabled:
        return LiveConfig(enabled=False, max_viewers=max_viewers, max_fps=max_fps, region=region)

    if not uplink.enabled:
        raise ConfigError(
            "live.enabled requiere 'uplink.enabled: true': el directo obtiene sus "
            "credenciales AWS con el certificado del uplink"
        )
    keys_raw = raw.get("session_keys")
    if not isinstance(keys_raw, dict) or not keys_raw:
        raise ConfigError(
            "live.enabled es true pero 'live.session_keys' está vacío — pega la clave "
            "que imprime scripts/live-public-key.sh del repo de arquitectura"
        )
    session_keys = {str(k): str(v) for k, v in keys_raw.items()}
    try:
        load_session_keys(session_keys)
    except SessionKeyError as exc:
        raise ConfigError(f"live.session_keys: {exc}") from exc

    env_values: dict[str, str] = {}
    for var in (
        "VITAHUB_IOT_CREDENTIALS_ENDPOINT",
        "VITAHUB_LIVE_ROLE_ALIAS",
        "VITAHUB_LIVE_CHANNEL_PREFIX",
    ):
        value = env.get(var, "")
        if not value:
            raise ConfigError(
                f"live.enabled es true pero falta la variable de entorno {var} "
                "(la imprime scripts/provision-hub.sh del repo de arquitectura)"
            )
        env_values[var] = value

    return LiveConfig(
        enabled=True,
        max_viewers=max_viewers,
        max_fps=max_fps,
        region=region,
        session_keys=session_keys,
        credentials_endpoint=env_values["VITAHUB_IOT_CREDENTIALS_ENDPOINT"],
        role_alias=env_values["VITAHUB_LIVE_ROLE_ALIAS"],
        channel_prefix=env_values["VITAHUB_LIVE_CHANNEL_PREFIX"],
    )
```

En `load_config`, sustituye el `return HubConfig(...)` final por:

```python
    detector = str(inf_raw.get("detector", "person_yolo"))
    fall = _fall_from_raw(inf_raw.get("fall"), detector)
    uplink = _uplink_from_raw(up_raw, env)
    return HubConfig(
        hub_id=hub_id,
        discovery=DiscoveryConfig(interval_seconds=interval_seconds),
        inference=InferenceConfig(
            detector=detector,
            sample_fps=sample_fps,
            confidence=confidence,
            stream=stream,
            fall=fall,
            identity=_identity_from_raw(inf_raw.get("identity"), fall),
        ),
        cameras=_cameras_from_raw(raw.get("cameras") or []),
        credentials=credentials,
        uplink=uplink,
        live=_live_from_raw(raw.get("live"), env, uplink),
    )
```

(Las dos primeras líneas ya existen; no las dupliques.)

- [ ] **Step 4: Documenta la sección en `config/hub.example.yaml`**

Detrás del bloque `uplink:` y su comentario de variables:

```yaml
live:
  enabled: false            # true para el vídeo en directo desde la app (requiere uplink.enabled)
  max_viewers: 3            # visores simultáneos como máximo (1-10); cada uno es un flujo de subida
  max_fps: 15               # fps máximos del directo
  region: eu-west-1
  session_keys: {}          # kid -> clave pública PEM; la imprime scripts/live-public-key.sh
# Con enabled: true hacen falta además estas variables de entorno:
#   VITAHUB_IOT_CREDENTIALS_ENDPOINT, VITAHUB_LIVE_ROLE_ALIAS, VITAHUB_LIVE_CHANNEL_PREFIX
#   (las imprime provision-hub.sh del repo de arquitectura)
```

- [ ] **Step 5: Ejecuta los tests**

Run: `pytest tests/test_config.py tests/test_config_edit.py -v && ruff check src tests && mypy`
Expected: PASS (los tests previos de config también: un hub.yaml sin `live` sigue cargando).

- [ ] **Step 6: Commit**

```bash
git add src/vitahub/config.py config/hub.example.yaml tests/test_config.py
git commit -m "feat: sección live de la config con validación fail-fast"
```

---

### Task 4: Protocolo del DataChannel y eventos de directo

**Files:**
- Create: `src/vitahub/live/protocol.py`, `src/vitahub/live/events.py`
- Test: `tests/test_live_protocol.py`, `tests/test_live_events.py`

**Interfaces:**
- Consumes: `Event` de `vitahub.models`; `SessionClaims` de `vitahub.live.token`.
- Produces:
  - `CLOSE_REASONS: frozenset[str]`
  - `AuthMsg(token: str)`, `SelectMsg(camera_id: str)` (frozen); `ClientMessage = AuthMsg | SelectMsg`
  - `parse_client_message(raw: str | bytes) -> ClientMessage | None`
  - `ready_message(cameras: list[tuple[str, str]]) -> str` (pares `(id, name)`)
  - `closed_message(reason: str) -> str` (lanza `ValueError` si `reason` no está en `CLOSE_REASONS`)
  - `live_view_started(hub_id: str, claims: SessionClaims, camera_id: str, camera_name: str, now: datetime) -> Event`
  - `live_view_ended(hub_id: str, claims: SessionClaims, camera_id: str, camera_name: str, reason: str, seconds: float, now: datetime) -> Event`

- [ ] **Step 1: Escribe los tests que fallan**

`tests/test_live_protocol.py`:

```python
import json

import pytest

from vitahub.live.protocol import (
    AuthMsg,
    SelectMsg,
    closed_message,
    parse_client_message,
    ready_message,
)


def test_parses_auth():
    assert parse_client_message('{"type":"auth","token":"t"}') == AuthMsg("t")


def test_parses_select_from_bytes():
    assert parse_client_message(b'{"type":"select","camera_id":"onvif-a"}') == SelectMsg("onvif-a")


@pytest.mark.parametrize(
    "raw",
    [
        "no es json",
        "[]",
        '{"type":"auth"}',
        '{"type":"auth","token":5}',
        '{"type":"select","camera_id":""}',
        '{"type":"desconocido"}',
        b"\xff\xfe",
    ],
)
def test_garbage_returns_none(raw):
    # Un viewer con un bug (o malicioso) no puede tumbar el peer con un mensaje raro.
    assert parse_client_message(raw) is None


def test_ready_lists_cameras():
    msg = json.loads(ready_message([("onvif-a", "salón")]))
    assert msg == {"type": "ready", "cameras": [{"id": "onvif-a", "name": "salón"}]}


def test_closed_message():
    assert json.loads(closed_message("replaced")) == {"type": "closed", "reason": "replaced"}


def test_closed_rejects_unknown_reason():
    # El frontend traduce `reason` a texto: una errata aquí sería un mensaje vacío en la app.
    with pytest.raises(ValueError):
        closed_message("expirado")
```

`tests/test_live_events.py`:

```python
from datetime import UTC, datetime

from vitahub.live.events import live_view_ended, live_view_started
from vitahub.live.token import SessionClaims

_CLAIMS = SessionClaims(sid="s-1", sub="acc-1", hub="hub-1", exp=1600.0)
_NOW = datetime(2026, 10, 15, 10, 0, tzinfo=UTC)


def test_started_event():
    ev = live_view_started("hub-1", _CLAIMS, "onvif-a", "salón", _NOW)
    assert ev.type == "live_view_started"
    assert ev.severity == "info"
    assert ev.camera_id == "onvif-a"
    assert ev.camera_name == "salón"
    assert ev.timestamp == _NOW.isoformat()
    assert ev.payload == {"sid": "s-1", "account_id": "acc-1", "camera_id": "onvif-a"}


def test_ended_event_without_camera():
    # Un viewer que se autentica y se va sin elegir cámara: camera_id vacío (spec §4.5).
    ev = live_view_ended("hub-1", _CLAIMS, "", "", "expired", 12.34, _NOW)
    assert ev.type == "live_view_ended"
    assert ev.camera_id == ""
    assert ev.payload == {"sid": "s-1", "account_id": "acc-1", "reason": "expired", "seconds": 12}
```

- [ ] **Step 2: Ejecuta y comprueba que fallan**

Run: `pytest tests/test_live_protocol.py tests/test_live_events.py -v`
Expected: FAIL con `ModuleNotFoundError`.

- [ ] **Step 3: Implementa `protocol.py`**

```python
from __future__ import annotations

import json
from dataclasses import dataclass

# Contrato con el frontend (spec §3.3). Cambiar algo aquí exige cambiar
# `LiveCloseReason` en shared/ de vita-web-family-app.
CLOSE_REASONS = frozenset(
    {"expired", "replaced", "limit", "invalid", "camera_unavailable", "shutdown"}
)


@dataclass(frozen=True)
class AuthMsg:
    token: str


@dataclass(frozen=True)
class SelectMsg:
    camera_id: str


ClientMessage = AuthMsg | SelectMsg


def parse_client_message(raw: str | bytes) -> ClientMessage | None:
    """Mensaje del viewer → tipo, o None si no es un mensaje válido del contrato."""
    try:
        data = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    kind = data.get("type")
    if kind == "auth":
        token = data.get("token")
        return AuthMsg(token) if isinstance(token, str) and token else None
    if kind == "select":
        camera_id = data.get("camera_id")
        return SelectMsg(camera_id) if isinstance(camera_id, str) and camera_id else None
    return None


def ready_message(cameras: list[tuple[str, str]]) -> str:
    return json.dumps(
        {"type": "ready", "cameras": [{"id": i, "name": n} for i, n in cameras]},
        ensure_ascii=False,
    )


def closed_message(reason: str) -> str:
    if reason not in CLOSE_REASONS:
        raise ValueError(f"reason fuera del contrato: {reason!r}")
    return json.dumps({"type": "closed", "reason": reason})
```

- [ ] **Step 4: Implementa `events.py`**

```python
from __future__ import annotations

from datetime import datetime

from vitahub.live.token import SessionClaims
from vitahub.models import Event


def live_view_started(
    hub_id: str, claims: SessionClaims, camera_id: str, camera_name: str, now: datetime
) -> Event:
    """Rastro de auditoría del lado hub (spec §8), independiente del audit_log de la API."""
    return Event(
        hub_id=hub_id,
        camera_id=camera_id,
        camera_name=camera_name,
        type="live_view_started",
        severity="info",
        timestamp=now.isoformat(),
        payload={"sid": claims.sid, "account_id": claims.sub, "camera_id": camera_id},
    )


def live_view_ended(
    hub_id: str,
    claims: SessionClaims,
    camera_id: str,
    camera_name: str,
    reason: str,
    seconds: float,
    now: datetime,
) -> Event:
    return Event(
        hub_id=hub_id,
        camera_id=camera_id,
        camera_name=camera_name,
        type="live_view_ended",
        severity="info",
        timestamp=now.isoformat(),
        payload={
            "sid": claims.sid,
            "account_id": claims.sub,
            "reason": reason,
            "seconds": int(seconds),
        },
    )
```

- [ ] **Step 5: Ejecuta los tests**

Run: `pytest tests/test_live_protocol.py tests/test_live_events.py -v && ruff check src tests && mypy`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/vitahub/live/protocol.py src/vitahub/live/events.py tests/test_live_protocol.py tests/test_live_events.py
git commit -m "feat: protocolo del DataChannel y eventos de auditoría del directo"
```

---

### Task 5: `SessionBook`

Las reglas de sesión de spec §3.4, sin red ni reloj propio.

**Files:**
- Create: `src/vitahub/live/sessions.py`
- Test: `tests/test_live_sessions.py`

**Interfaces:**
- Consumes: `SessionClaims` de `vitahub.live.token`.
- Produces:
  - `Admitted(renewed: bool, replaced: tuple[str, ...])` (frozen)
  - `Rejected(reason: str)` (frozen); `reason` ∈ `limit | invalid`
  - `SessionBook(max_viewers: int)` con:
    - `admit(conn_id: str, claims: SessionClaims) -> Admitted | Rejected`. Los `replaced` ya están fuera del libro cuando vuelve.
    - `release(conn_id: str) -> SessionClaims | None`
    - `claims_for(conn_id: str) -> SessionClaims | None`
    - `expired(now: float) -> list[str]`. No los quita: el llamador los cierra y llama a `release`.
    - `__len__() -> int`

- [ ] **Step 1: Escribe el test que falla**

```python
from vitahub.live.sessions import Admitted, Rejected, SessionBook
from vitahub.live.token import SessionClaims


def _c(sub: str, sid: str = "s", exp: float = 100.0) -> SessionClaims:
    return SessionClaims(sid=sid, sub=sub, hub="hub-1", exp=exp)


def test_admits_new_viewer():
    book = SessionBook(max_viewers=3)
    assert book.admit("c1", _c("a")) == Admitted(renewed=False, replaced=())
    assert len(book) == 1


def test_same_account_on_new_connection_replaces_old():
    book = SessionBook(max_viewers=3)
    book.admit("c1", _c("a", "s1"))
    assert book.admit("c2", _c("a", "s2")) == Admitted(renewed=False, replaced=("c1",))
    assert book.claims_for("c1") is None
    assert len(book) == 1


def test_renewal_on_same_connection_is_not_replacement():
    book = SessionBook(max_viewers=3)
    book.admit("c1", _c("a", "s1", exp=100))
    assert book.admit("c1", _c("a", "s2", exp=700)) == Admitted(renewed=True, replaced=())
    assert book.claims_for("c1") == _c("a", "s2", exp=700)


def test_renewal_does_not_hit_limit():
    book = SessionBook(max_viewers=1)
    book.admit("c1", _c("a"))
    assert isinstance(book.admit("c1", _c("a", "s2")), Admitted)


def test_other_account_on_same_connection_is_invalid():
    # Un token de otra cuenta por una conexión ya autenticada: alguien pasando
    # tokens ajenos. No se mezcla la sesión de dos personas.
    book = SessionBook(max_viewers=3)
    book.admit("c1", _c("a"))
    assert book.admit("c1", _c("b")) == Rejected("invalid")
    assert book.claims_for("c1") == _c("a")


def test_limit_rejects_new_account():
    book = SessionBook(max_viewers=2)
    book.admit("c1", _c("a"))
    book.admit("c2", _c("b"))
    assert book.admit("c3", _c("c")) == Rejected("limit")
    assert len(book) == 2


def test_replacement_frees_its_own_slot_at_limit():
    # Con el hogar lleno, la misma cuenta cambiando de móvil debe entrar: su
    # conexión vieja sale y deja hueco.
    book = SessionBook(max_viewers=2)
    book.admit("c1", _c("a"))
    book.admit("c2", _c("b"))
    assert book.admit("c3", _c("a")) == Admitted(renewed=False, replaced=("c1",))


def test_rejected_admission_keeps_existing_sessions():
    book = SessionBook(max_viewers=1)
    book.admit("c1", _c("a"))
    book.admit("c2", _c("b"))
    assert book.claims_for("c1") == _c("a")


def test_expired_lists_without_removing():
    book = SessionBook(max_viewers=3)
    book.admit("c1", _c("a", exp=100))
    book.admit("c2", _c("b", exp=200))
    assert book.expired(now=150) == ["c1"]
    assert len(book) == 2


def test_release_returns_claims_once():
    book = SessionBook(max_viewers=3)
    book.admit("c1", _c("a"))
    assert book.release("c1") == _c("a")
    assert book.release("c1") is None
```

- [ ] **Step 2: Ejecuta y comprueba que falla**

Run: `pytest tests/test_live_sessions.py -v`
Expected: FAIL con `ModuleNotFoundError`.

- [ ] **Step 3: Implementa `sessions.py`**

```python
from __future__ import annotations

from dataclasses import dataclass

from vitahub.live.token import SessionClaims


@dataclass(frozen=True)
class Admitted:
    renewed: bool
    replaced: tuple[str, ...]


@dataclass(frozen=True)
class Rejected:
    reason: str  # limit | invalid


class SessionBook:
    """Conexiones autenticadas del hub y sus reglas (spec §3.4).

    Solo cuenta conexiones que ya enviaron un `auth` válido: un peer que
    negocia WebRTC pero aún no se ha autenticado no ocupa plaza, para que
    nadie pueda llenar el hogar sin un token.
    """

    def __init__(self, max_viewers: int) -> None:
        self._max = max_viewers
        self._by_conn: dict[str, SessionClaims] = {}

    def __len__(self) -> int:
        return len(self._by_conn)

    def admit(self, conn_id: str, claims: SessionClaims) -> Admitted | Rejected:
        current = self._by_conn.get(conn_id)
        if current is not None:
            if current.sub != claims.sub:
                return Rejected("invalid")
            self._by_conn[conn_id] = claims
            return Admitted(renewed=True, replaced=())
        replaced = tuple(c for c, cl in self._by_conn.items() if cl.sub == claims.sub)
        # La plaza de la conexión que se sustituye cuenta como libre: se
        # decide el tope ANTES de sacar a nadie, para no expulsar a la sesión
        # vieja y luego rechazar la nueva.
        if len(self._by_conn) - len(replaced) >= self._max:
            return Rejected("limit")
        for c in replaced:
            del self._by_conn[c]
        self._by_conn[conn_id] = claims
        return Admitted(renewed=False, replaced=replaced)

    def release(self, conn_id: str) -> SessionClaims | None:
        return self._by_conn.pop(conn_id, None)

    def claims_for(self, conn_id: str) -> SessionClaims | None:
        return self._by_conn.get(conn_id)

    def expired(self, now: float) -> list[str]:
        return [c for c, cl in self._by_conn.items() if cl.exp <= now]
```

- [ ] **Step 4: Ejecuta los tests**

Run: `pytest tests/test_live_sessions.py -v && ruff check src tests && mypy`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/vitahub/live/sessions.py tests/test_live_sessions.py
git commit -m "feat: SessionBook — una conexión por cuenta, tope y caducidad"
```

---

### Task 6: Credenciales AWS vía IoT credentials provider

**Files:**
- Create: `src/vitahub/live/credentials.py`
- Test: `tests/test_live_credentials.py`

**Interfaces:**
- Consumes: nada del repo.
- Produces:
  - `AwsCredentials(access_key: str, secret_key: str, token: str, expiration: float)` (frozen; `expiration` en epoch)
  - `parse_credentials(body: bytes) -> AwsCredentials` (lanza `ValueError`)
  - `IotCredentialsProvider(fetch: Callable[[], bytes], clock: Callable[[], float] = time.time, refresh_margin_s: float = 300.0)` con `get() -> AwsCredentials`
  - `https_fetcher(endpoint: str, role_alias: str, thing_name: str, ca: Path, cert: Path, key: Path) -> Callable[[], bytes]`

- [ ] **Step 1: Escribe el test que falla**

```python
import json

import pytest

from vitahub.live.credentials import AwsCredentials, IotCredentialsProvider, parse_credentials


def _body(exp: str = "2026-10-15T10:00:00Z", key: str = "AK") -> bytes:
    return json.dumps(
        {"credentials": {"accessKeyId": key, "secretAccessKey": "SK",
                         "sessionToken": "TK", "expiration": exp}}
    ).encode()


def test_parse_credentials():
    creds = parse_credentials(_body())
    assert creds == AwsCredentials("AK", "SK", "TK", 1760522400.0)


@pytest.mark.parametrize("body", [b"no json", b"{}", b'{"credentials": {"accessKeyId": "AK"}}'])
def test_parse_rejects_incomplete(body):
    with pytest.raises(ValueError):
        parse_credentials(body)


def test_provider_caches_until_margin():
    calls: list[int] = []
    now = [1760522400.0 - 3600]

    def fetch() -> bytes:
        calls.append(1)
        return _body(key=f"AK{len(calls)}")

    provider = IotCredentialsProvider(fetch, clock=lambda: now[0], refresh_margin_s=300)
    assert provider.get().access_key == "AK1"
    now[0] = 1760522400.0 - 301
    assert provider.get().access_key == "AK1"
    # Dentro del margen se renueva: las credenciales que se firman ahora deben
    # seguir vivas mientras dura la conexión WSS que abren.
    now[0] = 1760522400.0 - 299
    assert provider.get().access_key == "AK2"
    assert len(calls) == 2
```

- [ ] **Step 2: Ejecuta y comprueba que falla**

Run: `pytest tests/test_live_credentials.py -v`
Expected: FAIL con `ModuleNotFoundError`.

- [ ] **Step 3: Implementa `credentials.py`**

```python
from __future__ import annotations

import json
import ssl
import threading
import time
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

_TIMEOUT_S = 10.0


@dataclass(frozen=True)
class AwsCredentials:
    access_key: str
    secret_key: str
    token: str
    expiration: float


def parse_credentials(body: bytes) -> AwsCredentials:
    try:
        creds = json.loads(body)["credentials"]
        return AwsCredentials(
            access_key=str(creds["accessKeyId"]),
            secret_key=str(creds["secretAccessKey"]),
            token=str(creds["sessionToken"]),
            expiration=datetime.fromisoformat(str(creds["expiration"])).timestamp(),
        )
    except (ValueError, KeyError, TypeError) as exc:
        raise ValueError(f"respuesta del credentials provider inválida: {exc}") from exc


class IotCredentialsProvider:
    """Credenciales temporales del rol `hub-live`, en caché hasta cerca de caducar.

    Sin secretos nuevos en el hogar: se piden con el mismo certificado X.509
    que ya usa el uplink (spec §3.1). Thread-safe porque las llamadas de
    botocore corren en el executor, no en el loop.
    """

    def __init__(
        self,
        fetch: Callable[[], bytes],
        clock: Callable[[], float] = time.time,
        refresh_margin_s: float = 300.0,
    ) -> None:
        self._fetch = fetch
        self._clock = clock
        self._margin = refresh_margin_s
        self._cached: AwsCredentials | None = None
        self._lock = threading.Lock()

    def get(self) -> AwsCredentials:
        with self._lock:
            if self._cached is None or self._cached.expiration - self._clock() <= self._margin:
                self._cached = parse_credentials(self._fetch())
            return self._cached


def https_fetcher(
    endpoint: str, role_alias: str, thing_name: str, ca: Path, cert: Path, key: Path
) -> Callable[[], bytes]:
    """GET al credentials provider con mTLS. Runtime only: requiere la cuenta real."""
    ctx = ssl.create_default_context(cafile=str(ca))
    ctx.load_cert_chain(certfile=str(cert), keyfile=str(key))
    url = f"https://{endpoint}/role-aliases/{role_alias}/credentials"

    def _fetch() -> bytes:
        req = urllib.request.Request(url, headers={"x-amzn-iot-thingname": thing_name})
        with urllib.request.urlopen(req, context=ctx, timeout=_TIMEOUT_S) as resp:
            body: bytes = resp.read()
            return body

    return _fetch
```

- [ ] **Step 4: Ejecuta los tests**

Run: `pytest tests/test_live_credentials.py -v && ruff check src tests && mypy`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/vitahub/live/credentials.py tests/test_live_credentials.py
git commit -m "feat: credenciales AWS del directo vía IoT credentials provider"
```

---

### Task 7: Señalización KVS como master

**Files:**
- Create: `src/vitahub/live/signaling.py`
- Test: `tests/test_live_signaling.py`

**Interfaces:**
- Consumes: `AwsCredentials`, `IotCredentialsProvider` (Tarea 6).
- Produces:
  - `SignalingMessage(type: str, client_id: str, payload: dict[str, object])` (frozen)
  - `SignalingClosed(Exception)`
  - `decode_message(raw: str | bytes) -> SignalingMessage | None`
  - `encode_answer(client_id: str, sdp: str) -> str`
  - `channel_name(prefix: str, hub_id: str) -> str`
  - `presign_wss_url(wss_endpoint: str, channel_arn: str, creds: AwsCredentials, region: str) -> str`
  - `ChannelEndpoints(arn: str, wss: str, ice_servers: list[RTCIceServer])` (frozen)
  - `resolve_endpoints(channel: str, region: str, creds: AwsCredentials) -> ChannelEndpoints` (botocore, runtime only)
  - `Signaling` (Protocol): atributo `ice_servers: list[RTCIceServer]`; `async open()`, `async recv() -> SignalingMessage` (lanza `SignalingClosed`), `async send_answer(client_id: str, sdp: str)`, `async close()`
  - `KvsSignaling(resolve: Callable[[], ChannelEndpoints], creds: IotCredentialsProvider, region: str)` implementa `Signaling`

- [ ] **Step 1: Escribe el test que falla**

```python
import asyncio
import base64
import json
from urllib.parse import parse_qs, urlsplit

from websockets.asyncio.server import ServerConnection, serve

from vitahub.live.credentials import AwsCredentials, IotCredentialsProvider
from vitahub.live.signaling import (
    ChannelEndpoints,
    KvsSignaling,
    SignalingMessage,
    channel_name,
    decode_message,
    encode_answer,
    presign_wss_url,
)

_CREDS = AwsCredentials("AKIDEXAMPLE", "SECRET", "TOKEN", 9_999_999_999.0)


def _b64(obj: object) -> str:
    return base64.b64encode(json.dumps(obj).encode()).decode()


def test_channel_name():
    assert channel_name("vita-dev-live", "hub-1") == "vita-dev-live-hub-1"


def test_decode_offer():
    raw = json.dumps({"messageType": "SDP_OFFER", "senderClientId": "v1",
                      "messagePayload": _b64({"type": "offer", "sdp": "v=0"})})
    assert decode_message(raw) == SignalingMessage(
        "SDP_OFFER", "v1", {"type": "offer", "sdp": "v=0"}
    )


def test_decode_go_away_without_payload():
    assert decode_message('{"messageType":"GO_AWAY"}') == SignalingMessage("GO_AWAY", "", {})


def test_decode_garbage_is_none():
    # KVS manda a veces frames vacíos (keepalive): no son errores.
    assert decode_message("") is None
    assert decode_message('{"messageType":"SDP_OFFER","messagePayload":"%%%"}') is None


def test_encode_answer():
    msg = json.loads(encode_answer("v1", "v=0"))
    assert msg["action"] == "SDP_ANSWER"
    assert msg["recipientClientId"] == "v1"
    assert json.loads(base64.b64decode(msg["messagePayload"])) == {"type": "answer", "sdp": "v=0"}


def test_presigned_url_carries_channel_and_signature():
    url = presign_wss_url("wss://v-1.kinesisvideo.eu-west-1.amazonaws.com",
                          "arn:aws:kinesisvideo:eu-west-1:1:channel/c/1", _CREDS, "eu-west-1")
    qs = parse_qs(urlsplit(url).query)
    assert qs["X-Amz-ChannelARN"] == ["arn:aws:kinesisvideo:eu-west-1:1:channel/c/1"]
    assert qs["X-Amz-Algorithm"] == ["AWS4-HMAC-SHA256"]
    assert qs["X-Amz-Security-Token"] == ["TOKEN"]
    assert "X-Amz-Signature" in qs


def test_kvs_signaling_against_local_server():
    async def scenario() -> None:
        received: list[str] = []

        async def handler(ws: ServerConnection) -> None:
            await ws.send(json.dumps({"messageType": "SDP_OFFER", "senderClientId": "v1",
                                      "messagePayload": _b64({"type": "offer", "sdp": "x"})}))
            received.append(str(await ws.recv()))

        async with serve(handler, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            body = json.dumps({"credentials": {
                "accessKeyId": "AK", "secretAccessKey": "SK", "sessionToken": "TK",
                "expiration": "2099-01-01T00:00:00Z"}}).encode()
            sig = KvsSignaling(
                resolve=lambda: ChannelEndpoints("arn:c", f"ws://127.0.0.1:{port}", []),
                creds=IotCredentialsProvider(lambda: body),
                region="eu-west-1",
            )
            await sig.open()
            msg = await sig.recv()
            assert msg.type == "SDP_OFFER" and msg.client_id == "v1"
            await sig.send_answer("v1", "y")
            await asyncio.sleep(0.05)
            await sig.close()
        assert json.loads(received[0])["recipientClientId"] == "v1"

    asyncio.run(scenario())
```

- [ ] **Step 2: Ejecuta y comprueba que falla**

Run: `pytest tests/test_live_signaling.py -v`
Expected: FAIL con `ModuleNotFoundError`.

- [ ] **Step 3: Implementa `signaling.py`**

```python
from __future__ import annotations

import asyncio
import base64
import binascii
import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import quote

from aiortc import RTCIceServer
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed

from vitahub.live.credentials import AwsCredentials, IotCredentialsProvider

# La URL prefirmada solo tiene que valer para el handshake: la conexión WSS
# ya abierta no caduca con ella.
_PRESIGN_EXPIRES_S = 300


@dataclass(frozen=True)
class SignalingMessage:
    type: str
    client_id: str
    payload: dict[str, object]


class SignalingClosed(Exception):
    pass


@dataclass(frozen=True)
class ChannelEndpoints:
    arn: str
    wss: str
    ice_servers: list[RTCIceServer]


def channel_name(prefix: str, hub_id: str) -> str:
    # Contrato con la policy IAM del rol hub-live (spec §3.1): no es configurable.
    return f"{prefix}-{hub_id}"


def decode_message(raw: str | bytes) -> SignalingMessage | None:
    try:
        data = json.loads(raw)
        if not isinstance(data, dict) or not isinstance(data.get("messageType"), str):
            return None
        payload: dict[str, object] = {}
        if data.get("messagePayload"):
            decoded = json.loads(base64.b64decode(data["messagePayload"], validate=True))
            if not isinstance(decoded, dict):
                return None
            payload = decoded
        return SignalingMessage(data["messageType"], str(data.get("senderClientId", "")), payload)
    except (ValueError, binascii.Error, UnicodeDecodeError):
        return None


def encode_answer(client_id: str, sdp: str) -> str:
    body = json.dumps({"type": "answer", "sdp": sdp}).encode()
    return json.dumps(
        {
            "action": "SDP_ANSWER",
            "messagePayload": base64.b64encode(body).decode(),
            "recipientClientId": client_id,
        }
    )


def presign_wss_url(
    wss_endpoint: str, channel_arn: str, creds: AwsCredentials, region: str
) -> str:
    from botocore.auth import SigV4QueryAuth
    from botocore.awsrequest import AWSRequest
    from botocore.credentials import Credentials

    url = f"{wss_endpoint.rstrip('/')}/?X-Amz-ChannelARN={quote(channel_arn, safe='')}"
    request = AWSRequest(method="GET", url=url)
    SigV4QueryAuth(
        Credentials(creds.access_key, creds.secret_key, creds.token),
        "kinesisvideo",
        region,
        expires=_PRESIGN_EXPIRES_S,
    ).add_auth(request)
    signed: str = request.url
    return signed


def resolve_endpoints(channel: str, region: str, creds: AwsCredentials) -> ChannelEndpoints:
    """ARN, endpoint WSS y servidores ICE (TURN) del canal. Runtime only: llama a AWS."""
    import botocore.session

    session = botocore.session.get_session()
    keys = {
        "aws_access_key_id": creds.access_key,
        "aws_secret_access_key": creds.secret_key,
        "aws_session_token": creds.token,
    }
    kv = session.create_client("kinesisvideo", region_name=region, **keys)
    arn = kv.describe_signaling_channel(ChannelName=channel)["ChannelInfo"]["ChannelARN"]
    endpoints = {
        e["Protocol"]: e["ResourceEndpoint"]
        for e in kv.get_signaling_channel_endpoint(
            ChannelARN=arn,
            SingleMasterChannelEndpointConfiguration={"Protocols": ["WSS", "HTTPS"], "Role": "MASTER"},
        )["ResourceEndpointList"]
    }
    sig = session.create_client(
        "kinesis-video-signaling", region_name=region, endpoint_url=endpoints["HTTPS"], **keys
    )
    ice = [RTCIceServer(urls=f"stun:stun.kinesisvideo.{region}.amazonaws.com:443")]
    for server in sig.get_ice_server_config(ChannelARN=arn, Service="TURN")["IceServerList"]:
        ice.append(
            RTCIceServer(urls=server["Uris"], username=server["Username"], credential=server["Password"])
        )
    return ChannelEndpoints(arn=arn, wss=endpoints["WSS"], ice_servers=ice)


class Signaling(Protocol):
    ice_servers: list[RTCIceServer]

    async def open(self) -> None: ...
    async def recv(self) -> SignalingMessage: ...
    async def send_answer(self, client_id: str, sdp: str) -> None: ...
    async def close(self) -> None: ...


class KvsSignaling:
    """Cliente WSS del canal KVS como master. Una instancia = una conexión.

    No reintenta: si la conexión cae, `recv` lanza `SignalingClosed` y quien
    reconecta (con backoff) es `LiveService`, creando una instancia nueva con
    credenciales y servidores TURN frescos.
    """

    def __init__(
        self,
        resolve: Callable[[], ChannelEndpoints],
        creds: IotCredentialsProvider,
        region: str,
    ) -> None:
        self._resolve = resolve
        self._creds = creds
        self._region = region
        self._ws: ClientConnection | None = None
        self.ice_servers: list[RTCIceServer] = []

    async def open(self) -> None:
        loop = asyncio.get_running_loop()
        # botocore y el fetch HTTPS son bloqueantes: al executor, para no
        # congelar a los viewers ya conectados mientras se resuelve.
        endpoints = await loop.run_in_executor(None, self._resolve)
        creds = await loop.run_in_executor(None, self._creds.get)
        self.ice_servers = endpoints.ice_servers
        url = presign_wss_url(endpoints.wss, endpoints.arn, creds, self._region)
        self._ws = await connect(url, open_timeout=10)

    async def recv(self) -> SignalingMessage:
        if self._ws is None:
            raise SignalingClosed("no abierto")
        while True:
            try:
                raw = await self._ws.recv()
            except ConnectionClosed as exc:
                raise SignalingClosed(str(exc)) from exc
            msg = decode_message(raw)
            if msg is not None:
                return msg

    async def send_answer(self, client_id: str, sdp: str) -> None:
        if self._ws is None:
            raise SignalingClosed("no abierto")
        try:
            await self._ws.send(encode_answer(client_id, sdp))
        except ConnectionClosed as exc:
            raise SignalingClosed(str(exc)) from exc

    async def close(self) -> None:
        if self._ws is not None:
            await self._ws.close()
            self._ws = None
```

- [ ] **Step 4: Ejecuta los tests**

Run: `pytest tests/test_live_signaling.py -v && ruff check src tests && mypy`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/vitahub/live/signaling.py tests/test_live_signaling.py
git commit -m "feat: señalización KVS como master (códec, URL prefirmada y cliente WSS)"
```

---

### Task 8: Fuentes de vídeo en directo

**Files:**
- Create: `src/vitahub/live/source.py`
- Test: `tests/test_live_source.py`

**Interfaces:**
- Consumes: `Camera` de `vitahub.models`; `with_credentials` de `vitahub.ingest.rtsp`.
- Produces:
  - `LiveTarget(camera_id: str, name: str, uri: str)` (frozen)
  - `live_targets(cameras: list[Camera], user: str, password: str) -> list[LiveTarget]`
  - `SourceUnavailable(Exception)`
  - `FpsLimitedTrack(source: MediaStreamTrack, max_fps: float, clock: Callable[[], float] = time.monotonic)`
  - `IdleTrack()`: pista de vídeo negra a 1 fps
  - `SourcePool(opener: Callable[[str], MediaStreamTrack], max_fps: float)` con `async acquire(target: LiveTarget) -> MediaStreamTrack` (lanza `SourceUnavailable`), `release(camera_id: str) -> None` y `close_all() -> None`
  - `open_rtsp_track(uri: str) -> MediaStreamTrack` (runtime only)

- [ ] **Step 1: Escribe el test que falla**

```python
import asyncio
import fractions

import pytest
from aiortc import MediaStreamTrack
from av import VideoFrame

from vitahub.live.source import (
    FpsLimitedTrack,
    IdleTrack,
    LiveTarget,
    SourcePool,
    SourceUnavailable,
    live_targets,
)
from vitahub.models import Camera


class _FakeTrack(MediaStreamTrack):
    kind = "video"

    def __init__(self) -> None:
        super().__init__()
        self.n = 0

    async def recv(self) -> VideoFrame:
        self.n += 1
        frame = VideoFrame(width=16, height=16)
        frame.pts, frame.time_base = self.n, fractions.Fraction(1, 90000)
        return frame


def test_live_targets_prefer_substream_and_skip_disabled():
    cams = [
        Camera("a", "salón", "1.1.1.1", rtsp_main="rtsp://h/main", rtsp_sub="rtsp://h/sub"),
        Camera("b", "cocina", "1.1.1.2", rtsp_main="rtsp://h/main2"),
        Camera("c", "baño", "1.1.1.3", enabled=False, rtsp_sub="rtsp://h/sub3"),
        Camera("d", "pasillo", "1.1.1.4"),
    ]
    assert live_targets(cams, "u", "p") == [
        LiveTarget("a", "salón", "rtsp://u:p@h/sub"),
        LiveTarget("b", "cocina", "rtsp://u:p@h/main2"),
    ]


def test_fps_limiter_drops_frames():
    # El reloj avanza una lectura por frame de la fuente: frames 1..5 llegan
    # en t = 0, 0.05, 0.11, 0.15, 0.22. A 10 fps solo pasa uno cada 0.1 s.
    ticks = iter([0.0, 0.05, 0.11, 0.15, 0.22])
    limited = FpsLimitedTrack(_FakeTrack(), max_fps=10, clock=lambda: next(ticks))

    async def scenario() -> list[int]:
        return [(await limited.recv()).pts for _ in range(3)]

    assert asyncio.run(scenario()) == [1, 3, 5]


def test_idle_track_yields_frames():
    async def scenario() -> VideoFrame:
        return await asyncio.wait_for(IdleTrack(interval_s=0.01).recv(), 1)

    frame = asyncio.run(scenario())
    assert frame.width > 0


def test_pool_shares_one_source_per_camera():
    opened: list[str] = []
    tracks: list[_FakeTrack] = []

    def opener(uri: str) -> MediaStreamTrack:
        opened.append(uri)
        tracks.append(_FakeTrack())
        return tracks[-1]

    pool = SourcePool(opener, max_fps=15)
    target = LiveTarget("a", "salón", "rtsp://x")

    async def scenario() -> None:
        await pool.acquire(target)
        await pool.acquire(target)
        assert opened == ["rtsp://x"]
        pool.release("a")
        assert tracks[0].readyState == "live"
        pool.release("a")
        # Con el último visor se cierra el RTSP: no se lee vídeo que nadie mira.
        assert tracks[0].readyState == "ended"

    asyncio.run(scenario())


def test_pool_maps_open_failure_to_unavailable():
    def opener(uri: str) -> MediaStreamTrack:
        raise OSError("connection refused")

    pool = SourcePool(opener, max_fps=15)
    with pytest.raises(SourceUnavailable):
        asyncio.run(pool.acquire(LiveTarget("a", "salón", "rtsp://x")))
```

- [ ] **Step 2: Ejecuta y comprueba que falla**

Run: `pytest tests/test_live_source.py -v`
Expected: FAIL con `ModuleNotFoundError`.

- [ ] **Step 3: Implementa `source.py`**

```python
from __future__ import annotations

import asyncio
import fractions
import time
from collections.abc import Callable
from dataclasses import dataclass

import numpy as np
from aiortc import MediaStreamTrack
from aiortc.contrib.media import MediaRelay
from av import VideoFrame

from vitahub.ingest.rtsp import with_credentials
from vitahub.logging_setup import get_logger
from vitahub.models import Camera

_log = get_logger("live.source")
_VIDEO_CLOCK = 90000


@dataclass(frozen=True)
class LiveTarget:
    camera_id: str
    name: str
    uri: str


def live_targets(cameras: list[Camera], user: str, password: str) -> list[LiveTarget]:
    """Cámaras que se pueden ver en directo, con su URI con credenciales.

    Siempre el substream si existe (spec §4.2): cada viewer es un flujo de
    subida del hogar y el principal multiplicaría el ancho de banda.
    """
    targets = []
    for cam in cameras:
        raw = cam.rtsp_sub or cam.rtsp_main
        if cam.enabled and raw:
            targets.append(LiveTarget(cam.id, cam.name, with_credentials(raw, user, password)))
    return targets


class SourceUnavailable(Exception):
    pass


class FpsLimitedTrack(MediaStreamTrack):
    kind = "video"

    def __init__(
        self,
        source: MediaStreamTrack,
        max_fps: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        super().__init__()
        self._source = source
        self._min_interval = 1.0 / max_fps
        self._clock = clock
        self._last: float | None = None

    async def recv(self) -> VideoFrame:
        while True:
            frame: VideoFrame = await self._source.recv()
            now = self._clock()
            if self._last is None or now - self._last >= self._min_interval:
                self._last = now
                return frame


class IdleTrack(MediaStreamTrack):
    """Negro a 1 fps mientras no hay cámara elegida (o antes del `auth`).

    Hace falta una pista desde la respuesta SDP para que `replaceTrack` pueda
    cambiar a la cámara sin renegociar. Y debe producir frames: el emisor de
    aiortc espera en `recv()` de la pista actual, y una que nunca devolviera
    nada bloquearía el cambio. Negro, nunca imagen de cámara: "sin auth válido
    no se envía ningún frame" (spec §3.4).
    """

    kind = "video"

    def __init__(self, interval_s: float = 1.0) -> None:
        super().__init__()
        self._interval = interval_s
        self._pts = 0

    async def recv(self) -> VideoFrame:
        await asyncio.sleep(self._interval)
        frame = VideoFrame.from_ndarray(np.zeros((180, 320, 3), dtype=np.uint8), format="bgr24")
        frame.pts = self._pts
        frame.time_base = fractions.Fraction(1, _VIDEO_CLOCK)
        self._pts += int(self._interval * _VIDEO_CLOCK)
        return frame


@dataclass
class _Entry:
    source: MediaStreamTrack
    relay: MediaRelay
    refs: int


class SourcePool:
    """Una conexión RTSP por cámara vista, compartida entre sus viewers.

    Es una conexión aparte de la de inferencia: el bucle de `worker.py` y su
    muestreo a ~2 fps no se tocan.
    """

    def __init__(self, opener: Callable[[str], MediaStreamTrack], max_fps: float) -> None:
        self._opener = opener
        self._max_fps = max_fps
        self._entries: dict[str, _Entry] = {}
        self._lock = asyncio.Lock()

    async def acquire(self, target: LiveTarget) -> MediaStreamTrack:
        async with self._lock:
            entry = self._entries.get(target.camera_id)
            if entry is None:
                loop = asyncio.get_running_loop()
                try:
                    # Abrir RTSP bloquea (handshake, hasta el timeout de
                    # ffmpeg): al executor, o congelaría a todos los viewers.
                    source = await loop.run_in_executor(None, self._opener, target.uri)
                except Exception as exc:  # noqa: BLE001 — cualquier fallo de apertura = no disponible
                    _log.warning("live: cam %s no abre: %s", target.camera_id, exc)
                    raise SourceUnavailable(target.camera_id) from exc
                entry = _Entry(source=source, relay=MediaRelay(), refs=0)
                self._entries[target.camera_id] = entry
            entry.refs += 1
            return FpsLimitedTrack(
                entry.relay.subscribe(entry.source, buffered=False), self._max_fps
            )

    def release(self, camera_id: str) -> None:
        entry = self._entries.get(camera_id)
        if entry is None:
            return
        entry.refs -= 1
        if entry.refs <= 0:
            del self._entries[camera_id]
            entry.source.stop()

    def close_all(self) -> None:
        for entry in self._entries.values():
            entry.source.stop()
        self._entries.clear()


def open_rtsp_track(uri: str) -> MediaStreamTrack:
    """Runtime only. No cubierto por unit tests (requiere stream real)."""
    from aiortc.contrib.media import MediaPlayer

    player = MediaPlayer(
        uri, format="rtsp", options={"rtsp_transport": "tcp", "timeout": "5000000"}
    )
    if player.video is None:
        raise SourceUnavailable(uri)
    track: MediaStreamTrack = player.video
    return track
```

- [ ] **Step 4: Ejecuta los tests**

Run: `pytest tests/test_live_source.py -v && ruff check src tests && mypy`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/vitahub/live/source.py tests/test_live_source.py
git commit -m "feat: fuentes del directo — substream compartido, límite de fps y pista en reposo"
```

---

### Task 9: `ViewerPeer`

**Files:**
- Create: `src/vitahub/live/peer.py`
- Test: `tests/test_live_peer.py`

**Interfaces:**
- Consumes: `IdleTrack` (Tarea 8).
- Produces: `ViewerPeer(conn_id: str, client_id: str, ice_servers: list[RTCIceServer], on_open: Callable[[ViewerPeer], None], on_message: Callable[[ViewerPeer, str | bytes], Awaitable[None]], on_closed: Callable[[ViewerPeer], Awaitable[None]])` con:
  - `async answer(offer_sdp: str) -> str`
  - `async add_remote_candidate(payload: Mapping[str, object]) -> None`
  - `send(text: str) -> None` (no-op si el canal no está abierto)
  - `set_track(track: MediaStreamTrack) -> None`
  - `async close() -> None` (idempotente)
  - atributos `conn_id`, `client_id`

- [ ] **Step 1: Escribe el test que falla**

Los tests de esta tarea y de la siguiente montan un viewer `aiortc` local. El helper va en `tests/live_viewer.py` para compartirlo:

```python
"""Viewer WebRTC local para tests: lo que hace la app web, en aiortc."""
from __future__ import annotations

import asyncio
import json

from aiortc import RTCPeerConnection, RTCSessionDescription


class LocalViewer:
    def __init__(self) -> None:
        self.pc = RTCPeerConnection()
        self.pc.addTransceiver("video", direction="recvonly")
        self.channel = self.pc.createDataChannel("control")
        self.inbox: asyncio.Queue[dict[str, object]] = asyncio.Queue()
        self.opened = asyncio.Event()
        self.frames = 0
        self.channel.on("open", self.opened.set)
        self.channel.on("message", lambda m: self.inbox.put_nowait(json.loads(m)))

        @self.pc.on("track")
        def _on_track(track):  # type: ignore[no-untyped-def]
            async def _drain() -> None:
                while True:
                    await track.recv()
                    self.frames += 1

            asyncio.ensure_future(_drain())

    async def offer(self) -> str:
        await self.pc.setLocalDescription(await self.pc.createOffer())
        return str(self.pc.localDescription.sdp)

    async def accept(self, answer_sdp: str) -> None:
        await self.pc.setRemoteDescription(RTCSessionDescription(answer_sdp, "answer"))

    def send(self, obj: dict[str, object]) -> None:
        self.channel.send(json.dumps(obj))

    async def next(self, timeout: float = 5.0) -> dict[str, object]:
        return await asyncio.wait_for(self.inbox.get(), timeout)

    async def close(self) -> None:
        await self.pc.close()
```

`tests/test_live_peer.py`:

```python
import asyncio

from aiortc.contrib.media import MediaStreamTrack

from tests.live_viewer import LocalViewer
from vitahub.live.peer import ViewerPeer
from vitahub.live.source import IdleTrack


def test_negotiates_and_exchanges_messages():
    async def scenario() -> None:
        got: list[str | bytes] = []
        opened = asyncio.Event()
        closed = asyncio.Event()

        async def on_message(peer: ViewerPeer, raw: str | bytes) -> None:
            got.append(raw)
            peer.send('{"type":"ready","cameras":[]}')

        async def on_closed(peer: ViewerPeer) -> None:
            closed.set()

        peer = ViewerPeer("c1", "v1", [], lambda p: opened.set(), on_message, on_closed)
        viewer = LocalViewer()
        await viewer.accept(await peer.answer(await viewer.offer()))
        await asyncio.wait_for(viewer.opened.wait(), 10)
        await asyncio.wait_for(opened.wait(), 10)
        viewer.send({"type": "auth", "token": "t"})
        assert await viewer.next() == {"type": "ready", "cameras": []}
        assert got == ['{"type": "auth", "token": "t"}']

        # Cambiar de pista no renegocia: siguen llegando frames.
        peer.set_track(IdleTrack(interval_s=0.02))
        await asyncio.sleep(0.5)
        assert viewer.frames > 0

        await viewer.close()
        await asyncio.wait_for(closed.wait(), 15)
        await peer.close()
        await peer.close()  # idempotente

    asyncio.run(scenario())
```

- [ ] **Step 2: Ejecuta y comprueba que falla**

Run: `pytest tests/test_live_peer.py -v`
Expected: FAIL con `ModuleNotFoundError: No module named 'vitahub.live.peer'`.

- [ ] **Step 3: Implementa `peer.py`**

```python
from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from aiortc import (
    MediaStreamTrack,
    RTCConfiguration,
    RTCIceServer,
    RTCPeerConnection,
    RTCSessionDescription,
)
from aiortc.sdp import candidate_from_sdp

from vitahub.live.source import IdleTrack
from vitahub.logging_setup import get_logger

_log = get_logger("live.peer")
_CONTROL_LABEL = "control"


class ViewerPeer:
    """Una RTCPeerConnection por viewer, con su DataChannel `control`.

    No decide nada: reenvía al servicio la apertura del canal, cada mensaje y
    el cierre. Las reglas viven en `LiveService` y `SessionBook`.
    """

    def __init__(
        self,
        conn_id: str,
        client_id: str,
        ice_servers: list[RTCIceServer],
        on_open: Callable[[ViewerPeer], None],
        on_message: Callable[[ViewerPeer, str | bytes], Awaitable[None]],
        on_closed: Callable[[ViewerPeer], Awaitable[None]],
    ) -> None:
        self.conn_id = conn_id
        self.client_id = client_id
        self._pc = RTCPeerConnection(RTCConfiguration(iceServers=ice_servers))
        # aiortc no publica tipos (override de mypy): Any es lo honesto aquí.
        self._channel: Any = None
        self._sender: Any = None
        self._closed = False

        @self._pc.on("datachannel")
        def _on_datachannel(channel):  # type: ignore[no-untyped-def]
            if channel.label != _CONTROL_LABEL:
                return
            self._channel = channel

            @channel.on("message")
            async def _on_msg(raw):  # type: ignore[no-untyped-def]
                await on_message(self, raw)

            # El canal del viewer llega ya abierto: el evento "datachannel" se
            # dispara al establecerse, así que el plazo del `auth` empieza aquí.
            on_open(self)

        @self._pc.on("connectionstatechange")
        async def _on_state():  # type: ignore[no-untyped-def]
            if self._pc.connectionState in ("failed", "closed") and not self._closed:
                await on_closed(self)

    async def answer(self, offer_sdp: str) -> str:
        await self._pc.setRemoteDescription(RTCSessionDescription(offer_sdp, "offer"))
        # Después de la oferta: addTrack engancha la pista al transceiver de
        # vídeo que la oferta (recvonly) ya creó, y la respuesta sale sendonly.
        self._sender = self._pc.addTrack(IdleTrack())
        await self._pc.setLocalDescription(await self._pc.createAnswer())
        # aiortc no hace trickle: setLocalDescription espera a reunir todos
        # los candidatos, así que la respuesta ya los lleva dentro.
        return str(self._pc.localDescription.sdp)

    async def add_remote_candidate(self, payload: Mapping[str, object]) -> None:
        raw = str(payload.get("candidate") or "")
        if not raw:
            return  # fin de candidatos
        candidate = candidate_from_sdp(raw.split(":", 1)[1] if raw.startswith("candidate:") else raw)
        candidate.sdpMid = payload.get("sdpMid")
        candidate.sdpMLineIndex = payload.get("sdpMLineIndex")
        await self._pc.addIceCandidate(candidate)

    def send(self, text: str) -> None:
        if self._channel is not None and self._channel.readyState == "open":
            self._channel.send(text)

    def set_track(self, track: MediaStreamTrack) -> None:
        if self._sender is not None:
            self._sender.replaceTrack(track)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            await self._pc.close()
        except Exception:  # noqa: BLE001 — cerrar un peer roto no debe propagar
            _log.exception("live: error cerrando peer %s", self.conn_id)
```

- [ ] **Step 4: Ejecuta los tests**

Run: `pytest tests/test_live_peer.py -v && ruff check src tests && mypy`
Expected: PASS. Si falla por no haber interfaz de red en el entorno (aioice sin candidatos host), ejecútalo en una máquina con red local: el test no sale a internet, pero necesita una interfaz no-loopback.

- [ ] **Step 5: Commit**

```bash
git add src/vitahub/live/peer.py tests/live_viewer.py tests/test_live_peer.py
git commit -m "feat: ViewerPeer — conexión WebRTC por viewer con DataChannel control"
```

---

### Task 10: `LiveService`

**Files:**
- Create: `src/vitahub/live/service.py`
- Test: `tests/test_live_service.py`

**Interfaces:**
- Consumes: todo lo anterior: `verify_token`, `load_session_keys`, `SessionBook`, `Admitted`, `Rejected`, `parse_client_message`, `AuthMsg`, `SelectMsg`, `ready_message`, `closed_message`, `live_view_started`, `live_view_ended`, `Signaling`, `SignalingClosed`, `SignalingMessage`, `SourcePool`, `SourceUnavailable`, `LiveTarget`, `ViewerPeer`, `EventSink`, `backoff_delay`, `next_attempt`.
- Produces: `LiveService(hub_id: str, max_viewers: int, keys: Mapping[str, ec.EllipticCurvePublicKey], signaling_factory: Callable[[], Signaling], sources: SourcePool, targets: Callable[[], list[LiveTarget]], sink: EventSink, clock: Callable[[], float] = time.time, auth_timeout_s: float = 5.0, connect_timeout_s: float = 30.0, tick_s: float = 1.0)` con:
  - `async run(stop: asyncio.Event) -> None`: entrada testeable
  - `start() -> None`: hilo propio con su loop
  - `stop(timeout_s: float = 5.0) -> None`

- [ ] **Step 1: Escribe los tests que fallan**

```python
import asyncio
import time

import jwt
from aiortc import MediaStreamTrack
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from tests.live_viewer import LocalViewer
from vitahub.live.service import LiveService
from vitahub.live.signaling import SignalingClosed, SignalingMessage
from vitahub.live.source import IdleTrack, LiveTarget, SourcePool
from vitahub.live.token import load_session_keys
from vitahub.models import Event
from vitahub.sinks.base import EventSink

_PRIV = ec.generate_private_key(ec.SECP256R1())
_KEYS = load_session_keys(
    {"k1": _PRIV.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    ).decode()}
)
_TARGETS = [LiveTarget("a", "salón", "rtsp://a"), LiveTarget("b", "cocina", "rtsp://b")]


def _token(sub: str, sid: str = "s", exp: float | None = None) -> str:
    now = time.time()
    return jwt.encode(
        {"sid": sid, "sub": sub, "hub": "hub-1", "iat": int(now), "exp": exp or now + 600},
        _PRIV, algorithm="ES256", headers={"kid": "k1"},
    )


class _Sink(EventSink):
    def __init__(self) -> None:
        self.events: list[Event] = []

    def emit(self, event: Event) -> None:
        self.events.append(event)

    def close(self) -> None:
        pass


class _FakeSignaling:
    """Lo que haría KVS, en memoria: entrega ofertas y recoge respuestas."""

    def __init__(self) -> None:
        self.ice_servers: list[object] = []
        self.inbox: asyncio.Queue[SignalingMessage | None] = asyncio.Queue()
        self.answers: dict[str, asyncio.Future[str]] = {}
        self.opens = 0

    async def open(self) -> None:
        self.opens += 1

    async def recv(self) -> SignalingMessage:
        msg = await self.inbox.get()
        if msg is None:
            raise SignalingClosed("cortado")
        return msg

    async def send_answer(self, client_id: str, sdp: str) -> None:
        self.answers[client_id].set_result(sdp)

    async def close(self) -> None:
        pass

    async def connect(self, viewer: LocalViewer, client_id: str) -> None:
        self.answers[client_id] = asyncio.get_running_loop().create_future()
        await self.inbox.put(
            SignalingMessage("SDP_OFFER", client_id, {"type": "offer", "sdp": await viewer.offer()})
        )
        await viewer.accept(await asyncio.wait_for(self.answers[client_id], 10))
        await asyncio.wait_for(viewer.opened.wait(), 10)


def _opener(uri: str) -> MediaStreamTrack:
    if uri == "rtsp://b":
        raise OSError("cámara apagada")
    return IdleTrack(interval_s=0.02)


async def _service(max_viewers: int = 3, clock=time.time, auth_timeout_s: float = 5.0):  # type: ignore[no-untyped-def]
    sig, sink, stop = _FakeSignaling(), _Sink(), asyncio.Event()
    svc = LiveService(
        hub_id="hub-1", max_viewers=max_viewers, keys=_KEYS,
        signaling_factory=lambda: sig, sources=SourcePool(_opener, max_fps=15),
        targets=lambda: _TARGETS, sink=sink, clock=clock,
        auth_timeout_s=auth_timeout_s, tick_s=0.05,
    )
    task = asyncio.ensure_future(svc.run(stop))
    return svc, sig, sink, stop, task


async def _auth(sig: _FakeSignaling, sub: str, client_id: str, **kw: object) -> LocalViewer:
    viewer = LocalViewer()
    await sig.connect(viewer, client_id)
    viewer.send({"type": "auth", "token": _token(sub, **kw)})
    return viewer


def test_valid_auth_gets_ready_and_video():
    async def scenario() -> None:
        _, sig, sink, stop, task = await _service()
        viewer = await _auth(sig, "acc-1", "v1")
        assert await viewer.next() == {
            "type": "ready",
            "cameras": [{"id": "a", "name": "salón"}, {"id": "b", "name": "cocina"}],
        }
        viewer.send({"type": "select", "camera_id": "a"})
        await asyncio.sleep(0.5)
        assert viewer.frames > 0
        assert [e.type for e in sink.events] == ["live_view_started"]
        stop.set()
        await task

    asyncio.run(scenario())


def test_no_auth_closes_invalid_without_camera_frames():
    async def scenario() -> None:
        _, sig, sink, stop, task = await _service(auth_timeout_s=0.3)
        viewer = LocalViewer()
        await sig.connect(viewer, "v1")
        assert await viewer.next() == {"type": "closed", "reason": "invalid"}
        assert sink.events == []  # nunca hubo sesión, no hay auditoría que emitir
        stop.set()
        await task

    asyncio.run(scenario())


def test_bad_token_closes_invalid():
    async def scenario() -> None:
        _, sig, _, stop, task = await _service()
        viewer = LocalViewer()
        await sig.connect(viewer, "v1")
        viewer.send({"type": "auth", "token": "basura"})
        assert await viewer.next() == {"type": "closed", "reason": "invalid"}
        stop.set()
        await task

    asyncio.run(scenario())


def test_same_account_second_device_replaces_first():
    async def scenario() -> None:
        _, sig, sink, stop, task = await _service()
        first = await _auth(sig, "acc-1", "v1", sid="s1")
        await first.next()  # ready
        second = await _auth(sig, "acc-1", "v2", sid="s2")
        assert await first.next() == {"type": "closed", "reason": "replaced"}
        assert (await second.next())["type"] == "ready"
        ended = [e for e in sink.events if e.type == "live_view_ended"]
        assert ended[0].payload["reason"] == "replaced"
        stop.set()
        await task

    asyncio.run(scenario())


def test_limit_rejects_other_account():
    async def scenario() -> None:
        _, sig, _, stop, task = await _service(max_viewers=1)
        first = await _auth(sig, "acc-1", "v1")
        await first.next()
        second = await _auth(sig, "acc-2", "v2")
        assert await second.next() == {"type": "closed", "reason": "limit"}
        stop.set()
        await task

    asyncio.run(scenario())


def test_expiry_closes_and_renewal_extends():
    async def scenario() -> None:
        now = [time.time()]
        _, sig, sink, stop, task = await _service(clock=lambda: now[0])
        viewer = await _auth(sig, "acc-1", "v1", exp=now[0] + 10)
        await viewer.next()
        viewer.send({"type": "auth", "token": _token("acc-1", sid="s2", exp=now[0] + 20)})
        assert (await viewer.next())["type"] == "ready"  # renovación: ready otra vez
        now[0] += 15  # pasado el primer exp, dentro del segundo
        await asyncio.sleep(0.3)
        assert viewer.inbox.empty()
        now[0] += 10
        assert await viewer.next() == {"type": "closed", "reason": "expired"}
        stop.set()
        await task

    asyncio.run(scenario())


def test_select_unavailable_camera_keeps_connection():
    async def scenario() -> None:
        _, sig, _, stop, task = await _service()
        viewer = await _auth(sig, "acc-1", "v1")
        await viewer.next()
        viewer.send({"type": "select", "camera_id": "b"})
        assert await viewer.next() == {"type": "closed", "reason": "camera_unavailable"}
        viewer.send({"type": "select", "camera_id": "a"})
        await asyncio.sleep(0.5)
        assert viewer.frames > 0
        stop.set()
        await task

    asyncio.run(scenario())


def test_signaling_drop_keeps_existing_viewer():
    async def scenario() -> None:
        _, sig, _, stop, task = await _service()
        viewer = await _auth(sig, "acc-1", "v1")
        await viewer.next()
        viewer.send({"type": "select", "camera_id": "a"})
        await sig.inbox.put(None)  # el WSS se corta
        await asyncio.sleep(1.5)  # backoff del primer reintento (1 s)
        assert sig.opens >= 2
        frames = viewer.frames
        await asyncio.sleep(0.3)
        assert viewer.frames > frames  # el P2P no depende de la señalización
        stop.set()
        await task

    asyncio.run(scenario())


def test_stop_sends_shutdown_and_emits_ended():
    async def scenario() -> None:
        _, sig, sink, stop, task = await _service()
        viewer = await _auth(sig, "acc-1", "v1")
        await viewer.next()
        stop.set()
        assert await viewer.next() == {"type": "closed", "reason": "shutdown"}
        await asyncio.wait_for(task, 5)
        assert sink.events[-1].type == "live_view_ended"
        assert sink.events[-1].payload["reason"] == "shutdown"

    asyncio.run(scenario())
```

- [ ] **Step 2: Ejecuta y comprueba que fallan**

Run: `pytest tests/test_live_service.py -v`
Expected: FAIL con `ModuleNotFoundError: No module named 'vitahub.live.service'`.

- [ ] **Step 3: Implementa `service.py`**

```python
from __future__ import annotations

import asyncio
import threading
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime

from cryptography.hazmat.primitives.asymmetric import ec

from vitahub.ingest.rtsp import backoff_delay, next_attempt
from vitahub.live.events import live_view_ended, live_view_started
from vitahub.live.peer import ViewerPeer
from vitahub.live.protocol import (
    AuthMsg,
    SelectMsg,
    closed_message,
    parse_client_message,
    ready_message,
)
from vitahub.live.sessions import Rejected, SessionBook
from vitahub.live.signaling import Signaling, SignalingClosed, SignalingMessage
from vitahub.live.source import LiveTarget, SourcePool, SourceUnavailable
from vitahub.live.token import SessionClaims, TokenRejected, verify_token
from vitahub.logging_setup import get_logger
from vitahub.models import Event
from vitahub.sinks.base import EventSink

_log = get_logger("live.service")


@dataclass
class _Conn:
    peer: ViewerPeer
    started_at: float
    camera: LiveTarget | None = None
    timers: list[asyncio.TimerHandle] = field(default_factory=list)


class LiveService:
    """Master WebRTC del hogar y árbitro de las reglas de sesión (spec §3.4).

    Corre en su propio hilo con su propio loop: nada de lo que pase aquí
    (señalización caída, un peer roto, una cámara que no abre) puede tocar la
    inferencia ni los workers de cámara.
    """

    def __init__(
        self,
        hub_id: str,
        max_viewers: int,
        keys: Mapping[str, ec.EllipticCurvePublicKey],
        signaling_factory: Callable[[], Signaling],
        sources: SourcePool,
        targets: Callable[[], list[LiveTarget]],
        sink: EventSink,
        clock: Callable[[], float] = time.time,
        auth_timeout_s: float = 5.0,
        connect_timeout_s: float = 30.0,
        tick_s: float = 1.0,
    ) -> None:
        self._hub_id = hub_id
        self._keys = keys
        self._signaling_factory = signaling_factory
        self._sources = sources
        self._targets = targets
        self._sink = sink
        self._clock = clock
        self._auth_timeout = auth_timeout_s
        self._connect_timeout = connect_timeout_s
        self._tick = tick_s
        self._book = SessionBook(max_viewers)
        self._conns: dict[str, _Conn] = {}
        self._by_client: dict[str, str] = {}
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop: asyncio.Event | None = None

    # --- ciclo de vida ------------------------------------------------------

    def start(self) -> None:
        ready = threading.Event()

        def _main() -> None:
            async def _run() -> None:
                self._loop = asyncio.get_running_loop()
                self._stop = asyncio.Event()
                ready.set()
                await self.run(self._stop)

            try:
                asyncio.run(_run())
            except Exception:  # noqa: BLE001 — el directo caído no tumba el hub
                _log.exception("live: el servicio terminó por un error")

        self._thread = threading.Thread(target=_main, name="live", daemon=True)
        self._thread.start()
        ready.wait(timeout=5.0)

    def stop(self, timeout_s: float = 5.0) -> None:
        if self._loop is not None and self._stop is not None:
            self._loop.call_soon_threadsafe(self._stop.set)
        if self._thread is not None:
            self._thread.join(timeout=timeout_s)

    async def run(self, stop: asyncio.Event) -> None:
        ticker = asyncio.ensure_future(self._tick_loop(stop))
        signaling = asyncio.ensure_future(self._signaling_loop(stop))
        await stop.wait()
        signaling.cancel()
        ticker.cancel()
        for conn_id in list(self._conns):
            await self._close(conn_id, "shutdown")
        self._sources.close_all()

    # --- señalización -------------------------------------------------------

    async def _signaling_loop(self, stop: asyncio.Event) -> None:
        attempt = 0
        while not stop.is_set():
            signaling = self._signaling_factory()
            try:
                await signaling.open()
                _log.info("live: conectado al canal de señalización")
                attempt = 0
                while True:
                    msg = await signaling.recv()
                    if msg.type in ("GO_AWAY", "RECONNECT_ICE_SERVER"):
                        break
                    await self._on_signal(signaling, msg)
            except asyncio.CancelledError:
                await signaling.close()
                raise
            except SignalingClosed as exc:
                _log.warning("live: señalización cerrada (%s)", exc)
            except Exception:  # noqa: BLE001 — credenciales, red, AWS: se reintenta
                _log.exception("live: fallo en la señalización")
            try:
                await signaling.close()
            except Exception:  # noqa: BLE001 — cerrar algo ya roto no importa
                pass
            # Los viewers ya conectados siguen: el P2P no pasa por aquí.
            delay = backoff_delay(attempt)
            attempt = next_attempt(attempt)
            try:
                await asyncio.wait_for(stop.wait(), delay)
            except TimeoutError:
                pass

    async def _on_signal(self, signaling: Signaling, msg: SignalingMessage) -> None:
        if msg.type == "SDP_OFFER":
            previous = self._by_client.get(msg.client_id)
            if previous is not None:
                # El mismo navegador reconectando: la conexión vieja ya no sirve.
                await self._close(previous, None)
            conn_id = uuid.uuid4().hex
            peer = ViewerPeer(
                conn_id, msg.client_id, signaling.ice_servers,
                self._on_open, self._on_message, self._on_peer_closed,
            )
            conn = _Conn(peer=peer, started_at=self._clock())
            self._conns[conn_id] = conn
            self._by_client[msg.client_id] = conn_id
            loop = asyncio.get_running_loop()
            conn.timers.append(
                loop.call_later(self._connect_timeout, self._expire_unauth, conn_id)
            )
            try:
                answer = await peer.answer(str(msg.payload.get("sdp", "")))
                await signaling.send_answer(msg.client_id, answer)
            except SignalingClosed:
                raise
            except Exception:  # noqa: BLE001 — una oferta mala no tumba la señalización
                _log.exception("live: oferta inválida de %s", msg.client_id)
                await self._close(conn_id, None)
        elif msg.type == "ICE_CANDIDATE":
            conn_id = self._by_client.get(msg.client_id)
            if conn_id is not None and conn_id in self._conns:
                try:
                    await self._conns[conn_id].peer.add_remote_candidate(msg.payload)
                except Exception:  # noqa: BLE001 — un candidato malo no rompe la sesión
                    _log.warning("live: candidato ICE inválido de %s", msg.client_id)

    # --- callbacks del peer -------------------------------------------------

    def _on_open(self, peer: ViewerPeer) -> None:
        conn = self._conns.get(peer.conn_id)
        if conn is not None:
            loop = asyncio.get_running_loop()
            conn.timers.append(
                loop.call_later(self._auth_timeout, self._expire_unauth, peer.conn_id)
            )

    def _expire_unauth(self, conn_id: str) -> None:
        if conn_id in self._conns and self._book.claims_for(conn_id) is None:
            asyncio.ensure_future(self._close(conn_id, "invalid"))

    async def _on_message(self, peer: ViewerPeer, raw: str | bytes) -> None:
        conn_id = peer.conn_id
        if conn_id not in self._conns:
            return
        msg = parse_client_message(raw)
        if isinstance(msg, AuthMsg):
            await self._on_auth(conn_id, msg)
        elif isinstance(msg, SelectMsg):
            await self._on_select(conn_id, msg)

    async def _on_auth(self, conn_id: str, msg: AuthMsg) -> None:
        verdict = verify_token(msg.token, self._keys, self._hub_id, self._clock())
        if isinstance(verdict, TokenRejected):
            _log.info("live: token rechazado (%s)", verdict.reason)
            await self._close(conn_id, "invalid")
            return
        # SessionBook saca a los sustituidos del libro antes de devolver: sus
        # claims se guardan aquí para que su live_view_ended no se pierda.
        before = {c: self._book.claims_for(c) for c in list(self._conns)}
        result = self._book.admit(conn_id, verdict)
        if isinstance(result, Rejected):
            await self._close(conn_id, result.reason)
            return
        for old in result.replaced:
            await self._close(old, "replaced", claims=before.get(old))
        cameras = [(t.camera_id, t.name) for t in self._targets()]
        self._conns[conn_id].peer.send(ready_message(cameras))

    async def _on_select(self, conn_id: str, msg: SelectMsg) -> None:
        claims = self._book.claims_for(conn_id)
        conn = self._conns.get(conn_id)
        if claims is None or conn is None:
            return  # select antes del auth: se ignora, el plazo del auth ya corre
        target = next((t for t in self._targets() if t.camera_id == msg.camera_id), None)
        if target is None:
            conn.peer.send(closed_message("camera_unavailable"))
            return
        try:
            track = await self._sources.acquire(target)
        except SourceUnavailable:
            conn.peer.send(closed_message("camera_unavailable"))
            return
        if conn_id not in self._conns:
            self._sources.release(target.camera_id)
            return
        conn.peer.set_track(track)
        if conn.camera is not None:
            self._sources.release(conn.camera.camera_id)
        conn.camera = target
        self._emit(live_view_started(self._hub_id, claims, target.camera_id, target.name, self._now()))

    async def _on_peer_closed(self, peer: ViewerPeer) -> None:
        await self._close(peer.conn_id, None, ended_reason="disconnected")

    # --- cierre y caducidad -------------------------------------------------

    async def _tick_loop(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            await asyncio.sleep(self._tick)
            for conn_id in self._book.expired(self._clock()):
                await self._close(conn_id, "expired")

    async def _close(
        self,
        conn_id: str,
        reason: str | None,
        claims: SessionClaims | None = None,
        ended_reason: str | None = None,
    ) -> None:
        conn = self._conns.pop(conn_id, None)
        if conn is None:
            return
        if self._by_client.get(conn.peer.client_id) == conn_id:
            del self._by_client[conn.peer.client_id]
        for timer in conn.timers:
            timer.cancel()
        if reason is not None:
            conn.peer.send(closed_message(reason))
            await asyncio.sleep(0.05)  # deja salir el `closed` antes de cortar
        claims = self._book.release(conn_id) or claims
        if conn.camera is not None:
            self._sources.release(conn.camera.camera_id)
        if claims is not None:
            cam_id = conn.camera.camera_id if conn.camera else ""
            cam_name = conn.camera.name if conn.camera else ""
            self._emit(live_view_ended(
                self._hub_id, claims, cam_id, cam_name, ended_reason or reason or "closed",
                self._clock() - conn.started_at, self._now(),
            ))
        await conn.peer.close()

    def _emit(self, event: Event) -> None:
        try:
            self._sink.emit(event)
        except Exception:  # noqa: BLE001 — la auditoría caída no corta el vídeo
            _log.exception("live: no se pudo emitir el evento")

    def _now(self) -> datetime:
        return datetime.fromtimestamp(self._clock(), tz=UTC)
```

- [ ] **Step 4: Ejecuta los tests**

Run: `pytest tests/test_live_service.py -v && ruff check src tests && mypy`
Expected: PASS (9 tests). Si `test_expiry_closes_and_renewal_extends` tiene problemas de tiempos, revisa que `_tick_loop` use `self._clock()` y no `time.time()`.

- [ ] **Step 5: Commit**

```bash
git add src/vitahub/live/service.py tests/test_live_service.py
git commit -m "feat: LiveService — master WebRTC y árbitro de sesiones del directo"
```

---

### Task 11: Cableado en el arranque y documentación

**Files:**
- Modify: `src/vitahub/factory.py`
- Modify: `src/vitahub/app.py`
- Modify: `docs/como-funciona.md`, `docs/como-probar.md`, `docs/backlog.md`
- Test: `tests/test_factory.py`, `tests/test_app_loop.py`

**Interfaces:**
- Consumes: `LiveService`, `SourcePool`, `open_rtsp_track`, `live_targets`, `KvsSignaling`, `resolve_endpoints`, `channel_name`, `IotCredentialsProvider`, `https_fetcher`, `load_session_keys`.
- Produces: `build_live_service(cfg: HubConfig, sink: EventSink) -> LiveService | None`. `_shutdown(httpd, live, supervisor, sink)`, con `live: LiveService | None`.

- [ ] **Step 1: Escribe los tests que fallan**

En `tests/test_factory.py`:

```python
from vitahub.factory import build_live_service


def test_build_live_service_disabled_returns_none():
    # `_hub_config` ya existe en este fichero; su LiveConfig es el default (apagado).
    assert build_live_service(_hub_config(), StdoutJsonSink()) is None
```

Añade `build_live_service` al import existente de `vitahub.factory` en vez de una línea nueva.

En `tests/test_app_loop.py`:

```python
from unittest.mock import MagicMock

from vitahub.app import _shutdown


def test_shutdown_stops_live_before_sink():
    # Los live_view_ended de los viewers que se cortan salen por el sink:
    # cerrarlo antes perdería la auditoría del apagado.
    order: list[str] = []
    live = MagicMock(stop=lambda **_: order.append("live"))
    supervisor = MagicMock(stop_all=lambda **_: order.append("supervisor"))
    sink = MagicMock(close=lambda: order.append("sink"))
    _shutdown(None, live, supervisor, sink)
    assert order == ["live", "supervisor", "sink"]


def test_shutdown_closes_sink_even_if_live_stop_fails():
    sink = MagicMock()
    live = MagicMock()
    live.stop.side_effect = RuntimeError("boom")
    try:
        _shutdown(None, live, MagicMock(), sink)
    except RuntimeError:
        pass
    sink.close.assert_called_once()
```

- [ ] **Step 2: Ejecuta y comprueba que fallan**

Run: `pytest tests/test_factory.py tests/test_app_loop.py -v`
Expected: FAIL con `ImportError: cannot import name 'build_live_service'` y un `TypeError` de `_shutdown`.

- [ ] **Step 3: Implementa `build_live_service`**

En `src/vitahub/factory.py`:

```python
from vitahub.live.credentials import IotCredentialsProvider, https_fetcher
from vitahub.live.service import LiveService
from vitahub.live.signaling import KvsSignaling, channel_name, resolve_endpoints
from vitahub.live.source import SourcePool, live_targets, open_rtsp_track
from vitahub.live.token import load_session_keys


def build_live_service(cfg: HubConfig, sink: EventSink) -> LiveService | None:
    """El servicio de directo, o None si `live.enabled` es false.

    No conecta nada aquí: la primera llamada a AWS ocurre en el hilo del
    servicio, con reintento. Un hogar sin internet arranca igual y vigila.
    """
    if not cfg.live.enabled:
        return None
    live = cfg.live
    creds = IotCredentialsProvider(
        https_fetcher(
            live.credentials_endpoint, live.role_alias, cfg.hub_id,
            Path(cfg.uplink.ca_path), Path(cfg.uplink.cert_path), Path(cfg.uplink.key_path),
        )
    )
    channel = channel_name(live.channel_prefix, cfg.hub_id)
    _log.info("vídeo en directo habilitado (canal %s, máx. %d visores)", channel, live.max_viewers)
    return LiveService(
        hub_id=cfg.hub_id,
        max_viewers=live.max_viewers,
        keys=load_session_keys(live.session_keys),
        signaling_factory=lambda: KvsSignaling(
            resolve=lambda: resolve_endpoints(channel, live.region, creds.get()),
            creds=creds,
            region=live.region,
        ),
        sources=SourcePool(open_rtsp_track, max_fps=live.max_fps),
        # Se lee cfg.cameras en cada llamada: RescanService la sustituye en
        # cada ciclo, y así el directo ve las cámaras nuevas sin reiniciar.
        targets=lambda: live_targets(
            cfg.cameras, cfg.credentials.onvif_user, cfg.credentials.onvif_password
        ),
        sink=sink,
    )
```

- [ ] **Step 4: Cablea `app.py`**

Importa `build_live_service` en el bloque de `vitahub.factory` y `LiveService` de `vitahub.live.service`. Detrás de `httpd = _start_control_server_safe(...)`:

```python
    live = build_live_service(cfg, sink)
    if live is not None:
        live.start()
```

En el `finally` del bucle de latido: `_shutdown(httpd, live, supervisor, sink)`. Sustituye `_shutdown` por:

```python
def _shutdown(
    httpd: ThreadingHTTPServer | None,
    live: LiveService | None,
    supervisor: CameraSupervisor,
    sink: EventSink,
) -> None:
    """Cierra en orden y con cada paso garantizado pase lo que pase con el anterior.

    Primero el control HTTP: si se cerrara después de parar los workers, una
    petición POST /rescan que ya estuviera en vuelo podría llegar a
    `supervisor.apply()` tras `stop_all()` y arrancar workers huérfanos que ya
    nadie señalará. Luego el directo, que avisa a sus viewers con
    `closed:shutdown` y emite sus `live_view_ended` por el sink — por eso va
    antes de cerrarlo. Luego los workers, con un plazo global acotado (no por
    cámara, ver `_CAMERA_SHUTDOWN_DEADLINE_S`). El sink se cierra siempre,
    incluso si algo de lo anterior falla.
    """
    try:
        if httpd is not None:
            httpd.shutdown()
            httpd.server_close()
    finally:
        try:
            if live is not None:
                live.stop(timeout_s=_LIVE_SHUTDOWN_DEADLINE_S)
        finally:
            try:
                supervisor.stop_all(deadline_s=_CAMERA_SHUTDOWN_DEADLINE_S)
            finally:
                sink.close()
```

Y junto a `_CAMERA_SHUTDOWN_DEADLINE_S`:

```python
# El directo cierra sus peers en paralelo con este plazo; sumado al de las
# cámaras debe seguir con margen por debajo del stop_grace_period de Docker.
_LIVE_SHUTDOWN_DEADLINE_S = 5.0
```

Comprueba en `docker-compose.yml` que `stop_grace_period` sigue por encima de 20 + 5 s con margen. Si no, súbelo y explica por qué en un comentario.

- [ ] **Step 5: Ejecuta toda la suite**

Run: `pytest && ruff check src tests && mypy`
Expected: PASS completa.

- [ ] **Step 6: Actualiza la documentación (spec §9, lado hub)**

- `docs/como-funciona.md`:
  - En «Qué es», sustituye «Todo el vídeo se procesa en el hogar; al exterior solo sale texto» por: «El vídeo se procesa en el hogar y al exterior salen eventos de texto. La única excepción es el vídeo en directo: bajo demanda, para cuentas habilitadas por el técnico con consentimiento, por WebRTC y sin almacenarse nunca (ver más abajo)».
  - Añade a la tabla de módulos una fila por cada fichero de `live/`, con la responsabilidad de la tabla «File Structure» de este plan.
  - Añade una sección «Vídeo en directo» que resuma el flujo de spec §3 (diagrama incluido), las reglas de §3.4 y la config de §4.4.
- `docs/como-probar.md`: añade un paso «Vídeo en directo (opcional)» con lo siguiente:
  - requisitos: infra desplegada, canal creado, claves en `hub.yaml` y variables de entorno;
  - cómo ver los logs `live:` al arrancar;
  - prueba con la app web: ver una cámara, abrir un segundo dispositivo con la misma cuenta y comprobar `replaced`, esperar a que caduque;
  - qué eventos `live_view_*` deben aparecer en `docker logs` y en `hub-events`.
- `docs/backlog.md`: añade dos entradas, cada una con su motivo tomado de spec §2 y §3.5:
  - «Indicador en el hogar de que alguien está viendo» (deuda de privacidad).
  - «Corte duro entre hubs distintos de la misma cuenta» (hoy cooperativo, con un máximo de 10 min).

- [ ] **Step 7: Commit**

```bash
git add src/vitahub/factory.py src/vitahub/app.py tests/test_factory.py tests/test_app_loop.py docs/como-funciona.md docs/como-probar.md docs/backlog.md docker-compose.yml
git commit -m "feat: vídeo en directo cableado en el arranque y documentado"
```
