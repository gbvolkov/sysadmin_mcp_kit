from __future__ import annotations

import fnmatch
import posixpath
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
    ) -> PersistentShellCommandResult:
        with self._lock:
            if self.is_closed:
                raise SSHServiceError("Persistent shell session is not available")

            token = uuid.uuid4().hex.upper()
            script = self._build_script(command, token, working_directory)
            started_at = time.monotonic()
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
        command_literal = shlex.quote(command)
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
        normalized_path = self._validate_remote_path(target, path)

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
    ) -> CommandRunResult:
        target = self.get_target(target_id)
        progress_callback(0.05, "Connecting to remote target")
        started_at = time.monotonic()
        remote_command = command
        if working_directory is not None:
            remote_command = f"cd -- {shlex.quote(working_directory)} && {command}"

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
                        stdout_chunks.append(channel.recv(32768))
                        drained = True
                    while channel.recv_stderr_ready():
                        stderr_chunks.append(channel.recv_stderr(32768))
                        drained = True

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
                        bytes_seen = sum(len(chunk) for chunk in stdout_chunks) + sum(len(chunk) for chunk in stderr_chunks)
                        progress_callback(progress, f"Remote command still running; collected {bytes_seen} bytes")
                        last_progress_time = now

                    time.sleep(0.1)

                while channel.recv_ready():
                    stdout_chunks.append(channel.recv(32768))
                while channel.recv_stderr_ready():
                    stderr_chunks.append(channel.recv_stderr(32768))

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

    @staticmethod
    def _is_path_allowed(target: TargetSettings, normalized_path: str) -> bool:
        for allowed in target.allowed_paths:
            if normalized_path == allowed or normalized_path.startswith(f"{allowed}/"):
                return True
        return False
