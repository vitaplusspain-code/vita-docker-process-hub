# Diseño — Administración local: enrolamiento y config para el técnico (v1)

- Fecha: 2026-09-12
- Estado: aprobado en brainstorming, pendiente de plan de implementación
- Repo: `vita-docker-process-hub`
- Slice anterior: `2026-09-02-identidad-persona-design.md`
- Abre: la API JSON que la app familiar consumirá en el futuro

## 0. El problema

La identidad de persona ya está integrada, pero enrolar a alguien exige acceso por terminal al
dispositivo: copiar fotos a `/data/faces/<person_id>/`, editar `hub.yaml` a mano y reiniciar el
contenedor. El técnico que hace la instalación en el hogar no debería necesitar SSH ni saber YAML.

Este slice añade al hub una **página de administración local** servida por el propio hub, con la
que el técnico — conectado a la WiFi del hogar desde su móvil o portátil — enrola personas
(fotos incluidas, con la cámara del móvil), ajusta un subconjunto curado de la config y aplica
los cambios con un reinicio limpio. La página consume una **API JSON** que en el futuro
reutilizará la app familiar; hoy todo es local.

## 1. Alcance

Dentro:

- Rutas nuevas en el servidor de control existente (puerto 8787, mismo token
  `VITAHUB_ADMIN_TOKEN`, mismo `compare_digest`): API JSON de enrolamiento y config, y una
  página estática en `GET /`.
- `admin/enrollment.py`: listar/crear/borrar personas y fotos en `/data/faces/`, validando cada
  foto al subirla (exactamente una cara) con un `InsightFaceEngine` propio y perezoso.
- `admin/config_edit.py`: leer y escribir **solo** `inference.fall.enabled`,
  `inference.identity.enabled` y `inference.identity.match_threshold` sobre `hub.yaml`,
  preservando el resto del fichero y validando la candidata con el `load_config` real antes de
  escribir.
- `POST /api/apply`: validación final + apagado limpio; `restart: unless-stopped` del compose
  relevanta el hub con lo nuevo.
- Página estática (HTML + JS vanilla, sin build): personas con miniaturas, subida de fotos con
  feedback inmediato, tres controles de config, botón "Aplicar y reiniciar" con polling de
  vuelta.

Fuera, con motivo:

- **Editar el `hub.yaml` completo desde la UI.** Invita a romper campos que el arranque
  fail-fast convertirá en un hub que no levanta; el resto de la config se sigue editando a mano
  en instalación.
- **Recarga en caliente.** Exigiría una segunda ruta de inicialización (supervisor, workers,
  `FaceIdentifier`) que puede divergir de la del arranque. Un corte de segundos durante una
  visita técnica es irrelevante.
- **Captura de fotos desde las cámaras del hub.** Las fotos de enrolamiento buenas son de cerca,
  con luz variada; la cámara del móvil del técnico ya lo resuelve.
- **Usuarios/roles y HTTPS local.** Un solo token en la LAN del hogar, como el `/rescan` actual.
- **La app familiar.** Consumirá esta API cuando llegue; aquí solo se diseña la API para no
  cerrarle la puerta (JSON, rutas REST, token en cabecera).

## 2. Arquitectura

Tres piezas, HTTP fino que delega en lógica pura:

| Pieza | Responsabilidad |
|---|---|
| `admin/enrollment.py` | Lógica pura de personas y fotos. Sin HTTP. |
| `admin/config_edit.py` | Lógica pura del subconjunto de config. Sin HTTP. |
| `control.py` (crece) | Rutas nuevas, auth, multipart, servir la página estática. |

### API JSON

| Ruta | Qué hace |
|---|---|
| `GET /api/people` | Personas enroladas: `[{id, photos: [nombre...]}]` |
| `GET /api/people/<id>/photos/<n>` | La foto (miniaturas en la UI) |
| `POST /api/people/<id>/photos` | Sube una foto (multipart); crea la persona si no existe |
| `DELETE /api/people/<id>/photos/<n>` | Borra una foto |
| `DELETE /api/people/<id>` | Borra la persona y todas sus fotos |
| `GET /api/config` | `{fall_enabled, identity_enabled, match_threshold}` |
| `PUT /api/config` | Escribe esos tres campos, validados; el resto del YAML intacto |
| `POST /api/apply` | Validación final → 200 "reiniciando" → apagado limpio |

Todas las rutas exigen el token; sin `VITAHUB_ADMIN_TOKEN` el servidor sigue sin abrir puerto
(comportamiento actual).

### El engine del servidor de control

Los `InsightFaceEngine` de los workers viven en sus hilos; para validar "exactamente una cara"
al subir, el servidor de control instancia el suyo **perezosamente en la primera subida** y lo
reutiliza. Los pesos ya están embebidos en la imagen; el coste es memoria solo cuando de verdad
se enrola.

### Aplicar cambios

`POST /api/apply` responde antes de apagar y luego dispara el mismo camino de apagado limpio que
`SIGTERM`. Docker (`restart: unless-stopped`) relevanta el proceso, que arranca con la galería y
la config nuevas por el camino fail-fast de siempre. **No hay segunda ruta de carga.**

## 3. La página del técnico

Una sola página estática embebida en el paquete, servida en `GET /`:

- **Acceso**: pide el token la primera vez, lo guarda en `localStorage`, lo manda en
  `Authorization`; un 401 lo vuelve a pedir.
- **Personas**: lista con miniaturas y nº de fotos; añadir persona (el nombre visible se
  normaliza a un `person_id` válido); subir fotos con
  `<input type="file" accept="image/*" capture="environment">` — en el móvil abre la cámara.
  Cada foto sube de una en una con respuesta inmediata: ✓ o el motivo ("2 caras", "no se
  detecta cara", "imagen ilegible").
- **Config**: interruptor de caídas, interruptor de identidad, deslizador de `match_threshold`
  con la nota "0.4 = sin calibrar". La dependencia `identity requiere fall` se refleja en la
  UI, pero la que manda es la validación del servidor.
- **Aplicar**: resumen de lo que va a pasar, confirmación, y polling a `GET /api/people` hasta
  que el hub vuelve: "hub reiniciado con N personas enroladas".

## 4. Validación y errores

Todo se valida **en el servidor y antes de tocar disco**; la UI solo mejora la experiencia.

- **Fotos**: límite 10 MB → `cv2.imdecode` → `engine.extract` debe devolver exactamente 1 cara.
  Son las mismas reglas de `load_gallery` al arrancar: una foto aceptada por la API jamás rompe
  el arranque. Se guarda con nombre generado secuencial (`001.jpg`, `002.jpg`…), nunca el
  nombre que venga del cliente.
- **`person_id`**: patrón estricto `[a-z0-9-]{1,32}` — es nombre de carpeta y sale en los
  eventos; nada de `../` ni espacios.
- **Config**: se carga el `hub.yaml` actual, se aplican solo los tres campos permitidos y la
  candidata pasa por `load_config` real (fichero temporal). Solo si valida se escribe, con
  escritura atómica (tmp + rename, como la persistencia de cámaras de `config.py`). La regla
  `identity requiere fall` ya vive en `_identity_from_raw` y se hereda gratis.
- **`/api/apply`**: última red — con `identity.enabled: true` exige ≥1 persona con ≥1 foto
  válida en disco.
- **Errores HTTP**: 400 con `{error: "mensaje legible"}` para entrada inválida, 401 sin token,
  404 para persona/foto inexistente, 413 para foto demasiado grande.

## 5. Tests

Mismo estilo TDD del repo:

- `tests/test_enrollment.py` — lógica pura con el `StubEngine` de `identity/stub.py`: foto
  válida, 0 caras, 2 caras, imagen ilegible, `person_id` inválido, borrados, listar.
- `tests/test_config_edit.py` — lectura del subconjunto; escritura que preserva el resto del
  YAML (cámaras, uplink…); candidata inválida no escribe nada; escritura atómica.
- `tests/test_control.py` (ampliar) — auth en todas las rutas nuevas, subida multipart, `apply`
  dispara el apagado (shutdown falso inyectado), 404/413/400.

## 6. Decisiones tomadas en brainstorming

| Decisión | Elección | Por qué |
|---|---|---|
| Alcance del técnico | Enrolamiento + subconjunto curado | El YAML entero invita a romper el arranque; solo-fotos deja fuera el umbral que hay que calibrar por hogar |
| Interfaz | Página servida por el hub + API JSON | Sin artefactos que distribuir; el móvil hace las fotos; la API queda para la app familiar |
| Aplicar cambios | Validar → escribir → reinicio limpio vía Docker | Reutiliza el arranque fail-fast; sin segunda ruta de carga; el corte de segundos es irrelevante en una visita |
| Dónde vive | Servidor de control existente (8787) | Mismo token, misma superficie, cero puertos nuevos |
