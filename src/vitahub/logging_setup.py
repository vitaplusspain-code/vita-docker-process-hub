from __future__ import annotations

import json
import logging
import sys
from collections.abc import Iterable

_secrets: list[str] = []


def register_secret(secret: str) -> None:
    """Registra un secreto para que se redacte de todos los logs futuros."""
    if secret:
        _secrets.append(secret)


def _clear_secrets() -> None:
    """Solo para tests: vacía el registro de secretos entre casos."""
    _secrets.clear()


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        message = redact(record.getMessage(), _secrets)
        payload = {
            "level": record.levelname,
            "logger": record.name,
            "message": message,
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def configure_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler(stream=sys.stderr)
    handler.setFormatter(_JsonFormatter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


def redact(text: str, secrets: Iterable[str]) -> str:
    for secret in secrets:
        if secret:
            text = text.replace(secret, "***")
    return text
