from __future__ import annotations

import argparse
import getpass
import json
import sys
import threading
from collections.abc import Callable, Sequence
from typing import Any, TextIO

from .config import AppSettings, load_settings
from .pagination import CursorCodec, CursorError, Paginator
from .policy import CommandPolicy
from .redaction import RedactionResult, Redactor
from .session import SessionStoreError, TerminalSessionService, parse_builtin_command
from .ssh import PasswordPromptRequest, SSHService, SSHServiceError

YES_ANSWERS = {"y", "yes"}
QUIT_ANSWERS = {"5", "q", "quit", "exit"}
CLI_SESSION_OWNER = "interactive-cli"
ANSI_RED = "\x1b[31m"
ANSI_RESET = "\x1b[0m"


class CliError(RuntimeError):
    """Raised when CLI arguments or approvals are invalid."""


class DirectCLI:
    def __init__(
        self,
        settings: AppSettings,
        *,
        ssh_service: SSHService | None = None,
        session_service: TerminalSessionService | None = None,
        stdout: TextIO | None = None,
        stderr: TextIO | None = None,
        input_fn: Callable[[str], str] | None = None,
    ):
        self._settings = settings
        self._ssh_service = ssh_service or SSHService(settings)
        self._session_service = session_service or TerminalSessionService(self._ssh_service, settings.server.cache_ttl_seconds)
        self._redactor = Redactor(settings.redaction)
        self._policy = CommandPolicy(settings.command_policy)
        self._stdout = stdout or sys.stdout
        self._stderr = stderr or sys.stderr
        self._input = input_fn or input

    def list_targets(self) -> dict[str, Any]:
        return {
            "targets": [
                {
                    "target_id": target.target_id,
                    "ssh_alias": target.ssh_alias,
                    "allowed_paths": target.allowed_paths,
                    "default_timeout_seconds": target.default_timeout_seconds,
                }
                for target in self._ssh_service.list_targets()
            ]
        }

    def browse_files(
        self,
        *,
        target_id: str,
        directory: str,
        glob_pattern: str,
        limit: int | None,
        offset: int,
    ) -> dict[str, Any]:
        browse = self._ssh_service.browse_files(target_id, directory, glob_pattern)
        page_limit = self._list_limit(limit)
        page = Paginator.paginate_items(browse.entries, offset, page_limit)
        next_cursor = None
        if page.next_index is not None:
            next_cursor = CursorCodec.encode(
                {
                    "kind": "browse",
                    "target_id": target_id,
                    "directory": directory,
                    "glob": glob_pattern,
                    "offset": page.next_index,
                }
            )
        return {
            "summary": {
                "target_id": target_id,
                "directory": directory,
                "glob": glob_pattern,
                "total_entries": page.total_items,
                "returned_entries": len(page.items),
                "limit": page_limit,
                "offset": offset,
            },
            "entries": [
                {
                    "path": item.path,
                    "name": item.name,
                    "is_dir": item.is_dir,
                    "size": item.size,
                    "modified_time": item.modified_time,
                }
                for item in page.items
            ],
            "next_cursor": next_cursor,
        }

    def read_file(
        self,
        *,
        target_id: str,
        path: str,
        page_lines: int | None,
        start_line: int,
    ) -> dict[str, Any]:
        file_result = self._ssh_service.read_file_bytes(target_id, path, self._settings.server.max_file_bytes)
        redaction = self._redactor.redact_bytes(
            file_result.data,
            path=file_result.path,
            source_truncated=file_result.source_truncated,
        )
        content = None
        next_cursor = None
        page_size = self._page_size(page_lines)
        if redaction.text is not None:
            page = Paginator.paginate_lines(
                redaction.text,
                start_line,
                page_size,
                self._settings.server.hard_page_char_limit,
            )
            if page.next_index is not None:
                next_cursor = CursorCodec.encode(
                    {
                        "kind": "file",
                        "target_id": target_id,
                        "path": file_result.path,
                        "offset": page.next_index,
                    }
                )
            content = {
                "text": page.text,
                "start_line": page.start_line,
                "end_line": page.end_line,
                "returned_lines": page.returned_lines,
                "total_lines": page.total_lines,
                "truncated_by_page": page.truncated_by_page,
            }
        return {
            "summary": {
                "target_id": target_id,
                "path": file_result.path,
                "size_bytes": file_result.size_bytes,
                "binary": redaction.binary,
                "parser": redaction.parser,
                "redaction_replacements": redaction.replacements,
                "source_truncated": redaction.source_truncated,
                "start_line": start_line,
                "page_lines": page_size,
            },
            "content": content,
            "next_cursor": next_cursor,
            "redaction": self._redaction_info(redaction),
        }

    def run_command(
        self,
        *,
        target_id: str,
        command: str,
        timeout_seconds: int | None,
        assume_yes: bool,
        confirmation_token: str | None,
    ) -> dict[str, Any]:
        target = self._ssh_service.get_target(target_id)
        timeout = timeout_seconds or target.default_timeout_seconds
        decision = self._policy.evaluate(command)
        if decision.blocked:
            raise CliError(f"Command is blocked by policy: {', '.join(decision.reasons)}")

        self._confirm_command(
            target_id=target_id,
            command=command,
            timeout_seconds=timeout,
            sensitive=decision.sensitive,
            assume_yes=assume_yes,
            confirmation_token=confirmation_token,
        )

        cancel_event = threading.Event()
        result = self._ssh_service.run_command(
            target_id,
            command,
            timeout,
            self._report_progress,
            cancel_event,
            password_prompt_callback=self._prompt_password,
        )
        return self._build_command_payload(
            target_id=target_id,
            result=result,
            sensitive=decision.sensitive,
            policy_reasons=decision.reasons,
        )

    def create_terminal_session(self, *, target_id: str, working_directory: str | None = None) -> dict[str, Any]:
        session = self._session_service.create_session(CLI_SESSION_OWNER, target_id, working_directory)
        return {
            "session_id": session.session_id,
            "target_id": session.target_id,
            "current_directory": session.current_directory,
        }

    def run_session_command(
        self,
        *,
        session_id: str,
        command: str,
        timeout_seconds: int | None,
        assume_yes: bool,
        confirmation_token: str | None,
    ) -> dict[str, Any]:
        session = self._session_service.get_session(CLI_SESSION_OWNER, session_id)
        builtin = parse_builtin_command(command)
        decision = self._policy.evaluate(command) if builtin is None else None
        if decision is not None and decision.blocked:
            raise CliError(f"Command is blocked by policy: {', '.join(decision.reasons)}")

        target = self._ssh_service.get_target(session.target_id)
        timeout = timeout_seconds or target.default_timeout_seconds
        if builtin is None:
            self._confirm_command(
                target_id=session.target_id,
                command=command,
                timeout_seconds=timeout,
                sensitive=decision.sensitive if decision is not None else False,
                assume_yes=assume_yes,
                confirmation_token=confirmation_token,
            )

        cancel_event = threading.Event()
        session_result = self._session_service.execute_command(
            CLI_SESSION_OWNER,
            session_id,
            command,
            timeout_seconds,
            self._report_progress,
            cancel_event,
            password_prompt_callback=self._prompt_password,
        )
        payload = self._build_command_payload(
            target_id=session_result.session.target_id,
            result=session_result.result,
            sensitive=decision.sensitive if decision is not None else False,
            policy_reasons=decision.reasons if decision is not None else [],
        )
        payload.update(
            {
                "session_id": session_id,
                "current_directory": session_result.session.current_directory,
                "builtin": session_result.builtin,
            }
        )
        return payload

    def close_terminal_session(self, *, session_id: str) -> dict[str, Any]:
        self._session_service.close_session(CLI_SESSION_OWNER, session_id)
        return {"session_id": session_id, "closed": True}

    def _build_command_payload(
        self,
        *,
        target_id: str,
        result,
        sensitive: bool,
        policy_reasons: list[str],
    ) -> dict[str, Any]:
        stdout_text = result.stdout.decode("utf-8", errors="replace")
        stderr_text = result.stderr.decode("utf-8", errors="replace")
        stdout_redaction = self._redactor.redact_text(stdout_text)
        stderr_redaction = self._redactor.redact_text(stderr_text)
        return {
            "summary": {
                "target_id": target_id,
                "exit_code": result.exit_code,
                "duration_seconds": result.duration_seconds,
                "timed_out": result.timed_out,
                "cancelled": result.cancelled,
                "sensitive": sensitive,
                "policy_reasons": policy_reasons,
                "stdout_bytes": len(result.stdout),
                "stderr_bytes": len(result.stderr),
                "stdout_lines": len((stdout_redaction.text or "").splitlines()),
                "stderr_lines": len((stderr_redaction.text or "").splitlines()),
            },
            "stdout": {
                "text": stdout_redaction.text or "",
                "returned_lines": len((stdout_redaction.text or "").splitlines()),
            },
            "stderr": {
                "text": stderr_redaction.text or "",
                "returned_lines": len((stderr_redaction.text or "").splitlines()),
            },
            "redaction": {
                "stdout": self._redaction_info(stdout_redaction),
                "stderr": self._redaction_info(stderr_redaction),
            },
        }

    def interactive_shell(self) -> int:
        print("Interactive sysadmin-mcp-cli", file=self._stderr)
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
                    _emit_json(self.list_targets(), self._stdout)
                elif choice in {"2", "browse", "browse-files"}:
                    self._interactive_browse_files()
                elif choice in {"3", "read", "read-file"}:
                    self._interactive_read_file()
                elif choice in {"4", "execute", "exec", "run", "run-command", "terminal", "terminal-session"}:
                    self._interactive_terminal_session()
                else:
                    print("Unknown choice. Select 1-5 or a command name.", file=self._stderr)
            except (CliError, CursorError, SSHServiceError, SessionStoreError) as exc:
                print(f"Error: {exc}", file=self._stderr)

    def _interactive_browse_files(self) -> None:
        target_id = self._require_text("Target id: ")
        directory = self._require_text("Directory: ")
        glob_pattern = self._ask_with_default("Glob [*]: ", "*")
        limit = self._ask_optional_int("Limit (blank for configured default): ")
        offset = 0

        while True:
            result = self.browse_files(
                target_id=target_id,
                directory=directory,
                glob_pattern=glob_pattern,
                limit=limit,
                offset=offset,
            )
            _emit_json(result, self._stdout)
            if not result["next_cursor"]:
                break
            if self._ask("Show next page? [y/N]: ").strip().lower() not in YES_ANSWERS:
                break
            offset = _decode_browse_cursor(
                result["next_cursor"],
                target_id=target_id,
                directory=directory,
                glob_pattern=glob_pattern,
            )

    def _interactive_read_file(self) -> None:
        target_id = self._require_text("Target id: ")
        path = self._require_text("Remote file path: ")
        page_lines = self._ask_optional_int("Page lines (blank for configured default): ")
        start_line = 0

        while True:
            result = self.read_file(
                target_id=target_id,
                path=path,
                page_lines=page_lines,
                start_line=start_line,
            )
            _emit_json(result, self._stdout)
            if not result["next_cursor"]:
                break
            if self._ask("Show next page? [y/N]: ").strip().lower() not in YES_ANSWERS:
                break
            start_line = _decode_file_cursor(
                result["next_cursor"],
                target_id=target_id,
                path=path,
            )

    def _interactive_terminal_session(self) -> None:
        target_id = self._require_text("Target id: ")
        initial_directory = self._ask("Initial directory (blank for target default): ").strip() or None
        session = self.create_terminal_session(target_id=target_id, working_directory=initial_directory)
        last_result: dict[str, Any] | None = None
        print(
            f"Terminal session started for {target_id} in {session['current_directory']}. Type 'exit' to return to the menu.",
            file=self._stderr,
        )
        print("Use '$info' to print the last full JSON payload.", file=self._stderr)
        try:
            while True:
                prompt = f"[{target_id} {session['current_directory']}]$ "
                command = self._ask(prompt).strip()
                if not command:
                    continue
                if command.lower() in {"exit", "quit", "back"}:
                    break
                if command == "$info":
                    if last_result is None:
                        print("No command result is available yet.", file=self._stderr)
                    else:
                        _emit_json(last_result, self._stdout)
                    continue
                try:
                    timeout_seconds = self._ask_optional_int("Timeout seconds (blank for target default): ")
                    result = self.run_session_command(
                        session_id=session["session_id"],
                        command=command,
                        timeout_seconds=timeout_seconds,
                        assume_yes=False,
                        confirmation_token=None,
                    )
                except SessionStoreError:
                    print("Error: terminal session is no longer available.", file=self._stderr)
                    break
                except (CliError, SSHServiceError) as exc:
                    print(f"Error: {exc}", file=self._stderr)
                    continue
                except Exception as exc:
                    print(f"Error: {exc}", file=self._stderr)
                    continue
                last_result = result
                self._render_command_output(result)
                session["current_directory"] = result["current_directory"]
        finally:
            try:
                self.close_terminal_session(session_id=session["session_id"])
            except SessionStoreError:
                pass
            print("Terminal session closed.", file=self._stderr)

    def _confirm_command(
        self,
        *,
        target_id: str,
        command: str,
        timeout_seconds: int,
        sensitive: bool,
        assume_yes: bool,
        confirmation_token: str | None,
    ) -> None:
        if not assume_yes:
            print("Approve remote command execution.", file=self._stderr)
            print(f"Target: {target_id}", file=self._stderr)
            print(f"Timeout: {timeout_seconds}s", file=self._stderr)
            print("Command:", file=self._stderr)
            print(command, file=self._stderr)
            answer = self._ask("Approve? [y/N]: ").strip().lower()
            if answer not in YES_ANSWERS:
                raise CliError("Remote command execution was not approved")

        if sensitive:
            expected_token = self._policy.confirmation_token(command)
            provided_token = confirmation_token
            if provided_token is None:
                provided_token = self._ask(f"Sensitive command. Type {expected_token} to continue: ").strip()
            if provided_token.strip() != expected_token:
                raise CliError("Sensitive remote command execution was not approved")

    def _render_command_output(self, result: dict[str, Any]) -> None:
        stdout_text = str(result.get("stdout", {}).get("text", ""))
        stderr_text = str(result.get("stderr", {}).get("text", ""))
        if stdout_text:
            self._write_output(self._stdout, stdout_text)
        if stderr_text:
            self._write_output(self._stderr, stderr_text, color=ANSI_RED)

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

    def _report_progress(self, progress: float, message: str) -> None:
        print(f"[{progress:0.2f}] {message}", file=self._stderr)

    def _prompt_password(self, request: PasswordPromptRequest) -> str | None:
        print(request.prompt, file=self._stderr)
        if self._input is input:
            return getpass.getpass("Password: ", stream=self._stderr)
        print("Password: ", end="", file=self._stderr)
        return self._input("")

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
            raise CliError(f"Expected an integer value, got: {raw}") from exc

    def _require_text(self, prompt: str) -> str:
        value = self._ask(prompt).strip()
        if not value:
            raise CliError("A value is required")
        return value

    def _page_size(self, requested: int | None) -> int:
        if requested is None:
            return self._settings.server.default_page_lines
        return max(1, min(requested, self._settings.server.max_page_lines))

    def _list_limit(self, requested: int | None) -> int:
        if requested is None:
            return self._settings.server.list_limit
        return max(1, min(requested, max(self._settings.server.list_limit, 200)))

    @staticmethod
    def _redaction_info(redaction: RedactionResult) -> dict[str, Any]:
        return {
            "parser": redaction.parser,
            "replacements": redaction.replacements,
            "binary": redaction.binary,
            "source_truncated": redaction.source_truncated,
        }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Direct CLI for sysadmin-mcp-kit utility functions")
    parser.add_argument("--config", help="Path to server TOML config")

    subparsers = parser.add_subparsers(dest="command_name")
    subparsers.add_parser("interactive", help="Start interactive mode")
    subparsers.add_parser("list-targets", help="List configured SSH targets")

    browse_parser = subparsers.add_parser("browse-files", help="Browse allowlisted remote files")
    browse_parser.add_argument("target_id")
    browse_parser.add_argument("directory")
    browse_parser.add_argument("--glob", default="*", dest="glob_pattern")
    browse_parser.add_argument("--limit", type=int)
    browse_parser.add_argument("--offset", type=int, default=0)
    browse_parser.add_argument("--cursor")

    read_parser = subparsers.add_parser("read-file", help="Read and redact a remote text file")
    read_parser.add_argument("target_id")
    read_parser.add_argument("path")
    read_parser.add_argument("--page-lines", type=int)
    read_parser.add_argument("--start-line", type=int, default=0)
    read_parser.add_argument("--cursor")

    command_parser = subparsers.add_parser("run-command", help="Run a remote command directly over SSH")
    command_parser.add_argument("target_id")
    command_parser.add_argument("command")
    command_parser.add_argument("--timeout-seconds", type=int)
    command_parser.add_argument("--yes", action="store_true", help="Skip the initial approval prompt")
    command_parser.add_argument("--confirmation-token", help="Typed confirmation token for sensitive commands")

    return parser


def _decode_browse_cursor(
    cursor: str,
    *,
    target_id: str,
    directory: str,
    glob_pattern: str,
) -> int:
    payload = CursorCodec.decode(cursor)
    if payload.get("kind") != "browse":
        raise CliError("Cursor does not belong to browse-files")
    if payload.get("target_id") != target_id or payload.get("directory") != directory or payload.get("glob") != glob_pattern:
        raise CliError("Cursor does not match the requested browse operation")
    return int(payload.get("offset", 0))


def _decode_file_cursor(
    cursor: str,
    *,
    target_id: str,
    path: str,
) -> int:
    payload = CursorCodec.decode(cursor)
    if payload.get("kind") != "file":
        raise CliError("Cursor does not belong to read-file")
    if payload.get("target_id") != target_id or payload.get("path") != path:
        raise CliError("Cursor does not match the requested file")
    return int(payload.get("offset", 0))


def _decode_browse_position(args: argparse.Namespace) -> int:
    if args.cursor and args.offset:
        raise CliError("Use either --cursor or --offset, not both")
    if not args.cursor:
        return max(0, args.offset)
    return _decode_browse_cursor(
        args.cursor,
        target_id=args.target_id,
        directory=args.directory,
        glob_pattern=args.glob_pattern,
    )


def _decode_file_position(args: argparse.Namespace) -> int:
    if args.cursor and args.start_line:
        raise CliError("Use either --cursor or --start-line, not both")
    if not args.cursor:
        return max(0, args.start_line)
    return _decode_file_cursor(
        args.cursor,
        target_id=args.target_id,
        path=args.path,
    )


def _emit_json(data: dict[str, Any], stdout: TextIO) -> None:
    json.dump(data, stdout, indent=2)
    stdout.write("\n")


def main(
    argv: Sequence[str] | None = None,
    *,
    settings: AppSettings | None = None,
    ssh_service: SSHService | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
    input_fn: Callable[[str], str] | None = None,
) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    stdout = stdout or sys.stdout
    stderr = stderr or sys.stderr

    try:
        settings = settings or load_settings(args.config)
        cli = DirectCLI(settings, ssh_service=ssh_service, stdout=stdout, stderr=stderr, input_fn=input_fn)

        if args.command_name in {None, "interactive"}:
            return cli.interactive_shell()
        if args.command_name == "list-targets":
            result = cli.list_targets()
        elif args.command_name == "browse-files":
            result = cli.browse_files(
                target_id=args.target_id,
                directory=args.directory,
                glob_pattern=args.glob_pattern,
                limit=args.limit,
                offset=_decode_browse_position(args),
            )
        elif args.command_name == "read-file":
            result = cli.read_file(
                target_id=args.target_id,
                path=args.path,
                page_lines=args.page_lines,
                start_line=_decode_file_position(args),
            )
        elif args.command_name == "run-command":
            result = cli.run_command(
                target_id=args.target_id,
                command=args.command,
                timeout_seconds=args.timeout_seconds,
                assume_yes=args.yes,
                confirmation_token=args.confirmation_token,
            )
        else:  # pragma: no cover - argparse guarantees valid subcommands
            raise CliError(f"Unknown command: {args.command_name}")
    except (CliError, CursorError, SSHServiceError, SessionStoreError) as exc:
        print(f"Error: {exc}", file=stderr)
        return 1

    _emit_json(result, stdout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
