FROM python:3.11-slim
WORKDIR /app
COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir .
ENV RULES_CONFIG_PATH=/config/hub/rules.yaml
ENV EVENT_DB_PATH=/data/events.db
CMD ["python", "-m", "event_engine.main"]
