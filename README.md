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
5. **Aviso**: una cámara añadida DESPUÉS de que el hub ya esté arrancado **no**
   se detecta automáticamente en esta versión — el descubrimiento solo corre
   al arranque. Para que la recoja, ejecuta `docker compose restart` (el
   redescubrimiento periódico queda para una futura versión).

## Despliegue en Jetson Orin (GPU)

```bash
docker build --build-arg BASE_IMAGE=nvcr.io/nvidia/l4t-pytorch:r36.2.0-pth2.2-py3 -t vitahub:orin .
```
