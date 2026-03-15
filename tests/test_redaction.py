from sysadmin_mcp_kit.redaction import Redactor


class _Settings:
    sensitive_key_patterns = [
        r"password",
        r"token",
        r"secret",
        r"api[_-]?key",
        r"private[_-]?key",
    ]
    text_patterns = []


def test_redactor_redacts_json_and_bearer_tokens() -> None:
    redactor = Redactor(_Settings())
    payload = b'{"password": "hunter2", "nested": {"token": "abc"}, "ok": 1}'

    result = redactor.redact_bytes(payload, path="config.json")

    assert result.binary is False
    assert result.parser == "json"
    assert "hunter2" not in result.text
    assert "abc" not in result.text
    assert result.replacements == 2


def test_redactor_falls_back_to_text_patterns() -> None:
    redactor = Redactor(_Settings())
    text = "Authorization: Bearer top-secret-token\npassword = letmein\npostgres://user:secret@host/db"

    result = redactor.redact_text(text)

    assert "top-secret-token" not in result.text
    assert "letmein" not in result.text
    assert "secret@host" not in result.text
    assert result.replacements >= 3


def test_redactor_preserves_env_reference_values() -> None:
    redactor = Redactor(_Settings())
    payload = b"PASSWORD=${DB_PASSWORD}\nSECRET=$APP_SECRET\nTOKEN=env_variable\nPASSWORD=actual-secret\n"

    result = redactor.redact_bytes(payload, path=".env")

    assert "PASSWORD=${DB_PASSWORD}" in result.text
    assert "SECRET=$APP_SECRET" in result.text
    assert "TOKEN=env_variable" in result.text
    assert "PASSWORD=<REDACTED>" in result.text
    assert "actual-secret" not in result.text
    assert result.replacements == 1
