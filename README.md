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

```bash
export VITAHUB_ONVIF_USER=admin VITAHUB_ONVIF_PASSWORD=xxxx
docker compose up --build
```

## Instalación en campo (por hogar)

1. Con el móvil: mete cada cámara en el WiFi del hogar, activa ONVIF y ponle la
   contraseña **del hogar** (la misma para todas).
2. En el Jetson: define `VITAHUB_ONVIF_USER`/`VITAHUB_ONVIF_PASSWORD` (esa clave) y
   `docker compose up -d`.
3. Verifica: `docker logs -f <container>` — deberías ver "cam onvif-... conectada" y
   eventos `person_detected` por stdout.
4. El contenedor se relanza solo tras cortes de luz (`restart: unless-stopped`).

## Despliegue en Jetson Orin (GPU)

```bash
docker build --build-arg BASE_IMAGE=nvcr.io/nvidia/l4t-pytorch:r36.2.0-pth2.2-py3 -t vitahub:orin .
```
