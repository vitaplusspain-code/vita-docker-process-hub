import json

from vitahub.logging_setup import configure_logging, get_logger, redact


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
