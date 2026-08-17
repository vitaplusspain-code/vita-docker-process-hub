# Diseño — Rescan de cámaras en caliente (periódico + comando LAN)

- Fecha: 2026-08-17
- Estado: aprobado en brainstorming, pendiente de plan de implementación
- Repo: `vita-docker-process-hub`
- Slice anterior: `2026-08-15-hub-procesamiento-camaras-design.md`

## 0. Contexto y problema

El primer slice dejó el hub funcionando extremo a extremo, pero con una limitación conocida y
documentada: **el descubrimiento ONVIF corre una sola vez, al arranque** (`app.run()`). Los hilos de
cámara se crean de golpe a partir de esa lista, comparten un único `threading.Event` de parada y
llevan su URI RTSP congelada desde el momento de su creación.

Consecuencias en campo:

- Una cámara añadida con el hub ya corriendo no se detecta: hay que hacer `docker compose restart`
  (hoy documentado como aviso en el README, paso 5).
- Una cámara que cambia de IP a mitad de ejecución no se reabsorbe hasta reiniciar.
- El `last_ip` que el registro persiste — cuyo propósito era sobrevivir reinicios — **nunca se usa**
  para conectar.

Este slice ataca el ítem #1 del backlog y, sobre el mismo motor, añade el disparo explícito que
motivó la petición original: que la app de un técnico pueda decirle al hub "busca cámaras ahora".

## 1. Alcance

Dentro:

- Supervisor de workers dinámico: arrancar y reemplazar hilos de cámara en caliente.
- Bucle de redescubrimiento periódico, cadencia `discovery.interval_seconds` (campo ya existente en
  la config, hasta ahora muerto).
- Endpoint HTTP local `POST /rescan` protegido por token, opt-in.
- Validación de `interval_seconds` en `load_config`.
- Actualización de docs que el cambio invalida.

Fuera, con motivo:

- **Comando remoto desde la nube.** Requiere el uplink completo (AWS IoT Core, certificado por hub,
  aprovisionamiento en campo, stack `02-hub-ingest` en el repo de arquitectura) — backlog #3, un
  slice propio. Este diseño deja la costura lista: el downlink, cuando exista, llamará al mismo
  `RescanService.run_once()` que llaman el timer y el HTTP.
- **Fallback RTSP por `last_ip`.** Construir una URL RTSP desde la IP guardada obliga a adivinar la
  ruta del stream, que varía por modelo — la misma fragilidad del `:10000` hardcodeado que ya
  costó una sesión de depuración. Con rescan cada 60s, una cámara conocida que vuelve se recupera
  sola por descubrimiento, que devuelve la URI **real**. El fallback deja de ser necesario.
- **`GET /status`.** El `POST /rescan` ya devuelve el estado y el técnico tiene `docker logs`.
- **Parada de workers por ausencia** (ver §3, decisión cerrada).

## 2. Decisiones cerradas en brainstorming

| Decisión | Elegido | Motivo |
|---|---|---|
| Qué dispara el rescan | Periódico **y** comando LAN | El periódico cubre el caso "el cliente coloca la cámara y el técnico está fuera", que hoy sin nube no tiene otra solución. El comando aporta inmediatez y confirmación al técnico presencial. |
| Transporte del comando | HTTP + token compartido | Es lo único que una app móvil puede llamar directamente. Señal y fichero centinela exigen shell en el Jetson. |
| Comando remoto ya | No | Arrastra el uplink entero para un solo botón. Se cablea al mismo motor cuando exista. |
| Cámara conocida ausente en el rescan | No se toca | El probe ONVIF multicast falla a menudo por WiFi. Parar un worker por un probe perdido apagaría una cámara sana; el worker ya reintenta solo con backoff. |
| Auth ausente | El servidor no arranca | Nunca hay un puerto sin auth. Los hogares ya instalados se actualizan sin exponer nada. |

## 3. Arquitectura

Tres componentes nuevos, todos testeables sin red ni cámara, y `app.run()` reducido a cableado.

### 3.1 `supervisor.py` — `CameraSupervisor`

Dueño único de los hilos de cámara. Mantiene `dict[camera_id → WorkerHandle]`, donde cada handle
lleva **su propio `threading.Event`** (no el global) y la URI con la que arrancó.

Método convergente e idempotente:

```
apply(cameras, uris) -> SupervisorChange(started=[ids], restarted=[ids])
```

Reglas, por cámara:

| Situación | Acción |
|---|---|
| Habilitada, con URI, sin worker vivo | Arranca worker |
| Habilitada, con URI distinta a la del worker vivo | Para el viejo, espera su muerte, arranca el nuevo |
| Habilitada, misma URI, worker vivo | No hace nada |
| Sin URI (conocida pero no descubierta) | No hace nada |
| Ausente del descubrimiento | No hace nada |
| `enabled: false` | No arranca; si tuviera worker vivo, lo para |

`stop_all()` señala todos los events y hace join para el apagado limpio.

Llamarlo dos veces seguidas con la misma entrada no produce efectos: eso es lo que permite usar el
mismo camino para el rescan inicial y para los periódicos, sin ramas duplicadas.

### 3.2 `rescan.py` — `RescanService`

La secuencia completa en un solo sitio:

1. `discover(credentials)` — inyectado como callable, para poder testear sin multicast.
2. `reconcile(cfg.cameras, discovered)` — la función existente, sin cambios.
3. `save_cameras(path, cameras)` **solo si `changes` no está vacío**. Hoy `run()` guarda siempre;
   repetido cada 60s serían escrituras a disco sin motivo.
4. Construir URIs con `with_credentials`, respetando `cfg.inference.stream`.
5. `supervisor.apply(...)`.
6. Devolver `RescanResult`, que alimenta a la vez la respuesta HTTP y el log.

Lleva un `threading.Lock` **no bloqueante**: timer y HTTP llaman a la misma función; si ya hay un
rescan en vuelo, el segundo no se encola — devuelve "ocupado".

### 3.3 `control.py` — servidor HTTP

`ThreadingHTTPServer` de la stdlib, **cero dependencias nuevas**. Recibe un `RescanService`
inyectado, así que se testea con un doble.

### 3.4 `app.run()` resultante

Cargar config → instalar handlers de señal (siguen siendo lo primero) → detector, engine, sink →
supervisor → **rescan inicial por el mismo camino que el periódico** → arrancar HTTP si hay token →
arrancar el hilo de rescan periódico → bucle de heartbeat cada 5s.

**El rescan periódico corre en su propio hilo daemon, no en el hilo del heartbeat.** El healthcheck
marca el contenedor `unhealthy` si el heartbeat pasa de 60s (`scripts/healthcheck.py`); si el rescan
compartiera hilo con el latido, un `discover()` lento o colgado provocaría el reinicio de un hub que
está funcionando perfectamente. El heartbeat no debe depender nunca de la duración del
descubrimiento. Ese hilo espera `interval_seconds` sobre el event global de parada (no `sleep`), de
modo que el apagado es inmediato y no aguarda al siguiente ciclo.

**El rescan no recarga el YAML del disco.** Opera sobre la lista de cámaras en memoria y la persiste
cuando hay cambios. Por tanto, editar `hub.yaml` a mano (p. ej. poner `enabled: false`) sigue
requiriendo reiniciar el contenedor; la regla de `enabled` en §3.1 es defensiva, no un mecanismo de
control en caliente.

Apagado: event global → `supervisor.stop_all()` → `httpd.shutdown()` → `sink.close()`.

## 4. Contrato del endpoint

```
POST /rescan
Authorization: Bearer <VITAHUB_ADMIN_TOKEN>
```

Sin parámetros ni body: no hay nada que parsear, luego no hay superficie de inyección.

| Variable | Por defecto | Nota |
|---|---|---|
| `VITAHUB_ADMIN_TOKEN` | — | Si falta, el servidor **no arranca** y se logea `control HTTP deshabilitado: define VITAHUB_ADMIN_TOKEN`. |
| `VITAHUB_ADMIN_PORT` | `8787` | Con `network_mode: host` es un puerto del Jetson; puede chocar con otro servicio. |

- Token comparado con `hmac.compare_digest` (evita el timing leak).
- El token se pasa por `register_secret()` para que la redacción de logs lo tape, igual que la
  credencial ONVIF.
- Bind a `0.0.0.0`: con `network_mode: host` es la única forma de que lo alcance el móvil del
  técnico. Implicación aceptada conscientemente: el puerto es visible para toda la WiFi del hogar,
  y por eso el token es obligatorio.

Respuestas:

| Código | Cuándo | Cuerpo |
|---|---|---|
| `200` | Rescan completado | `{"found": 2, "added": [{"id","name","ip"}], "ip_changed": [...], "started": [...], "cameras": 2}` |

Semántica de los campos: `found` = cámaras devueltas por el descubrimiento en **este** ciclo;
`added` = altas nuevas en el registro; `ip_changed` = ids cuya IP se reabsorbió; `started` = ids cuyo
worker se arrancó o relanzó; `cameras` = total de cámaras registradas tras el rescan.
| `401` | Token ausente o incorrecto | Vacío, sin pistas |
| `404` | Cualquier otra ruta o método | Vacío |
| `409` | Ya hay un rescan en vuelo | Vacío |
| `500` | El descubrimiento lanzó | Vacío; el detalle va al log |

## 5. Fallos y su tratamiento

**Reemplazo por cambio de IP.** El worker viejo puede estar bloqueado dentro de `cap.read()` y no
morir al instante. Regla: **no se arranca el reemplazo hasta confirmar que el anterior murió**
(`join` con timeout ~15s). Si no muere, se logea y se reintenta en el siguiente ciclo. Arrancar los
dos a la vez produciría eventos duplicados de la misma cámara, que el motor anti-parpadeo no puede
distinguir de actividad real.

**`save_cameras` falla** (disco lleno, `/data` en solo lectura). Se logea el error pero **no se
aborta el `apply`**: es preferible que las cámaras emitan aunque el registro no haya persistido. Se
reintenta en el siguiente rescan.

**`discover()` lanza.** Se captura dentro del servicio: no tumba el hilo del timer ni el proceso.
Los workers vivos siguen intactos — un rescan fallido nunca degrada lo que ya funciona.

**Estado del motor de eventos.** El `EventEngine` es compartido y guarda estado por cámara, así que
reiniciar un worker por cambio de IP no pierde el estado anti-parpadeo de esa cámara.

## 6. Guardarraíl de config

`discovery.interval_seconds` pasa a usarse de verdad. `load_config` debe rechazar valores `<= 0` con
`ConfigError`: hoy nadie los valida y producirían un rescan en bucle cerrado martilleando la LAN.

## 7. Testing

Todo lo nuevo se verifica sin cámara y sin multicast, en línea con el criterio del repo (lógica pura
en CI, red a mano).

**`supervisor`**, con una función de worker falsa que solo espera su event:

- Arranca una cámara nueva.
- Segunda llamada idéntica no arranca nada (idempotencia).
- URI cambiada → para el viejo y arranca uno nuevo.
- Cámara ausente del descubrimiento → no la toca.
- Cámara sin URI → no arranca.
- `enabled: false` → no arranca; si tenía worker, lo para.
- `stop_all` los para todos.
- Worker falso que tarda en morir → **el reemplazo no arranca hasta que el anterior ha muerto**.

**`rescan`**, con `discover` falso:

- El alta de una cámara nueva persiste en el YAML.
- Sin cambios **no se llama a `save_cameras`** (blinda que no se escribe a disco cada 60s).
- `discover` que lanza → resultado de error, sin excepción propagada.
- `save_cameras` que falla → el `apply` ocurre igualmente.
- Segunda llamada concurrente → "ocupado".

**`control`**, con servicio doble y server en `127.0.0.1:0` (puerto efímero, localhost — corre en CI
sin tocar la LAN):

- Sin token configurado, el server no arranca.
- Token incorrecto → 401; correcto → 200 con el JSON esperado.
- Ruta y método desconocidos → 404.
- Servicio ocupado → 409.
- El token sale **redactado** en los logs.

**`config`**: `interval_seconds` `<= 0` → `ConfigError`.

**Hilo de rescan periódico**: con el event de parada señalado, el hilo sale sin esperar al siguiente
ciclo (verifica que espera sobre el event y no con `sleep`).

## 8. Despliegue y documentación

`docker-compose.yml` pasa `VITAHUB_ADMIN_TOKEN` y `VITAHUB_ADMIN_PORT`. El token se genera con
`openssl rand -hex 32` y va por entorno, nunca en `hub.yaml` — mismo criterio que la credencial
ONVIF.

Docs que el cambio **invalida** y hay que corregir, no dejar contradiciendo al código:

- `README.md` paso 5: el aviso de que una cámara nueva exige `docker compose restart` deja de ser
  cierto. Sustituir por la explicación del rescan periódico y del comando.
- `docs/como-probar.md`: misma fila en la tabla de diagnóstico; añadir cómo probar el endpoint con
  `curl`; actualizar el recuento de tests.
- `docs/como-funciona.md`: describir el ciclo de rescan.
- `docs/backlog.md`: el ítem #1 se cierra a medias — el redescubrimiento periódico entra; el
  fallback por `last_ip` queda descartado **con la razón escrita** (§1), no tachado.

Ejemplo de uso en campo:

```bash
curl -X POST -H "Authorization: Bearer $VITAHUB_ADMIN_TOKEN" \
  http://192.168.1.50:8787/rescan
```

## 9. Criterios de aceptación

1. Con el hub corriendo, conectar una cámara nueva a la LAN: aparece sola en `docker logs` en
   ≤ `interval_seconds` y empieza a emitir eventos, sin reiniciar el contenedor.
2. `POST /rescan` con token válido la da de alta en el momento y responde el JSON con `added` y
   `started`.
3. `POST /rescan` sin token o con token incorrecto → 401; con `VITAHUB_ADMIN_TOKEN` sin definir, no
   hay puerto escuchando.
4. Cambiar la IP de una cámara ya registrada: el hub reabsorbe el cambio y su worker se relanza con
   la URI nueva, sin duplicar eventos.
5. Apagar una cámara: su worker sigue reintentando con backoff y **no** se da de baja del registro.
6. `pytest`, `ruff check src tests` y `mypy` limpios.
