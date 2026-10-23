# Estado de conexión de las cámaras (lado hub) — Plan de implementación

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** que el hub emita `camera_reachable` con `initial: true` en el primer fotograma de cada cámara desde que arrancó el proceso, de modo que una cámara que vuelve mientras el hub se reinicia no quede dada por caída para siempre en la nube; y que la emisión al volver tras una caída avisada lleve `initial: false`.

**Architecture:** cambio localizado en `ConnectionMonitor.on_frame` (`src/vitahub/analytics/connection_monitor.py`). Un flag nuevo por cámara en `_ConnState` (`emitted_initial: bool`) distingue el primer fotograma del proceso del resto. Payload de `camera_reachable` gana el campo `initial`. Sin cambios en `on_failed`, en `camera_unreachable`, en el worker ni en los sinks.

**Tech Stack:** Python 3.12, pytest.

**Spec:** `vita-docker-rule-engine:docs/estado-conexion-camara@aec7778:docs/superpowers/specs/2026-10-23-estado-conexion-camara-design.md`, §7 (y §3.2 para el contrato del payload). Léelo antes de la tarea; este plan no repite su razonamiento.

## Global Constraints

- `on_frame` nunca lanza: cualquier excepción propia pasaría por el `except Exception` del worker como hoy, pero no debe aparecer una nueva vía.
- `initial` va **siempre** que se emita `camera_reachable` (recuperación tras caída → `false`; primer fotograma del proceso → `true`). Un hub sin este cambio seguía mandando `camera_reachable` sin el campo, y el motor lo trata igual (spec §3.2).
- La semántica del umbral de 300 s de `on_failed` no se toca: la cámara que arranca caída sigue acabando reportada aunque nunca haya habido un fotograma.
- Comentarios y mensajes en español, como el resto del repo. `ruff` y `mypy --strict` limpios.
- Tests: `pytest -v` desde la raíz del repo.

## Review Focus

1. **Cámara que arranca caída, se emite `camera_unreachable`, luego vuelve con un fotograma:** debe salir `camera_reachable` con `initial: false` (es una recuperación avisada, no la primera vez), aunque sea literalmente el primer fotograma de la cámara en este proceso. El flag `emitted_initial` se marca `True` igualmente, para que un fotograma posterior sin caída no vuelva a emitir.
2. **Fallo inicial corto (bajo umbral) sin `camera_unreachable`, luego primer fotograma:** debe salir `camera_reachable` con `initial: true` (nunca se avisó caída, el fotograma sigue siendo el primero del proceso).
3. **Primer fotograma de la segunda cámara no debe reemitirse:** `emitted_initial` es por cámara (vive en `_states[camera.id]`), no compartido.
4. **Compatibilidad con el motor:** un `camera_reachable` sin `initial` (hubs viejos) sigue siendo válido; el motor lo trata igual (spec §3.2). Este cambio solo añade el campo cuando el hub ya está actualizado.

---

### Task 1: `camera_reachable` con `initial` en `ConnectionMonitor`

**Files:**
- Modify: `src/vitahub/analytics/connection_monitor.py`
- Modify: `tests/test_connection_monitor.py`

**Interfaces:**
- `_ConnState` gana `emitted_initial: bool = False`.
- `on_frame(camera, now)`:
  1. `state = self._states.setdefault(camera.id, _ConnState(last_ok=now))`
  2. `was_down = state.reported_down`; `emitted_initial = state.emitted_initial`
  3. `state.last_ok = now`; `state.reported_down = False`; `state.emitted_initial = True`
  4. `if was_down:` → emitir `camera_reachable` con payload `{"last_ip": camera.last_ip, "initial": False}`.
  5. `elif not emitted_initial:` → emitir `camera_reachable` con payload `{"last_ip": camera.last_ip, "initial": True}`.
  6. `else:` → `[]`.
- `on_failed` no cambia.

- [ ] **Step 1: Write the failing tests.** Añadir y actualizar:
  - `test_initial_true_on_first_frame` (nuevo): sin fallos previos, `on_frame(cam, 0.0)` emite un único evento `type == "camera_reachable"`, `severity == "info"`, `payload == {"last_ip": "10.0.0.5", "initial": True}`.
  - `test_second_frame_without_prior_outage_is_quiet` (nuevo, sustituye al nombre viejo `test_reachable_only_after_an_unreachable` para la parte "primer fotograma vale, segundo no"): tras el primer `on_frame`, un `on_frame` posterior sin `camera_unreachable` entre medias devuelve `[]`.
  - `test_initial_true_after_brief_failure_below_threshold` (nuevo, Review Focus 2): `on_failed(cam, 0.0)`; `on_failed(cam, 100.0)` (bajo umbral, nada); primer `on_frame(cam, 150.0)` → `initial: True` (nunca se avisó caída).
  - `test_recovery_after_announced_outage_is_initial_false` (nuevo, Review Focus 1): `on_failed(cam, 0.0)`; `on_failed(cam, 301.0)` → emite `camera_unreachable`; primer `on_frame(cam, 500.0)` → `camera_reachable` con `initial: False` **aunque sea el primer fotograma**.
  - `test_recovery_from_outage_mid_stream_is_initial_false` (actualizar el existente `test_reachable_only_after_an_unreachable`): tras algún `on_frame`, un episodio de caída avisado y un `on_frame` de vuelta → `initial: False`. El test actual no verifica el payload de `initial`; añadir la aserción.
  - `test_initial_fires_once_per_camera` (nuevo, Review Focus 3): `on_frame(a, 0.0)` emite `initial: True`; `on_frame(b, 0.0)` emite otro `initial: True`; `on_frame(a, 10.0)` no emite nada.
  - Revisar los tests existentes que llaman a `on_frame` después de un `on_failed` con `camera_unreachable` (`test_a_new_outage_emits_again`, `test_reachable_only_after_an_unreachable`, `test_cameras_do_not_contaminate_each_other`): ninguno verifica el payload de `camera_reachable`; añadir la clave `initial` donde haga falta para que el contrato quede fijado.

- [ ] **Step 2: Run** `pytest tests/test_connection_monitor.py -v` → FAIL (varios).

- [ ] **Step 3: Implement** los cambios en `_ConnState` y `on_frame` descritos arriba. El docstring de `on_frame` gana una frase: en el primer fotograma de cada cámara desde que arrancó el proceso emite `camera_reachable` con `initial: true`; en la vuelta tras una caída avisada va `initial: false`; cualquier otro fotograma no emite nada.

- [ ] **Step 4: Run** `pytest -v` desde la raíz; `ruff check . && mypy` limpios.

- [ ] **Step 5: Commit** `feat(connection): camera_reachable con initial para distinguir arranque de recuperación`.

### Task 2: Documentación

**Files:**
- Modify: `docs/como-funciona.md` (sección «eventos de conexión», líneas 168–178).

- [ ] **Step 1:** Actualizar la viñeta de `camera_reachable` para explicar los dos casos:
  - Un fotograma válido tras un `camera_unreachable` → `camera_reachable` con `initial: false` (recuperación).
  - El primer fotograma válido de cada cámara desde que arrancó el proceso → `camera_reachable` con `initial: true`.
  Y añadir en una frase corta por qué: si la cámara vuelve mientras el hub está reiniciado, nunca llegaría un `camera_reachable` y la nube la daría por caída para siempre (motivo del spec §7).

- [ ] **Step 2: Commit** `docs(connection): camera_reachable inicial tras arranque del hub`.

---

Fuera de este plan: el motor (`vita-docker-rule-engine`), la migración (`vitaplus-aws-architecture`), la API y el frontend (`vita-web-family-app`). Cada repo sale con su propio plan; ver §8 del spec para el orden de implementación.
