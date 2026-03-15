from __future__ import annotations

import fnmatch
import posixpath
import re
import shlex
import socket
import stat
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import paramiko
from paramiko.proxy import ProxyCommand

from .config import AppSettings, TargetSettings


class SSHServiceError(RuntimeError):
    """Raised when SSH or remote filesystem operations fail."""


@dataclass(frozen=True)
class RemoteFileEntry:
    path: str
    name: str
    is_dir: bool
    size: int
    modified_time: int | None


@dataclass(frozen=True)
class BrowseResult:
    entries: list[RemoteFileEntry]
    total_entries: int


@dataclass(frozen=True)
class FileBytesResult:
    path: str
    size_bytes: int
    data: bytes
    source_truncated: bool


@dataclass(frozen=True)
class PasswordPromptRequest:
    prompt: str
    attempt: int
    timeout_seconds: float | None


PasswordPromptHandler = Callable[[PasswordPromptRequest], str | None]
_PASSWORD_PROMPT_RE = re.compile(r"(?im)(?:^|[\r\n])(?P<prompt>[^\r\n]*(?:password|passphrase)[^\r\n]*:\s*)$")
_MAX_PASSWORD_PROMPTS = 3
_MCP_SUDO_PROMPT = "[sysadmin-mcp] password: "
_SUDO_SHORT_OPTIONS_WITH_VALUE = {"C", "D", "R", "T", "U", "g", "h", "p", "r", "t", "u"}
_SUDO_LONG_OPTIONS_WITH_VALUE = {
    "--chdir",
    "--chroot",
    "--close-from",
    "--command-timeout",
    "--group",
    "--host",
    "--other-user",
    "--prompt",
    "--role",
    "--type",
    "--user",
}


@dataclass(frozen=True)
class CommandRunResult:
    exit_code: int | None
    stdout: bytes
    stderr: bytes
    duration_seconds: float
    timed_out: bool
    cancelled: bool


@dataclass(frozen=True)
class PersistentShellCommandResult:
    result: CommandRunResult
    current_directory: str


def _inspect_sudo_command(command: str) -> tuple[str | None, bool, bool, bool, bool]:
    try:
        tokens = shlex.split(command, posix=True)
    except ValueError:
        return None, False, False, False, False

    if not tokens or posixpath.basename(tokens[0]) != "sudo":
        return None, False, False, False, False

    has_stdin = False
    has_prompt = False
    has_askpass = False
    has_noninteractive = False
    index = 1
    while index < len(tokens):
        token = tokens[index]
        if token == "--":
            break
        if not token.startswith("-") or token == "-":
            break

        if token.startswith("--"):
            option_name, separator, _value = token.partition("=")
            if option_name == "--stdin":
                has_stdin = True
            elif option_name == "--prompt":
                has_prompt = True
            elif option_name == "--askpass":
                has_askpass = True
            elif option_name == "--non-interactive":
                has_noninteractive = True
            if option_name in _SUDO_LONG_OPTIONS_WITH_VALUE and not separator:
                index += 1
            index += 1
            continue

        skip_next_value = False
        short_options = token[1:]
        for position, option in enumerate(short_options):
            if option == "S":
                has_stdin = True
            elif option == "p":
                has_prompt = True
            elif option == "A":
                has_askpass = True
            elif option == "n":
                has_noninteractive = True
            if option in _SUDO_SHORT_OPTIONS_WITH_VALUE:
                if position == len(short_options) - 1:
                    skip_next_value = True
                break
        index += 2 if skip_next_value else 1

    return tokens[0], has_stdin, has_prompt, has_askpass, has_noninteractive


def _insert_sudo_options(command: str, sudo_token: str, insertions: list[str]) -> str:
    if not insertions:
        return command
    rewritten, substitutions = re.subn(
        rf"^(\s*{re.escape(sudo_token)}\b)",
        rf"\1 {' '.join(insertions)}",
        command,
        count=1,
    )
    return rewritten if substitutions else command


def _prepare_command_for_password_prompts(command: str) -> str:
    sudo_token, has_stdin, has_prompt, has_askpass, has_noninteractive = _inspect_sudo_command(command)
    if sudo_token is None or has_askpass or has_noninteractive:
        return command

    insertions: list[str] = []
    if not has_stdin:
        insertions.append("-S")
    if not has_prompt:
        insertions.extend(["-p", shlex.quote(_MCP_SUDO_PROMPT)])
    return _insert_sudo_options(command, sudo_token, insertions)


def _prepare_command_for_persistent_password_prompts(
    command: str,
    *,
    timeout_seconds: int,
    progress_callback: Callable[[float, str], None],
    password_prompt_callback: PasswordPromptHandler | None,
) -> str:
    sudo_token, has_stdin, has_prompt, has_askpass, has_noninteractive = _inspect_sudo_command(command)
    if sudo_token is None or has_askpass or has_noninteractive or has_stdin:
        return command
    if password_prompt_callback is None:
        return command

    progress_callback(0.04, f"Remote command is waiting for password input: {_MCP_SUDO_PROMPT.strip()}")
    password = password_prompt_callback(
        PasswordPromptRequest(
            prompt=_MCP_SUDO_PROMPT.strip(),
            attempt=1,
            timeout_seconds=float(timeout_seconds),
        )
    )
    if password is None:
        raise SSHServiceError("Remote password prompt was declined")

    secret = password.rstrip("\r\n")
    if not secret:
        raise SSHServiceError("Remote password prompt was not completed")

    insertions = ["-A"]
    if not has_prompt:
        insertions.extend(["-p", shlex.quote(_MCP_SUDO_PROMPT)])
    wrapped_command = _insert_sudo_options(command, sudo_token, insertions)
    quoted_secret = shlex.quote(secret)
    lines = [
        '__sysadmin_mcp_askpass=$(mktemp)',
        'cat > "$__sysadmin_mcp_askpass" <<\'__SYSADMIN_MCP_ASKPASS__\'',
        '#!/bin/sh',
        f"printf '%s\\n' {quoted_secret}",
        '__SYSADMIN_MCP_ASKPASS__',
        'chmod 700 "$__sysadmin_mcp_askpass"',
        'export SUDO_ASKPASS="$__sysadmin_mcp_askpass"',
        wrapped_command,
        '__sysadmin_mcp_inner_status=$?',
        'rm -f "$__sysadmin_mcp_askpass"',
        'unset SUDO_ASKPASS',
        '(exit "$__sysadmin_mcp_inner_status")',
    ]
    return "\n".join(lines)

class PersistentShellSession:
    def __init__(
        self,
        *,
        target_id: str,
        client: paramiko.SSHClient,
        sock: socket.socket | ProxyCommand | None,
        channel: paramiko.Channel,
        current_directory: str,
        progress_interval_seconds: float,
    ) -> None:
        self.target_id = target_id
        self._client = client
        self._sock = sock
        self._channel = channel
        self._current_directory = current_directory
        self._progress_interval_seconds = progress_interval_seconds
        self._stdout_buffer = b""
        self._stderr_buffer = b""
        self._lock = threading.Lock()
        self._closed = False

    @property
    def current_directory(self) -> str:
        return self._current_directory

    @property
    def is_closed(self) -> bool:
        return self._closed or self._channel.closed or self._channel.exit_status_ready()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._channel.close()
        except Exception:
            pass
        try:
            self._client.close()
        except Exception:
            pass
        if self._sock is not None:
            try:
                self._sock.close()
            except Exception:
                pass

    def run_command(
        self,
        command: str,
        timeout_seconds: int,
        progress_callback: Callable[[float, str], None],
        cancel_event: threading.Event,
        working_directory: str | None = None,
        password_prompt_callback: PasswordPromptHandler | None = None,
    ) -> PersistentShellCommandResult:
        with self._lock:
            if self.is_closed:
                raise SSHServiceError("Persistent shell session is not available")

            token = uuid.uuid4().hex.upper()
            prepared_command = _prepare_command_for_persistent_password_prompts(
                command,
                timeout_seconds=timeout_seconds,
                progress_callback=progress_callback,
                password_prompt_callback=password_prompt_callback,
            )
            script = self._build_script(prepared_command, token, working_directory)
            started_at = time.monotonic()
            prompt_attempts = 0
            last_prompt_state: tuple[str, str, int, int] | None = None
            progress_callback(0.05, "Running command in persistent shell")
            try:
                self._channel.sendall(script.encode("utf-8"))
            except Exception as exc:
                self.close()
                raise SSHServiceError("Failed to send command to persistent shell") from exc
            progress_callback(0.15, "Command started")

            heartbeat = 0
            last_progress_time = started_at
            while True:
                if cancel_event.is_set():
                    stdout, stderr = self._collect_partial_output_and_close()
                    progress_callback(1.0, "Remote command finished")
                    return PersistentShellCommandResult(
                        result=CommandRunResult(
                            exit_code=None,
                            stdout=stdout,
                            stderr=stderr,
                            duration_seconds=round(time.monotonic() - started_at, 3),
                            timed_out=False,
                            cancelled=True,
                        ),
                        current_directory=self._current_directory,
                    )

                drained = self._drain_channel()
                completed = self._extract_completed_command(token)
                if completed is not None:
                    stdout, stderr, exit_code, current_directory = completed
                    self._current_directory = current_directory
                    progress_callback(1.0, "Remote command finished")
                    return PersistentShellCommandResult(
                        result=CommandRunResult(
                            exit_code=exit_code,
                            stdout=stdout,
                            stderr=stderr,
                            duration_seconds=round(time.monotonic() - started_at, 3),
                            timed_out=False,
                            cancelled=False,
                        ),
                        current_directory=current_directory,
                    )

                try:
                    prompt_attempts, last_prompt_state = _maybe_handle_password_prompt(
                        channel=self._channel,
                        stdout_tail=self._stdout_buffer[-1024:],
                        stderr_tail=self._stderr_buffer[-1024:],
                        stdout_length=len(self._stdout_buffer),
                        stderr_length=len(self._stderr_buffer),
                        timeout_seconds=timeout_seconds,
                        started_at=started_at,
                        progress_callback=progress_callback,
                        password_prompt_callback=password_prompt_callback,
                        prompt_attempts=prompt_attempts,
                        last_prompt_state=last_prompt_state,
                    )
                except SSHServiceError:
                    self.close()
                    raise

                if self._channel.closed or self._channel.exit_status_ready():
                    self._collect_partial_output_and_close()
                    raise SSHServiceError("Persistent shell session terminated unexpectedly")

                elapsed = time.monotonic() - started_at
                if elapsed > timeout_seconds:
                    stdout, stderr = self._collect_partial_output_and_close()
                    progress_callback(1.0, "Remote command finished")
                    return PersistentShellCommandResult(
                        result=CommandRunResult(
                            exit_code=None,
                            stdout=stdout,
                            stderr=stderr,
                            duration_seconds=round(elapsed, 3),
                            timed_out=True,
                            cancelled=False,
                        ),
                        current_directory=self._current_directory,
                    )

                now = time.monotonic()
                if drained or now - last_progress_time >= self._progress_interval_seconds:
                    heartbeat += 1
                    progress = min(0.95, 0.15 + (heartbeat * 0.05))
                    bytes_seen = len(self._stdout_buffer) + len(self._stderr_buffer)
                    progress_callback(progress, f"Remote command still running; collected {bytes_seen} bytes")
                    last_progress_time = now

                time.sleep(0.05)

    def _build_script(self, command: str, token: str, working_directory: str | None) -> str:
        command_literal = shlex.quote(_prepare_command_for_password_prompts(command))
        lines: list[str] = []
        if working_directory is not None:
            lines.extend(
                [
                    f"cd -- {shlex.quote(working_directory)}",
                    "__sysadmin_mcp_cd_status=$?",
                    'if [ "$__sysadmin_mcp_cd_status" -eq 0 ]; then',
                    f"  eval -- {command_literal}",
                    "  __sysadmin_mcp_status=$?",
                    "else",
                    "  __sysadmin_mcp_status=$__sysadmin_mcp_cd_status",
                    "fi",
                ]
            )
        else:
            lines.extend([f"eval -- {command_literal}", "__sysadmin_mcp_status=$?"])
        lines.extend(
            [
                f"printf '\\n__SYSADMIN_MCP_STATUS_{token}__:%s\\n' \"$__sysadmin_mcp_status\"",
                f"printf '__SYSADMIN_MCP_PWD_{token}__:%s\\n' \"$PWD\"",
                f"printf '__SYSADMIN_MCP_STDOUT_END_{token}__\\n'",
                f"printf '\\n__SYSADMIN_MCP_STDERR_END_{token}__\\n' >&2",
            ]
        )
        return "\n".join(lines) + "\n"

    def _drain_channel(self) -> bool:
        drained = False
        while self._channel.recv_ready():
            self._stdout_buffer += self._channel.recv(32768)
            drained = True
        while self._channel.recv_stderr_ready():
            self._stderr_buffer += self._channel.recv_stderr(32768)
            drained = True
        return drained

    def _extract_completed_command(self, token: str) -> tuple[bytes, bytes, int, str] | None:
        status_prefix = f"\n__SYSADMIN_MCP_STATUS_{token}__:".encode("utf-8")
        pwd_prefix = f"__SYSADMIN_MCP_PWD_{token}__:".encode("utf-8")
        stdout_end = f"__SYSADMIN_MCP_STDOUT_END_{token}__\n".encode("utf-8")
        stderr_end = f"\n__SYSADMIN_MCP_STDERR_END_{token}__\n".encode("utf-8")

        status_start = self._stdout_buffer.find(status_prefix)
        if status_start == -1:
            return None
        status_end = self._stdout_buffer.find(b"\n", status_start + len(status_prefix))
        if status_end == -1:
            return None

        pwd_start = self._stdout_buffer.find(pwd_prefix, status_end + 1)
        if pwd_start == -1:
            return None
        pwd_end = self._stdout_buffer.find(b"\n", pwd_start + len(pwd_prefix))
        if pwd_end == -1:
            return None

        stdout_end_start = self._stdout_buffer.find(stdout_end, pwd_end + 1)
        if stdout_end_start == -1:
            return None

        stderr_end_start = self._stderr_buffer.find(stderr_end)
        if stderr_end_start == -1:
            return None

        stdout = self._stdout_buffer[:status_start]
        stderr = self._stderr_buffer[:stderr_end_start]
        status_text = self._stdout_buffer[status_start + len(status_prefix) : status_end].decode("utf-8", errors="replace")
        pwd = self._stdout_buffer[pwd_start + len(pwd_prefix) : pwd_end].decode("utf-8", errors="replace")

        self._stdout_buffer = self._stdout_buffer[stdout_end_start + len(stdout_end) :]
        self._stderr_buffer = self._stderr_buffer[stderr_end_start + len(stderr_end) :]
        return stdout, stderr, int(status_text.strip() or "0"), pwd

    def _collect_partial_output_and_close(self) -> tuple[bytes, bytes]:
        self._drain_channel()
        stdout = self._stdout_buffer
        stderr = self._stderr_buffer
        self._stdout_buffer = b""
        self._stderr_buffer = b""
        self.close()
        return stdout, stderr


def _detect_password_prompt(stdout_tail: bytes, stderr_tail: bytes) -> tuple[str, str] | None:
    for stream_name, tail in (("stderr", stderr_tail), ("stdout", stdout_tail)):
        decoded = tail.decode("utf-8", errors="replace")
        match = _PASSWORD_PROMPT_RE.search(decoded)
        if match is not None:
            return stream_name, match.group("prompt").strip()
    return None


def _maybe_handle_password_prompt(
    *,
    channel: paramiko.Channel,
    stdout_tail: bytes,
    stderr_tail: bytes,
    stdout_length: int,
    stderr_length: int,
    timeout_seconds: int,
    started_at: float,
    progress_callback: Callable[[float, str], None],
    password_prompt_callback: PasswordPromptHandler | None,
    prompt_attempts: int,
    last_prompt_state: tuple[str, str, int, int] | None,
) -> tuple[int, tuple[str, str, int, int] | None]:
    detected_prompt = _detect_password_prompt(stdout_tail, stderr_tail)
    if detected_prompt is None:
        return prompt_attempts, None

    stream_name, prompt = detected_prompt
    prompt_state = (stream_name, prompt, stdout_length, stderr_length)
    if prompt_state == last_prompt_state:
        return prompt_attempts, last_prompt_state

    if prompt_attempts >= _MAX_PASSWORD_PROMPTS:
        raise SSHServiceError(f"Remote command requested password input too many times: {prompt}")

    if password_prompt_callback is None:
        raise SSHServiceError(f"Remote command requested password input: {prompt}")

    remaining_timeout = max(1.0, timeout_seconds - (time.monotonic() - started_at))
    attempt = prompt_attempts + 1
    progress_callback(min(0.95, 0.2 + (attempt * 0.05)), f"Remote command is waiting for password input: {prompt}")
    password = password_prompt_callback(
        PasswordPromptRequest(
            prompt=prompt,
            attempt=attempt,
            timeout_seconds=remaining_timeout,
        )
    )
    if password is None:
        raise SSHServiceError("Remote password prompt was declined")

    secret = password.rstrip("\r\n")
    if not secret:
        raise SSHServiceError("Remote password prompt was not completed")

    try:
        channel.sendall((secret + "\n").encode("utf-8"))
    except Exception as exc:
        raise SSHServiceError("Failed to send password input to the remote command") from exc

    return attempt, prompt_state


class SSHService:
    def __init__(self, settings: AppSettings):
        self._settings = settings
        self._ssh_config_path = Path(settings.ssh_config_path).expanduser()

    def list_targets(self) -> list[TargetSettings]:
        return list(self._settings.targets)

    def get_target(self, target_id: str) -> TargetSettings:
        try:
            return self._settings.target_by_id(target_id)
        except KeyError as exc:
            raise SSHServiceError(f"Unknown target_id: {target_id}") from exc

    def browse_files(self, target_id: str, directory: str, glob_pattern: str) -> BrowseResult:
        target = self.get_target(target_id)
        normalized_directory = self._validate_remote_path(target, directory)

        with self._connect(target) as client:
            sftp = client.open_sftp()
            try:
                entries = []
                try:
                    attrs_list = sftp.listdir_attr(normalized_directory)
                except FileNotFoundError as exc:
                    raise SSHServiceError(f"Remote path does not exist: {normalized_directory}") from exc
                except OSError as exc:
                    raise SSHServiceError(str(exc)) from exc
                for attr in attrs_list:
                    full_path = posixpath.join(normalized_directory, attr.filename)
                    if not self._is_path_allowed(target, full_path):
                        continue
                    if not fnmatch.fnmatch(attr.filename, glob_pattern):
                        continue
                    entries.append(
                        RemoteFileEntry(
                            path=full_path,
                            name=attr.filename,
                            is_dir=stat.S_ISDIR(attr.st_mode),
                            size=int(attr.st_size),
                            modified_time=int(attr.st_mtime) if attr.st_mtime else None,
                        )
                    )
                entries.sort(key=lambda item: (not item.is_dir, item.name.lower()))
                return BrowseResult(entries=entries, total_entries=len(entries))
            finally:
                sftp.close()

    def read_file_bytes(self, target_id: str, path: str, max_bytes: int) -> FileBytesResult:
        target = self.get_target(target_id)
        normalized_path = self._validate_readable_file_path(target, path)

        with self._connect(target) as client:
            sftp = client.open_sftp()
            try:
                try:
                    attrs = sftp.stat(normalized_path)
                except FileNotFoundError as exc:
                    raise SSHServiceError(f"Remote path does not exist: {normalized_path}") from exc
                except OSError as exc:
                    raise SSHServiceError(str(exc)) from exc
                if stat.S_ISDIR(attrs.st_mode):
                    raise SSHServiceError(f"Remote path is a directory: {normalized_path}")
                size_bytes = int(attrs.st_size)
                source_truncated = size_bytes > max_bytes
                try:
                    with sftp.file(normalized_path, mode="rb") as handle:
                        data = handle.read(max_bytes)
                except FileNotFoundError as exc:
                    raise SSHServiceError(f"Remote path does not exist: {normalized_path}") from exc
                except OSError as exc:
                    raise SSHServiceError(str(exc)) from exc
                return FileBytesResult(
                    path=normalized_path,
                    size_bytes=size_bytes,
                    data=data,
                    source_truncated=source_truncated,
                )
            finally:
                sftp.close()

    def resolve_directory(self, target_id: str, path: str, current_directory: str | None = None) -> str:
        target = self.get_target(target_id)
        normalized_path = self._normalize_directory_path(target, path, current_directory=current_directory)

        with self._connect(target) as client:
            sftp = client.open_sftp()
            try:
                try:
                    attrs = sftp.stat(normalized_path)
                except FileNotFoundError as exc:
                    raise SSHServiceError(f"Remote path does not exist: {normalized_path}") from exc
                except OSError as exc:
                    raise SSHServiceError(str(exc)) from exc
                if not stat.S_ISDIR(attrs.st_mode):
                    raise SSHServiceError(f"Remote path is not a directory: {normalized_path}")
                return normalized_path
            finally:
                sftp.close()

    def resolve_command_directory(self, target_id: str, path: str, current_directory: str | None = None) -> str:
        target = self.get_target(target_id)
        normalized_path = self._normalize_directory_path(
            target,
            path,
            current_directory=current_directory,
            enforce_allowlist=False,
        )

        with self._connect(target) as client:
            sftp = client.open_sftp()
            try:
                try:
                    attrs = sftp.stat(normalized_path)
                except FileNotFoundError as exc:
                    raise SSHServiceError(f"Remote path does not exist: {normalized_path}") from exc
                except OSError as exc:
                    raise SSHServiceError(str(exc)) from exc
                if not stat.S_ISDIR(attrs.st_mode):
                    raise SSHServiceError(f"Remote path is not a directory: {normalized_path}")
                return normalized_path
            finally:
                sftp.close()

    def run_command(
        self,
        target_id: str,
        command: str,
        timeout_seconds: int,
        progress_callback: Callable[[float, str], None],
        cancel_event: threading.Event,
        working_directory: str | None = None,
        password_prompt_callback: PasswordPromptHandler | None = None,
    ) -> CommandRunResult:
        target = self.get_target(target_id)
        progress_callback(0.05, "Connecting to remote target")
        started_at = time.monotonic()
        prompt_attempts = 0
        last_prompt_state: tuple[str, str, int, int] | None = None
        remote_command = _prepare_command_for_password_prompts(command)
        if working_directory is not None:
            remote_command = f"cd -- {shlex.quote(working_directory)} && {remote_command}"

        with self._connect(target) as client:
            transport = client.get_transport()
            if transport is None:
                raise SSHServiceError("SSH transport is unavailable")
            channel = transport.open_session()
            channel.set_combine_stderr(False)
            channel.exec_command(remote_command)
            progress_callback(0.15, "Command started")

            stdout_chunks: list[bytes] = []
            stderr_chunks: list[bytes] = []
            stdout_tail = b""
            stderr_tail = b""
            stdout_bytes = 0
            stderr_bytes = 0
            timed_out = False
            cancelled = False
            heartbeat = 0
            last_progress_time = started_at

            try:
                while True:
                    if cancel_event.is_set():
                        cancelled = True
                        channel.close()
                        break

                    drained = False
                    while channel.recv_ready():
                        chunk = channel.recv(32768)
                        stdout_chunks.append(chunk)
                        stdout_tail = (stdout_tail + chunk)[-1024:]
                        stdout_bytes += len(chunk)
                        drained = True
                    while channel.recv_stderr_ready():
                        chunk = channel.recv_stderr(32768)
                        stderr_chunks.append(chunk)
                        stderr_tail = (stderr_tail + chunk)[-1024:]
                        stderr_bytes += len(chunk)
                        drained = True

                    try:
                        prompt_attempts, last_prompt_state = _maybe_handle_password_prompt(
                            channel=channel,
                            stdout_tail=stdout_tail,
                            stderr_tail=stderr_tail,
                            stdout_length=stdout_bytes,
                            stderr_length=stderr_bytes,
                            timeout_seconds=timeout_seconds,
                            started_at=started_at,
                            progress_callback=progress_callback,
                            password_prompt_callback=password_prompt_callback,
                            prompt_attempts=prompt_attempts,
                            last_prompt_state=last_prompt_state,
                        )
                    except SSHServiceError:
                        channel.close()
                        raise

                    if channel.exit_status_ready() and not channel.recv_ready() and not channel.recv_stderr_ready():
                        break

                    elapsed = time.monotonic() - started_at
                    if elapsed > timeout_seconds:
                        timed_out = True
                        channel.close()
                        break

                    now = time.monotonic()
                    if drained or now - last_progress_time >= self._settings.server.progress_report_interval_seconds:
                        heartbeat += 1
                        progress = min(0.95, 0.15 + (heartbeat * 0.05))
                        progress_callback(progress, f"Remote command still running; collected {stdout_bytes + stderr_bytes} bytes")
                        last_progress_time = now

                    time.sleep(0.1)

                while channel.recv_ready():
                    chunk = channel.recv(32768)
                    stdout_chunks.append(chunk)
                    stdout_bytes += len(chunk)
                while channel.recv_stderr_ready():
                    chunk = channel.recv_stderr(32768)
                    stderr_chunks.append(chunk)
                    stderr_bytes += len(chunk)

                exit_code = None
                if not timed_out and not cancelled:
                    exit_code = channel.recv_exit_status()
                return CommandRunResult(
                    exit_code=exit_code,
                    stdout=b"".join(stdout_chunks),
                    stderr=b"".join(stderr_chunks),
                    duration_seconds=round(time.monotonic() - started_at, 3),
                    timed_out=timed_out,
                    cancelled=cancelled,
                )
            finally:
                channel.close()
                progress_callback(1.0, "Remote command finished")

    def open_persistent_shell(self, target_id: str, initial_directory: str) -> PersistentShellSession:
        target = self.get_target(target_id)
        client, sock = self._open_client(target)
        channel: paramiko.Channel | None = None
        try:
            transport = client.get_transport()
            if transport is None:
                raise SSHServiceError("SSH transport is unavailable")
            channel = transport.open_session()
            channel.set_combine_stderr(False)
            channel.exec_command("bash --noprofile --norc")
            shell = PersistentShellSession(
                target_id=target_id,
                client=client,
                sock=sock,
                channel=channel,
                current_directory=initial_directory,
                progress_interval_seconds=self._settings.server.progress_report_interval_seconds,
            )
            init_result = shell.run_command(
                ":",
                target.default_timeout_seconds,
                lambda _progress, _message: None,
                threading.Event(),
                working_directory=initial_directory,
            )
            if shell.is_closed or init_result.result.timed_out or init_result.result.cancelled or init_result.result.exit_code not in {0, None}:
                shell.close()
                raise SSHServiceError("Failed to initialize persistent shell session")
            return shell
        except Exception as exc:
            if channel is not None:
                try:
                    channel.close()
                except Exception:
                    pass
            try:
                client.close()
            except Exception:
                pass
            if sock is not None:
                try:
                    sock.close()
                except Exception:
                    pass
            if isinstance(exc, SSHServiceError):
                raise
            raise SSHServiceError(str(exc)) from exc

    def _load_ssh_config(self) -> paramiko.SSHConfig:
        if not self._ssh_config_path.exists():
            raise SSHServiceError(
                f"SSH config file not found: {self._ssh_config_path}. "
                "Set ssh_config_path in config/server.toml or create the SSH config file."
            )
        return paramiko.SSHConfig.from_path(self._ssh_config_path)

    def _resolve_host_config(self, target: TargetSettings) -> tuple[dict[str, object], socket.socket | ProxyCommand | None]:
        host_cfg = self._load_ssh_config().lookup(target.ssh_alias)
        hostname = host_cfg.get("hostname", target.ssh_alias)
        username = host_cfg.get("user")
        if username is None:
            raise SSHServiceError(f"SSH alias '{target.ssh_alias}' does not define a user")

        key_files = [str(Path(item).expanduser()) for item in host_cfg.get("identityfile", [])]
        port = int(host_cfg.get("port", 22))

        proxy_command = host_cfg.get("proxycommand")
        sock: socket.socket | ProxyCommand | None = None
        if proxy_command:
            sock = ProxyCommand(proxy_command)

        connect_kwargs: dict[str, object] = {
            "hostname": hostname,
            "port": port,
            "username": username,
            "key_filename": key_files or None,
            "look_for_keys": True,
            "allow_agent": True,
            "timeout": target.connect_timeout_seconds,
            "auth_timeout": target.connect_timeout_seconds,
            "banner_timeout": target.connect_timeout_seconds,
            "sock": sock,
        }
        return connect_kwargs, sock

    def _open_client(self, target: TargetSettings) -> tuple[paramiko.SSHClient, socket.socket | ProxyCommand | None]:
        connect_kwargs, sock = self._resolve_host_config(target)
        client = paramiko.SSHClient()
        client.load_system_host_keys()
        try:
            client.connect(**connect_kwargs)
            return client, sock
        except Exception as exc:
            client.close()
            if sock is not None:
                try:
                    sock.close()
                except Exception:
                    pass
            raise SSHServiceError(str(exc)) from exc

    @contextmanager
    def _connect(self, target: TargetSettings):
        client, sock = self._open_client(target)
        try:
            yield client
        finally:
            client.close()
            if sock is not None:
                try:
                    sock.close()
                except Exception:
                    pass

    def _normalize_directory_path(
        self,
        target: TargetSettings,
        path: str,
        current_directory: str | None = None,
        *,
        enforce_allowlist: bool = True,
    ) -> str:
        raw = path.strip()
        if not raw:
            raise SSHServiceError("Remote directory path must not be empty")
        if raw.startswith("/"):
            normalized = posixpath.normpath(raw)
        else:
            if current_directory is None:
                raise SSHServiceError("Relative remote directory requires a current directory")
            normalized = posixpath.normpath(posixpath.join(current_directory, raw))
        if not normalized.startswith("/"):
            raise SSHServiceError("Remote path normalization escaped the root")
        if enforce_allowlist and not self._is_path_allowed(target, normalized):
            raise SSHServiceError(f"Remote path is outside the allowlist: {normalized}")
        return normalized

    def _validate_remote_path(self, target: TargetSettings, path: str) -> str:
        return self._normalize_directory_path(target, path)

    def _validate_readable_file_path(self, target: TargetSettings, path: str) -> str:
        return self._normalize_directory_path(target, path, enforce_allowlist=False)

    @staticmethod
    def _is_path_allowed(target: TargetSettings, normalized_path: str) -> bool:
        for allowed in target.allowed_paths:
            if normalized_path == allowed or normalized_path.startswith(f"{allowed}/"):
                return True
        return False
