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
