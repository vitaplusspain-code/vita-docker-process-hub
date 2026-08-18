# Diseño — URL RTSP persistida y telemetría de conexión

- Fecha: 2026-08-18
- Estado: aprobado en brainstorming, pendiente de plan de implementación
- Repo: `vita-docker-process-hub`
- Slice anterior: `2026-08-17-rescan-camaras-design.md`

## 0. El problema, con evidencia de campo

Durante la validación con la cámara piloto (Tuya, 2026-08-17) se midió esto:

1. Con ONVIF activado desde la app del móvil: puerto `10000` abierto, la sonda multicast
   responde, el hub descubre la cámara, obtiene sus URIs y conecta. Todo correcto.
2. Se desenchufa y se vuelve a enchufar la cámara.
3. Al volver: **RTSP vivo, ONVIF muerto**. Puerto `10000` en `Connection refused` y sonda
   multicast **0 de 6**. El firmware desactiva ONVIF en cada reinicio.

El hub siguió emitiendo eventos porque su worker ya tenía la URL en memoria. Pero el registro
persistido solo guarda `id`, `name` y `last_ip`: **la URL del stream no sobrevive a un reinicio del
hub**.

Consecuencia en un hogar real, que es el escenario explícito del producto ("se reinicia solo tras
cortes de luz"): un corte de luz reinicia el Jetson **y** la cámara. El hub arranca, lanza su sonda,
no encuentra nada —porque la cámara volvió sin ONVIF— y no tiene forma de reconectar con una cámara
que está ahí delante sirviendo vídeo. **La vivienda queda sin vigilancia y nadie se entera.**

Este slice cierra las dos mitades de ese agujero: que el hub recuerde cómo conectar, y que se note
cuando no puede.

## 1. Alcance

Dentro:

- Persistir en el registro las URIs RTSP (principal y substream) que ONVIF resolvió.
- Usarlas para conectar cuando el descubrimiento no encuentre a esa cámara.
- Evento `camera_unreachable` / `camera_reachable` por el sink.

Fuera, con motivo:

- **Adivinar la URL a partir de `last_ip`**: sigue descartado, y por la misma razón de siempre — la
  ruta varía por fabricante. Este slice *recuerda* lo que ONVIF dijo; no infiere nada.
- **Reserva DHCP por MAC**: la defensa contra el cambio de IP con ONVIF apagado es de despliegue,
  no de código. Va al protocolo de instalación en campo, no aquí.
- **Uplink de estos eventos a la nube**: siguen saliendo por `stdout` como el resto. El transporte
  es el backlog #3.

## 2. Decisiones cerradas en brainstorming

| Decisión | Elegido | Motivo |
|---|---|---|
| Dependencia de ONVIF | Híbrido: alta por ONVIF, operar por RTSP | ONVIF aporta descubrimiento e identidad estable; el vídeo no lo necesita. Con el firmware medido, exigirlo en cada arranque es exigir que un humano entre en la app tras cada apagón. |
| Qué se guarda | Las dos URIs, principal y substream | Cambiar `inference.stream` no debe obligar a redescubrir. |
| Precedencia | Lo descubierto gana sobre lo recordado | Lo descubierto es más fresco; lo recordado es la red de seguridad. |
| Credenciales en el fichero | Nunca | Se inyectan en memoria con `with_credentials`, como hoy. |
| Cámara inalcanzable | Evento por el sink | Un log que nadie lee no protege a nadie. Los eventos son el producto y el cauce que mañana llega a la nube. |

## 3. Persistencia y uso de la URL

### 3.1 Modelo

`Camera` gana dos campos opcionales, y con ellos la sección `cameras` del YAML:

```yaml
cameras:
- id: onvif-szjsa81a81e6adf9fe8b
  name: camera-1
  last_ip: 192.168.1.190
  rtsp_main: rtsp://192.168.1.190:554/V_ENC_000
  rtsp_sub: rtsp://192.168.1.190:554/V_ENC_001
  enabled: true
```

**Retrocompatibilidad obligatoria**: un `hub.yaml` sin esos campos debe cargar sin error. Son
opcionales y su ausencia significa "todavía no se ha descubierto esta cámara".

**Sin credenciales, nunca.** Se guarda la URI tal cual la devuelve ONVIF. Si por lo que sea llegara
con `usuario:clave@` en el netloc, hay que **eliminar el userinfo antes de persistir**: el fichero
de config no contiene secretos, y esa es una invariante del proyecto, no una preferencia.

### 3.2 Escritura

`reconcile()` pasa a actualizar también las URIs cuando el descubrimiento las trae, y a reportar el
cambio para que se persista. Una URI distinta a la guardada es un cambio real (cuenta para decidir
si se reescribe el YAML) y debe además provocar el relanzamiento del worker, igual que hoy hace un
cambio de IP.

### 3.3 Lectura

En cada rescan, al construir el mapa de URIs para el supervisor:

- Cámara **descubierta**: su URI sale del descubrimiento (gana siempre).
- Cámara **conocida pero no descubierta**, con URI recordada: su URI sale del registro.
- Cámara conocida sin URI recordada: no entra en el mapa (como hoy: el supervisor no la toca).

El supervisor no distingue el origen. Para él sigue siendo un `dict[camera_id, uri]`.

### 3.4 Uso a mano

El mismo campo puede rellenarse a mano. Eso cubre, con un único mecanismo, tres casos que de otro
modo pedirían tres soluciones: cámaras sin ONVIF, cámaras cuyo firmware lo apaga, y entornos donde
el multicast no llega (Docker Desktop en macOS, WiFi que aísla clientes). Debe quedar documentado
como uso legítimo, no como apaño.

## 4. Telemetría de conexión

### 4.1 Vocabulario

| Tipo | Severidad | Cuándo |
|---|---|---|
| `camera_unreachable` | `medium` | La cámara lleva **5 minutos** sin conseguir abrir el stream |
| `camera_reachable` | `info` | Vuelve a conectar, **solo si** antes se emitió un `unreachable` |

Payload de `camera_unreachable`: `last_ip` y `minutes_down`, siendo `minutes_down` el tiempo real
transcurrido desde la última conexión buena (no el umbral fijo). El envelope es el existente
(`Event.to_json()`), sin cambios.

Cada episodio de caída emite **un solo** `camera_unreachable`: no se repite mientras siga caída. Y
el reloj arranca con el propio hub, de modo que una cámara que nunca llega a conectar desde el
arranque también acaba emitiéndolo — es el caso del corte de luz que deja la cámara sin ONVIF y con
la IP cambiada, precisamente el que hay que ver.

Los cinco minutos no son arbitrarios: se midió que un tirón de cable tarda ~30 s solo en que el
watchdog de FFmpeg lo detecte, y a eso se suma el backoff de reconexión. El umbral tiene que quedar
cómodamente por encima de un reinicio normal de cámara o generará falsos avisos cada vez que alguien
desenchufe algo. La condición de `reachable` —solo tras un `unreachable`— evita ruido en cada
arranque.

### 4.2 Dónde vive

Componente propio, `analytics/connection_monitor.py`, con la misma forma que el motor anti-parpadeo:
estado por cámara más marca de tiempo entran, eventos salen. Función pura, sin red ni hilos, testeable
con tiempos inyectados.

`_camera_loop` solo le reporta dos hechos: "conecté" y "falló la conexión". No decide nada.

Esto abre el vocabulario de eventos más allá de la presencia de personas. Cuando exista el uplink,
son los eventos que permiten avisar a un humano de que una vivienda se ha quedado a ciegas —
justamente lo que hoy no ocurre.

## 5. Fallos

**La URI recordada apunta a una IP muerta** (el router cambió la IP con ONVIF apagado). El worker
reintenta con backoff; a los 5 minutos se emite `camera_unreachable`. Si ONVIF vuelve alguna vez, el
rescan la redescubre, actualiza IP y URIs, y el worker se relanza. La defensa real es la reserva
DHCP (§1, fuera de alcance).

**El YAML no se puede escribir.** Ya está resuelto en el slice anterior: se logea y no se aborta el
arranque de los workers. Las URIs recordadas se reintentan persistir en el siguiente rescan.

**Persistir una URI con credenciales.** Sería una fuga de secretos al fichero de config. Se ataja
eliminando el userinfo antes de guardar (§3.1), con test dedicado.

## 6. Testing

Sin red ni cámaras, como el resto del proyecto:

- **Modelo y config**: un `hub.yaml` sin los campos nuevos carga (retrocompatibilidad); con ellos,
  se leen y se reescriben íntegros; una URI con `usuario:clave@` se persiste **sin** el userinfo.
- **`reconcile`**: cámara nueva guarda sus URIs; URI cambiada se actualiza y se reporta como cambio;
  URIs iguales no generan cambio (no se reescribe el YAML cada 60 s).
- **Construcción del mapa de URIs**: descubierta gana sobre recordada; no descubierta usa la
  recordada; sin ninguna de las dos, no entra en el mapa.
- **`connection_monitor`**: no emite antes del umbral; emite `unreachable` al superarlo; emite
  `reachable` solo si hubo `unreachable` previo; no repite `unreachable` mientras siga caída; una
  cámara que nunca conectó desde el arranque acaba emitiéndolo; un episodio nuevo tras un
  `reachable` vuelve a emitir; dos cámaras no se contaminan entre sí.

## 7. Documentación

- `docs/como-funciona.md`: el registro guarda ahora cómo reconectar; el hub opera sin ONVIF tras el
  primer descubrimiento; los dos eventos nuevos.
- `docs/como-probar.md`: cómo declarar una cámara a mano (cubre el caso de macOS, que hoy está
  documentado como limitación sin salida).
- `docs/backlog.md`: anotar la reserva DHCP como paso del protocolo de instalación en campo, y el
  hallazgo del firmware que desactiva ONVIF al reiniciar — es información de compra, no solo de
  software.

## 8. Criterios de aceptación

1. Con la cámara descubierta y el hub parado: **desactivar ONVIF**, arrancar el hub, y que conecte
   igualmente usando la URI recordada. Es el escenario del corte de luz, y hoy falla.
2. Un `hub.yaml` de la versión anterior (sin los campos nuevos) arranca sin error.
3. Declarando a mano una cámara con su URI, el hub conecta sin descubrimiento alguno — verificable
   incluso en Docker sobre macOS.
4. Con la cámara apagada más de 5 minutos, sale un `camera_unreachable` por stdout; al encenderla,
   un `camera_reachable`.
5. Ninguna URI persistida en `hub.yaml` contiene credenciales.
6. `pytest`, `ruff check src tests` y `mypy` limpios.
