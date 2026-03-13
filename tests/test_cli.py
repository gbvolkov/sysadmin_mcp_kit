import json
from io import StringIO

from sysadmin_mcp_kit.cli import main
from sysadmin_mcp_kit.policy import CommandPolicy


def _parse_json_stream(text: str) -> list[dict]:
    decoder = json.JSONDecoder()
    payloads: list[dict] = []
    index = 0
    while index < len(text):
        while index < len(text) and text[index].isspace():
            index += 1
        if index >= len(text):
            break
        payload, next_index = decoder.raw_decode(text, index)
        payloads.append(payload)
        index = next_index
    return payloads


def test_list_targets_outputs_configured_targets(settings, fake_ssh_service) -> None:
    stdout = StringIO()
    stderr = StringIO()

    exit_code = main(["list-targets"], settings=settings, ssh_service=fake_ssh_service, stdout=stdout, stderr=stderr)

    assert exit_code == 0
    payload = json.loads(stdout.getvalue())
    assert payload["targets"][0]["target_id"] == "cheetan"
    assert stderr.getvalue() == ""


def test_read_file_redacts_and_supports_cursor_paging(settings, fake_ssh_service) -> None:
    stdout = StringIO()
    stderr = StringIO()

    exit_code = main(
        ["read-file", "cheetan", "/etc/app/config.env", "--page-lines", "20"],
        settings=settings,
        ssh_service=fake_ssh_service,
        stdout=stdout,
        stderr=stderr,
    )

    assert exit_code == 0
    payload = json.loads(stdout.getvalue())
    assert "hunter2" not in payload["content"]["text"]
    assert payload["next_cursor"] is not None

    stdout = StringIO()
    stderr = StringIO()
    exit_code = main(
        [
            "read-file",
            "cheetan",
            "/etc/app/config.env",
            "--page-lines",
            "20",
            "--cursor",
            payload["next_cursor"],
        ],
        settings=settings,
        ssh_service=fake_ssh_service,
        stdout=stdout,
        stderr=stderr,
    )

    assert exit_code == 0
    next_payload = json.loads(stdout.getvalue())
    assert next_payload["content"]["start_line"] == 20


def test_run_command_supports_non_mcp_execution(settings, fake_ssh_service) -> None:
    stdout = StringIO()
    stderr = StringIO()
    command = "sudo systemctl restart nginx"
    token = CommandPolicy(settings.command_policy).confirmation_token(command)

    exit_code = main(
        [
            "run-command",
            "cheetan",
            command,
            "--yes",
            "--confirmation-token",
            token,
        ],
        settings=settings,
        ssh_service=fake_ssh_service,
        stdout=stdout,
        stderr=stderr,
    )

    assert exit_code == 0
    payload = json.loads(stdout.getvalue())
    assert fake_ssh_service.command_calls == [("cheetan", command, 90, None)]
    assert payload["summary"]["sensitive"] is True
    assert "super-secret" not in payload["stderr"]["text"]
    assert "command running" in stderr.getvalue()


def test_interactive_mode_lists_targets_and_quits(settings, fake_ssh_service) -> None:
    stdout = StringIO()
    stderr = StringIO()
    answers = iter(["1", "5"])

    exit_code = main(
        [],
        settings=settings,
        ssh_service=fake_ssh_service,
        stdout=stdout,
        stderr=stderr,
        input_fn=lambda _prompt: next(answers),
    )

    assert exit_code == 0
    payloads = _parse_json_stream(stdout.getvalue())
    assert payloads[0]["targets"][0]["target_id"] == "cheetan"
    assert "Interactive sysadmin-mcp-cli" in stderr.getvalue()


def test_interactive_terminal_session_recovers_from_command_error(settings, fake_ssh_service) -> None:
    stdout = StringIO()
    stderr = StringIO()
    answers = iter([
        "4",
        "cheetan",
        "/etc",
        "cd ngnx",
        "",
        "pwd",
        "",
        "exit",
        "5",
    ])

    exit_code = main(
        [],
        settings=settings,
        ssh_service=fake_ssh_service,
        stdout=stdout,
        stderr=stderr,
        input_fn=lambda _prompt: next(answers),
    )

    assert exit_code == 0
    assert stdout.getvalue() == "/etc\n"
    assert "Error: Remote path does not exist: /etc/ngnx" in stderr.getvalue()
    assert "Terminal session closed." in stderr.getvalue()


def test_interactive_terminal_session_persists_working_directory(settings, fake_ssh_service) -> None:
    stdout = StringIO()
    stderr = StringIO()
    answers = iter([
        "4",
        "cheetan",
        "/etc/app",
        "cd subdir",
        "",
        "pwd",
        "",
        "ls -l",
        "",
        "y",
        "exit",
        "5",
    ])

    exit_code = main(
        [],
        settings=settings,
        ssh_service=fake_ssh_service,
        stdout=stdout,
        stderr=stderr,
        input_fn=lambda _prompt: next(answers),
    )

    assert exit_code == 0
    assert stdout.getvalue() == "/etc/app/subdir\n/etc/app/subdir\nlisting /etc/app/subdir\n"
    assert fake_ssh_service.command_calls == [("cheetan", "ls -l", 90, "/etc/app/subdir")]
    assert "Terminal session started" in stderr.getvalue()
    assert "Use '$info' to print the last full JSON payload." in stderr.getvalue()


def test_interactive_terminal_session_info_prints_last_json(settings, fake_ssh_service) -> None:
    stdout = StringIO()
    stderr = StringIO()
    answers = iter([
        "4",
        "cheetan",
        "/etc",
        "pwd",
        "",
        "$info",
        "exit",
        "5",
    ])

    exit_code = main(
        [],
        settings=settings,
        ssh_service=fake_ssh_service,
        stdout=stdout,
        stderr=stderr,
        input_fn=lambda _prompt: next(answers),
    )

    assert exit_code == 0
    rendered_output, json_payload = stdout.getvalue().split('{', 1)
    assert rendered_output == "/etc\n"
    payload = json.loads("{" + json_payload)
    assert payload["current_directory"] == "/etc"
    assert payload["stdout"]["text"].strip() == "/etc"

