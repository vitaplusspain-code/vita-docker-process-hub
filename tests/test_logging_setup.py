import json

import pytest

from vitahub.logging_setup import (
    _clear_secrets,
    configure_logging,
    get_logger,
    redact,
    register_secret,
)


@pytest.fixture(autouse=True)
def _reset_secret_registry():
    """Evita que los secretos registrados en un test se filtren a otros."""
    _clear_secrets()
    yield
    _clear_secrets()


def test_redact_hides_secret():
    assert redact("user=admin pass=secret123", ["secret123"]) == "user=admin pass=***"
    assert redact("nada que ocultar", ["secret123"]) == "nada que ocultar"


def test_logs_go_to_stderr_as_json(capsys):
    configure_logging("INFO")
    get_logger("test").info("hola %s", "mundo")
    captured = capsys.readouterr()
    assert captured.out == ""  # nada por stdout (reservado a eventos)
    record = json.loads(captured.err.strip().splitlines()[-1])
    assert record["level"] == "INFO"
    assert record["message"] == "hola mundo"
    assert record["logger"] == "test"


def test_register_secret_redacts_future_logs(capsys):
    configure_logging("INFO")
    register_secret("topsecretpw")
    get_logger("test").info("conectando con clave %s", "topsecretpw")
    captured = capsys.readouterr()
    record = json.loads(captured.err.strip().splitlines()[-1])
    assert "topsecretpw" not in record["message"]
    assert "***" in record["message"]
