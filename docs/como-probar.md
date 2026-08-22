# Cómo probar el hub

Cuatro niveles, de más rápido a más real:

1. **Tests automáticos** — verifican la lógica; no necesitan cámara ni Docker.
2. **Arranque local en seco** (detector `stub`) — confirma que el proceso arranca y descubre.
3. **Extremo a extremo con cámara real** — ver eventos `person_detected` de verdad.
4. **Uplink a AWS** — confirmar que los eventos también llegan a la cuenta real (opcional).

Para entender qué hace por dentro, ver [como-funciona.md](como-funciona.md).

---

## 1. Tests automáticos

Instala en modo editable con las herramientas de desarrollo y corre la suite:

```bash
pip install -e ".[dev]"
pytest -v
```

Deberías ver **234 tests en verde**. Además, las mismas puertas que corren en CI:

```bash
ruff check src tests scripts
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
- Uplink a AWS: el `AwsIotSink` y el `FanoutSink` con un cliente MQTT falso — topic, payload exacto,
  que un `publish` fallido no propaga y que el fanout aísla fallos entre sinks. La validación de la
  config (`uplink.enabled` sin endpoint o sin certificados → el hub no arranca).
- Caídas: `test_fall_signals.py` (señales y score, puros), `test_fall_engine.py` (máquina de estados
  de episodio con reloj inyectado y poses sintéticas), `test_pose_detector.py` (mapeo de resultados
  Ultralytics falsos a `Detection` con `keypoints`) y `test_replay_video.py` (el CLI de calibración
  sobre frames sintéticos).

**No se cubren en CI (por diseño):** el descubrimiento ONVIF real por multicast, la captura RTSP con
OpenCV, la conexión MQTT real contra AWS IoT Core y el rendimiento real de `yolo11n-pose` en el Orin
— necesitan una LAN, una cámara, una cuenta AWS o el hardware del Jetson. Eso se valida a mano (paso
3, «Uplink a AWS» y «Calibrar caídas con un vídeo» más abajo); el coste de `yolo11n-pose` con 4
cámaras en Orin se mide en campo y se anota aquí cuando haya datos (ver también
[backlog.md](backlog.md)).

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

Para `person_pose`, `VITAHUB_POSE_WEIGHTS=yolo11n-pose.pt` y descárgalo antes con
`python scripts/download_model.py yolo11n-pose.pt` (desde este slice, si el fichero no existe el
hub no arranca y lo dice).

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

> **Para el hub antes de editar.** `RescanService` trabaja sobre la lista de cámaras que tiene en
> memoria, y `save_cameras` **sustituye entera** la sección `cameras` del YAML. Si editas
> `hub.yaml` a mano con el hub en marcha, tu cámara añadida desaparece sin ningún evento en el
> siguiente rescan que produzca un cambio — nunca llegó a tener worker, así que ni el
> `ConnectionMonitor` se entera. Procedimiento seguro: **para el hub, edita el fichero, arráncalo
> de nuevo** (`docker compose stop` / `Ctrl-C`, editar, `docker compose up` / `python -m
> vitahub.app`).

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

### 3.6 Calibrar caídas con un vídeo

`scripts/replay_video.py` pasa un `.mp4` grabado por el pipeline de caídas (detector `person_pose` +
`FallEngine`) sin necesidad de cámara en vivo ni de esperar a que alguien se caiga de verdad. Sirve
para calibrar pesos y umbrales antes de ir a un hogar.

Graba 3-4 clips cortos (con una persona real, en la oficina): caída frontal, caída lateral, tumbarse
en el sofá y agacharse a coger algo. Con los pesos de pose descargados (§3.2 más arriba):

```bash
python scripts/replay_video.py caida-frontal.mp4 --weights yolo11n-pose.pt
```

Imprime por **stdout** los eventos `fall_*` en JSON-lines (igual que el hub) y por **stderr** un
resumen (`frames`, recuento de eventos por tipo, `max_score`). Flags: `--fps` (muestreo, 2 por
defecto), `--min-score` (umbral, 0.3 por defecto), `--presence` (imprime también los `person_*`, que
por defecto se filtran).

Qué comprobar: el **orden de los scores** debe ser caída > tumbarse en el sofá > agacharse — y
agacharse **no** debe llegar a emitir `fall_detected` (se queda en `candidate` y vuelve a `upright`).
Si el orden sale distinto, o agacharse dispara, toca revisar los pesos/umbrales de
`fall_signals.py`/`fall_engine.py` antes de desplegar en un hogar.

---

## 4. Uplink a AWS

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

---

## Diagnóstico rápido

| Síntoma | Causa probable / qué mirar |
|---|---|
| `config inválida: Falta la variable de entorno VITAHUB_ONVIF_PASSWORD` | No exportaste la credencial. |
| `config inválida: Falta 'hub_id'` | El fichero de config no existe o no tiene `hub_id` (en Docker: no sembraste `./data/hub.yaml`). |
| `uplink.enabled es true pero falta la variable de entorno VITAHUB_IOT_ENDPOINT` | No exportaste el endpoint que imprimió `provision-hub.sh`. |
| `uplink.enabled es true pero no existe el fichero de certificado ...` | Falta sembrar `./data/certs/` con lo que dejó `provision-hub.sh` (ver README, «Uplink a AWS»). |
| `descubrimiento: 0 cámaras encontradas` | La cámara no está en la LAN, ONVIF apagado, o el multicast no llega (WiFi que aísla clientes; con Docker asegúrate de `network_mode: host`). |
| `cam ... no abre, reintento en Ns` en bucle | La cámara se descubrió pero el RTSP no abre: credencial incorrecta, ruta/puerto RTSP distintos, o la cámara requiere auth que no cuadra. |
| No salen eventos aunque hay alguien | ¿`detector: stub`? (no emite). ¿confianza muy alta? Baja `confidence`. ¿Muy poca resolución? Prueba `stream: main`. |
| Una cámara nueva no aparece | Espera a `discovery.interval_seconds` (60 s) o dispara `POST /rescan`. Si sigue sin salir, el problema es de descubrimiento (ONVIF/red), no de registro. |
| `POST /rescan` da 401 | El token de la cabecera no coincide con `VITAHUB_ADMIN_TOKEN`. |
| `POST /rescan` no conecta | El hub arrancó sin `VITAHUB_ADMIN_TOKEN` (mira el log `control HTTP deshabilitado`), o el puerto 8787 está ocupado por otro servicio del Jetson. |
| `POST /rescan` da 409 | No es un error: ya hay un escaneo en curso (puede tardar minutos, ver arriba). Espera y consulta el log, no reintentes en bucle corto. |
| `OSError: Read-only file system: '/app'` al arrancar en local | `VITAHUB_WEIGHTS` apunta a la ruta del contenedor (`/app/models/...`). En local: `export VITAHUB_WEIGHTS=yolo11n.pt` (o usa `detector: stub`). |
| `pesos del modelo no encontrados en X` | Descarga con `python scripts/download_model.py X` o usa `detector: stub`. |
| Sale `camera_unreachable` | La cámara lleva 5 min sin conectar: comprueba que está encendida, que su IP no ha cambiado y que la URI del registro sigue siendo válida. |

## Verificación previa a integrar (checklist)

- [ ] `pytest -v` → 234 verdes.
- [ ] `ruff check src tests scripts` y `mypy` limpios.
- [ ] Arranque en seco (`stub`): descubre o avisa de 0 cámaras, sin caerse.
- [ ] Extremo a extremo con cámara real: `person_detected` al entrar y `person_absent` al salir.
- [ ] Conectar una cámara con el hub ya corriendo: aparece sola en ≤60 s (o al instante con `POST /rescan`), sin reiniciar el contenedor.
