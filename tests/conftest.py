from __future__ import annotations

import posixpath
import sys
from pathlib import Path

import pytest
from mcp.server.auth.provider import AccessToken

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from sysadmin_mcp_kit.config import AppSettings
from sysadmin_mcp_kit.ssh import BrowseResult, CommandRunResult, FileBytesResult, PersistentShellCommandResult, RemoteFileEntry, SSHServiceError


class StaticTokenVerifier:
    async def verify_token(self, token: str) -> AccessToken | None:
        if token != "good-token":
            return None
        return AccessToken(
            token=token,
            client_id="test-client",
            scopes=["sysadmin:mcp"],
            resource="http://127.0.0.1/mcp",
        )


class FakePersistentShell:
    def __init__(self, service: "FakeSSHService", target_id: str, current_directory: str):
        self._service = service
        self.target_id = target_id
        self._current_directory = current_directory
        self._env: dict[str, str] = {}
        self._closed = False

    @property
    def is_closed(self) -> bool:
        return self._closed

    @property
    def current_directory(self) -> str:
        return self._current_directory

    def close(self) -> None:
        self._closed = True

    def run_command(self, command: str, timeout_seconds: int, progress_callback, cancel_event, working_directory: str | None = None) -> PersistentShellCommandResult:
        if self._closed:
            raise SSHServiceError("Persistent shell session is not available")
        if working_directory is not None:
            self._current_directory = working_directory

        progress_callback(0.4, "command running")
        progress_callback(0.8, "command almost done")

        stripped = command.strip()
        stdout = b""
        stderr = b""
        if cancel_event.is_set():
            self.close()
            return PersistentShellCommandResult(
                result=CommandRunResult(
                    exit_code=None,
                    stdout=b"",
                    stderr=b"",
                    duration_seconds=1.234,
                    timed_out=False,
                    cancelled=True,
                ),
                current_directory=self._current_directory,
            )

        if stripped != ":":
            self._service.command_calls.append((self.target_id, command, timeout_seconds, self._current_directory))

        if stripped == ":":
            pass
        elif stripped == "pwd":
            stdout = f"{self._current_directory}\n".encode("utf-8")
        elif stripped.startswith("ls"):
            stdout = f"listing {self._current_directory}\n".encode("utf-8")
        elif stripped.startswith("export ") and "=" in stripped[7:]:
            name, value = stripped[7:].split("=", 1)
            self._env[name.strip()] = value.strip().strip('"').strip("'")
        elif stripped.startswith("echo $"):
            key = stripped[6:].strip()
            stdout = f"{self._env.get(key, '')}\n".encode("utf-8")
        else:
            stdout = self._service._stdout
            stderr = self._service._stderr

        return PersistentShellCommandResult(
            result=CommandRunResult(
                exit_code=0,
                stdout=stdout,
                stderr=stderr,
                duration_seconds=1.234,
                timed_out=False,
                cancelled=False,
            ),
            current_directory=self._current_directory,
        )


class FakeSSHService:
    def __init__(self, settings: AppSettings):
        self.settings = settings
        self.command_calls: list[tuple[str, str, int, str | None]] = []
        self.file_reads: list[tuple[str, str, int]] = []
        self.browse_calls: list[tuple[str, str, str]] = []
        self._targets = {target.target_id: target for target in settings.targets}
        self._browse_entries = [
            RemoteFileEntry(path="/etc/app/config.env", name="config.env", is_dir=False, size=128, modified_time=1),
            RemoteFileEntry(path="/etc/app/settings.toml", name="settings.toml", is_dir=False, size=512, modified_time=2),
            RemoteFileEntry(path="/etc/app/subdir", name="subdir", is_dir=True, size=0, modified_time=3),
        ]
        file_lines = [f"LINE {index}" for index in range(1, 241)]
        file_lines.insert(1, "password = hunter2")
        self._file_data = "\n".join(file_lines).encode("utf-8")
        stdout_lines = [f"stdout {index}" for index in range(1, 251)]
        stderr_lines = [f"stderr {index}" for index in range(1, 11)]
        self._stdout = "\n".join(stdout_lines).encode("utf-8")
        self._stderr = ("token=super-secret\n" + "\n".join(stderr_lines)).encode("utf-8")

    def list_targets(self):
        return list(self._targets.values())

    def get_target(self, target_id: str):
        return self._targets[target_id]

    def browse_files(self, target_id: str, directory: str, glob_pattern: str) -> BrowseResult:
        self.browse_calls.append((target_id, directory, glob_pattern))
        return BrowseResult(entries=self._browse_entries, total_entries=len(self._browse_entries))

    def read_file_bytes(self, target_id: str, path: str, max_bytes: int) -> FileBytesResult:
        self.file_reads.append((target_id, path, max_bytes))
        return FileBytesResult(path=path, size_bytes=len(self._file_data), data=self._file_data[:max_bytes], source_truncated=False)

    def resolve_directory(self, target_id: str, path: str, current_directory: str | None = None) -> str:
        return self._normalize_directory(target_id, path, current_directory=current_directory, enforce_allowlist=True)

    def resolve_command_directory(self, target_id: str, path: str, current_directory: str | None = None) -> str:
        return self._normalize_directory(target_id, path, current_directory=current_directory, enforce_allowlist=False)

    def open_persistent_shell(self, target_id: str, initial_directory: str) -> FakePersistentShell:
        return FakePersistentShell(self, target_id, initial_directory)

    def run_command(self, target_id: str, command: str, timeout_seconds: int, progress_callback, cancel_event, working_directory: str | None = None) -> CommandRunResult:
        self.command_calls.append((target_id, command, timeout_seconds, working_directory))
        progress_callback(0.4, "command running")
        progress_callback(0.8, "command almost done")
        if command == "pwd":
            stdout = f"{working_directory or '/'}\n".encode("utf-8")
        elif command.startswith("ls") and working_directory is not None:
            stdout = f"listing {working_directory}\n".encode("utf-8")
        else:
            stdout = self._stdout
        return CommandRunResult(
            exit_code=0,
            stdout=stdout,
            stderr=self._stderr,
            duration_seconds=1.234,
            timed_out=False,
            cancelled=cancel_event.is_set(),
        )

    def _normalize_directory(
        self,
        target_id: str,
        path: str,
        *,
        current_directory: str | None = None,
        enforce_allowlist: bool,
    ) -> str:
        target = self.get_target(target_id)
        raw = path.strip()
        if not raw:
            raise SSHServiceError("Remote directory path must not be empty")
        if raw.startswith("/"):
            normalized = posixpath.normpath(raw)
        else:
            base = current_directory or target.allowed_paths[0]
            normalized = posixpath.normpath(posixpath.join(base, raw))
        if enforce_allowlist and not any(normalized == allowed or normalized.startswith(f"{allowed}/") for allowed in target.allowed_paths):
            raise SSHServiceError(f"Remote path is outside the allowlist: {normalized}")
        if normalized.endswith("/ngnx"):
            raise SSHServiceError(f"Remote path does not exist: {normalized}")
        return normalized


@pytest.fixture()
def settings() -> AppSettings:
    return AppSettings.model_validate(
        {
            "ssh_config_path": str(ROOT / "tests" / "fixtures" / "ssh_config"),
            "server": {
                "host": "127.0.0.1",
                "port": 8000,
                "streamable_http_path": "/mcp",
                "json_response": False,
                "stateless_http": False,
                "list_limit": 2,
                "default_page_lines": 50,
                "max_page_lines": 100,
                "hard_page_char_limit": 4096,
                "cache_ttl_seconds": 900,
                "max_file_bytes": 1024 * 1024,
                "progress_report_interval_seconds": 0.01,
                "log_level": "INFO",
            },
            "oauth": {
                "issuer_url": "http://127.0.0.1/auth",
                "introspection_endpoint": "http://127.0.0.1/introspect",
                "resource_server_url": "http://127.0.0.1/mcp",
                "client_id": "mcp-test",
                "client_secret": "secret",
                "required_scope": "sysadmin:mcp",
                "allow_insecure_transport": True,
            },
            "targets": [
                {
                    "target_id": "cheetan",
                    "ssh_alias": "cheetan",
                    "allowed_paths": ["/etc", "/opt/app/config"],
                    "default_timeout_seconds": 90,
                    "connect_timeout_seconds": 5,
                }
            ],
            "command_policy": {
                "sensitive_patterns": [r"(?i)\bsudo\b"],
                "blocked_patterns": [r"(?i)rm\s+-rf\s+/\s*$"],
                "confirmation_token_length": 8,
            },
        }
    )


@pytest.fixture()
def fake_ssh_service(settings: AppSettings) -> FakeSSHService:
    return FakeSSHService(settings)


@pytest.fixture()
def token_verifier() -> StaticTokenVerifier:
    return StaticTokenVerifier()
