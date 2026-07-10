# VitaPlus — Especificación de desarrollo: Hub de Procesamiento Local

**Destinatario:** desarrollador responsable del hub
**Versión:** 1.0 — documento de trabajo
**Contexto:** VitaPlus es una plataforma de monitorización de personas mayores con tres fuentes de datos: cámaras (rama core), pulsera/smartwatch y llamadas con IA. El hub es la pieza que se instala en el hogar y convierte señales sensibles (sobre todo vídeo) en eventos estructurados que viajan a la nube.

---

## 1. Objetivo del hub

El hub local es el cerebro del hogar. Su misión NO es grabar ni retransmitir vídeo, sino **transformar señales brutas en eventos semánticos** ("posible caída", "entrada en cocina", "4 horas sin movimiento") y enviarlos al backend cloud. Esto responde a tres motivos de diseño:

1. **Privacidad (privacy-first):** el vídeo bruto no debe salir del hogar salvo clips mínimos ante alerta crítica y con consentimiento.
2. **Coste:** procesar en local reduce drásticamente ancho de banda y coste de nube.
3. **Latencia y resiliencia:** una caída debe detectarse aunque se corte internet.

### Alcance del hub por cada una de las tres patas

| Pata | Papel del hub |
|---|---|
| Cámaras (core) | Procesamiento completo: ingesta RTSP, inferencia IA, motor de eventos, clips de alerta. **Es el grueso del desarrollo.** |
| Pulsera | El wearable sincroniza vía app móvil puente → backend cloud directamente. El hub NO procesa biometría en el MVP; solo puede recibir del cloud eventos del wearable (ej. SOS) para correlación local en fases futuras. |
| Llamadas IA | Se ejecutan en cloud (telefonía + LLM). El hub no interviene. Opcionalmente, el motor de eventos del hub puede disparar una petición de llamada de comprobación (ej. tras inactividad prolongada). |

**Conclusión de alcance:** el hub del MVP es esencialmente el pipeline de visión artificial + motor de eventos + sincronización con cloud + salud del sistema.

---

## 2. Hardware y entorno de ejecución

| Componente | Requisito | Motivo |
|---|---|---|
| Equipo | Mini-PC x86 o NVIDIA Jetson (Orin Nano recomendado si se usa aceleración GPU) | Inferencia en tiempo real de 1-4 cámaras. |
| Almacenamiento | SSD local, partición cifrada para buffer de clips | Resiliencia y revisión puntual de incidentes. |
| Red | Ethernet al switch PoE de cámaras; WiFi y 4G/5G como respaldo | Continuidad del servicio. |
| SO | Ubuntu LTS + Docker Compose (alternativa futura: BalenaOS para gestión de flota) | Mantenimiento y actualizaciones controladas. |
| Cámaras | IP PoE con RTSP/ONVIF (Reolink RLC-810A/520A en MVP). **No** cámaras cloud cerradas tipo Ring/Nest | Acceso local al flujo de vídeo, control de privacidad. |
| Red local | VLAN dedicada para cámaras, sin puertos abiertos al exterior | Ciberseguridad. |

Patrón de flujos de vídeo (configurar en cada cámara):

- **Substream de detección:** 640x360 o 704x480, 5-10 FPS, H.264 → se procesa, no se guarda.
- **Mainstream:** 2K/4K, 15-25 FPS → solo para clips de 10-30 s alrededor de un evento crítico.
- **Snapshot:** frame puntual para validación/debug, eliminado o anonimizado pronto.

---

## 3. Arquitectura interna del hub

Todo corre en contenedores Docker orquestados con Docker Compose. Servicios desacoplados que se comunican por MQTT local (broker Mosquitto) y/o Redis.

```
┌──────────────────────────── HUB LOCAL ────────────────────────────┐
│                                                                    │
│  Cámaras RTSP ──► [camera-connector / Frigate] ──► detecciones     │
│                            │                                       │
│                            ▼ (MQTT)                                │
│                     [event-engine]  ◄── config/reglas por usuario  │
│                            │                                       │
│              eventos       ▼            clips                      │
│                     [local-cache (SQLite)]──►[clip-manager]        │
│                            │                                       │
│                            ▼                                       │
│                     [edge-sync] ──HTTPS/MQTT TLS──► Cloud backend  │
│                                                                    │
│  [health-monitor] vigila todo y emite heartbeats/alertas técnicas  │
│  [ota-updater] recibe actualizaciones firmadas de modelos/reglas   │
└────────────────────────────────────────────────────────────────────┘
```

### 3.1 Módulos a desarrollar

| Módulo | Responsabilidad | Tecnología sugerida | Prioridad |
|---|---|---|---|
| `camera-connector` | Conexión RTSP, extracción de frames, reconexión automática | Frigate (recomendado como base) o FFmpeg/OpenCV propio | P0 |
| `vision-inference` | Detección de persona/objetos y pose | Frigate + YOLO (Ultralytics, export ONNX/TensorRT); MediaPipe Pose para caídas | P0 |
| `tracker` | Seguimiento temporal de la persona | ByteTrack/SORT o tracking integrado de Ultralytics | P1 |
| `event-engine` | Convertir detecciones en eventos de negocio con reglas temporales y zonas | Servicio Python propio (núcleo del desarrollo, ver §4) | P0 |
| `local-cache` | Estado reciente, cola de eventos pendientes, clips temporales | SQLite/DuckDB + disco cifrado | P0 |
| `clip-manager` | Grabar clip mainstream 10-30 s pre/post evento crítico, cifrar, purgar | FFmpeg + política de retención | P1 |
| `edge-sync` | Envío de eventos al cloud con cola local, reintentos y backoff | HTTPS o MQTT sobre TLS, idempotencia por `event_id` | P0 |
| `health-monitor` | Heartbeat de cámaras, CPU, disco, temperatura, red; alertas técnicas | Watchdog propio + node exporter, logs a Sentry | P0 |
| `ota-updater` | Actualización remota de modelos, reglas y contenedores, firmada | Docker pull firmado / watchtower controlado | P1 |
| `privacy-guard` | Modo privacidad (pausa por estancia/horario), difuminado, descarte de vídeo bruto | Configuración + lógica transversal | P0 |

**Recomendación de arranque:** no reinventar el NVR. Levantar Frigate como base de ingesta + detección (soporta zonas, MQTT, substream/mainstream) y concentrar el desarrollo propio en el `event-engine`, `edge-sync` y `health-monitor`, que es donde está el valor diferencial.

---

## 4. Event-engine: el corazón del desarrollo

El event-engine escucha detecciones (vía MQTT desde Frigate) y mantiene una máquina de estados por persona/zona para generar eventos de negocio. No basta un frame: hay que razonar sobre secuencias temporales.

### 4.1 Eventos del MVP (en orden de implementación)

| Evento | Lógica | Viabilidad |
|---|---|---|
| `presence_zone` | Persona detectada dentro de zona virtual definida por polígono | Alta |
| `home_exit` / `home_entry` | Cruce de zona puerta + dirección (mejor con sensor de puerta) | Alta |
| `inactivity_prolonged` | Sin detección de persona/movimiento durante X min en horario en que debería haber actividad | Alta |
| `kitchen_activity` | Presencia en cocina + tiempo + movimiento (proxy de alimentación, nunca "ha comido bien") | Media-alta |
| `night_activity_unusual` | Actividad fuera de la franja de sueño pactada | Alta |
| `probable_fall` | Pose horizontal/baja + transición brusca + inmovilidad posterior ≥ N segundos | Media — requiere validación (llamada/wearable) antes de escalar |
| `routine_missed` | No se cumple rutina pactada (ej. no entra en cocina antes de las 11:00) | Alta |

**No implementar en MVP:** reconocimiento facial, inferencia emocional por cámara, detección de toma de medicación solo por visión, promesa de detección de caídas al 100%.

### 4.2 Diseño técnico del event-engine

- Máquina de estados por zona y por persona, con timers (ej. `apscheduler` o loop asyncio con ticks).
- Reglas parametrizadas por usuario descargadas del cloud (umbrales, horarios, zonas). Nunca hardcodear.
- Cada evento lleva `confidence` y ventana temporal (`start_time`, `end_time`).
- Histéresis y debouncing para evitar parpadeo de eventos (persona que entra/sale del borde de una zona).
- Los eventos críticos (`probable_fall`) disparan además al `clip-manager`.

### 4.3 Contrato de evento (JSON hacia el cloud)

```json
{
  "event_id": "evt_9f3a...",            // UUID, idempotencia
  "hub_id": "hub_0042",
  "user_id": "usr_001",
  "camera_id": "cam_cocina",
  "type": "probable_fall",
  "severity": "critical",               // info | warning | critical
  "confidence": 0.81,
  "zone": "cocina",
  "start_time": "2026-07-10T10:32:05Z",
  "end_time": "2026-07-10T10:33:40Z",
  "metadata": { "pose": "horizontal", "immobile_seconds": 95 },
  "evidence": { "clip_ref": "clip_local_123", "uploaded": false },
  "schema_version": "1.0"
}
```

El backend responde con ACK; sin ACK, el evento permanece en la cola local (SQLite) y se reintenta con backoff exponencial.

---

## 5. Sincronización con el cloud (`edge-sync`)

- Transporte: HTTPS POST a la API de ingesta o MQTT sobre TLS. Elegir uno y mantenerlo simple (Kafka no es necesario en MVP).
- **Cola local persistente:** todos los eventos se escriben primero en SQLite; un worker los envía y marca como confirmados. Si internet cae, nada se pierde.
- Idempotencia por `event_id` (el backend debe deduplicar).
- Autenticación: clave/certificado único por hub, aprovisionado en fábrica u onboarding (QR). Rotación posible desde cloud.
- Los clips solo se suben bajo demanda (alerta crítica confirmada o petición autorizada), cifrados, a object storage.
- Heartbeat cada 60 s con estado de cámaras, CPU, disco y versión de software. La ausencia de heartbeat genera alerta técnica en cloud.

---

## 6. Privacidad y seguridad (requisitos no negociables)

- El vídeo continuo **nunca** sale del hogar. Solo eventos, métricas y clips mínimos de incidentes.
- Sin streaming continuo para familiares. El producto muestra eventos y semáforo, no vigilancia.
- Sin cámaras en baños; máxima restricción en dormitorios.
- Modo privacidad activable (por horario, estancia o botón) que pausa procesamiento y lo registra en auditoría.
- Disco de clips cifrado (LUKS o equivalente); claves por dispositivo.
- Retención de clips: 10-30 s por evento, purga automática según política de consentimiento.
- VLAN de cámaras aislada, cero puertos abiertos hacia internet (el hub siempre inicia conexiones salientes).
- Actualizaciones firmadas; rechazar imágenes/modelos no verificados.
- Log de auditoría local + remoto: quién/qué accedió a clips, cambios de configuración, activaciones de modo privacidad.
- Todo el diseño debe pasar revisión RGPD antes del piloto real.

---

## 7. Resiliencia y operación

| Situación | Comportamiento esperado |
|---|---|
| Corte de internet | El hub sigue detectando; eventos en cola local; al volver la red se sincronizan en orden. |
| Cámara caída | `health-monitor` lo detecta por ausencia de frames/heartbeat RTSP y emite alerta técnica. |
| Reinicio/corte de luz | Arranque automático de todos los contenedores (restart: always) y recuperación de estado desde SQLite. Valorar SAI pequeño. |
| Disco lleno | Purga de clips más antiguos primero; nunca bloquear el pipeline de eventos. |
| Actualización fallida | Rollback automático a versión anterior del contenedor/modelo. |

Observabilidad: logs estructurados, Sentry para errores, métricas Prometheus (frames procesados/s, latencia de inferencia, cola pendiente, temperatura, uso de disco).

---

## 8. Stack tecnológico recomendado (resumen)

| Área | Recomendado | Alternativa |
|---|---|---|
| Runtime edge | Ubuntu + Docker Compose | BalenaOS (gestión de flota, fase 2) |
| Ingesta/NVR/IA base | Frigate | Pipeline propio Python + YOLO |
| Detección | Ultralytics YOLO + ONNX/TensorRT | MediaPipe Pose, OpenVINO |
| Vídeo | FFmpeg + OpenCV | GStreamer |
| Mensajería interna | MQTT (Mosquitto) / Redis | — |
| Cache local | SQLite | DuckDB |
| Servicios propios | Python 3.11+, asyncio | — |
| Sync a cloud | HTTPS o MQTT TLS con reintentos | — |
| Observabilidad | Sentry + Prometheus/Grafana | — |
| CI/CD | GitHub Actions + registry Docker | GitLab CI |

---

## 9. Plan de desarrollo propuesto (≈12 semanas, solo hub)

1. **S1:** montar red local, cámaras, RTSP; validar substream/mainstream.
2. **S2:** Frigate en Docker con detección de persona; MQTT funcionando.
3. **S3:** zonas virtuales por cámara; eventos crudos de presencia.
4. **S4:** `event-engine` v1: presencia, actividad, inactividad prolongada con reglas configurables.
5. **S5:** `local-cache` + contrato JSON de eventos definitivo (coordinar schema con backend).
6. **S6:** `edge-sync` con cola persistente, reintentos e idempotencia; primer evento en cloud.
7. **S7:** `health-monitor` + heartbeats + alertas técnicas.
8. **S8:** `clip-manager`: clip cifrado pre/post evento crítico, subida bajo demanda.
9. **S9:** detección de caída probable (pose + reglas temporales).
10. **S10:** pruebas internas, medición de falsos positivos/negativos, ajuste de umbrales.
11. **S11:** hardening: cifrado de disco, modo privacidad, auditoría, OTA firmada.
12. **S12:** piloto en 1-3 hogares controlados; informe de métricas.

### Criterios de aceptación del MVP del hub

- Detecta presencia/inactividad/entrada-salida con < X% de falsos positivos (fijar objetivo en piloto, sugerido < 1 alerta falsa/día/hogar).
- Ningún fotograma de vídeo continuo llega al cloud; solo eventos y clips de alerta.
- Cero pérdida de eventos con cortes de internet de hasta 24 h.
- Recuperación automática total tras corte de luz sin intervención.
- Caída probable genera evento + clip en < 30 s desde el suceso.
- Actualización remota de reglas y modelo sin visita al hogar.

---

## 10. Fuera de alcance del hub (para evitar confusión)

- Procesamiento de biometría del wearable (va por app puente → cloud).
- Ejecución de llamadas IA (va en cloud con proveedor de telefonía).
- Lógica de escalado de alertas a familiares (vive en el backend; el hub solo clasifica severidad inicial).
- Cualquier diagnóstico médico. El hub genera eventos de bienestar y seguridad; el lenguaje de los eventos nunca debe ser clínico.

---

## 11. Riesgos técnicos a vigilar

| Riesgo | Mitigación |
|---|---|
| Falsos positivos de caída | Reglas temporales + confirmación por llamada/wearable antes de escalar a emergencia. |
| Mala iluminación nocturna | Cámaras con IR y pruebas nocturnas en S10. |
| Coste de inferencia por hogar | Substream de baja resolución, aceleración HW (TensorRT), 2 cámaras máx. en MVP. |
| Manipulación del hub | Disco cifrado, secure boot si el hardware lo permite, actualizaciones firmadas. |
| Rechazo por privacidad | Modo privacidad, transparencia, eventos-no-vídeo, consentimiento documentado. |
