from __future__ import annotations

import shlex
import threading
import time
import uuid
from dataclasses import dataclass, field, replace
from hashlib import sha256
from typing import Callable

from .ssh import CommandRunResult, PersistentShellSession, SSHService, SSHServiceError


class SessionStoreError(KeyError):
    """Raised when a stored terminal session is missing or inaccessible."""


@dataclass
class TerminalSession:
    session_id: str
    owner_id: str
    target_id: str
    current_directory: str
    created_at: float
    last_used_at: float
    shell: PersistentShellSession = field(repr=False, compare=False)


@dataclass(frozen=True)
class BuiltinCommand:
    kind: str
    argument: str | None = None


@dataclass(frozen=True)
class SessionCommandResult:
    session: TerminalSession
    result: CommandRunResult
    builtin: bool


class InMemorySessionStore:
    def __init__(self, ttl_seconds: int):
        self._ttl_seconds = ttl_seconds
        self._items: dict[str, TerminalSession] = {}
        self._lock = threading.Lock()

    def _purge_expired_locked(self) -> None:
        threshold = time.time() - self._ttl_seconds
        expired = [key for key, item in self._items.items() if item.last_used_at < threshold]
        for key in expired:
            session = self._items.pop(key, None)
            if session is not None:
                self._close_shell(session)

    @staticmethod
    def _close_shell(session: TerminalSession) -> None:
        try:
            session.shell.close()
        except Exception:
            pass

    def create(
        self,
        owner_id: str,
        target_id: str,
        current_directory: str,
        shell: PersistentShellSession,
        *,
        session_id: str | None = None,
    ) -> TerminalSession:
        now = time.time()
        session = TerminalSession(
            session_id=session_id or uuid.uuid4().hex,
            owner_id=owner_id,
            target_id=target_id,
            current_directory=current_directory,
            created_at=now,
            last_used_at=now,
            shell=shell,
        )
        with self._lock:
            self._purge_expired_locked()
            existing = self._items.get(session.session_id)
            if existing is not None:
                self._close_shell(existing)
            self._items[session.session_id] = session
        return session

    def get(self, owner_id: str, session_id: str) -> TerminalSession:
        with self._lock:
            self._purge_expired_locked()
            session = self._items.get(session_id)
            if session is None or session.owner_id != owner_id:
                raise SessionStoreError(session_id)
            if session.shell.is_closed:
                self._items.pop(session_id, None)
                self._close_shell(session)
                raise SessionStoreError(session_id)
            refreshed = replace(session, last_used_at=time.time())
            self._items[session_id] = refreshed
            return refreshed

    def update_directory(self, owner_id: str, session_id: str, current_directory: str) -> TerminalSession:
        with self._lock:
            self._purge_expired_locked()
            session = self._items.get(session_id)
            if session is None or session.owner_id != owner_id:
                raise SessionStoreError(session_id)
            updated = replace(session, current_directory=current_directory, last_used_at=time.time())
            self._items[session_id] = updated
            return updated

    def delete(self, owner_id: str, session_id: str) -> TerminalSession:
        with self._lock:
            self._purge_expired_locked()
            session = self._items.get(session_id)
            if session is None or session.owner_id != owner_id:
                raise SessionStoreError(session_id)
            self._items.pop(session_id, None)
        self._close_shell(session)
        return session

    def discard(self, owner_id: str, session_id: str) -> TerminalSession | None:
        with self._lock:
            session = self._items.get(session_id)
            if session is None or session.owner_id != owner_id:
                return None
            self._items.pop(session_id, None)
        self._close_shell(session)
        return session


def parse_builtin_command(command: str) -> BuiltinCommand | None:
    stripped = command.strip()
    if not stripped:
        return None
    try:
        tokens = shlex.split(stripped, posix=True)
    except ValueError:
        return None
    if not tokens:
        return None
    if tokens[0] == "pwd" and len(tokens) == 1:
        return BuiltinCommand(kind="pwd")
    if tokens[0] == "cd" and len(tokens) <= 2:
        return BuiltinCommand(kind="cd", argument=tokens[1] if len(tokens) == 2 else None)
    return None


class TerminalSessionService:
    def __init__(self, ssh_service: SSHService, ttl_seconds: int):
        self._ssh_service = ssh_service
        self._store = InMemorySessionStore(ttl_seconds)

    def create_session(self, owner_id: str, target_id: str, working_directory: str | None = None) -> TerminalSession:
        target = self._ssh_service.get_target(target_id)
        default_directory = target.allowed_paths[0]
        requested_directory = working_directory or default_directory
        resolved_directory = self._ssh_service.resolve_directory(
            target_id,
            requested_directory,
            current_directory=default_directory,
        )
        shell = self._ssh_service.open_persistent_shell(target_id, resolved_directory)
        return self._store.create(owner_id, target_id, resolved_directory, shell)

    @staticmethod
    def _context_session_id(owner_id: str, context_id: str, target_id: str) -> str:
        digest = sha256(f"{owner_id}\0{context_id}\0{target_id}".encode("utf-8")).hexdigest()
        return f"ctx-{digest}"

    def get_or_create_context_session(self, owner_id: str, context_id: str, target_id: str) -> TerminalSession:
        session_id = self._context_session_id(owner_id, context_id, target_id)
        try:
            return self._store.get(owner_id, session_id)
        except SessionStoreError:
            target = self._ssh_service.get_target(target_id)
            resolved_directory = target.allowed_paths[0]
            shell = self._ssh_service.open_persistent_shell(target_id, resolved_directory)
            return self._store.create(
                owner_id,
                target_id,
                resolved_directory,
                shell,
                session_id=session_id,
            )

    def get_session(self, owner_id: str, session_id: str) -> TerminalSession:
        return self._store.get(owner_id, session_id)

    def close_session(self, owner_id: str, session_id: str) -> TerminalSession:
        return self._store.delete(owner_id, session_id)

    def execute_command(
        self,
        owner_id: str,
        session_id: str,
        command: str,
        timeout_seconds: int | None,
        progress_callback: Callable[[float, str], None],
        cancel_event: threading.Event,
        working_directory: str | None = None,
    ) -> SessionCommandResult:
        session = self._store.get(owner_id, session_id)
        builtin = parse_builtin_command(command)
        if builtin is not None:
            session, result = self._execute_builtin(
                session,
                owner_id,
                builtin,
                timeout_seconds,
                progress_callback,
                cancel_event,
            )
            return SessionCommandResult(session=session, result=result, builtin=True)

        target = self._ssh_service.get_target(session.target_id)
        timeout = timeout_seconds or target.default_timeout_seconds
        try:
            shell_result = session.shell.run_command(
                command,
                timeout,
                progress_callback,
                cancel_event,
                working_directory=working_directory,
            )
        except SSHServiceError:
            if session.shell.is_closed:
                self._store.discard(owner_id, session_id)
            raise

        response_session = replace(session, current_directory=shell_result.current_directory, last_used_at=time.time())
        if session.shell.is_closed:
            self._store.discard(owner_id, session_id)
        else:
            response_session = self._store.update_directory(owner_id, session_id, shell_result.current_directory)
        return SessionCommandResult(session=response_session, result=shell_result.result, builtin=False)

    def execute_context_command(
        self,
        owner_id: str,
        context_id: str,
        target_id: str,
        command: str,
        timeout_seconds: int | None,
        progress_callback: Callable[[float, str], None],
        cancel_event: threading.Event,
        working_directory: str | None = None,
    ) -> SessionCommandResult:
        session = self.get_or_create_context_session(owner_id, context_id, target_id)
        return self.execute_command(
            owner_id,
            session.session_id,
            command,
            timeout_seconds,
            progress_callback,
            cancel_event,
            working_directory=working_directory,
        )

    def _execute_builtin(
        self,
        session: TerminalSession,
        owner_id: str,
        builtin: BuiltinCommand,
        timeout_seconds: int | None,
        progress_callback: Callable[[float, str], None],
        cancel_event: threading.Event,
    ) -> tuple[TerminalSession, CommandRunResult]:
        if builtin.kind == "pwd":
            progress_callback(1.0, "Session command finished")
            return session, self._builtin_result(stdout=f"{session.current_directory}\n".encode("utf-8"))

        target = self._ssh_service.get_target(session.target_id)
        requested_directory = builtin.argument or target.allowed_paths[0]
        resolved_directory = self._ssh_service.resolve_directory(
            session.target_id,
            requested_directory,
            current_directory=session.current_directory,
        )
        timeout = timeout_seconds or target.default_timeout_seconds
        try:
            session.shell.run_command(
                ":",
                timeout,
                progress_callback,
                cancel_event,
                working_directory=resolved_directory,
            )
        except SSHServiceError:
            if session.shell.is_closed:
                self._store.discard(owner_id, session.session_id)
            raise

        updated = replace(session, current_directory=resolved_directory, last_used_at=time.time())
        if session.shell.is_closed:
            self._store.discard(owner_id, session.session_id)
        else:
            updated = self._store.update_directory(owner_id, session.session_id, resolved_directory)
        progress_callback(1.0, "Session command finished")
        return updated, self._builtin_result(stdout=f"{resolved_directory}\n".encode("utf-8"))

    @staticmethod
    def _builtin_result(*, stdout: bytes, stderr: bytes = b"") -> CommandRunResult:
        return CommandRunResult(
            exit_code=0,
            stdout=stdout,
            stderr=stderr,
            duration_seconds=0.0,
            timed_out=False,
            cancelled=False,
        )
