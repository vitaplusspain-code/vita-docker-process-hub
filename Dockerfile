# Base por defecto CPU (Mac/dev). Para Orin: --build-arg BASE_IMAGE=nvcr.io/nvidia/l4t-pytorch:<tag>
ARG BASE_IMAGE=python:3.12-slim
FROM ${BASE_IMAGE} AS base

RUN apt-get update && apt-get install -y --no-install-recommends \
    libgl1 libglib2.0-0 ffmpeg && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir .

COPY scripts ./scripts

# Embebe los pesos del modelo (arranca sin internet). Falla el build si no se
# pueden descargar/colocar los pesos: mejor un build roto que una imagen que
# arranca sin modelo.
RUN python scripts/download_model.py /app/models/yolo11n.pt

ENV VITAHUB_CONFIG=/data/hub.yaml \
    VITAHUB_WEIGHTS=/app/models/yolo11n.pt \
    PYTHONUNBUFFERED=1

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD python /app/scripts/healthcheck.py || exit 1

ENTRYPOINT ["python", "-m", "vitahub.app"]
