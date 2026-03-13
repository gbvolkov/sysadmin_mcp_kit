from __future__ import annotations

import argparse
import asyncio
import json
import os
import shlex
import sys
from collections.abc import Callable, Sequence
from contextlib import AsyncExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TextIO

import httpx
import mcp.types as types
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import McpError

YES_ANSWERS = {"y", "yes"}
QUIT_ANSWERS = {"5", "q", "quit", "exit"}
ANSI_RED = "\x1b[31m"
ANSI_RESET = "\x1b[0m"
DEFAULT_URL = "http://127.0.0.1:8000/mcp"
DEFAULT_CLIENT_ID_ENV = "SYSADMIN_MCP_OAUTH_CLIENT_ID"
DEFAULT_CLIENT_SECRET_ENV = "SYSADMIN_MCP_OAUTH_CLIENT_SECRET"
DEFAULT_TOKEN_ENDPOINT_ENV = "SYSADMIN_MCP_OAUTH_TOKEN_ENDPOINT"
DEFAULT_ISSUER_URL_ENV = "SYSADMIN_MCP_OAUTH_ISSUER_URL"
DEFAULT_SCOPE_ENV = "SYSADMIN_MCP_OAUTH_SCOPE"
DEFAULT_SCOPE = "sysadmin:mcp"
DEFAULT_DOTENV_FILENAME = ".env"


class MCPClientCliError(RuntimeError):
    """Raised when the MCP CLI cannot continue."""


@dataclass
class ElicitationOptions:
    assume_yes: bool = False
    confirmation_token: str | None = None


HttpClientFactory = Callable[[dict[str, str]], httpx.AsyncClient]
TokenHttpClientFactory = Callable[[], httpx.AsyncClient]


class MCPCLI:
    def __init__(
        self,
        *,
        url: str,
        token: str,
        stdout: TextIO | None = None,
        stderr: TextIO | None = None,
        input_fn: Callable[[str], str] | None = None,
        http_client_factory: HttpClientFactory | None = None,
    ) -> None:
        self._url = url
        self._token = token
        self._stdout = stdout or sys.stdout
        self._stderr = stderr or sys.stderr
        self._input = input_fn or input
        self._http_client_factory = http_client_factory or self._default_http_client_factory
        self._stack = AsyncExitStack()
        self._session: ClientSession | None = None
        self._elicitation_options = ElicitationOptions()
        self._last_command_payload: dict[str, Any] | None = None
        self._output_cursors: dict[str, str | None] = {"stdout": None, "stderr": None}

    async def __aenter__(self) -> "MCPCLI":
        headers = {"Authorization": f"Bearer {self._token}"}
        client = self._http_client_factory(headers)
        await self._stack.enter_async_context(client)
        await self._preflight(client)

        try:
            read_stream, write_stream, _ = await self._stack.enter_async_context(
                streamable_http_client(self._url, http_client=client)
            )
            self._session = await self._stack.enter_async_context(
                ClientSession(
                    read_stream,
                    write_stream,
                    elicitation_callback=self._handle_elicitation,
                    client_info=types.Implementation(name="sysadmin-mcp-client-cli", version="0.1.0"),
                )
            )
            await self._session.initialize()
        except BaseException as exc:
            await self._safe_close()
            raise self._translate_transport_exception("initialize MCP session", exc) from None
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self._safe_close()

    async def list_targets(self) -> dict[str, Any]:
        return await self._call_tool("list_targets", {})

    async def browse_files(
        self,
        *,
        target_id: str,
        directory: str,
        glob_pattern: str = "*",
        limit: int | None = None,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        arguments: dict[str, Any] = {
            "target_id": target_id,
            "directory": directory,
            "glob": glob_pattern,
        }
        if limit is not None:
            arguments["limit"] = limit
        if cursor is not None:
            arguments["cursor"] = cursor
        return await self._call_tool("browse_files", arguments)

    async def read_file(
        self,
        *,
        target_id: str,
        path: str,
        page_lines: int | None = None,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        arguments: dict[str, Any] = {
            "target_id": target_id,
            "path": path,
        }
        if page_lines is not None:
            arguments["page_lines"] = page_lines
        if cursor is not None:
            arguments["cursor"] = cursor
        return await self._call_tool("read_file", arguments)

    async def run_command(
        self,
        *,
        target_id: str,
        command: str,
        timeout_seconds: int | None = None,
        working_dir: str | None = None,
        assume_yes: bool = False,
        confirmation_token: str | None = None,
    ) -> dict[str, Any]:
        arguments: dict[str, Any] = {
            "target_id": target_id,
            "command": command,
        }
        if timeout_seconds is not None:
            arguments["timeout_seconds"] = timeout_seconds
        if working_dir is not None:
            arguments["working_dir"] = working_dir
        payload = await self._call_tool(
            "run_command",
            arguments,
            elicitation=ElicitationOptions(
                assume_yes=assume_yes,
                confirmation_token=confirmation_token,
            ),
        )
        self._last_command_payload = payload
        self._output_cursors["stdout"] = payload.get("stdout", {}).get("next_cursor")
        self._output_cursors["stderr"] = payload.get("stderr", {}).get("next_cursor")
        return payload

    async def read_command_output(
        self,
        *,
        execution_id: str,
        stream: str,
        cursor: str,
        page_lines: int | None = None,
    ) -> dict[str, Any]:
        arguments: dict[str, Any] = {
            "execution_id": execution_id,
            "stream": stream,
            "cursor": cursor,
        }
        if page_lines is not None:
            arguments["page_lines"] = page_lines
        return await self._call_tool("read_command_output", arguments)

    async def interactive_shell(self) -> int:
        print("Interactive sysadmin-mcp-client", file=self._stderr)
        print("Choose an action by number or name. Type 'quit' to exit.", file=self._stderr)
        while True:
            try:
                self._print_menu()
                choice = self._ask("Choice: ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                print("\nExiting interactive mode.", file=self._stderr)
                return 0

            if choice in QUIT_ANSWERS:
                print("Exiting interactive mode.", file=self._stderr)
                return 0

            try:
                if choice in {"1", "list", "list-targets"}:
                    _emit_json(await self.list_targets(), self._stdout)
                elif choice in {"2", "browse", "browse-files"}:
                    await self._interactive_browse_files()
                elif choice in {"3", "read", "read-file"}:
                    await self._interactive_read_file()
                elif choice in {"4", "execute", "exec", "run", "run-command", "terminal", "terminal-session"}:
                    await self._interactive_terminal_session()
                else:
                    print("Unknown choice. Select 1-5 or a command name.", file=self._stderr)
            except (MCPClientCliError, McpError, httpx.HTTPError) as exc:
                print(f"Error: {exc}", file=self._stderr)

    async def _interactive_browse_files(self) -> None:
        target_id = self._require_text("Target id: ")
        directory = self._require_text("Directory: ")
        glob_pattern = self._ask_with_default("Glob [*]: ", "*")
        limit = self._ask_optional_int("Limit (blank for configured default): ")
        cursor: str | None = None

        while True:
            result = await self.browse_files(
                target_id=target_id,
                directory=directory,
                glob_pattern=glob_pattern,
                limit=limit,
                cursor=cursor,
            )
            _emit_json(result, self._stdout)
            cursor = result.get("next_cursor")
            if not cursor:
                break
            if self._ask("Show next page? [y/N]: ").strip().lower() not in YES_ANSWERS:
                break

    async def _interactive_read_file(self) -> None:
        target_id = self._require_text("Target id: ")
        path = self._require_text("Remote file path: ")
        page_lines = self._ask_optional_int("Page lines (blank for server default): ")
        cursor: str | None = None

        while True:
            result = await self.read_file(
                target_id=target_id,
                path=path,
                page_lines=page_lines,
                cursor=cursor,
            )
            _emit_json(result, self._stdout)
            cursor = result.get("next_cursor")
            if not cursor:
                break
            if self._ask("Show next page? [y/N]: ").strip().lower() not in YES_ANSWERS:
                break

    async def _interactive_terminal_session(self) -> None:
        target_id = self._require_text("Target id: ")
        initial_directory = self._ask("Initial directory (blank for current session state): ").strip()
        current_directory = await self._prepare_terminal_session(target_id, initial_directory or None)
        self._last_command_payload = None
        self._output_cursors = {"stdout": None, "stderr": None}

        print(
            f"Terminal session ready for {target_id} in {current_directory}. Type 'exit' to return to the menu.",
            file=self._stderr,
        )
        print("Use '$info' to print the last full JSON payload.", file=self._stderr)
        print("Use '$more stdout' or '$more stderr' when additional command output is available.", file=self._stderr)

        while True:
            prompt = f"[{target_id} {current_directory}]$ "
            command = self._ask(prompt).strip()
            if not command:
                continue
            lowered = command.lower()
            if lowered in {"exit", "quit", "back"}:
                break
            if lowered == "$info":
                if self._last_command_payload is None:
                    print("No command result is available yet.", file=self._stderr)
                else:
                    _emit_json(self._last_command_payload, self._stdout)
                continue
            if lowered in {"$more", "$more stdout", "$more stderr"}:
                stream = "stdout" if lowered in {"$more", "$more stdout"} else "stderr"
                await self._render_more_output(stream)
                continue

            try:
                timeout_seconds = self._ask_optional_int("Timeout seconds (blank for target default): ")
                result = await self.run_command(
                    target_id=target_id,
                    command=command,
                    timeout_seconds=timeout_seconds,
                )
            except (MCPClientCliError, McpError, httpx.HTTPError) as exc:
                print(f"Error: {exc}", file=self._stderr)
                continue

            self._render_command_output(result)
            current_directory = str(result.get("current_directory") or current_directory)
            self._print_more_hint(result)

        print("Terminal session closed.", file=self._stderr)

    async def _prepare_terminal_session(self, target_id: str, initial_directory: str | None) -> str:
        if initial_directory:
            quoted = shlex.quote(initial_directory)
            await self.run_command(
                target_id=target_id,
                command=f"cd {quoted}",
            )
        pwd_result = await self.run_command(
            target_id=target_id,
            command="pwd",
        )
        return str(pwd_result.get("current_directory") or pwd_result.get("stdout", {}).get("text", "").strip() or "/")

    async def _render_more_output(self, stream: str) -> None:
        if self._last_command_payload is None:
            print("No command result is available yet.", file=self._stderr)
            return
        cursor = self._output_cursors.get(stream)
        if not cursor:
            print(f"No additional {stream} output is available.", file=self._stderr)
            return
        result = await self.read_command_output(
            execution_id=str(self._last_command_payload["execution_id"]),
            stream=stream,
            cursor=cursor,
        )
        self._output_cursors[stream] = result.get("next_cursor")
        content = result.get("content", {})
        text = str(content.get("text", ""))
        if text:
            if stream == "stderr":
                self._write_output(self._stderr, text, color=ANSI_RED)
            else:
                self._write_output(self._stdout, text)
        if self._output_cursors[stream]:
            print(f"More {stream} output is still available. Use '$more {stream}'.", file=self._stderr)

    async def _call_tool(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        elicitation: ElicitationOptions | None = None,
    ) -> dict[str, Any]:
        if self._session is None:
            raise MCPClientCliError("MCP session is not initialized")

        self._elicitation_options = elicitation or ElicitationOptions()
        try:
            result = await self._session.call_tool(
                name,
                arguments,
                progress_callback=self._progress_callback,
            )
        except BaseException as exc:
            raise self._translate_transport_exception(f"call tool '{name}'", exc) from None
        finally:
            self._elicitation_options = ElicitationOptions()

        if result.isError:
            raise MCPClientCliError(_tool_error_message(result))
        if result.structuredContent is None:
            raise MCPClientCliError(f"Tool {name} did not return structured content")
        return result.structuredContent

    async def _handle_elicitation(
        self,
        _context,
        params: types.ElicitRequestParams,
    ) -> types.ElicitResult | types.ErrorData:
        if isinstance(params, types.ElicitRequestURLParams):
            print(params.message, file=self._stderr)
            print(params.url, file=self._stderr)
            if self._elicitation_options.assume_yes:
                return types.ElicitResult(action="accept")
            answer = self._ask("Open the URL and continue? [y/N]: ").strip().lower()
            if answer in YES_ANSWERS:
                return types.ElicitResult(action="accept")
            return types.ElicitResult(action="decline")

        print(params.message, file=self._stderr)
        schema = params.requestedSchema
        properties = schema.get("properties", {})
        content: dict[str, str | int | float | bool | list[str] | None] = {}

        for field_name, field_schema in properties.items():
            field_type = str(field_schema.get("type", "string"))
            title = str(field_schema.get("title") or field_name)
            description = field_schema.get("description")
            if description:
                print(f"{title}: {description}", file=self._stderr)

            if field_name == "approve" and field_type == "boolean":
                approved = self._elicitation_options.assume_yes
                if not approved:
                    answer = self._ask("Approve? [y/N]: ").strip().lower()
                    approved = answer in YES_ANSWERS
                if not approved:
                    return types.ElicitResult(action="decline")
                content[field_name] = True
                continue

            if field_name == "confirmation_token":
                value = self._elicitation_options.confirmation_token
                if value is None:
                    value = self._ask(f"{title}: ").strip()
                if not value:
                    return types.ElicitResult(action="cancel")
                content[field_name] = value
                continue

            prompted = self._ask(f"{title}: ").strip()
            if not prompted:
                content[field_name] = None
                continue
            if field_type == "boolean":
                content[field_name] = prompted.lower() in YES_ANSWERS
            elif field_type == "integer":
                content[field_name] = int(prompted)
            elif field_type == "number":
                content[field_name] = float(prompted)
            elif field_type == "array":
                content[field_name] = [item.strip() for item in prompted.split(",") if item.strip()]
            else:
                content[field_name] = prompted

        return types.ElicitResult(action="accept", content=content)

    async def _progress_callback(self, progress: float, total: float | None, message: str | None) -> None:
        total_text = f"/{total:0.2f}" if total is not None else ""
        label = f"[{progress:0.2f}{total_text}]"
        if message:
            print(f"{label} {message}", file=self._stderr)
        else:
            print(label, file=self._stderr)

    async def _preflight(self, client: httpx.AsyncClient) -> None:
        try:
            response = await client.options(self._url, headers={"Accept": "application/json"})
        except httpx.HTTPError as exc:
            raise MCPClientCliError(f"Could not reach MCP server at {self._url}: {exc}") from exc

        if response.status_code in {200, 204, 400, 405}:
            return
        if response.status_code == 404:
            raise MCPClientCliError(f"MCP endpoint not found: {self._url}")
        if response.status_code in {401, 403}:
            detail = _response_detail(response)
            if detail:
                raise MCPClientCliError(f"Authentication failed: {detail}")
            raise MCPClientCliError(
                "Authentication failed. Check the bearer token and the server OAuth configuration."
            )
        if response.status_code >= 500:
            detail = _response_detail(response)
            suffix = f" Server response: {detail}" if detail else ""
            raise MCPClientCliError(
                f"MCP server returned HTTP {response.status_code} before session initialization. "
                "Check the server logs and OAuth configuration."
                f"{suffix}"
            )
        raise MCPClientCliError(f"Unexpected HTTP {response.status_code} from MCP endpoint {self._url}")

    async def _safe_close(self) -> None:
        try:
            await self._stack.aclose()
        except BaseException:
            pass

    def _translate_transport_exception(self, action: str, exc: BaseException) -> MCPClientCliError:
        http_error = _find_exception(exc, httpx.HTTPError)
        if isinstance(http_error, httpx.HTTPStatusError):
            detail = _response_detail(http_error.response)
            suffix = f" Server response: {detail}" if detail else ""
            return MCPClientCliError(
                f"Failed to {action}: HTTP {http_error.response.status_code} from MCP server.{suffix}"
            )
        if isinstance(http_error, httpx.HTTPError):
            return MCPClientCliError(f"Failed to {action}: {http_error}")
        if isinstance(exc, asyncio.CancelledError):
            return MCPClientCliError(
                f"Failed to {action}. The MCP transport was cancelled, usually because the server returned an error."
            )
        text = str(exc).strip()
        if text:
            return MCPClientCliError(f"Failed to {action}: {text}")
        return MCPClientCliError(f"Failed to {action}.")

    def _render_command_output(self, result: dict[str, Any]) -> None:
        stdout_text = str(result.get("stdout", {}).get("text", ""))
        stderr_text = str(result.get("stderr", {}).get("text", ""))
        if stdout_text:
            self._write_output(self._stdout, stdout_text)
        if stderr_text:
            self._write_output(self._stderr, stderr_text, color=ANSI_RED)

    def _print_more_hint(self, result: dict[str, Any]) -> None:
        stdout_cursor = result.get("stdout", {}).get("next_cursor")
        stderr_cursor = result.get("stderr", {}).get("next_cursor")
        if stdout_cursor:
            print("More stdout output is available. Use '$more stdout'.", file=self._stderr)
        if stderr_cursor:
            print("More stderr output is available. Use '$more stderr'.", file=self._stderr)

    @staticmethod
    def _default_http_client_factory(headers: dict[str, str]) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            headers=headers,
            timeout=httpx.Timeout(30.0, connect=5.0, read=300.0),
        )

    @staticmethod
    def _write_output(stream: TextIO, text: str, *, color: str | None = None) -> None:
        if color:
            stream.write(color)
        stream.write(text)
        if not text.endswith("\n"):
            stream.write("\n")
        if color:
            stream.write(ANSI_RESET)
        stream.flush()

    def _print_menu(self) -> None:
        print("", file=self._stderr)
        print("1. list-targets", file=self._stderr)
        print("2. browse-files", file=self._stderr)
        print("3. read-file", file=self._stderr)
        print("4. terminal-session", file=self._stderr)
        print("5. quit", file=self._stderr)

    def _ask(self, prompt: str) -> str:
        print(prompt, end="", file=self._stderr)
        return self._input("")

    def _ask_with_default(self, prompt: str, default: str) -> str:
        value = self._ask(prompt).strip()
        return value or default

    def _ask_optional_int(self, prompt: str) -> int | None:
        raw = self._ask(prompt).strip()
        if not raw:
            return None
        try:
            return int(raw)
        except ValueError as exc:
            raise MCPClientCliError(f"Expected an integer value, got: {raw}") from exc

    def _require_text(self, prompt: str) -> str:
        value = self._ask(prompt).strip()
        if not value:
            raise MCPClientCliError("A value is required")
        return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Native MCP CLI client for sysadmin-mcp-kit")
    parser.add_argument("--url", default=DEFAULT_URL, help="Streamable HTTP MCP endpoint URL")
    parser.add_argument("--token", help="Explicit bearer token. When omitted, the client fetches a fresh token from Keycloak using client credentials.")
    parser.add_argument("--env-file", help="Optional .env file to load before resolving auth settings")
    parser.add_argument("--client-id", help=f"OAuth client id. Defaults to ${DEFAULT_CLIENT_ID_ENV}")
    parser.add_argument("--client-secret", help=f"OAuth client secret. Defaults to ${DEFAULT_CLIENT_SECRET_ENV}")
    parser.add_argument("--token-endpoint", help=f"OAuth token endpoint. Defaults to ${DEFAULT_TOKEN_ENDPOINT_ENV}")
    parser.add_argument("--issuer-url", help=f"OAuth issuer URL. Used to derive a Keycloak token endpoint. Defaults to ${DEFAULT_ISSUER_URL_ENV}")
    parser.add_argument("--scope", help=f"OAuth scope for client_credentials. Defaults to ${DEFAULT_SCOPE_ENV} or {DEFAULT_SCOPE}")

    subparsers = parser.add_subparsers(dest="command_name")
    subparsers.add_parser("interactive", help="Start interactive mode")
    subparsers.add_parser("list-targets", help="List configured SSH targets through MCP")

    browse_parser = subparsers.add_parser("browse-files", help="Browse allowlisted remote files through MCP")
    browse_parser.add_argument("target_id")
    browse_parser.add_argument("directory")
    browse_parser.add_argument("--glob", default="*", dest="glob_pattern")
    browse_parser.add_argument("--limit", type=int)
    browse_parser.add_argument("--cursor")

    read_parser = subparsers.add_parser("read-file", help="Read and redact a remote file through MCP")
    read_parser.add_argument("target_id")
    read_parser.add_argument("path")
    read_parser.add_argument("--page-lines", type=int)
    read_parser.add_argument("--cursor")

    command_parser = subparsers.add_parser("run-command", help="Run a remote command through MCP")
    command_parser.add_argument("target_id")
    command_parser.add_argument("command")
    command_parser.add_argument("--timeout-seconds", type=int)
    command_parser.add_argument("--working-dir")
    command_parser.add_argument("--yes", action="store_true", help="Auto-approve form elicitation prompts")
    command_parser.add_argument("--confirmation-token", help="Typed confirmation token for sensitive commands")

    output_parser = subparsers.add_parser("read-command-output", help="Read additional paged command output through MCP")
    output_parser.add_argument("execution_id")
    output_parser.add_argument("stream", choices=("stdout", "stderr"))
    output_parser.add_argument("cursor")
    output_parser.add_argument("--page-lines", type=int)

    return parser


def _emit_json(data: dict[str, Any], stdout: TextIO) -> None:
    json.dump(data, stdout, indent=2)
    stdout.write("\n")


def _tool_error_message(result: types.CallToolResult) -> str:
    messages: list[str] = []
    for item in result.content:
        if getattr(item, "type", None) == "text":
            text = getattr(item, "text", "")
            if text:
                messages.append(text)
    if messages:
        return " ".join(messages)
    if result.structuredContent is not None:
        return json.dumps(result.structuredContent)
    return "Tool call failed"


def _response_detail(response: httpx.Response) -> str | None:
    try:
        text = response.text.strip()
    except Exception:
        return None
    if not text:
        return None
    if len(text) > 240:
        return f"{text[:237]}..."
    return text


def _find_exception(exc: BaseException, expected_type: type[BaseException]) -> BaseException | None:
    if isinstance(exc, expected_type):
        return exc
    if isinstance(exc, BaseExceptionGroup):
        for child in exc.exceptions:
            found = _find_exception(child, expected_type)
            if found is not None:
                return found
    cause = getattr(exc, "__cause__", None)
    if isinstance(cause, BaseException):
        found = _find_exception(cause, expected_type)
        if found is not None:
            return found
    context = getattr(exc, "__context__", None)
    if isinstance(context, BaseException):
        found = _find_exception(context, expected_type)
        if found is not None:
            return found
    return None


def _resolve_dotenv_path(path: str | None) -> Path | None:
    candidate_name = path or DEFAULT_DOTENV_FILENAME
    candidate = Path(candidate_name).expanduser()
    if candidate.is_absolute():
        if candidate.exists():
            return candidate
        if path is not None:
            raise MCPClientCliError(f".env file not found: {candidate}")
        return None

    search_roots: list[Path] = [Path.cwd().resolve(), *Path.cwd().resolve().parents]
    package_root = Path(__file__).resolve().parents[2]
    if package_root not in search_roots:
        search_roots.append(package_root)

    for root in search_roots:
        resolved = root / candidate
        if resolved.exists():
            return resolved

    if path is not None:
        raise MCPClientCliError(f".env file not found: {candidate}")
    return None


def _parse_dotenv_value(raw_value: str) -> str:
    value = raw_value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def _load_dotenv(path: str | None) -> Path | None:
    dotenv_path = _resolve_dotenv_path(path)
    if dotenv_path is None:
        return None

    for line in dotenv_path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("export "):
            stripped = stripped[len("export ") :].lstrip()
        if "=" not in stripped:
            continue
        key, raw_value = stripped.split("=", 1)
        key = key.strip()
        if not key or key in os.environ:
            continue
        os.environ[key] = _parse_dotenv_value(raw_value)
    return dotenv_path


def _derive_token_endpoint(issuer_url: str) -> str:
    base = issuer_url.rstrip("/")
    if base.endswith("/protocol/openid-connect/token"):
        return base
    return f"{base}/protocol/openid-connect/token"


def _default_token_http_client_factory() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=5.0, read=30.0))


async def _fetch_access_token(
    *,
    token_endpoint: str,
    client_id: str,
    client_secret: str,
    scope: str | None,
    token_http_client_factory: TokenHttpClientFactory | None,
) -> str:
    factory = token_http_client_factory or _default_token_http_client_factory
    body = {
        "grant_type": "client_credentials",
        "client_id": client_id,
        "client_secret": client_secret,
    }
    if scope:
        body["scope"] = scope

    try:
        async with factory() as client:
            response = await client.post(
                token_endpoint,
                data=body,
                headers={"Accept": "application/json"},
            )
        response.raise_for_status()
        payload = response.json()
    except httpx.HTTPStatusError as exc:
        detail = _response_detail(exc.response)
        suffix = f" Response: {detail}" if detail else ""
        raise MCPClientCliError(
            f"OAuth token endpoint returned HTTP {exc.response.status_code}.{suffix}"
        ) from exc
    except (httpx.HTTPError, ValueError) as exc:
        raise MCPClientCliError(f"Could not obtain OAuth access token: {exc}") from exc

    access_token = payload.get("access_token")
    print(f"[DEBUG] keycloak token response: {payload}", file=sys.stderr)
    print(f"[DEBUG] keycloak access token: {access_token}", file=sys.stderr)
    if not isinstance(access_token, str) or not access_token.strip():
        raise MCPClientCliError("OAuth token endpoint did not return access_token")
    return access_token


async def _resolve_access_token(
    args: argparse.Namespace,
    token_http_client_factory: TokenHttpClientFactory | None,
) -> str:
    explicit_token = args.token
    if explicit_token:
        print(f"[DEBUG] explicit access token: {explicit_token}", file=sys.stderr)
        return explicit_token

    client_id = args.client_id or os.getenv(DEFAULT_CLIENT_ID_ENV)
    client_secret = args.client_secret or os.getenv(DEFAULT_CLIENT_SECRET_ENV)
    issuer_url = args.issuer_url or os.getenv(DEFAULT_ISSUER_URL_ENV)
    token_endpoint = args.token_endpoint or os.getenv(DEFAULT_TOKEN_ENDPOINT_ENV)
    if token_endpoint is None and issuer_url:
        token_endpoint = _derive_token_endpoint(issuer_url)
    scope = args.scope or os.getenv(DEFAULT_SCOPE_ENV) or DEFAULT_SCOPE

    print(f"[DEBUG] token request issuer_url: {issuer_url}", file=sys.stderr)
    print(f"[DEBUG] token request endpoint: {token_endpoint}", file=sys.stderr)
    print(f"[DEBUG] token request client_id: {client_id}", file=sys.stderr)
    print(f"[DEBUG] token request scope: {scope}", file=sys.stderr)

    if not client_id or not client_secret or not token_endpoint:
        raise MCPClientCliError(
            "Authentication token is required. Pass --token, or provide --client-id and --client-secret with --token-endpoint "
            f"or --issuer-url. Environment fallbacks: {DEFAULT_CLIENT_ID_ENV}, {DEFAULT_CLIENT_SECRET_ENV}, "
            f"{DEFAULT_TOKEN_ENDPOINT_ENV}, {DEFAULT_ISSUER_URL_ENV}."
        )

    return await _fetch_access_token(
        token_endpoint=token_endpoint,
        client_id=client_id,
        client_secret=client_secret,
        scope=scope,
        token_http_client_factory=token_http_client_factory,
    )


async def _run_async(
    args: argparse.Namespace,
    *,
    stdout: TextIO,
    stderr: TextIO,
    input_fn: Callable[[str], str] | None,
    http_client_factory: HttpClientFactory | None,
    token_http_client_factory: TokenHttpClientFactory | None,
) -> int:
    token = await _resolve_access_token(args, token_http_client_factory)

    async with MCPCLI(
        url=args.url,
        token=token,
        stdout=stdout,
        stderr=stderr,
        input_fn=input_fn,
        http_client_factory=http_client_factory,
    ) as cli:
        if args.command_name in {None, "interactive"}:
            return await cli.interactive_shell()
        if args.command_name == "list-targets":
            result = await cli.list_targets()
        elif args.command_name == "browse-files":
            result = await cli.browse_files(
                target_id=args.target_id,
                directory=args.directory,
                glob_pattern=args.glob_pattern,
                limit=args.limit,
                cursor=args.cursor,
            )
        elif args.command_name == "read-file":
            result = await cli.read_file(
                target_id=args.target_id,
                path=args.path,
                page_lines=args.page_lines,
                cursor=args.cursor,
            )
        elif args.command_name == "run-command":
            result = await cli.run_command(
                target_id=args.target_id,
                command=args.command,
                timeout_seconds=args.timeout_seconds,
                working_dir=args.working_dir,
                assume_yes=args.yes,
                confirmation_token=args.confirmation_token,
            )
        elif args.command_name == "read-command-output":
            result = await cli.read_command_output(
                execution_id=args.execution_id,
                stream=args.stream,
                cursor=args.cursor,
                page_lines=args.page_lines,
            )
        else:  # pragma: no cover - argparse guarantees valid subcommands
            raise MCPClientCliError(f"Unknown command: {args.command_name}")

    _emit_json(result, stdout)
    return 0


def main(
    argv: Sequence[str] | None = None,
    *,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
    input_fn: Callable[[str], str] | None = None,
    http_client_factory: HttpClientFactory | None = None,
    token_http_client_factory: TokenHttpClientFactory | None = None,
) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    stdout = stdout or sys.stdout
    stderr = stderr or sys.stderr

    try:
        print(f"[DEBUG] resolved env file argument: {args.env_file}", file=stderr)
        print(f"[DEBUG] loaded dotenv path: {_load_dotenv(args.env_file)}", file=stderr)
        return asyncio.run(
            _run_async(
                args,
                stdout=stdout,
                stderr=stderr,
                input_fn=input_fn,
                http_client_factory=http_client_factory,
                token_http_client_factory=token_http_client_factory,
            )
        )
    except (MCPClientCliError, McpError, httpx.HTTPError) as exc:
        print(f"Error: {exc}", file=stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())