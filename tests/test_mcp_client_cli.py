import asyncio
import json
import socket
import threading
import time
from io import StringIO
from pathlib import Path

import httpx
import mcp.types as types
import uvicorn

from sysadmin_mcp_kit.config import AppSettings
from sysadmin_mcp_kit.mcp_client_cli import ElicitationOptions, MCPCLI, main
from sysadmin_mcp_kit.server import build_server


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class _RunningServer:
    def __init__(self, app, port: int):
        self._port = port
        self._config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
        self._server = uvicorn.Server(self._config)
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        asyncio.run(self._server.serve())

    def __enter__(self) -> "_RunningServer":
        self._thread.start()
        deadline = time.time() + 10.0
        while time.time() < deadline:
            try:
                response = httpx.get(f"http://127.0.0.1:{self._port}/mcp", timeout=0.2)
                if response.status_code in {401, 405}:
                    return self
            except httpx.HTTPError:
                time.sleep(0.05)
        raise RuntimeError("MCP test server did not start in time")

    def __exit__(self, exc_type, exc, tb) -> None:
        self._server.should_exit = True
        self._thread.join(timeout=2.0)
        if self._thread.is_alive():
            self._server.force_exit = True
            self._thread.join(timeout=2.0)


class _StubTerminalCLI(MCPCLI):
    async def _prepare_terminal_session(self, target_id: str, initial_directory: str | None) -> str:
        assert target_id == "cheetan"
        return initial_directory or "/etc"

    async def run_command(
        self,
        *,
        target_id: str,
        command: str,
        timeout_seconds: int | None = None,
        working_dir: str | None = None,
        assume_yes: bool = False,
        confirmation_token: str | None = None,
    ) -> dict[str, object]:
        assert target_id == "cheetan"
        assert working_dir is None
        assert assume_yes is False
        assert confirmation_token is None
        if command == "export DEMO_ENV=active":
            payload = {
                "execution_id": "exec-1",
                "current_directory": "/etc",
                "stdout": {"text": "", "next_cursor": None},
                "stderr": {"text": "", "next_cursor": None},
            }
        elif command == "echo $DEMO_ENV":
            payload = {
                "execution_id": "exec-2",
                "current_directory": "/etc",
                "stdout": {"text": "active\n", "next_cursor": None},
                "stderr": {"text": "", "next_cursor": None},
            }
        else:
            payload = {
                "execution_id": "exec-3",
                "current_directory": "/etc",
                "stdout": {"text": _stdout_lines(1, 50), "next_cursor": "cursor-1"},
                "stderr": {"text": "", "next_cursor": None},
            }
        self._last_command_payload = payload  # type: ignore[assignment]
        self._output_cursors["stdout"] = payload["stdout"]["next_cursor"]  # type: ignore[index]
        self._output_cursors["stderr"] = payload["stderr"]["next_cursor"]  # type: ignore[index]
        return payload  # type: ignore[return-value]

    async def read_command_output(
        self,
        *,
        execution_id: str,
        stream: str,
        cursor: str,
        page_lines: int | None = None,
    ) -> dict[str, object]:
        assert execution_id == "exec-3"
        assert stream == "stdout"
        assert cursor == "cursor-1"
        assert page_lines is None
        return {
            "execution_id": execution_id,
            "stream": stream,
            "content": {"text": _stdout_lines(51, 100)},
            "next_cursor": "cursor-2",
        }


def _stdout_lines(start: int, end: int) -> str:
    return "\n".join(f"stdout {index}" for index in range(start, end + 1)) + "\n"


def _test_settings(settings, port: int) -> AppSettings:
    data = settings.model_dump(mode="json")
    data["server"]["port"] = port
    data["oauth"]["resource_server_url"] = f"http://127.0.0.1:{port}/mcp"
    return AppSettings.model_validate(data)


def _start_server(settings, fake_ssh_service, token_verifier):
    port = _free_port()
    test_settings = _test_settings(settings, port)
    app = build_server(test_settings, ssh_service=fake_ssh_service, token_verifier=token_verifier).streamable_http_app()
    return _RunningServer(app, port), f"http://127.0.0.1:{port}/mcp"


def test_mcp_client_lists_targets_over_streamable_http(settings, fake_ssh_service, token_verifier) -> None:
    stdout = StringIO()
    stderr = StringIO()
    server, url = _start_server(settings, fake_ssh_service, token_verifier)

    with server:
        exit_code = main(
            ["--url", url, "--token", "good-token", "list-targets"],
            stdout=stdout,
            stderr=stderr,
        )

    assert exit_code == 0
    payload = json.loads(stdout.getvalue())
    assert payload["targets"][0]["target_id"] == "cheetan"
    assert "[DEBUG] loaded dotenv path:" in stderr.getvalue()

def test_mcp_client_fetches_token_via_client_credentials(settings, fake_ssh_service, token_verifier) -> None:
    stdout = StringIO()
    stderr = StringIO()
    server, url = _start_server(settings, fake_ssh_service, token_verifier)

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/realms/test/protocol/openid-connect/token"
        body = (await request.aread()).decode()
        assert "grant_type=client_credentials" in body
        assert "client_id=cli-client" in body
        assert "client_secret=cli-secret" in body
        assert "scope=sysadmin%3Amcp" in body
        return httpx.Response(200, json={"access_token": "good-token"})

    def token_http_client_factory() -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            base_url="https://oauth.example.test",
        )

    with server:
        exit_code = main(
            [
                "--url",
                url,
                "--issuer-url",
                "https://oauth.example.test/realms/test",
                "--client-id",
                "cli-client",
                "--client-secret",
                "cli-secret",
                "list-targets",
            ],
            stdout=stdout,
            stderr=stderr,
            token_http_client_factory=token_http_client_factory,
        )

    assert exit_code == 0
    payload = json.loads(stdout.getvalue())
    assert payload["targets"][0]["target_id"] == "cheetan"



def test_mcp_client_ignores_bearer_token_from_dotenv_and_fetches_client_credentials(
    settings,
    fake_ssh_service,
    token_verifier,
    monkeypatch,
) -> None:
    stdout = StringIO()
    stderr = StringIO()
    server, url = _start_server(settings, fake_ssh_service, token_verifier)

    for env_name in (
        "SYSADMIN_MCP_BEARER_TOKEN",
        "SYSADMIN_MCP_OAUTH_CLIENT_ID",
        "SYSADMIN_MCP_OAUTH_CLIENT_SECRET",
        "SYSADMIN_MCP_OAUTH_TOKEN_ENDPOINT",
        "SYSADMIN_MCP_OAUTH_ISSUER_URL",
        "SYSADMIN_MCP_OAUTH_SCOPE",
    ):
        monkeypatch.delenv(env_name, raising=False)

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/realms/test/protocol/openid-connect/token"
        body = (await request.aread()).decode()
        assert "grant_type=client_credentials" in body
        assert "client_id=cli-client" in body
        assert "client_secret=cli-secret" in body
        assert "scope=sysadmin%3Amcp" in body
        return httpx.Response(200, json={"access_token": "good-token"})

    def token_http_client_factory() -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            base_url="https://oauth.example.test",
        )

    fixture_root = Path(__file__).resolve().parent / "fixtures" / "dotenv_bearer"
    monkeypatch.chdir(fixture_root / "src")

    with server:
        exit_code = main(
            ["--url", url, "list-targets"],
            stdout=stdout,
            stderr=stderr,
            token_http_client_factory=token_http_client_factory,
        )

    assert exit_code == 0
    payload = json.loads(stdout.getvalue())
    assert payload["targets"][0]["target_id"] == "cheetan"
    assert "[DEBUG] explicit access token:" not in stderr.getvalue()


def test_mcp_client_loads_client_credentials_from_dotenv(
    settings,
    fake_ssh_service,
    token_verifier,
    monkeypatch,
) -> None:
    stdout = StringIO()
    stderr = StringIO()
    server, url = _start_server(settings, fake_ssh_service, token_verifier)

    for env_name in (
        "SYSADMIN_MCP_BEARER_TOKEN",
        "SYSADMIN_MCP_OAUTH_CLIENT_ID",
        "SYSADMIN_MCP_OAUTH_CLIENT_SECRET",
        "SYSADMIN_MCP_OAUTH_TOKEN_ENDPOINT",
        "SYSADMIN_MCP_OAUTH_ISSUER_URL",
        "SYSADMIN_MCP_OAUTH_SCOPE",
    ):
        monkeypatch.delenv(env_name, raising=False)

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/realms/test/protocol/openid-connect/token"
        body = (await request.aread()).decode()
        assert "grant_type=client_credentials" in body
        assert "client_id=cli-client" in body
        assert "client_secret=cli-secret" in body
        assert "scope=sysadmin%3Amcp" in body
        return httpx.Response(200, json={"access_token": "good-token"})

    def token_http_client_factory() -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            base_url="https://oauth.example.test",
        )

    fixture_root = Path(__file__).resolve().parent / "fixtures" / "dotenv_oauth"
    monkeypatch.chdir(fixture_root / "src")

    with server:
        exit_code = main(
            ["--url", url, "list-targets"],
            stdout=stdout,
            stderr=stderr,
            token_http_client_factory=token_http_client_factory,
        )

    assert exit_code == 0
    payload = json.loads(stdout.getvalue())
    assert payload["targets"][0]["target_id"] == "cheetan"


def test_mcp_client_reports_auth_failure_cleanly() -> None:
    stdout = StringIO()
    stderr = StringIO()

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "invalid_token", "error_description": "Authentication required"})

    def http_client_factory(headers: dict[str, str]) -> httpx.AsyncClient:
        assert headers["Authorization"] == "Bearer bad-token"
        return httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            base_url="http://127.0.0.1:8000",
            headers=headers,
        )

    exit_code = main(
        ["--url", "http://127.0.0.1:8000/mcp", "--token", "bad-token", "list-targets"],
        stdout=stdout,
        stderr=stderr,
        http_client_factory=http_client_factory,
    )

    assert exit_code == 1
    assert "Authentication failed" in stderr.getvalue()


def test_elicitation_auto_accepts_sensitive_form_without_prompt() -> None:
    stderr = StringIO()
    cli = MCPCLI(
        url="http://127.0.0.1:8000/mcp",
        token="good-token",
        stdout=StringIO(),
        stderr=stderr,
        input_fn=lambda _prompt: (_ for _ in ()).throw(AssertionError("Unexpected prompt")),
    )
    cli._elicitation_options = ElicitationOptions(assume_yes=True, confirmation_token="ABCD1234")
    params = types.ElicitRequestFormParams(
        message="Approve remote command execution.",
        requestedSchema={
            "type": "object",
            "properties": {
                "approve": {"type": "boolean", "title": "Approve"},
                "confirmation_token": {"type": "string", "title": "Confirmation Token"},
            },
            "required": ["approve", "confirmation_token"],
        },
    )

    result = asyncio.run(cli._handle_elicitation(None, params))

    assert isinstance(result, types.ElicitResult)
    assert result.action == "accept"
    assert result.content == {"approve": True, "confirmation_token": "ABCD1234"}
    assert "Approve remote command execution." in stderr.getvalue()



def test_interactive_terminal_session_confirms_command_over_streamable_http(settings, fake_ssh_service, token_verifier) -> None:
    stdout = StringIO()
    stderr = StringIO()
    answers = iter(["4", "cheetan", "", "ls", "", "y", "exit", "5"])
    server, url = _start_server(settings, fake_ssh_service, token_verifier)

    with server:
        exit_code = main(
            ["--url", url, "--token", "good-token", "interactive"],
            stdout=stdout,
            stderr=stderr,
            input_fn=lambda _prompt: next(answers),
        )

    assert exit_code == 0
    assert stdout.getvalue() == "listing /etc\n"
    assert "Approve remote command execution." in stderr.getvalue()
    assert "Failed to call tool 'run_command'" not in stderr.getvalue()


def test_interactive_terminal_session_handles_password_elicitation_over_streamable_http(settings, fake_ssh_service, token_verifier) -> None:
    stdout = StringIO()
    stderr = StringIO()
    answers = iter(["4", "cheetan", "", "need-password", "", "y", "opensesame", "exit", "5"])
    server, url = _start_server(settings, fake_ssh_service, token_verifier)

    with server:
        exit_code = main(
            ["--url", url, "--token", "good-token", "interactive"],
            stdout=stdout,
            stderr=stderr,
            input_fn=lambda _prompt: next(answers),
        )

    assert exit_code == 0
    assert stdout.getvalue() == "password accepted\n"
    assert "Remote command requested password input." in stderr.getvalue()
    assert "Password:" in stderr.getvalue()
    assert "Failed to call tool 'run_command'" not in stderr.getvalue()


def test_interactive_terminal_session_renders_output_and_pages_more() -> None:
    stdout = StringIO()
    stderr = StringIO()
    answers = iter(
        [
            "4",
            "cheetan",
            "/etc",
            "export DEMO_ENV=active",
            "",
            "echo $DEMO_ENV",
            "",
            "whoami",
            "",
            "$more stdout",
            "$info",
            "exit",
            "5",
        ]
    )
    cli = _StubTerminalCLI(
        url="http://127.0.0.1:8000/mcp",
        token="good-token",
        stdout=stdout,
        stderr=stderr,
        input_fn=lambda _prompt: next(answers),
    )

    exit_code = asyncio.run(cli.interactive_shell())

    assert exit_code == 0
    output = stdout.getvalue()
    json_start = output.rfind('{\n  "execution_id"')
    assert json_start > 0
    rendered_output = output[:json_start]
    payload = json.loads(output[json_start:])

    assert "active\n" in rendered_output
    assert "stdout 1\n" in rendered_output
    assert "stdout 100\n" in rendered_output
    assert payload["current_directory"] == "/etc"
    assert payload["stdout"]["text"].startswith("stdout 1")
    assert "Use '$more stdout' or '$more stderr'" in stderr.getvalue()
    assert "More stdout output is available. Use '$more stdout'." in stderr.getvalue()
    assert "More stdout output is still available. Use '$more stdout'." in stderr.getvalue()