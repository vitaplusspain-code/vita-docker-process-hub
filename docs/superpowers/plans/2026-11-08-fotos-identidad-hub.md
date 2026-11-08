# Fotos de identidad desde la nube: hub. Plan de implementación

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** el hub recibe por el named shadow `face-gallery` la galería que el administrador sube en la
web, la descarga de S3, la valida con las reglas de `enrollment.py`, la escribe en `/data/faces`,
publica el estado de cada foto en `reported` y se reinicia si la identidad está encendida y la
galería cambió.

**Architecture:** paquete nuevo `src/vitahub/gallery/` con cuatro piezas: `manifest` (parseo
estricto de `manifest.json`), `shadow` (topics y payloads del shadow), `apply` (descarga, validación
y sustitución atómica de carpetas) y `service` (hilo con debounce, reintentos y reinicio). El canal
MQTT existente gana suscripciones que sobreviven a reconexiones (`MqttLink`). Las credenciales S3
salen del role alias de clips con el `IotCredentialsProvider` que ya existe.

**Tech Stack:** Python 3, paho-mqtt (v2 callbacks), botocore, OpenCV, InsightFace, pytest.

**Spec:** contrato en
`vita-web-family-app/docs/superpowers/specs/2026-11-08-fotos-identidad-web-design.md` (commit
`bf995b4`), secciones 5.1, 5.4, 5.5, 5.6 y 5.7. Infra en
`vitaplus-aws-architecture/docs/superpowers/plans/2026-11-08-fotos-identidad-infra.md`. Este plan
también modifica el spec propio
`docs/superpowers/specs/2026-09-02-identidad-persona-design.md` (privacidad).

## Global Constraints

- Shadow: named shadow `face-gallery`. Topics bajo `$aws/things/<hub_id>/shadow/name/face-gallery/`. El thing se llama igual que `hub_id`.
- `desired` = `{ "version": <ms epoch> }`. La versión siempre crece.
- `reported` = `{ "version": n, "identity_enabled": bool, "photos": { "<photoId>": { "status": "accepted" } | { "status": "rejected", "reason": "no_face"|"multiple_faces"|"decode_error" } } }`. Un `photoId` ausente se lee como pendiente.
- S3, solo lectura: `hubs/<hub_id>/faces/manifest.json` = `{ "version": number, "persons": { "<person_key>": ["<photoId>", ...] } }`. Fotos en `hubs/<hub_id>/faces/<person_key>/<photoId>.jpg`.
- `person_key`: `^[0-9a-f]{32}$`. `photoId`: UUID canónico en minúscula, `^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$` (confirmado con la web, ver "Acuerdos").
- Debounce: 30 s sin cambios en `desired.version` antes de aplicar.
- La nube no cambia `inference.fall` ni `inference.identity`. Con la identidad apagada el hub descarga, valida y guarda igual, reporta `identity_enabled: false` y no reinicia.
- Solo se tocan carpetas de `/data/faces` con forma de `person_key`. Las carpetas del técnico con otro nombre no se tocan.
- El hub nunca deja de arrancar por una foto ni por una galería vacía (antiobjetivo del contrato, §4).
- Ni bytes de imagen, ni URLs, ni embeddings en logs ni en eventos. `person_key` y `photoId` sí pueden salir en logs.
- Variables de entorno nuevas: `VITAHUB_FACE_PHOTOS_BUCKET` (activa la función) y `VITAHUB_FACE_PHOTOS_REGION` (por defecto `eu-west-1`). Se reutilizan `VITAHUB_CLIPS_ROLE_ALIAS` y `VITAHUB_IOT_CREDENTIALS_ENDPOINT`.

## Decisiones de este plan

- **Identidades activas sin editar `hub.yaml`.** No hace falta ningún cambio de configuración: hoy
  `hub.yaml` no enumera personas, y `load_gallery` carga toda carpeta de `/data/faces`. Una carpeta
  `person_key` escrita por la sincronización queda activa en el siguiente arranque.
- **Galería vacía.** `load_gallery` deja de fallar si no hay personas y devuelve una galería vacía.
  `build_face_identifier` avisa y devuelve `None`: el hub vigila caídas sin identidad, con
  `person: null`. Se mantiene el error si `/data/faces` no existe (instalación, el técnico está
  delante). La validación de la consola local (`validate_apply`, `409` al borrar) no cambia.
- **Versión aplicada = `reported.version` del shadow.** No hay fichero de estado local. Aplicar es
  idempotente: se calcula siempre desde el manifiesto. Si `reported` no llegó a publicarse, el
  siguiente arranque vuelve a aplicar, no encuentra cambios en disco y no reinicia.
- **Reinicio solo si cambió el disco.** "Cambió" = el conjunto de ficheros de alguna carpeta
  `person_key` es distinto antes y después. Con el mismo `photoId` el contenido es el mismo.
- **Borrar entradas de `reported.photos`.** El shadow fusiona JSON: una clave ausente no se borra.
  El hub manda `null` para cada `photoId` que reportó antes y ya no está en el manifiesto.
- **Nombre de fichero en disco:** `<photoId>.jpg`. La consola local acepta también ese nombre para
  listar, ver y borrar, para que `validate_apply` y el `409` cuenten bien las fotos de la nube.
- **Listar antes de leer.** La política del rol solo concede `s3:ListBucket` con la condición
  `s3:prefix`. Un `GetObject` de una clave inexistente no lleva `s3:prefix`, así que devuelve `403`
  y no `404`. Por eso cada aplicación hace primero `ListObjectsV2` con
  `Prefix="hubs/<hub_id>/faces/"`, paginado. La barra final es obligatoria: sin ella S3 deniega el
  listado. Solo se pide con `GetObject` lo que aparece en el listado.
- **Manifiesto ausente del listado = galería vacía.** Un `403`, o cualquier otro error al listar o
  al leer, aborta sin tocar el disco y reintenta: indica un permiso mal puesto, no un manifiesto
  ausente.
- **Foto ausente del listado = pendiente.** No se escribe y no sale en `reported.photos`. Cubre la carrera entre
  el borrado de una foto y la reescritura del manifiesto.
- **`manifest.version` < `desired.version`:** el manifiesto aún no es el bueno. Se reintenta.
  Si es mayor o igual, se aplica y se reporta `manifest.version`.
- **Motor facial compartido.** Un único proveedor perezoso y cacheado da el motor a la consola local
  y a la sincronización. No se cargan dos copias de InsightFace solo para validar.
- **Activación.** La sincronización arranca si `uplink.enabled` es true y
  `VITAHUB_FACE_PHOTOS_BUCKET` no está vacía. Si el bucket está definido pero faltan
  `VITAHUB_CLIPS_ROLE_ALIAS` o `VITAHUB_IOT_CREDENTIALS_ENDPOINT`: `ConfigError` al arrancar, como
  `clips`. No depende de `clips.enabled`.

## Review Focus

1. **Corte de luz a mitad de la sustitución de carpetas.** Al volver, el hub arranca con la galería
   anterior o sin esa persona, nunca con un JPEG truncado ni con una carpeta de staging cargada como
   persona. Test en la Tarea 4: `test_leftover_staging_dirs_are_ignored_and_cleaned`.
2. **Todas las fotos de la única persona rechazadas, con la identidad encendida.** El hub reinicia y
   arranca sin identidad, sin bucle de reinicio. Tests en la Tarea 2
   (`test_build_face_identifier_empty_gallery_returns_none`) y en la Tarea 4
   (`test_person_with_no_valid_photos_folder_removed`).
3. **Manifiesto malicioso o corrupto** (`person_key` con `../`, `photoId` con `/`, JSON roto, tipos
   incorrectos). No se escribe nada fuera de `/data/faces/<person_key>/`. Test en la Tarea 3:
   `test_parse_manifest_rejects_path_traversal`.
4. **Hub sin internet durante horas con un `desired` pendiente.** Reintenta con backoff acotado, no
   llena el log y aplica al volver. Test en la Tarea 7: `test_fetch_error_backs_off_and_retries`.
5. **El técnico cambia `identity_enabled` en la consola local y no hay cambios de galería.** Tras el
   reinicio, `reported.identity_enabled` refleja el valor nuevo. Test en la Tarea 7:
   `test_startup_republishes_identity_enabled_when_stale`.

---

### Task 1: Reglas de validación compartidas en `enrollment.py`

**Files:**
- Modify: `src/vitahub/admin/enrollment.py:20-21` (regex de nombre de foto) y `:74-114` (`save_photo`)
- Test: `tests/test_enrollment.py`

**Interfaces:**
- Produces:
  - `PhotoReason = Literal["no_face", "multiple_faces", "decode_error"]`
  - `class PhotoRejected(EnrollmentError)` con atributo `reason: PhotoReason`. Status 400, mismos mensajes que hoy.
  - `validate_photo(data: bytes, engine: FaceEngine) -> np.ndarray`: decodifica y exige exactamente una cara. Devuelve la imagen decodificada.
  - `encode_jpeg(image: np.ndarray) -> bytes`: reencoda a JPEG (quita EXIF). Lanza `EnrollmentError` si falla.
  - `write_atomic(path: Path, data: bytes) -> None`: tempfile en el mismo directorio + fsync + `os.replace`.
  - `CLOUD_PHOTO_ID_RE`: regex de `photoId` de las Global Constraints.

- [ ] **Step 1: Tests que fallan**

```python
def test_validate_photo_reasons(tmp_path):
    with pytest.raises(PhotoRejected) as e:
        validate_photo(b"no-es-imagen", StubFaceEngine([]))
    assert e.value.reason == "decode_error"
    with pytest.raises(PhotoRejected) as e:
        validate_photo(_jpeg_bytes(), StubFaceEngine([[]]))
    assert e.value.reason == "no_face"
    with pytest.raises(PhotoRejected) as e:
        validate_photo(_jpeg_bytes(), StubFaceEngine([[_face(), _face()]]))
    assert e.value.reason == "multiple_faces"

def test_list_people_includes_cloud_photo_names(tmp_path):
    d = tmp_path / ("a" * 32); d.mkdir()
    (d / "0b6c1c8e-3f0a-4c55-9d7e-2a1b3c4d5e6f.jpg").write_bytes(_jpeg_bytes())
    assert list_people(tmp_path)[0].photos == ["0b6c1c8e-3f0a-4c55-9d7e-2a1b3c4d5e6f.jpg"]

def test_save_photo_numbering_ignores_cloud_names(tmp_path):
    d = tmp_path / "ana"; d.mkdir()
    (d / "0b6c1c8e-3f0a-4c55-9d7e-2a1b3c4d5e6f.jpg").write_bytes(_jpeg_bytes())
    assert save_photo(tmp_path, "ana", _jpeg_bytes(), StubFaceEngine([[_face()]])) == "001.jpg"
```

Añade también: `photo_bytes` y `delete_photo` aceptan un nombre `<uuid>.jpg` y siguen rechazando
`../x.jpg`.

- [ ] **Step 2: Ejecuta y comprueba que fallan**

Run: `pytest tests/test_enrollment.py -v`
Expected: FAIL con `ImportError` de `PhotoRejected` / `validate_photo`.

- [ ] **Step 3: Implementa**

`_PHOTO_NAME_RE` acepta `\d{3}\.jpg` o `<uuid>\.jpg`. `save_photo` pasa a ser `validate_photo` +
`encode_jpeg` + `write_atomic`, y numera solo con los nombres `\d{3}.jpg`. Los mensajes de error no
cambian: los tests actuales de `save_photo` deben pasar sin tocarlos.

- [ ] **Step 4: Ejecuta y comprueba que pasan**

Run: `pytest tests/test_enrollment.py tests/test_control.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/vitahub/admin/enrollment.py tests/test_enrollment.py
git commit -m "refactor(enrollment): validación de foto con motivo y nombres de foto de la nube"
```

### Task 2: Una galería vacía no impide arrancar

**Files:**
- Modify: `src/vitahub/identity/gallery.py:58-92`
- Modify: `src/vitahub/factory.py:308-327` (`build_face_identifier`)
- Test: `tests/test_identity_gallery.py`, `tests/test_factory.py`

**Interfaces:**
- Produces: `load_gallery(faces_dir, engine) -> Gallery` devuelve `Gallery(people={})` si no hay personas. Ignora directorios cuyo nombre empieza por `.`.

- [ ] **Step 1: Tests que fallan**

Sustituye `test_load_gallery_empty_dir_raises` por:

```python
def test_load_gallery_empty_dir_returns_empty(tmp_path):
    assert load_gallery(tmp_path, StubFaceEngine([])).people == {}

def test_load_gallery_ignores_dot_dirs(tmp_path):
    (tmp_path / ".cloud-staging" / ("a" * 32)).mkdir(parents=True)
    assert load_gallery(tmp_path, StubFaceEngine([])).people == {}
```

En `tests/test_factory.py`, `test_build_face_identifier_empty_gallery_returns_none`: identidad
encendida, `/data/faces` existe y está vacía, motor stub con `monkeypatch` sobre
`InsightFaceEngine.from_weights` y `_require_face_weights`. Resultado: `None` y un warning en el log
que contiene `"ninguna persona enrolada"`.

- [ ] **Step 2: Ejecuta y comprueba que fallan**

Run: `pytest tests/test_identity_gallery.py tests/test_factory.py -v`
Expected: FAIL (hoy se lanza `ConfigError`).

- [ ] **Step 3: Implementa**

Mantén el `ConfigError` de carpeta inexistente, de foto ilegible y de carpeta sin fotos válidas.
Actualiza el docstring del módulo y el comentario de `build_face_identifier`: sin personas, el hub
arranca sin identidad.

- [ ] **Step 4: Ejecuta y comprueba que pasan**

Run: `pytest tests/test_identity_gallery.py tests/test_factory.py tests/test_config_edit.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/vitahub/identity/gallery.py src/vitahub/factory.py tests/test_identity_gallery.py tests/test_factory.py
git commit -m "fix(identity): una galería vacía arranca sin identidad en vez de fallar"
```

### Task 3: Manifiesto y payloads del shadow

**Files:**
- Create: `src/vitahub/gallery/__init__.py`, `src/vitahub/gallery/manifest.py`, `src/vitahub/gallery/shadow.py`
- Test: `tests/test_gallery_manifest.py`, `tests/test_gallery_shadow.py`

**Interfaces:**
- Consumes: `CLOUD_PHOTO_ID_RE`, `PhotoReason` (Task 1).
- Produces (`manifest.py`):
  - `PERSON_KEY_RE = re.compile(r"^[0-9a-f]{32}$")`
  - `@dataclass(frozen=True) class Manifest: version: int; persons: dict[str, tuple[str, ...]]`
  - `class ManifestError(Exception)`
  - `parse_manifest(raw: bytes) -> Manifest`: lanza `ManifestError` si el JSON está roto, si `version` no es un entero ≥ 0 (un `bool` no vale), si `persons` no es un objeto, o si una clave o un `photoId` no cumple su regex. Elimina `photoId` duplicados conservando el orden.
- Produces (`shadow.py`):
  - `SHADOW_NAME = "face-gallery"`
  - `@dataclass(frozen=True) class ShadowTopics` con `get`, `get_accepted`, `get_rejected`, `update`, `update_delta`, `update_rejected` (str), y `shadow_topics(hub_id: str) -> ShadowTopics`.
  - `@dataclass(frozen=True) class ShadowSnapshot: desired_version: int | None; reported_version: int | None; reported_identity_enabled: bool | None; reported_photo_ids: frozenset[str]`
  - `parse_get_accepted(raw: bytes) -> ShadowSnapshot`
  - `parse_delta_version(raw: bytes) -> int | None`: `state.version` del delta, o `None` si falta o no es entero.
  - `PhotoStatus = tuple[Literal["accepted"], None] | tuple[Literal["rejected"], PhotoReason]`
  - `reported_payload(version: int, identity_enabled: bool, statuses: dict[str, PhotoStatus], previous_ids: frozenset[str]) -> str`: JSON `{"state":{"reported":{...}}}`. Cada id de `previous_ids` que no esté en `statuses` va como `null`.
  - `identity_only_payload(identity_enabled: bool) -> str`

- [ ] **Step 1: Tests que fallan**

```python
def test_parse_manifest_ok():
    m = parse_manifest(b'{"version": 5, "persons": {"' + b"a" * 32 + b'": ["' + PID.encode() + b'"]}}')
    assert m == Manifest(version=5, persons={"a" * 32: (PID,)})

def test_parse_manifest_empty_persons():
    assert parse_manifest(b'{"version": 1, "persons": {}}').persons == {}

@pytest.mark.parametrize("raw", [
    b'{"version": 1, "persons": {"../etc": []}}',
    b'{"version": 1, "persons": {"' + b"a" * 32 + b'": ["../x"]}}',
    b'{"version": 1, "persons": {"' + b"A" * 32 + b'": []}}',
])
def test_parse_manifest_rejects_path_traversal(raw):
    with pytest.raises(ManifestError):
        parse_manifest(raw)

@pytest.mark.parametrize("raw", [b"{", b'{"version": true, "persons": {}}', b'{"version": 1, "persons": []}'])
def test_parse_manifest_rejects_bad_shape(raw):
    with pytest.raises(ManifestError):
        parse_manifest(raw)

def test_reported_payload_nulls_removed_photos():
    body = json.loads(reported_payload(7, True, {"p1": ("accepted", None), "p2": ("rejected", "no_face")}, frozenset({"p1", "old"})))
    assert body == {"state": {"reported": {"version": 7, "identity_enabled": True, "photos": {
        "p1": {"status": "accepted"}, "p2": {"status": "rejected", "reason": "no_face"}, "old": None}}}}

def test_shadow_topics():
    t = shadow_topics("hub-1")
    assert t.update_delta == "$aws/things/hub-1/shadow/name/face-gallery/update/delta"
    assert t.get == "$aws/things/hub-1/shadow/name/face-gallery/get"
```

Añade: `parse_get_accepted` con `desired` sin `reported`, con los dos, y con `state` vacío (todo
`None` y `frozenset()`).

- [ ] **Step 2: Ejecuta y comprueba que fallan**

Run: `pytest tests/test_gallery_manifest.py tests/test_gallery_shadow.py -v`
Expected: FAIL con `ModuleNotFoundError: vitahub.gallery`.

- [ ] **Step 3: Implementa** las firmas de arriba. Solo `json` de la librería estándar.

- [ ] **Step 4: Ejecuta y comprueba que pasan**

Run: `pytest tests/test_gallery_manifest.py tests/test_gallery_shadow.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/vitahub/gallery tests/test_gallery_manifest.py tests/test_gallery_shadow.py
git commit -m "feat(gallery): manifiesto de la nube y payloads del shadow face-gallery"
```

### Task 4: Aplicar un manifiesto a `/data/faces`

**Files:**
- Create: `src/vitahub/gallery/apply.py`
- Test: `tests/test_gallery_apply.py`

**Interfaces:**
- Consumes: `Manifest`, `PERSON_KEY_RE` (Task 3). `validate_photo`, `encode_jpeg`, `write_atomic`, `PhotoRejected` (Task 1). `PhotoStatus` (Task 3).
- Produces:
  - `FetchPhoto = Callable[[str, str], bytes | None]`: `(person_key, photo_id)` → bytes, o `None` si la foto no está en el listado de S3. Cualquier otro fallo lanza.
  - `@dataclass(frozen=True) class ApplyResult: statuses: dict[str, PhotoStatus]; changed: bool`
  - `apply_manifest(faces_dir: Path, manifest: Manifest, fetch: FetchPhoto, engine: FaceEngine) -> ApplyResult`

Algoritmo:

1. Borra `faces_dir/.cloud-staging` y `faces_dir/.cloud-old` si existen (restos de un corte).
2. Descarga y valida **todas** las fotos antes de tocar `faces_dir`. Si `fetch` lanza, la excepción sale y el disco queda igual.
3. Por cada `person_key` del manifiesto con al menos una foto aceptada, escribe las aceptadas como `<photoId>.jpg` (bytes de `encode_jpeg`) en `.cloud-staging/<person_key>/`.
4. Carpetas objetivo = claves del manifiesto ∪ carpetas actuales que cumplen `PERSON_KEY_RE`. Para cada una:
   - con staging: si `faces_dir/<key>` existe, `os.replace` a `.cloud-old/<key>`. Después, `os.replace` de staging a `faces_dir/<key>`;
   - sin staging (persona sin fotos válidas o fuera del manifiesto): `os.replace` de `faces_dir/<key>` a `.cloud-old/<key>`.
5. `rmtree` de `.cloud-old` y `.cloud-staging`. `fsync` del directorio `faces_dir`.
6. `changed` = el mapa `{key: set(ficheros)}` de las carpetas objetivo es distinto antes y después.

`os.replace` exige el mismo sistema de ficheros: por eso staging y old viven dentro de `faces_dir`,
y la Task 2 hace que `load_gallery` ignore los directorios con punto.

- [ ] **Step 1: Tests que fallan**

Usa un `fetch` en memoria (`dict`) y `StubFaceEngine`. Tests:

- `test_writes_accepted_and_reports_rejected`: tres fotos, respuestas del motor `[[f]]`, `[]`, `[[f, f]]`. En disco solo `<p1>.jpg`. `statuses` = `{p1: accepted, p2: (rejected, no_face), p3: (rejected, multiple_faces)}`. `changed is True`.
- `test_decode_error`: `fetch` devuelve `b"basura"`, estado `("rejected", "decode_error")`.
- `test_missing_photo_is_pending`: `fetch` devuelve `None`, sin entrada en `statuses` y sin fichero.
- `test_person_with_no_valid_photos_folder_removed`: la carpeta `<key>` existía con una foto y ahora todas sus fotos se rechazan. Resultado: la carpeta ya no existe.
- `test_removes_person_keys_not_in_manifest_but_keeps_technician_dirs`: existen `ana/001.jpg` y `<key2>/x.jpg`, y el manifiesto está vacío. `ana` sigue y `<key2>` desaparece.
- `test_replaces_technician_photos_in_cloud_folder`: `<key>/001.jpg` del técnico. Tras aplicar queda solo `<key>/<p1>.jpg`.
- `test_second_apply_same_manifest_is_unchanged`: aplicar dos veces da `changed is False` la segunda.
- `test_fetch_error_leaves_disk_untouched`: `fetch` lanza `RuntimeError` en la segunda foto. La excepción sale y el árbol de `faces_dir` es idéntico al de antes.
- `test_leftover_staging_dirs_are_ignored_and_cleaned`: crea `.cloud-staging/<key>/x.jpg` y `.cloud-old/<key>/` antes. Tras aplicar no existen, y `load_gallery` sobre `faces_dir` no los carga en ningún momento.

- [ ] **Step 2: Ejecuta y comprueba que fallan**

Run: `pytest tests/test_gallery_apply.py -v`
Expected: FAIL con `ModuleNotFoundError`.

- [ ] **Step 3: Implementa `apply_manifest`** con el algoritmo de arriba.

- [ ] **Step 4: Ejecuta y comprueba que pasan**

Run: `pytest tests/test_gallery_apply.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/vitahub/gallery/apply.py tests/test_gallery_apply.py
git commit -m "feat(gallery): sustitución atómica de las carpetas de la nube en /data/faces"
```

### Task 5: Configuración y lectura de S3

**Files:**
- Modify: `src/vitahub/config.py` (dataclass nueva y campo en `HubConfig`, junto a `ClipsConfig` en `:97-116`; lectura en `load_config` cerca de `:499`)
- Create: `src/vitahub/gallery/s3.py`
- Modify: `docker-compose.yml:44-48` y `docker-compose.mac.yml` (si declara las variables de clips)
- Test: `tests/test_config.py`

**Interfaces:**
- Produces:
  - `@dataclass class FaceGalleryConfig: enabled: bool = False; bucket: str = ""; region: str = "eu-west-1"; role_alias: str = ""; credentials_endpoint: str = ""` y `HubConfig.face_gallery: FaceGalleryConfig`.
  - `s3_list(bucket: str, region: str, creds: IotCredentialsProvider) -> Callable[[str], frozenset[str]]`: `ListObjectsV2` paginado. Devuelve las claves completas bajo el prefijo. Lanza `ValueError` si el prefijo no acaba en `/`.
  - `s3_get(bucket: str, region: str, creds: IotCredentialsProvider) -> Callable[[str], bytes]`: `GetObject`. Cualquier error lanza: un `404` no se espera porque solo se piden claves listadas.
  - Los dos solo corren en runtime, como `s3_put` (`src/vitahub/clips/uploader.py:333`).
  - `gallery_key(hub_id: str, *parts: str) -> str` → `hubs/<hub_id>/faces/<parts...>`

- [ ] **Step 1: Tests que fallan** (en `tests/test_config.py`)

- `test_face_gallery_disabled_without_bucket`: uplink encendido, sin `VITAHUB_FACE_PHOTOS_BUCKET` → `enabled is False`.
- `test_face_gallery_disabled_without_uplink`: con bucket y uplink apagado → `enabled is False`.
- `test_face_gallery_enabled_reuses_clips_role_alias`: uplink, bucket, `VITAHUB_CLIPS_ROLE_ALIAS` y `VITAHUB_IOT_CREDENTIALS_ENDPOINT` → `enabled`, `role_alias` y `region == "eu-west-1"`. Con `VITAHUB_FACE_PHOTOS_REGION=eu-south-2`, `region == "eu-south-2"`.
- `test_face_gallery_bucket_without_role_alias_raises`: `ConfigError` cuyo mensaje nombra `VITAHUB_CLIPS_ROLE_ALIAS`.
- `test_gallery_key`: `gallery_key("hub-1", "manifest.json") == "hubs/hub-1/faces/manifest.json"` y `gallery_key("hub-1") == "hubs/hub-1/faces/"` (con barra final, para el listado).

- [ ] **Step 2: Ejecuta y comprueba que fallan**

Run: `pytest tests/test_config.py -k face_gallery -v`
Expected: FAIL

- [ ] **Step 3: Implementa** la lectura desde `env` (no desde `hub.yaml`) y `s3_list` y `s3_get` con el mismo
patrón de botocore que `s3_put`: credenciales nuevas en cada llamada. Añade al compose
`VITAHUB_FACE_PHOTOS_BUCKET: ${VITAHUB_FACE_PHOTOS_BUCKET:-}` y
`VITAHUB_FACE_PHOTOS_REGION: ${VITAHUB_FACE_PHOTOS_REGION:-eu-west-1}`.

- [ ] **Step 4: Ejecuta y comprueba que pasan**

Run: `pytest tests/test_config.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/vitahub/config.py src/vitahub/gallery/s3.py docker-compose*.yml tests/test_config.py
git commit -m "feat(gallery): configuración por entorno y lectura de S3 con el role alias de clips"
```

### Task 6: Suscripciones MQTT que sobreviven a reconexiones

**Files:**
- Modify: `src/vitahub/sinks/aws_iot.py:23-36` (protocolo), `:93-133` (`build_client`), `:155-216` (callbacks)
- Modify: `src/vitahub/factory.py:62-98` (`build_sink` pasa a `build_uplink`), `src/vitahub/app.py:98`
- Test: `tests/test_aws_iot_sink.py`, `tests/test_factory.py`

**Interfaces:**
- Produces:
  - `MqttClient` gana `subscribe(topic: str, qos: int) -> object` y `message_callback_add(sub: str, callback: Callable[[object, object, Any], None]) -> object`.
  - `class MqttLink` en `aws_iot.py`:
    - `attach(client: MqttClient) -> None`
    - `subscribe(topic_filter: str, handler: Callable[[bytes], None]) -> None`: registra y suscribe ya, con QoS 1.
    - `add_connect_listener(fn: Callable[[], None]) -> None`
    - `on_connected() -> None`: vuelve a suscribir todo y después llama a los listeners. Cada listener va en su propio try/except: uno que falla no rompe los demás.
    - `publish(topic: str, payload: str) -> None`: QoS 1. Atrapa y loguea las excepciones.
  - `build_client(..., on_connected: Callable[[], None] | None = None) -> MqttClient`. Se llama solo cuando la conexión tiene éxito (`reason_code` 0/`Success`).
  - `@dataclass class Uplink: sink: EventSink; link: MqttLink | None` y `build_uplink(cfg: HubConfig) -> Uplink`. Sustituye a `build_sink`, con el mismo comportamiento y los mismos `ConfigError`.

- [ ] **Step 1: Tests que fallan**

- `test_link_resubscribes_on_every_connect`: un doble de cliente registra las llamadas a `subscribe`. Tras `subscribe("a/#", h)` y dos `on_connected()`, ve tres suscripciones a `a/#`.
- `test_link_routes_payload_to_handler`: el callback registrado con `message_callback_add` recibe un objeto con `.payload = b"x"` y el handler recibe `b"x"`.
- `test_connect_listener_runs_after_resubscribe_and_errors_are_isolated`.
- `test_on_connected_not_called_on_rejected_connect`: `_make_connect_callbacks(on_connected)` con `reason_code` 5 no llama a `on_connected`.
- En `test_factory.py`, adapta los dos tests de `build_sink` a `build_uplink`: con el uplink apagado, `link is None`.

- [ ] **Step 2: Ejecuta y comprueba que fallan**

Run: `pytest tests/test_aws_iot_sink.py tests/test_factory.py -v`
Expected: FAIL

- [ ] **Step 3: Implementa.** En `build_uplink`, crea `link = MqttLink()`, pasa
`on_connected=link.on_connected` a `build_client` y llama a `link.attach(client)`. En `app.run`:
`uplink = build_uplink(cfg); sink = uplink.sink`.

- [ ] **Step 4: Ejecuta y comprueba que pasan**

Run: `pytest -q`
Expected: PASS (suite completa: `build_sink` ya no existe)

- [ ] **Step 5: Commit**

```bash
git add src/vitahub/sinks/aws_iot.py src/vitahub/factory.py src/vitahub/app.py tests/test_aws_iot_sink.py tests/test_factory.py
git commit -m "feat(uplink): suscripciones MQTT que se renuevan en cada reconexión"
```

### Task 7: Servicio de sincronización

**Files:**
- Create: `src/vitahub/gallery/service.py`
- Test: `tests/test_gallery_service.py`

**Interfaces:**
- Consumes: `MqttLink` (Task 6). `shadow_topics`, `parse_get_accepted`, `parse_delta_version`, `reported_payload`, `identity_only_payload` (Task 3). `parse_manifest`, `ManifestError` (Task 3). `apply_manifest`, `ApplyResult` (Task 4).
- Produces:

```python
class CloudGalleryService:
    def __init__(
        self,
        link: MqttLink,
        hub_id: str,
        faces_dir: Path,
        list_keys: Callable[[str], frozenset[str]],    # prefijo con "/" final → claves completas
        get_object: Callable[[str], bytes],            # clave S3 completa → bytes; lanza si falla
        engine: Callable[[], FaceEngine],              # proveedor perezoso
        identity_enabled: bool,
        request_restart: Callable[[], None],
        clock: Callable[[], float] = time.monotonic,
        debounce_s: float = 30.0,
    ) -> None: ...
    def start(self, stop: threading.Event) -> None: ...   # suscribe, registra el listener y arranca el hilo
    def step(self, now: float) -> float | None: ...       # hace lo pendiente; devuelve segundos hasta la próxima acción
```

`step` existe para testear sin hilos. El hilo es un bucle: `step`, y después esperar el tiempo
devuelto o la próxima señal de un callback MQTT, hasta `stop`.

Comportamiento:

- **Conexión:** en cada `on_connected`, publica `get` con payload `{}`. Repite el `get` cada 30 s hasta recibir `get/accepted` o `get/rejected`.
- **`get/accepted`:** guarda `reported_photo_ids`. Aplicada = `reported_version or 0`. Si `desired_version` > aplicada, programa la aplicación a `now + debounce_s`. Si no, y `reported_identity_enabled` es distinto de `identity_enabled` (y hay `reported_version`), publica `identity_only_payload`.
- **`get/rejected`** (shadow inexistente, `404`): nada que hacer. Deja de repetir el `get`.
- **delta:** guarda la versión deseada y reprograma a `now + debounce_s` (cada delta reinicia la espera).
- **Aplicación:** lista `gallery_key(hub_id)`. Si `manifest.json` no está en el listado: `Manifest(version=desired, persons={})`. Si está, lo descarga. El `FetchPhoto` que recibe `apply_manifest` devuelve `None` sin llamar a S3 para las fotos que no están en el listado. `ManifestError`, o un error al listar o al leer (`403` incluido) → reintento con backoff 60 s, 120 s, ..., máximo 900 s. Se loguea el primero de cada episodio y uno de cada 10. Si `manifest.version` < versión deseada → reintento. Si no: `apply_manifest`, publicar `reported_payload(manifest.version, identity_enabled, statuses, previous_ids)`, actualizar `previous_ids` y la versión aplicada. Si `changed and identity_enabled`, llamar a `request_restart()` **después** de publicar.
- Un motor facial que no carga (`ConfigError`) se trata como un error de aplicación: backoff, sin tumbar el hub.

- [ ] **Step 1: Tests que fallan**

Doble de `MqttLink` que guarda las publicaciones y permite inyectar mensajes por topic. `list_keys` y `get_object`
sobre un `dict` en memoria. `StubFaceEngine`. Reloj falso.

- `test_get_on_connect_and_apply_after_debounce`: el `get/accepted` trae `desired 10`, sin `reported`. `step(0)` no aplica y `step(30)` aplica: hay una publicación en `.../update` con `version 10` y el fichero en disco.
- `test_delta_resets_debounce`: delta en t=0 y otro en t=20. `step(40)` no aplica y `step(50)` sí, con la última versión.
- `test_restart_only_when_identity_enabled_and_changed`: `identity_enabled=True` → `request_restart` se llama una vez. Reaplicar el mismo manifiesto no lo llama. `identity_enabled=False` → nunca, y `reported` lleva `"identity_enabled": false`.
- `test_manifest_not_listed_is_empty_gallery`: borra las carpetas `person_key` y reporta `photos` con `null` para los ids anteriores.
- `test_stale_manifest_retries`: `manifest.version` 9 con `desired 10` no publica nada y programa un reintento.
- `test_photo_not_listed_is_pending_without_get`: el manifiesto nombra `p2`, que no está en el listado. `get_object` nunca se llama con su clave y `p2` no sale en `reported.photos`.
- `test_fetch_error_backs_off_and_retries`: `list_keys` lanza tres veces (simula un `403`). Los tiempos devueltos por `step` son 60, 120 y 240, y el cuarto intento aplica.
- `test_startup_republishes_identity_enabled_when_stale`: `desired == reported == 10`, `reported.identity_enabled False`, servicio con `True`. Publica exactamente `identity_only_payload(True)` y no descarga nada.
- `test_get_rejected_stops_get_retries`.
- `test_restart_happens_after_report`: el orden de llamadas es primero la publicación y después `request_restart`.

- [ ] **Step 2: Ejecuta y comprueba que fallan**

Run: `pytest tests/test_gallery_service.py -v`
Expected: FAIL con `ModuleNotFoundError`.

- [ ] **Step 3: Implementa `CloudGalleryService`.** Estado protegido por un `threading.Lock`: los
callbacks llegan en el hilo de paho y `step` corre en el hilo propio. Un `threading.Event` despierta
el bucle cuando llega un mensaje.

- [ ] **Step 4: Ejecuta y comprueba que pasan**

Run: `pytest tests/test_gallery_service.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/vitahub/gallery/service.py tests/test_gallery_service.py
git commit -m "feat(gallery): sincronización de la galería con el shadow face-gallery"
```

### Task 8: Cableado, motor compartido y documentación

**Files:**
- Modify: `src/vitahub/factory.py` (`build_admin_engine_factory` en `:330-345` pasa a cachear; nueva `build_cloud_gallery`)
- Modify: `src/vitahub/app.py:100-150`
- Modify: `docs/como-funciona.md` (sección nueva tras "Identidad de la persona", `:420`; tabla de módulos `:65-110`; configuración `:601`)
- Modify: `docs/superpowers/specs/2026-09-02-identidad-persona-design.md:46-47`, `:54`, `:129-130`
- Modify: `docs/backlog.md:20-22`
- Test: `tests/test_factory.py`

**Interfaces:**
- Consumes: `Uplink` (Task 6), `FaceGalleryConfig`, `s3_list`, `s3_get`, `gallery_key` (Task 5), `CloudGalleryService` (Task 7).
- Produces:
  - `build_admin_engine_factory(env)` devuelve un proveedor cacheado y con lock: la primera llamada carga el motor y las siguientes devuelven el mismo. Lo usan `AdminContext.engine_factory` y el servicio.
  - `build_cloud_gallery(cfg: HubConfig, link: MqttLink | None, faces_dir: Path, engine: Callable[[], FaceEngine], request_restart: Callable[[], None]) -> CloudGalleryService | None`: `None` si `not cfg.face_gallery.enabled` o `link is None`. El PEM ilegible se traduce a `ConfigError`, como en `build_clip_service`.

- [ ] **Step 1: Tests que fallan**

- `test_build_cloud_gallery_none_when_disabled`
- `test_build_cloud_gallery_none_without_link`
- `test_admin_engine_factory_caches`: con `from_weights` parcheado, dos llamadas devuelven el mismo objeto y `from_weights` se llama una vez.

- [ ] **Step 2: Ejecuta y comprueba que fallan**

Run: `pytest tests/test_factory.py -v`
Expected: FAIL

- [ ] **Step 3: Implementa y cablea.** En `app.run`, después del control HTTP:
`gallery = build_cloud_gallery(cfg, uplink.link, faces_dir, engine_provider, stop.set)`; si no es
`None`, `gallery.start(stop)`. El hilo es daemon: el apagado no lo espera, y una sustitución a
medias se recupera en el siguiente arranque (Task 4).

- [ ] **Step 4: Documenta**

- `como-funciona.md`: sección "Galería desde la nube". Debe explicar el shadow, el manifiesto, el debounce, `reported`, el reinicio, la galería vacía y la convivencia con la consola local: una foto subida a mano a una carpeta `person_key` se pierde en la siguiente sincronización. Añade también las variables nuevas a "Configuración".
- Spec de identidad: sustituye "Enrolamiento remoto... downlink que no existe" y la frase de privacidad por la decisión nueva. Las fotos se guardan en la nube por decisión del usuario (spec web §3.1), solo en el bucket `face-photos` y bajo consentimiento registrado. Los embeddings siguen sin salir del hogar. Los eventos siguen llevando solo `person_key` y confianza.
- `backlog.md`: anota que ya existe un downlink (el shadow `face-gallery`) y que el rescan remoto podría usar el mismo patrón.

- [ ] **Step 5: Verifica la suite y los tipos**

Run: `pytest -q && mypy src && ruff check src tests`
Expected: todo en verde.

- [ ] **Step 6: Prueba manual sin la web** (necesita el bloque admin de infra desplegado)

1. Sube `manifest.json` y una foto a `hubs/<hub_id>/faces/` con `aws s3 cp`.
2. Ejecuta `aws iot-data update-thing-shadow --thing-name <hub_id> --shadow-name face-gallery --cli-binary-format raw-in-base64-out --payload '{"state":{"desired":{"version":<ms>}}}' /dev/stdout`.
3. A los 30 s, `get-thing-shadow` muestra `reported` con la foto `accepted`, y el hub reinicia si la identidad está encendida.
4. Repite con `persons: {}`: la carpeta desaparece y el hub arranca sin identidad.

- [ ] **Step 7: Commit**

```bash
git add src/vitahub/factory.py src/vitahub/app.py docs tests/test_factory.py
git commit -m "feat(gallery): cableado en el hub y documentación de la galería desde la nube"
```

## Acuerdos con la web y la infra (confirmados el 2026-11-08)

1. **Formato de `photoId`:** UUID canónico en minúscula con guiones, generado con
   `crypto.randomUUID()`. La API también lo valida. Si un id no cumple, el hub rechaza el
   manifiesto entero.
2. **Desvínculo del hub (5.7):** la web siempre escribe `manifest.json`. Al desvincular escribe
   `persons: {}` y borra los `.jpg` sobrantes. El orden siempre es: fotos, manifiesto (con la misma
   `version` que irá en `desired`) y shadow. Un manifiesto ausente del listado se trata como
   galería vacía: queda como red de seguridad.
5. **Política IAM del hub (bloque 14 del área admin):** `s3:GetObject` sobre
   `hubs/${credentials-iot:ThingName}/*` y `s3:ListBucket` con `StringLike s3:prefix =
   hubs/${credentials-iot:ThingName}/*`. Una clave inexistente da `403`, no `404`. Por eso el hub
   lista con `Prefix="hubs/<hub_id>/faces/"` antes de leer.
3. **Aprovisionamiento:** Tarea 5 del plan de infra. `provision-hub-clips.sh` lee
   `/vita/<env>/face-photos/bucket-name` y escribe `VITAHUB_FACE_PHOTOS_BUCKET` y
   `VITAHUB_FACE_PHOTOS_REGION` en el `.env` del hub, junto a `VITAHUB_CLIPS_ROLE_ALIAS` y
   `VITAHUB_IOT_CREDENTIALS_ENDPOINT`. Siempre, aunque los clips estén apagados.
4. **`reported.photos` usa `null` para borrar entradas.** `GetThingShadow` no devuelve claves
   `null`. La web trata un `photoId` ausente como pendiente.
