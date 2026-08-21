# vita-docker-process-hub

Hub de procesamiento edge de VitaPlus (rama cámaras). Descubre cámaras ONVIF,
ingesta RTSP, detecta presencia/conteo de personas y emite eventos JSON por stdout.

## Desarrollo local (Mac, CPU)

```bash
pip install -e ".[dev]"
export VITAHUB_ONVIF_USER=admin VITAHUB_ONVIF_PASSWORD=xxxx
export VITAHUB_CONFIG=./config/hub.example.yaml
python -m vitahub.app
```

Para probar sin cámaras ni modelo: pon `detector: stub` en la config.

## Docker

El volumen `/data` empieza vacío: hay que sembrar la config antes del primer
arranque o `load_config` falla (y el contenedor entra en crash-loop bajo
`restart: unless-stopped`). La credencial ONVIF sigue yendo por variable de
entorno, nunca en ese fichero.

```bash
mkdir -p ./data
cp config/hub.example.yaml ./data/hub.yaml
# edita ./data/hub.yaml y pon un hub_id único

export VITAHUB_ONVIF_USER=admin VITAHUB_ONVIF_PASSWORD=xxxx
docker compose up --build
```

## Instalación en campo (por hogar)

1. Con el móvil: mete cada cámara en el WiFi del hogar, activa ONVIF y ponle la
   contraseña **del hogar** (la misma para todas).
2. En el Jetson: siembra la config antes del primer arranque —
   `mkdir -p ./data && cp config/hub.example.yaml ./data/hub.yaml` — y edita
   `./data/hub.yaml` para poner un `hub_id` único de ese hogar. La credencial
   ONVIF **no** va en ese fichero: define `VITAHUB_ONVIF_USER`/
   `VITAHUB_ONVIF_PASSWORD` (esa clave) como variables de entorno y luego
   `docker compose up -d`.
3. Verifica: `docker logs -f <container>` — deberías ver "cam onvif-... conectada" y
   eventos `person_detected` por stdout.
4. El contenedor se relanza solo tras cortes de luz (`restart: unless-stopped`).
5. **Cámaras nuevas**: el hub re-descubre la LAN cada `discovery.interval_seconds`
   (60 s por defecto), así que una cámara añadida después se da de alta sola. Para
   no esperar, define `VITAHUB_ADMIN_TOKEN` (genera uno con `openssl rand -hex 32`)
   y dispara el escaneo desde la misma red:

   ```bash
   curl -X POST -H "Authorization: Bearer $VITAHUB_ADMIN_TOKEN" \
     http://<ip-del-jetson>:8787/rescan
   ```

   Responde con las cámaras encontradas y las dadas de alta. Sin ese token el
   endpoint no escucha en ningún puerto y solo funciona el escaneo periódico.

## Uplink a AWS (opcional)

Con `uplink.enabled: true` en `hub.yaml`, el hub publica cada evento en AWS IoT Core
por MQTT con TLS mutuo, **además** de seguir escribiéndolos por stdout. El topic es
`vita/hub/<hub_id>/events` y el payload es el mismo JSON que ves en `docker logs`.

Antes de encenderlo hay que dar de alta el hogar en AWS. Desde el repo
`vitaplus-aws-architecture`:

```bash
./scripts/provision-hub.sh <hub_id>
```

Ese script crea el "thing", su certificado y su policy, y te imprime el endpoint. Copia
los tres ficheros que deja a `./data/certs/` del Jetson y ajusta permisos:

```bash
mkdir -p ./data/certs && chmod 700 ./data/certs && chmod 600 ./data/certs/private.pem.key
```

Después, en el arranque:

```bash
export VITAHUB_IOT_ENDPOINT=<el que imprimió provision-hub.sh>
docker compose up -d
```

Si algo falta, el hub **no arranca** y lo dice: es un error de instalación y estás
delante. Si el enlace se cae *después*, el hub sigue detectando y logueando con
normalidad; los eventos de ese rato **se pierden** (no hay cola — es una decisión
consciente, ver `docs/backlog.md`).

Certificado comprometido o Jetson perdido: se revoca ese hogar y solo ese, con
`aws iot update-certificate --new-status REVOKED --certificate-id <id>`.

## Despliegue en Jetson Orin (GPU)

```bash
docker build --build-arg BASE_IMAGE=nvcr.io/nvidia/l4t-pytorch:r36.2.0-pth2.2-py3 -t vitahub:orin .
```
