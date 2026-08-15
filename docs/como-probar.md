# Cómo probar el hub

Tres niveles, de más rápido a más real:

1. **Tests automáticos** — verifican la lógica; no necesitan cámara ni Docker.
2. **Arranque local en seco** (detector `stub`) — confirma que el proceso arranca y descubre.
3. **Extremo a extremo con cámara real** — ver eventos `person_detected` de verdad.

Para entender qué hace por dentro, ver [como-funciona.md](como-funciona.md).

---

## 1. Tests automáticos

Instala en modo editable con las herramientas de desarrollo y corre la suite:

```bash
pip install -e ".[dev]"
pytest -v
```

Deberías ver **54 tests en verde**. Además, las mismas puertas que corren en CI:

```bash
ruff check src tests
mypy
```

### Qué cubren (y qué no)

Los tests cubren toda la **lógica pura y determinista**, sin red ni hardware:
- Motor de eventos (anti-parpadeo, aislamiento entre cámaras, reset del temporizador).
- Reconciliación del registro (identidad estable, cambio de IP).
- Validación de config (*fail-fast*, escritura atómica, credencial por entorno).
- Parsers ONVIF (probe, serie, URI de stream, digest WS-Security sin filtrar la clave).
- Helpers RTSP (backoff, muestreo, watchdog, inyección de credenciales en la URL).
- Filtro del detector de personas (clase persona + umbral) con un modelo falso inyectado.
- Redacción de secretos en logs; envelope de evento; healthcheck.

**No se cubren en CI (por diseño):** el descubrimiento ONVIF real por multicast y la captura RTSP
con OpenCV — necesitan una LAN y una cámara. Eso se valida a mano (paso 3).

---

## 2. Arranque local en seco (sin cámara ni modelo)

Sirve para confirmar que el proceso **arranca, lee la config, intenta descubrir y late** — sin
necesidad de cámara ni de descargar pesos YOLO.

```bash
pip install -e ".[dev]"

# Config de ejemplo con detector "stub"
export VITAHUB_CONFIG=./config/hub.example.yaml
# la credencial es obligatoria aunque no haya cámara
export VITAHUB_ONVIF_USER=admin VITAHUB_ONVIF_PASSWORD=test

# edita ./config/hub.example.yaml y pon detector: stub  (evita cargar YOLO)
python -m vitahub.app
```

Qué esperar en **stderr** (los logs):
- `hub hub-... arrancando`
- `descubrimiento: N cámaras` o, si no hay ninguna en la LAN,
  `descubrimiento: 0 cámaras encontradas — revisa ONVIF/credencial/red`.

> **Importante:** el `StubDetector` siempre "ve" 0 personas, así que en modo `stub` **no se emiten
> eventos** por stdout — es solo para validar el cableado y el arranque. Para ver eventos de verdad,
> hace falta una cámara real y `detector: person_yolo` (paso 3).

Para parar: `Ctrl-C` (apagado limpio).

---

## 3. Extremo a extremo con una cámara real

Requisitos: una cámara IP con **ONVIF activado** en la misma LAN, y su usuario/contraseña.

### 3.1 (Opcional) Comprobar la cámara antes

Antes de levantar el hub, confirma que la cámara responde ONVIF y sirve RTSP. Si tienes `ffmpeg`:

```bash
# ¿abre el stream con credenciales? (ajusta IP/ruta/credenciales)
ffprobe -rtsp_transport tcp 'rtsp://admin:CLAVE@192.168.1.190:554/Streaming/Channels/2'
```

Que abra confirma dos cosas que el hub necesita: la ruta RTSP y que **la cámara pide auth**
(el hub inyecta las credenciales en la URL automáticamente).

### 3.2 Correr el hub (local, CPU)

```bash
pip install -e ".[dev]"          # instala opencv + ultralytics (torch)

# config con detector real
cp config/hub.example.yaml ./hub.yaml
# edita ./hub.yaml: hub_id único y detector: person_yolo
export VITAHUB_CONFIG=./hub.yaml
export VITAHUB_ONVIF_USER=admin VITAHUB_ONVIF_PASSWORD=CLAVE

python -m vitahub.app
```

Qué esperar:
- En **stderr**: `descubrimiento: 1 cámaras`, `registro: added onvif-...`, `cam onvif-... conectada`.
- En **stdout**, al ponerte delante de la cámara (tras ~2 s):

```json
{"schema_version":1,"hub_id":"...","camera_id":"onvif-...","camera_name":"camera-1","type":"person_detected","severity":"info","timestamp":"...","payload":{"person_count":1,"confidence":0.8}}
```

Y al salir del encuadre (tras ~5 s), un `person_absent`.

Para separar eventos de logs, redirige stdout:

```bash
python -m vitahub.app > eventos.jsonl 2> hub.log
tail -f eventos.jsonl   # solo eventos
```

### 3.3 Con Docker

```bash
mkdir -p ./data
cp config/hub.example.yaml ./data/hub.yaml      # edita hub_id y detector
export VITAHUB_ONVIF_USER=admin VITAHUB_ONVIF_PASSWORD=CLAVE
docker compose up --build

# en otra terminal, ver eventos + logs:
docker compose logs -f
```

El contenedor usa `network_mode: host` (necesario para el multicast ONVIF) y
`restart: unless-stopped` (se relanza tras un corte de luz).

---

## Diagnóstico rápido

| Síntoma | Causa probable / qué mirar |
|---|---|
| `config inválida: Falta la variable de entorno VITAHUB_ONVIF_PASSWORD` | No exportaste la credencial. |
| `config inválida: Falta 'hub_id'` | El fichero de config no existe o no tiene `hub_id` (en Docker: no sembraste `./data/hub.yaml`). |
| `descubrimiento: 0 cámaras encontradas` | La cámara no está en la LAN, ONVIF apagado, o el multicast no llega (WiFi que aísla clientes; con Docker asegúrate de `network_mode: host`). |
| `cam ... no abre, reintento en Ns` en bucle | La cámara se descubrió pero el RTSP no abre: credencial incorrecta, ruta/puerto RTSP distintos, o la cámara requiere auth que no cuadra. |
| No salen eventos aunque hay alguien | ¿`detector: stub`? (no emite). ¿confianza muy alta? Baja `confidence`. ¿Muy poca resolución? Prueba `stream: main`. |
| Una cámara nueva no aparece | El descubrimiento es solo al arranque en esta versión: `docker compose restart`. |

## Verificación previa a integrar (checklist)

- [ ] `pytest -v` → 54 verdes.
- [ ] `ruff check src tests` y `mypy` limpios.
- [ ] Arranque en seco (`stub`): descubre o avisa de 0 cámaras, sin caerse.
- [ ] Extremo a extremo con cámara real: `person_detected` al entrar y `person_absent` al salir.
