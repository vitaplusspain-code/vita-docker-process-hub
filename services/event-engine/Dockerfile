FROM python:3.11-slim
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends sqlite3 \
    && rm -rf /var/lib/apt/lists/*
COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir .
ENV RULES_CONFIG_PATH=/config/hub/rules.yaml
ENV EVENT_DB_PATH=/data/events.db
CMD ["python", "-m", "event_engine.main"]
