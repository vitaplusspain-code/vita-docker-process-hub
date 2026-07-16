# Verificación manual — Esqueleto del hub + event-engine

No automatizada (requiere webcam real). Ejecutar tras completar las Tareas 1-13
del plan de implementación, antes de dar por cerrada la entrega.

## Arranque

1. `docker compose up --build`
2. Confirmar que los 5 servicios arrancan sin reinicios en bucle:
   `docker compose ps` — todos `Up`.
3. `docker compose logs mosquitto` — sin errores de conexión.
4. `docker compose logs frigate` — confirma que detecta el stream RTSP de
   `mediamtx` y publica en MQTT (`frigate/available` → `online`).

## Criterios de aceptación (spec §8)

- [ ] **Criterio 1 — Latencia y persistencia de `presence_zone`:** ponerse
      delante de la webcam, dentro de la zona `cocina`. En
      `docker compose logs event-engine` debe aparecer
      `event generated: {... "type": "presence_zone" ...}` en menos de 5 s.
      Verificar persistencia:
      `docker compose exec event-engine sqlite3 /data/events.db "select type, status from events;"`
      debe listar la fila con `status` en `pending` o `synced`.
- [ ] **Criterio 2 — Resiliencia MQTT:** `docker compose stop mosquitto`,
      esperar 15 s, `docker compose start mosquitto`. En los logs de
      `event-engine` debe verse el mensaje de reconexión (`retrying in Xs`)
      y, tras reconectar, ningún evento se pierde ni se duplica (comparar
      `select count(*), event_id from events group by event_id having count(*) > 1;`
      → vacío).
- [ ] **Criterio 3 — Recuperación tras reinicio:** generar un evento,
      `docker compose restart event-engine`, revisar logs de arranque: no
      debe generarse un `inactivity_prolonged` espurio inmediatamente
      después del reinicio (confirma que `bootstrap_last_motion` recuperó
      `last_motion` desde SQLite).
- [ ] **Criterio 4 — Sin fuga de vídeo:** confirmar que ningún servicio
      publica frames fuera del host: solo `edge_sync_stub` escribe al log
      con el JSON del evento (sin campos de imagen/vídeo). El puerto 5000
      (UI de Frigate) y 8554 (RTSP de mediamtx) son solo para depuración
      local en el propio host, no se exponen a Internet.

## Registro de resultado

Fecha, hardware usado (Jetson Orin Nano u otro), y resultado de cada
criterio (PASS/FAIL + nota) deben añadirse aquí antes de cerrar la entrega.
