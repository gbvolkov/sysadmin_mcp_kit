from __future__ import annotations

import re
from types import SimpleNamespace

import httpx
import pytest

from sysadmin_mcp_kit.server import build_server


class FakeContext:
    def __init__(self, *, approve: bool = True, approve_sensitive: bool = True, mcp_session_id: str | None = "session-1"):
        self.request_id = "req-1"
        self.client_id = "test-client"
        self.mcp_session_id = mcp_session_id
        self.progress_events: list[tuple[float, float | None, str | None]] = []
        self.elicitation_messages: list[str] = []
        self._approve = approve
        self._approve_sensitive = approve_sensitive

    async def report_progress(self, progress: float, total: float | None = None, message: str | None = None) -> None:
        self.progress_events.append((progress, total, message))

    async def elicit(self, message: str, schema):
        self.elicitation_messages.append(message)
        if not self._approve:
            return SimpleNamespace(action="decline")
        payload = {"approve": True}
        if "confirmation_token" in schema.model_fields:
            token = re.search(r"Confirmation token: ([A-Z0-9]+)", message).group(1)
            payload["confirmation_token"] = token if self._approve_sensitive else "WRONG"
        return SimpleNamespace(action="accept", data=schema.model_validate(payload))


def _structured(result):
    return result[1]


@pytest.mark.asyncio
async def test_server_exposes_no_explicit_session_management_tools(settings, fake_ssh_service, token_verifier) -> None:
    server = build_server(settings, ssh_service=fake_ssh_service, token_verifier=token_verifier)

    tool_names = {tool.name for tool in await server.list_tools()}

    assert "run_command" in tool_names
    assert "create_terminal_session" not in tool_names
    assert "run_session_command" not in tool_names
    assert "close_terminal_session" not in tool_names


@pytest.mark.asyncio
async def test_unauthorized_http_request_returns_auth_challenge(settings, fake_ssh_service, token_verifier) -> None:
    app = build_server(settings, ssh_service=fake_ssh_service, token_verifier=token_verifier).streamable_http_app()

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8000") as client:
        response = await client.get("/mcp")

    assert response.status_code == 401
    assert "Bearer" in response.headers["www-authenticate"]
    assert "resource_metadata=" in response.headers["www-authenticate"]


@pytest.mark.asyncio
async def test_browse_files_and_read_file_paginate(settings, fake_ssh_service, token_verifier) -> None:
    server = build_server(settings, ssh_service=fake_ssh_service, token_verifier=token_verifier)
    ctx = FakeContext()
    server.get_context = lambda: ctx

    browse_data = _structured(await server.call_tool("browse_files", {"target_id": "cheetan", "directory": "/etc/app", "limit": 1}))
    assert browse_data["summary"]["total_entries"] == 3
    assert len(browse_data["entries"]) == 1
    assert browse_data["next_cursor"] is not None

    browse_next = _structured(
        await server.call_tool(
            "browse_files",
            {
                "target_id": "cheetan",
                "directory": "/etc/app",
                "limit": 1,
                "cursor": browse_data["next_cursor"],
            },
        )
    )
    assert len(browse_next["entries"]) == 1

    file_data = _structured(
        await server.call_tool(
            "read_file",
            {"target_id": "cheetan", "path": "/etc/app/config.env", "page_lines": 20},
        )
    )
    assert file_data["summary"]["result_id"]
    assert "hunter2" not in file_data["content"]["text"]
    assert file_data["next_cursor"] is not None

    next_page = _structured(
        await server.call_tool(
            "read_file",
            {
                "target_id": "cheetan",
                "path": "/etc/app/config.env",
                "page_lines": 20,
                "cursor": file_data["next_cursor"],
            },
        )
    )
    assert next_page["content"]["start_line"] == 20
    assert ctx.progress_events


@pytest.mark.asyncio
async def test_run_command_reports_progress_and_paginates_output(settings, fake_ssh_service, token_verifier) -> None:
    server = build_server(settings, ssh_service=fake_ssh_service, token_verifier=token_verifier)
    ctx = FakeContext()
    server.get_context = lambda: ctx

    data = _structured(
        await server.call_tool(
            "run_command",
            {"target_id": "cheetan", "command": "sudo systemctl restart nginx"},
        )
    )

    assert fake_ssh_service.command_calls == [("cheetan", "sudo systemctl restart nginx", 90, "/etc")]
    assert data["summary"]["sensitive"] is True
    assert data["summary"]["exit_code"] == 0
    assert data["stdout"]["next_cursor"] is not None
    assert "super-secret" not in data["stderr"]["text"]
    assert len(ctx.elicitation_messages) == 2
    assert ctx.progress_events

    next_data = _structured(
        await server.call_tool(
            "read_command_output",
            {
                "execution_id": data["execution_id"],
                "stream": "stdout",
                "cursor": data["stdout"]["next_cursor"],
                "page_lines": 25,
            },
        )
    )
    assert next_data["content"]["returned_lines"] == 25
    assert next_data["stream"] == "stdout"


@pytest.mark.asyncio
async def test_run_command_preserves_shell_environment_within_mcp_session(settings, fake_ssh_service, token_verifier) -> None:
    server = build_server(settings, ssh_service=fake_ssh_service, token_verifier=token_verifier)
    ctx = FakeContext()
    server.get_context = lambda: ctx

    export_result = _structured(
        await server.call_tool(
            "run_command",
            {"target_id": "cheetan", "command": "export DEMO_ENV=active"},
        )
    )
    assert export_result["summary"]["exit_code"] == 0

    echo_result = _structured(
        await server.call_tool(
            "run_command",
            {"target_id": "cheetan", "command": "echo $DEMO_ENV"},
        )
    )
    assert echo_result["stdout"]["text"].strip() == "active"


@pytest.mark.asyncio
async def test_run_command_accepts_explicit_working_dir(settings, fake_ssh_service, token_verifier) -> None:
    server = build_server(settings, ssh_service=fake_ssh_service, token_verifier=token_verifier)
    ctx = FakeContext(mcp_session_id=None)
    server.get_context = lambda: ctx

    result = _structured(
        await server.call_tool(
            "run_command",
            {"target_id": "cheetan", "command": "ls -l", "working_dir": "/etc/app"},
        )
    )

    assert fake_ssh_service.command_calls == [("cheetan", "ls -l", 90, "/etc/app")]
    assert result["current_directory"] == "/etc/app"
    assert result["builtin"] is False


@pytest.mark.asyncio
async def test_run_command_persists_working_directory_within_mcp_session(settings, fake_ssh_service, token_verifier) -> None:
    server = build_server(settings, ssh_service=fake_ssh_service, token_verifier=token_verifier)
    ctx = FakeContext()
    server.get_context = lambda: ctx

    cd_result = _structured(
        await server.call_tool(
            "run_command",
            {"target_id": "cheetan", "command": "cd /etc/app/subdir"},
        )
    )
    assert cd_result["builtin"] is True
    assert cd_result["current_directory"] == "/etc/app/subdir"

    pwd_result = _structured(
        await server.call_tool(
            "run_command",
            {"target_id": "cheetan", "command": "pwd"},
        )
    )
    assert pwd_result["stdout"]["text"].strip() == "/etc/app/subdir"
    assert pwd_result["builtin"] is True

    ls_result = _structured(
        await server.call_tool(
            "run_command",
            {"target_id": "cheetan", "command": "ls -l"},
        )
    )
    assert ls_result["current_directory"] == "/etc/app/subdir"
    assert ls_result["builtin"] is False
    assert fake_ssh_service.command_calls == [("cheetan", "ls -l", 90, "/etc/app/subdir")]

    fake_ssh_service.command_calls.clear()
    ls_with_override = _structured(
        await server.call_tool(
            "run_command",
            {"target_id": "cheetan", "command": "ls -l", "working_dir": "subdir"},
        )
    )
    assert ls_with_override["current_directory"] == "/etc/app/subdir/subdir"
    assert ls_with_override["builtin"] is False
    assert fake_ssh_service.command_calls == [("cheetan", "ls -l", 90, "/etc/app/subdir/subdir")]


@pytest.mark.asyncio
async def test_declined_confirmation_prevents_command_execution(settings, fake_ssh_service, token_verifier) -> None:
    server = build_server(settings, ssh_service=fake_ssh_service, token_verifier=token_verifier)
    ctx = FakeContext(approve=False)
    server.get_context = lambda: ctx

    with pytest.raises(Exception):
        await server.call_tool(
            "run_command",
            {"target_id": "cheetan", "command": "echo hello"},
        )

    assert fake_ssh_service.command_calls == []


@pytest.mark.asyncio
async def test_builtin_command_rejects_working_dir_override(settings, fake_ssh_service, token_verifier) -> None:
    server = build_server(settings, ssh_service=fake_ssh_service, token_verifier=token_verifier)
    ctx = FakeContext()
    server.get_context = lambda: ctx

    with pytest.raises(Exception):
        await server.call_tool(
            "run_command",
            {"target_id": "cheetan", "command": "pwd", "working_dir": "/etc/app"},
        )

    assert fake_ssh_service.command_calls == []


@pytest.mark.asyncio
async def test_builtin_command_requires_transport_session_for_persistence(settings, fake_ssh_service, token_verifier) -> None:
    server = build_server(settings, ssh_service=fake_ssh_service, token_verifier=token_verifier)
    ctx = FakeContext(mcp_session_id=None)
    server.get_context = lambda: ctx

    with pytest.raises(Exception):
        await server.call_tool(
            "run_command",
            {"target_id": "cheetan", "command": "cd /etc"},
        )

    assert fake_ssh_service.command_calls == []
