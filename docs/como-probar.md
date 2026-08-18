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

Deberías ver **139 tests en verde**. Además, las mismas puertas que corren en CI:

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

# IMPORTANTE en local: VITAHUB_WEIGHTS por defecto es /app/models/yolo11n.pt
# (la ruta DENTRO del contenedor, donde la imagen embebe los pesos). En tu
# máquina esa ruta no existe y Ultralytics intentaría descargar ahí y fallaría.
# Apúntalo a una ruta escribible; "yolo11n.pt" descarga al directorio actual
# la primera vez (necesita internet una vez).
export VITAHUB_WEIGHTS=yolo11n.pt

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

> **En macOS, el hub en Docker NO descubrirá tus cámaras.** No es un fallo del hub ni de la
> configuración: Docker Desktop corre una VM Linux, así que `network_mode: host` significa "la red
> de la VM" (`192.168.65.x`), no tu LAN. La sonda multicast a `239.255.255.250` se queda dentro de
> la VM y `discover()` devuelve siempre 0 cámaras. Comprobado: el tráfico **unicast** sí sale (un
> contenedor alcanza el `:10000` ONVIF y el `:554` RTSP de una cámara de la LAN), lo único que no
> atraviesa es el multicast.
>
> Consecuencia práctica: si quieres **descubrimiento** con cámara real, pruébalo en local (§3.2), no
> en Docker sobre Mac. Docker en el Mac sirve para validar el empaquetado, el arranque, el endpoint
> de control y el healthcheck — no el descubrimiento por multicast. La prueba de descubrimiento con
> Docker se hace en el Jetson, que es Linux nativo y donde esto funciona por diseño. Para probar
> **conexión e ingesta** con Docker en el Mac sin descubrimiento, declara la cámara a mano (§3.5).
>
> Añadido: en macOS el puerto de control tampoco es alcanzable desde el Mac con `network_mode:
> host` (`curl` responde "Couldn't connect to server"). Para eso está `docker-compose.mac.yml`, que
> pasa a red bridge y publica el `8787`:
>
> ```bash
> docker compose -f docker-compose.yml -f docker-compose.mac.yml up
> ```
>
> Hay una salida sin cambiar de motor de contenedores: **declarar la cámara a mano** en
> `./data/hub.yaml` con su URI RTSP (ver §3.5). El hub conecta sin descubrimiento alguno, así que
> el multicast deja de hacer falta. Si aun así quisieras descubrimiento real en Docker sobre Mac, la
> única vía es un motor con red *bridged* (Colima/Lima con `socket_vmnet`), que pone la VM en la LAN
> con su propia IP.

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

> **Importante — usa un timeout generoso al llamar a este endpoint.** `discover()` no tiene un
> tope de tiempo global (es trabajo del siguiente slice de ONVIF, ver `docs/backlog.md`): en el
> peor caso — varias IPs que contestan al multicast pero no hablan ONVIF de verdad, más 8 s por
> cada llamada SOAP — un escaneo puede tardar **minutos**. Un cliente (p. ej. la app del técnico)
> con un timeout corto verá lo que parece un cuelgue, reintentará y recibirá `409`. Un `409` **no
> es un fallo**: significa "ya hay un escaneo en marcha, espera y consulta el resultado en los
> logs (`docker compose logs -f`)", no que algo se rompió.

### 3.5 Declarar una cámara a mano

Sirve para cámaras sin ONVIF, para firmware que lo desactiva al reiniciar, y para entornos donde el
multicast no llega (Docker sobre macOS, WiFi que aísla clientes). La entrada va **dentro de la lista
`cameras:` que `hub.yaml` ya tiene** — no crees una segunda clave `cameras:` al final del fichero.
En cuanto el hub ha descubierto algo una vez, esa clave ya existe y ya tiene cámaras dentro; un YAML
con `cameras:` repetido no da error, se queda **en silencio** con el último bloque, así que pegar
uno nuevo puede borrar el registro de cámaras descubiertas (con sus URIs recordadas) o tu cámara
manual, según cuál quede segunda. Fichero completo, con una cámara ya descubierta y la entrada nueva
añadida a la misma lista:

```yaml
hub_id: hub-3f9a
discovery:
  interval_seconds: 60
inference:
  detector: person_yolo
  sample_fps: 2
  confidence: 0.4
  stream: substream
cameras:
- id: onvif-szjsa81a81e6adf9    # cámara ya descubierta — no tocar
  name: camera-1
  last_ip: 192.168.1.180
  rtsp_main: rtsp://192.168.1.180:554/V_ENC_000
  rtsp_sub: rtsp://192.168.1.180:554/V_ENC_001
  enabled: true
- id: camara-salon               # la entrada nueva, en la MISMA lista
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

---

## Diagnóstico rápido

| Síntoma | Causa probable / qué mirar |
|---|---|
| `config inválida: Falta la variable de entorno VITAHUB_ONVIF_PASSWORD` | No exportaste la credencial. |
| `config inválida: Falta 'hub_id'` | El fichero de config no existe o no tiene `hub_id` (en Docker: no sembraste `./data/hub.yaml`). |
| `descubrimiento: 0 cámaras encontradas` | La cámara no está en la LAN, ONVIF apagado, o el multicast no llega (WiFi que aísla clientes; con Docker asegúrate de `network_mode: host`). |
| `cam ... no abre, reintento en Ns` en bucle | La cámara se descubrió pero el RTSP no abre: credencial incorrecta, ruta/puerto RTSP distintos, o la cámara requiere auth que no cuadra. |
| No salen eventos aunque hay alguien | ¿`detector: stub`? (no emite). ¿confianza muy alta? Baja `confidence`. ¿Muy poca resolución? Prueba `stream: main`. |
| Una cámara nueva no aparece | Espera a `discovery.interval_seconds` (60 s) o dispara `POST /rescan`. Si sigue sin salir, el problema es de descubrimiento (ONVIF/red), no de registro. |
| `POST /rescan` da 401 | El token de la cabecera no coincide con `VITAHUB_ADMIN_TOKEN`. |
| `POST /rescan` no conecta | El hub arrancó sin `VITAHUB_ADMIN_TOKEN` (mira el log `control HTTP deshabilitado`), o el puerto 8787 está ocupado por otro servicio del Jetson. |
| `POST /rescan` da 409 | No es un error: ya hay un escaneo en curso (puede tardar minutos, ver arriba). Espera y consulta el log, no reintentes en bucle corto. |
| `OSError: Read-only file system: '/app'` al arrancar en local | `VITAHUB_WEIGHTS` apunta a la ruta del contenedor (`/app/models/...`). En local: `export VITAHUB_WEIGHTS=yolo11n.pt` (o usa `detector: stub`). |
| Sale `camera_unreachable` | La cámara lleva 5 min sin conectar: comprueba que está encendida, que su IP no ha cambiado y que la URI del registro sigue siendo válida. |

## Verificación previa a integrar (checklist)

- [ ] `pytest -v` → 139 verdes.
- [ ] `ruff check src tests` y `mypy` limpios.
- [ ] Arranque en seco (`stub`): descubre o avisa de 0 cámaras, sin caerse.
- [ ] Extremo a extremo con cámara real: `person_detected` al entrar y `person_absent` al salir.
- [ ] Conectar una cámara con el hub ya corriendo: aparece sola en ≤60 s (o al instante con `POST /rescan`), sin reiniciar el contenedor.
