# Clip de caída con riesgo (lado hub) — Plan de implementación

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** que el hub grabe en memoria los últimos segundos de vídeo de cada cámara habilitada y, ante un `fall_detected` con riesgo, monte un MP4 de 15 s antes + 10 s después y lo suba a S3, emitiendo `fall_clip_ready` / `fall_clip_discarded` / `fall_clip_failed`.

**Architecture:** subsistema nuevo `src/vitahub/clips/`. Un `ClipRecorder` por cámara (hilo, PyAV, sin decodificar) llena un `PacketBuffer`. Un `ClipTriggerSink` envuelve el sink del hub, abre ventanas con `fall_detected`, las cierra a los 10 s, evalúa el riesgo con `risk.assess` y, si hay riesgo, monta el MP4 (`assembler`) y lo deja en una `PendingQueue` en disco que un `ClipUploader` sube a S3 con credenciales del credentials provider de IoT.

**Tech Stack:** Python 3.12, PyAV (`av`, ya dependencia vía aiortc), botocore, pytest.

**Spec:** `docs/superpowers/specs/2026-10-22-clip-caida-design.md`. Léelo antes de cada tarea; este plan no repite su razonamiento.

## Global Constraints

- Constantes (no config), en `src/vitahub/clips/constants.py`: `PRE_S = 15.0`, `POST_S = 10.0`, `MAX_POST_S = 30.0`, `BUFFER_S = 45.0`, `RISK_MIN_DROP = 0.5`, `PENDING_MAX_MB = 200`, `PENDING_MAX_AGE_S = 3 * 24 * 3600`.
  - `BUFFER_S` es 45 y no 30 (decisión del plan): el clip más largo posible es `PRE_S + MAX_POST_S` = 45 s, y con 30 s el pre-roll se perdería al alargar la ventana.
- Todos los eventos nuevos con severidad `"info"` y el `camera_id`/`camera_name` de la cámara del clip.
- Clave S3: `{hub_id}/{camera_id}/{episode_id}.mp4` (`episode_id` = el primero del clip).
- Ningún fallo en `clips/` puede propagarse al hilo de cámara ni al sink de texto: `except Exception` + `_log.exception`, como el resto del hub.
- Un clip sin riesgo nunca se escribe a disco.
- Comentarios y mensajes en español, como el resto del repo. `ruff` y `mypy --strict` limpios.
- Tests: `pytest -v` desde la raíz del repo.

## Review Focus

1. **Cámara reconectada en medio de una ventana:** el buffer se reinicia con otro stream; el snapshot no puede mezclar paquetes de dos streams. Lo esperado: clip solo con el stream actual, o `no_video` si no cubre el disparo. Test en la Tarea 2.
2. **Reloj del sistema que salta (NTP) con una ventana abierta:** las ventanas usan `time.monotonic()`, nunca el reloj de pared. Test en la Tarea 3.
3. **`fall_detected` sin `drop_speed` en `payload["signals"]` o con payload mal formado:** se trata como `null` (riesgo), nunca lanza. Test en la Tarea 3.
4. **Fichero corrupto o JSON huérfano en `/data/clips-pending/` al arrancar:** se ignora con log, sin bloquear la cola. Test en la Tarea 5.
5. **Una cámara deshabilitada en `clips.cameras` que produce `fall_detected`:** se reenvía el evento y no se abre ventana. Test en la Tarea 3.

---

### Task 1: Riesgo, eventos y constantes (puro)

**Files:**
- Create: `src/vitahub/clips/__init__.py` (vacío), `src/vitahub/clips/constants.py`, `src/vitahub/clips/risk.py`, `src/vitahub/clips/events.py`
- Test: `tests/test_clips_risk.py`

**Interfaces:**
- Produces:
  - `risk.Verdict = Literal["risk", "slow", "got_up"]`
  - `risk.assess(drop_speed: float | None, resolved_reason: str | None) -> Verdict`. Orden: `resolved_reason == "upright"` → `"got_up"`; si no, `drop_speed is not None and drop_speed < RISK_MIN_DROP` → `"slow"`; si no, `"risk"`.
  - `events.clip_event(hub_id: str, camera_id: str, camera_name: str, type: str, payload: dict[str, object], clock: Callable[[], datetime]) -> Event` con `severity="info"` y `timestamp=clock().isoformat()`.

- [ ] **Step 1: Write the failing tests**

```python
@pytest.mark.parametrize("drop, reason, expected", [
    (0.9, None, "risk"), (0.5, None, "risk"), (0.49, None, "slow"),
    (None, None, "risk"), (0.9, "track_lost", "risk"), (0.1, "track_lost", "slow"),
    (0.9, "upright", "got_up"), (None, "upright", "got_up"),
])
def test_assess(drop, reason, expected):
    assert assess(drop, reason) == expected

def test_clip_event_shape():
    ev = clip_event("hub-1", "cam1", "Salón", "fall_clip_discarded", {"reason": "slow"},
                    clock=lambda: datetime(2026, 10, 22, tzinfo=UTC))
    assert (ev.type, ev.severity, ev.camera_id) == ("fall_clip_discarded", "info", "cam1")
    assert ev.timestamp == "2026-10-22T00:00:00+00:00"
```

- [ ] **Step 2: Run** `pytest tests/test_clips_risk.py -v` → FAIL (módulo no existe)
- [ ] **Step 3: Implement** `constants.py`, `risk.assess` y `events.clip_event` con las firmas de arriba.
- [ ] **Step 4: Run** `pytest tests/test_clips_risk.py -v` → PASS
- [ ] **Step 5: Commit** `feat(clips): criterio de riesgo y eventos de clip`

### Task 2: `PacketBuffer` (puro)

**Files:**
- Create: `src/vitahub/clips/buffer.py`
- Test: `tests/test_clips_buffer.py`

**Interfaces:**
- Produces:
  - `@dataclass(frozen=True) class BufferedPacket: t: float; keyframe: bool; packet: object`
  - `@dataclass(frozen=True) class Snapshot: template: object; packets: list[BufferedPacket]`
  - `class PacketBuffer(max_age_s: float = BUFFER_S)` thread-safe (un `threading.Lock`):
    - `reset(template: object) -> None`: vacía y fija el stream plantilla (se llama en cada (re)conexión).
    - `append(p: BufferedPacket) -> None`: ignora paquetes hasta el primer keyframe tras `reset`; recorta los anteriores a `p.t - max_age_s`, pero el buffer siempre empieza en keyframe (se recorta hasta el último keyframe ≤ límite).
    - `snapshot(start: float, end: float) -> Snapshot | None`: paquetes desde el último keyframe con `t ≤ start` (o el primero del buffer si no hay ninguno anterior) hasta `t ≤ end`. `None` si el buffer está vacío, sin plantilla o si su primer paquete es posterior a `end`.

- [ ] **Step 1: Write the failing tests** (paquetes sintéticos: keyframe cada 2 s, uno cada 0.5 s; `template` = `object()`)
  - `test_ignores_until_first_keyframe`
  - `test_trims_to_max_age_starting_on_keyframe`: tras 60 s con `max_age_s=45`, `snapshot(0, 1e9).packets[0]` es keyframe y su `t ≥ 60 - 45 - 2`.
  - `test_snapshot_starts_on_keyframe_before_start`: `snapshot(31.0, 41.0).packets[0].t == 30.0` y `.keyframe`; último `t ≤ 41.0`.
  - `test_snapshot_none_when_empty_or_after_end`
  - `test_reset_drops_old_stream` (Review Focus 1): tras `reset(t2)`, `snapshot(...)` no contiene paquetes del stream anterior y `template is t2`.
- [ ] **Step 2: Run** `pytest tests/test_clips_buffer.py -v` → FAIL
- [ ] **Step 3: Implement** `PacketBuffer` (`collections.deque`).
- [ ] **Step 4: Run** → PASS
- [ ] **Step 5: Commit** `feat(clips): buffer de paquetes por cámara`

### Task 3: `ClipTriggerSink` (ventanas y decisión)

**Files:**
- Create: `src/vitahub/clips/trigger.py`
- Test: `tests/test_clips_trigger.py`

**Interfaces:**
- Consumes: `risk.assess`, `events.clip_event`, `PacketBuffer.snapshot`, `Snapshot`, constantes.
- Produces:
  - `@dataclass(frozen=True) class ClipRequest: camera_id: str; camera_name: str; episode_ids: list[str]; clip_start: datetime; snapshot: Snapshot`
  - `class ClipTriggerSink(EventSink)`:
    - `__init__(self, inner: EventSink, hub_id: str, cameras: set[str], buffers: Callable[[str], PacketBuffer | None], on_clip: Callable[[ClipRequest], None], monotonic: Callable[[], float] = time.monotonic, wall: Callable[[], datetime] = _utcnow)`
    - `emit(event)`: **primero** `inner.emit(event)`; luego, bajo lock y sin E/S: `fall_detected` de una cámara en `cameras` abre ventana (`t0 = monotonic()`, `deadline = t0 + POST_S`) o, si ya hay una abierta en esa cámara, añade el `episode_id` y alarga `deadline = min(monotonic() + POST_S, t0 + MAX_POST_S)`; `fall_resolved` anota `reason` del episodio si pertenece a una ventana abierta. Cualquier excepción propia se loguea, nunca sale.
    - `tick() -> None`: cierra las ventanas con `deadline ≤ monotonic()`. Por episodio, `assess(drop_speed, reason)`; el clip tiene riesgo si alguno da `"risk"`. Sin riesgo → `fall_clip_discarded` con `episode_ids`, `drop_speed` (el del primer episodio) y `reason` (`"got_up"` si todos se levantaron, si no `"slow"`). Con riesgo → `buffers(camera_id).snapshot(t0 - PRE_S, deadline)`; `None` → `fall_clip_failed` `reason: "no_video"`; si no, `on_clip(ClipRequest(..., clip_start=wall() - timedelta(seconds=monotonic() - (t0 - PRE_S))))`.
    - `close()`: emite `fall_clip_failed` `reason: "shutdown"` por cada ventana abierta y después `inner.close()`.
  - `drop_speed` se lee de `event.payload["signals"]["drop_speed"]`; ausente, no numérico o payload mal formado → `None`.
  - Los eventos propios se emiten por `inner` (no por `self`).

- [ ] **Step 1: Write the failing tests** (reloj `monotonic` falso que avanza a mano; `inner` = sink en memoria; `buffers` devuelve un `PacketBuffer` lleno de paquetes sintéticos; `on_clip` = lista)
  - `test_forwards_every_event_to_inner_first`
  - `test_risk_closes_at_10s_and_calls_on_clip`: `fall_detected` con `drop_speed=0.9` en t=100; `tick()` en 109.9 no hace nada; en 110 → un `ClipRequest` con `episode_ids == ["e1"]` y `snapshot.packets[0].t ≤ 85`.
  - `test_slow_is_discarded_without_snapshot`: `drop_speed=0.2` → `fall_clip_discarded` `reason="slow"`, `on_clip` vacío.
  - `test_got_up_before_deadline`: `fall_resolved` `reason="upright"` en t=106 → `reason="got_up"`.
  - `test_resolved_after_deadline_is_ignored`
  - `test_track_lost_counts_as_risk`
  - `test_second_episode_extends_window`: segundo `fall_detected` en t=108 → cierra en 118, `episode_ids == ["e1", "e2"]`.
  - `test_extension_capped_at_30s`: `fall_detected` cada 5 s desde t=100 → cierra en 130.
  - `test_no_video`: `buffers` devuelve buffer vacío → `fall_clip_failed` `reason="no_video"`.
  - `test_camera_not_enabled_is_ignored` (Review Focus 5)
  - `test_missing_signals_counts_as_risk` (Review Focus 3): payload sin `signals` y con `signals` no dict → riesgo, sin excepción.
  - `test_wall_clock_jump_does_not_affect_window` (Review Focus 2): `wall` salta −1 h entre el disparo y el `tick`; cierra igual en t=110.
  - `test_close_emits_shutdown_then_closes_inner`
- [ ] **Step 2: Run** `pytest tests/test_clips_trigger.py -v` → FAIL
- [ ] **Step 3: Implement** `ClipTriggerSink` con las firmas de arriba.
- [ ] **Step 4: Run** → PASS
- [ ] **Step 5: Commit** `feat(clips): disparo y evaluación de riesgo por ventana`

### Task 4: `assembler` (remux a MP4)

**Files:**
- Create: `src/vitahub/clips/assembler.py`
- Create: `tests/clip_media.py` (helpers compartidos con las Tareas 5 y 6)
- Test: `tests/test_clips_assembler.py`

**Interfaces:**
- Consumes: `Snapshot`, `BufferedPacket`.
- Produces: `@dataclass(frozen=True) class ClipFile: path: Path; sha256: str; duration_s: float` y `assemble_mp4(snapshot: Snapshot, out_path: Path) -> ClipFile`.
  - `av.open(str(out_path), "w", format="mp4", options={"movflags": "+faststart"})`, `out.add_stream_from_template(snapshot.template)`, mux de cada `packet` reasignando `packet.stream` y desplazando `pts`/`dts` para que el primero valga 0. Sin decodificar.
  - `duration_s` = `packets[-1].t - packets[0].t`, redondeado a 3 decimales.
  - Si falla, borra `out_path` parcial y relanza.

- [ ] **Step 1: Write the failing test.** En `tests/clip_media.py`: `write_test_video(path: Path, frames: int = 50, fps: int = 10, gop: int = 10) -> None` (vídeo `mpeg4` 64x48 en `.mkv`, como `_write_av_file` de `tests/test_live_source.py`, sin audio) y `snapshot_from_file(path: Path) -> Snapshot` (demux con `t = i / fps`).
  - `test_assemble_produces_playable_mp4`: `av.open(out)` tiene un stream de vídeo y 50 frames decodificables; `ClipFile.sha256 == hashlib.sha256(out.read_bytes()).hexdigest()`; `duration_s == pytest.approx(4.9)`.
  - `test_assemble_cleans_partial_file_on_error`: `Snapshot` con un `packet` que no es un `av.Packet` → lanza y `out_path` no existe.
- [ ] **Step 2: Run** `pytest tests/test_clips_assembler.py -v` → FAIL
- [ ] **Step 3: Implement** `assemble_mp4`.
- [ ] **Step 4: Run** → PASS
- [ ] **Step 5: Commit** `feat(clips): montaje del MP4 sin recodificar`

### Task 5: `PendingQueue` y `ClipUploader`

**Files:**
- Create: `src/vitahub/clips/uploader.py`
- Test: `tests/test_clips_uploader.py`

**Interfaces:**
- Consumes: `ClipRequest`, `assemble_mp4`, `ClipFile`, `clip_event`, `backoff_delay`/`next_attempt` de `vitahub.ingest.rtsp`, `IotCredentialsProvider`/`AwsCredentials` de `vitahub.live.credentials`.
- Produces:
  - `@dataclass(frozen=True) class PendingClip: mp4: Path; key: str; camera_id: str; camera_name: str; episode_ids: list[str]; clip_start: str; sha256: str; duration_s: float; created_at: float`
  - `class PendingQueue(root: Path, max_bytes: int = PENDING_MAX_MB * 1024 * 1024, max_age_s: float = PENDING_MAX_AGE_S, clock: Callable[[], float] = time.time)`:
    - `add(request: ClipRequest, hub_id: str) -> tuple[PendingClip | None, list[PendingClip]]`: monta el MP4 en `root/<uuid>.mp4.part`, lo renombra a `.mp4`, escribe `root/<uuid>.json` (renombrado atómico desde `.json.part`) y devuelve los clips **expulsados** por tamaño (los más antiguos). Si el montaje falla, no relanza: borra lo parcial, loguea y devuelve `(None, [])`; el llamador emite `mux_error`.
    - `expire() -> list[PendingClip]`: borra y devuelve los de `created_at` anterior a `clock() - max_age_s`.
    - `oldest() -> PendingClip | None`, `remove(clip: PendingClip) -> None`.
    - Al construirse, escanea `root`: borra `*.part`, ignora con log un `.json` ilegible o sin su `.mp4` (y viceversa) (Review Focus 4).
  - `class ClipUploader`:
    - `__init__(self, queue: PendingQueue, hub_id: str, sink: EventSink, put: Callable[[str, Path, dict[str, str]], None], clock: Callable[[], datetime] = _utcnow)`. `put(key, path, metadata)` lanza `UploadRejected` en un 403 y cualquier otra excepción en un fallo transitorio.
    - `submit(request: ClipRequest) -> None`: hace `queue.add`; emite `fall_clip_failed` `queue_full` por cada expulsado o `mux_error` si `add` falló. Se llama desde el hilo del trigger.
    - `run_once() -> bool`: `expire()` (emite `expired`), toma `oldest()` y llama a `put`; éxito → `remove` y `fall_clip_ready` con `episode_ids`, `s3_key`, `sha256`, `duration_s`, `clip_start`; devuelve `True` si subió. `UploadRejected` → `fall_clip_failed` `upload_rejected` **una vez por clip** y lo deja en la cola.
    - `run(stop: threading.Event) -> None`: bucle con `stop.wait(backoff_delay(attempt))` tras un fallo y `stop.wait(1.0)` con la cola vacía.
  - `s3_put(bucket: str, region: str, creds: IotCredentialsProvider) -> Callable[[str, Path, dict[str, str]], None]`: cliente `botocore` `s3` creado con `creds.get()` en cada llamada (las credenciales rotan), `put_object(Bucket, Key, Body, ContentType="video/mp4", Metadata=...)`; `ClientError` con `HTTPStatusCode == 403` → `UploadRejected`. Runtime only, sin test.
  - Metadatos S3: `episode-ids` (separados por coma), `camera-id`, `clip-start`, `sha256`.

- [ ] **Step 1: Write the failing tests** (`ClipRequest` real con un `Snapshot` de la Tarea 4; `put` falso; reloj inyectado)
  - `test_upload_success_removes_and_emits_ready`: el evento lleva `s3_key == "hub-1/cam1/e1.mp4"` y el `sha256` del fichero; la carpeta queda vacía.
  - `test_transient_failure_keeps_clip_and_retries`
  - `test_rejected_emits_once_and_keeps_clip`
  - `test_queue_full_evicts_oldest`: `max_bytes` pequeño → el primer clip se expulsa con `queue_full`.
  - `test_expired_after_3_days`
  - `test_resume_after_restart`: otra `PendingQueue` sobre la misma carpeta ve el clip.
  - `test_ignores_orphans_and_partials` (Review Focus 4)
  - `test_mux_error_emits_failed`
- [ ] **Step 2: Run** `pytest tests/test_clips_uploader.py -v` → FAIL
- [ ] **Step 3: Implement** `PendingQueue`, `ClipUploader`, `UploadRejected` y `s3_put`.
- [ ] **Step 4: Run** → PASS
- [ ] **Step 5: Commit** `feat(clips): cola persistente y subida a S3`

### Task 6: `ClipRecorder`

**Files:**
- Create: `src/vitahub/clips/recorder.py`
- Test: `tests/test_clips_recorder.py`

**Interfaces:**
- Consumes: `PacketBuffer`, `BufferedPacket`, `backoff_delay`/`next_attempt`.
- Produces: `record(uri: str, buffer: PacketBuffer, stop: threading.Event, format: str | None = "rtsp", options: dict[str, str] | None = None, monotonic: Callable[[], float] = time.monotonic) -> None`. Bucle hasta `stop`: `av.open(uri, format=format, options=_RTSP_OPTIONS if options is None else options)` (las mismas opciones que `live/source.py`: reusar `_RTSP_OPTIONS` importándolo), `buffer.reset(container.streams.video[0])`, `container.demux(video)` y `buffer.append(BufferedPacket(monotonic(), packet.is_keyframe, packet))` para cada paquete con `size > 0`. Error o fin de stream → log, cierra el contenedor y espera `backoff_delay(attempt)` con `stop.wait`.

- [ ] **Step 1: Write the failing test**: `record` sobre un `.mkv` local (helper de la Tarea 4, `format=None, options={}`) en un hilo; tras terminar el fichero, el buffer tiene 50 paquetes y el primero es keyframe; `stop.set()` hace que el hilo termine en < 2 s.
- [ ] **Step 2: Run** `pytest tests/test_clips_recorder.py -v` → FAIL
- [ ] **Step 3: Implement** `record`.
- [ ] **Step 4: Run** → PASS
- [ ] **Step 5: Commit** `feat(clips): grabación en memoria por cámara`

### Task 7: Config `clips`

**Files:**
- Modify: `src/vitahub/config.py` (dataclass nueva, `_clips_from_raw`, campo en `HubConfig`, llamada en `load_config` después de `live`)
- Modify: `config/hub.example.yaml`
- Test: `tests/test_config.py`

**Interfaces:**
- Produces: `@dataclass class ClipsConfig: enabled: bool = False; cameras: list[str] = []; region: str = "eu-west-1"; bucket: str = ""; role_alias: str = ""; credentials_endpoint: str = ""` y `HubConfig.clips: ClipsConfig`.
- `_clips_from_raw(raw: object, env: Mapping[str, str], uplink: UplinkConfig, fall: FallConfig) -> ClipsConfig`, mismo estilo que `_live_from_raw`. Con `enabled: true`, `ConfigError` si: `uplink.enabled` false (mensaje con "uplink"), `fall.enabled` false (mensaje con "inference.fall"), `cameras` vacía o no lista de cadenas, falta `VITAHUB_CLIPS_BUCKET`, `VITAHUB_CLIPS_ROLE_ALIAS` o `VITAHUB_IOT_CREDENTIALS_ENDPOINT` (mensaje con el nombre de la variable). `region` como en `live`.

- [ ] **Step 1: Write the failing tests** (reutiliza `_write` y los helpers de `live`): `test_clips_defaults_disabled`, `test_clips_valid`, `test_clips_requires_uplink`, `test_clips_requires_fall`, `test_clips_requires_cameras`, `test_clips_requires_env[var]` parametrizado con las tres variables, `test_clips_section_must_be_mapping`.
- [ ] **Step 2: Run** `pytest tests/test_config.py -v -k clips` → FAIL
- [ ] **Step 3: Implement** y añade a `config/hub.example.yaml` el bloque `clips:` del spec §4.6 con comentarios en el estilo de `live:` (incluido "opt-in por cámara: deja fuera la del baño" y las dos variables de entorno).
- [ ] **Step 4: Run** `pytest tests/test_config.py -v` → PASS
- [ ] **Step 5: Commit** `feat(clips): sección clips de la config`

### Task 8: Cableado (`factory.py`, `app.py`, compose)

**Files:**
- Modify: `src/vitahub/factory.py`, `src/vitahub/app.py`, `docker-compose.yml`, `docker-compose.mac.yml`
- Test: `tests/test_factory.py`, `tests/test_app_loop.py`

**Interfaces:**
- Consumes: todo lo anterior.
- Produces:
  - `@dataclass class ClipService: sink: ClipTriggerSink; buffers: dict[str, PacketBuffer]; uploader: ClipUploader; start: Callable[[threading.Event], None]` — `start` lanza dos hilos daemon: `clips-trigger` (`while not stop.wait(0.5): trigger.tick()`) y `clips-upload` (`uploader.run(stop)`).
  - `build_clip_service(cfg: HubConfig, inner: EventSink, pending_dir: Path = Path("/data/clips-pending")) -> ClipService | None`: `None` si `clips.enabled` es false. `https_fetcher` con `cfg.clips.role_alias` y `IotCredentialsProvider`, traduciendo la excepción a `ConfigError` como `build_live_service`. Un `PacketBuffer` por id de `cfg.clips.cameras`. `on_clip=uploader.submit`. Log de aviso si `cfg.inference.stream == "main"`.
  - En `app.run`: `clips = build_clip_service(cfg, sink)`; si existe, `sink = clips.sink` **antes** de construir el `worker` y el directo, y `clips.start(stop)`. En `worker`, si `camera.id in clips.buffers`, lanzar `threading.Thread(target=record, args=(rtsp_url, clips.buffers[camera.id], cam_stop), name=f"clip-{camera.id}", daemon=True)` antes de `_camera_loop`. Así el supervisor gestiona altas, bajas y cambios de URI.
  - `warn_missing_clip_cameras(cfg: HubConfig) -> None` en `factory.py`: un `_log.warning` por cada id de `cfg.clips.cameras` que no está en `cfg.cameras`. Se llama tras el rescan inicial de `run` y tras cada `service.run_once()` de `_rescan_loop` (que recibe `cfg` como argumento nuevo).
  - `_shutdown` no cambia: el `sink` que recibe ya es el `ClipTriggerSink`, cuyo `close()` emite `shutdown` antes de cerrar el sink interno.
  - Compose: `VITAHUB_CLIPS_BUCKET: ${VITAHUB_CLIPS_BUCKET:-}` y `VITAHUB_CLIPS_ROLE_ALIAS: ${VITAHUB_CLIPS_ROLE_ALIAS:-}` con el mismo comentario que las del directo.

- [ ] **Step 1: Write the failing tests**
  - `test_build_clip_service_none_when_disabled`
  - `test_build_clip_service_bad_pem_is_config_error` (mismo patrón que el test equivalente de `build_live_service`)
  - `test_warn_missing_clip_cameras_logs` con `caplog`
  - `test_compose_passes_clip_env`: los dos `docker-compose*.yml` contienen las dos variables (lectura de texto, como protección del olvido del commit 51a52c6).
- [ ] **Step 2: Run** `pytest tests/test_factory.py tests/test_app_loop.py -v` → FAIL
- [ ] **Step 3: Implement** el cableado.
- [ ] **Step 4: Run** `pytest -v` → todo verde; `ruff check . && mypy` limpios.
- [ ] **Step 5: Commit** `feat(clips): cableado en el hub`

### Task 9: Documentación

**Files:**
- Modify: `docs/como-funciona.md`, `docs/como-probar.md`, `docs/backlog.md`

- [ ] **Step 1:** `como-funciona.md`: corrige la frase de la introducción (líneas 9-12) con la excepción del clip de caída con riesgo; añade `clips/` a la vista general y a la tabla de módulos; una sección "Clip de caída" con el criterio de §3 del spec, la ventana, la retención (30 días en S3, 3 en el hogar) y los tres eventos.
- [ ] **Step 2:** `como-probar.md`: prueba manual contra la cuenta real (cámara en `clips.cameras`, tirarse al suelo y quedarse 15 s → `fall_clip_ready` y objeto en S3; levantarse a los 3 s → `fall_clip_discarded` `got_up`; tumbarse despacio → `slow`; cortar internet → el clip queda en `/data/clips-pending/` y sube al volver). Actualiza el recuento de tests de la checklist.
- [ ] **Step 3:** `backlog.md`: sección "Diferidos del slice de clip de caída (2026-10-22)" con los cuatro puntos de §9 del spec y la verificación pendiente del pipeline `hub-events` en el repo de infra.
- [ ] **Step 4: Commit** `docs: clip de caída con riesgo`

---

Fuera de este plan: la infra de §5 del spec (repo `vitaplus-aws-architecture`, plan propio). Sin ella el hub funciona igual, pero las subidas acaban en `upload_rejected`.
