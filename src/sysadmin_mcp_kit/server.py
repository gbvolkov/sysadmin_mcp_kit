from __future__ import annotations

import asyncio
import json
import logging
import threading
from typing import Any, Literal

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.fastmcp import Context, FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field

from .auth import IntrospectionTokenVerifier
from .config import AppSettings
from .pagination import CursorCodec, CursorError, Paginator
from .policy import CommandPolicy
from .redaction import RedactionResult, Redactor
from .result_store import InMemoryResultStore, ResultStoreError
from .session import TerminalSessionService, parse_builtin_command
from .ssh import BrowseResult, RemoteFileEntry, SSHService, SSHServiceError

logger = logging.getLogger(__name__)


class ApprovalForm(BaseModel):
    approve: bool = Field(description="Set to true to approve execution of the exact command above.")


class SensitiveApprovalForm(BaseModel):
    approve: bool = Field(description="Set to true to approve the sensitive command.")
    confirmation_token: str = Field(description="Type the exact confirmation token shown in the prompt.")


class TargetInfo(BaseModel):
    target_id: str
    ssh_alias: str
    allowed_paths: list[str]
    default_timeout_seconds: int


class ListTargetsResponse(BaseModel):
    targets: list[TargetInfo]


class FileEntry(BaseModel):
    path: str
    name: str
    is_dir: bool
    size: int
    modified_time: int | None


class BrowseSummary(BaseModel):
    target_id: str
    directory: str
    glob: str
    total_entries: int
    returned_entries: int
    limit: int


class BrowseFilesResponse(BaseModel):
    summary: BrowseSummary
    entries: list[FileEntry]
    next_cursor: str | None = None


class RedactionInfo(BaseModel):
    parser: str
    replacements: int
    binary: bool
    source_truncated: bool = False


class ContentPage(BaseModel):
    text: str
    start_line: int
    end_line: int
    returned_lines: int
    total_lines: int
    truncated_by_page: bool
    next_cursor: str | None = None


class FileReadSummary(BaseModel):
    target_id: str
    path: str
    size_bytes: int
    result_id: str
    binary: bool
    parser: str
    redaction_replacements: int
    source_truncated: bool


class FileReadResponse(BaseModel):
    result_id: str
    summary: FileReadSummary
    content: ContentPage | None
    next_cursor: str | None
    redaction: RedactionInfo


class StreamRedactionInfo(BaseModel):
    stdout: RedactionInfo
    stderr: RedactionInfo


class CommandSummary(BaseModel):
    execution_id: str
    target_id: str
    exit_code: int | None
    duration_seconds: float
    timed_out: bool
    cancelled: bool
    sensitive: bool
    stdout_bytes: int
    stderr_bytes: int
    stdout_lines: int
    stderr_lines: int


class CommandRunResponse(BaseModel):
    execution_id: str
    summary: CommandSummary
    stdout: ContentPage
    stderr: ContentPage
    redaction: StreamRedactionInfo
    current_directory: str | None = None
    builtin: bool = False


class CommandOutputResponse(BaseModel):
    execution_id: str
    stream: Literal["stdout", "stderr"]
    content: ContentPage
    next_cursor: str | None


class ThreadsafeProgressReporter:
    def __init__(self, ctx: Context):
        self._ctx = ctx
        self._loop = asyncio.get_running_loop()

    def __call__(self, progress: float, message: str) -> None:
        future = asyncio.run_coroutine_threadsafe(
            self._ctx.report_progress(progress, 1.0, message),
            self._loop,
        )
        future.result(timeout=10)


def _owner_id(ctx: Context) -> str:
    token = get_access_token()
    if token and token.client_id:
        return token.client_id
    return ctx.client_id or "anonymous"


def _transport_session_id(ctx: Context) -> str | None:
    request_context = getattr(ctx, "request_context", None)
    request = getattr(request_context, "request", None)
    headers = getattr(request, "headers", None)
    if headers is not None:
        session_id = headers.get("mcp-session-id")
        if session_id:
            return str(session_id)

    session = getattr(request_context, "session", None)
    if session is not None:
        return f"session-object:{id(session)}"
    return getattr(ctx, "mcp_session_id", None)


def _command_hash(command: str) -> str:
    import hashlib

    return hashlib.sha256(command.encode("utf-8")).hexdigest()


def _audit(event: str, **fields: Any) -> None:
    logger.info(json.dumps({"event": event, **fields}, sort_keys=True))


class ServerDependencies:
    def __init__(
        self,
        settings: AppSettings,
        ssh_service: SSHService | None = None,
        result_store: InMemoryResultStore | None = None,
        redactor: Redactor | None = None,
        command_policy: CommandPolicy | None = None,
        token_verifier: IntrospectionTokenVerifier | None = None,
        session_service: TerminalSessionService | None = None,
    ):
        self.settings = settings
        self.ssh_service = ssh_service or SSHService(settings)
        self.result_store = result_store or InMemoryResultStore(settings.server.cache_ttl_seconds)
        self.redactor = redactor or Redactor(settings.redaction)
        self.command_policy = command_policy or CommandPolicy(settings.command_policy)
        self.token_verifier = token_verifier or IntrospectionTokenVerifier(settings.oauth)
        self.session_service = session_service or TerminalSessionService(self.ssh_service, settings.server.cache_ttl_seconds)


class CommandMetadata(BaseModel):
    target_id: str
    exit_code: int | None
    duration_seconds: float
    timed_out: bool
    cancelled: bool
    sensitive: bool
    stdout_bytes: int
    stderr_bytes: int
    stdout_lines: int
    stderr_lines: int
    stdout_redaction: RedactionInfo
    stderr_redaction: RedactionInfo
    command_hash: str


class FileMetadata(BaseModel):
    target_id: str
    path: str
    size_bytes: int
    redaction: RedactionInfo


class _ServerRuntime:
    def __init__(self, deps: ServerDependencies):
        self.deps = deps

    def page_size(self, requested: int | None) -> int:
        if requested is None:
            return self.deps.settings.server.default_page_lines
        return max(1, min(requested, self.deps.settings.server.max_page_lines))

    def list_limit(self, requested: int | None) -> int:
        default = self.deps.settings.server.list_limit
        if requested is None:
            return default
        return max(1, min(requested, max(default, 200)))

    def page_text(self, *, owner_id: str, result_id: str, stream: str, text: str, offset: int) -> ContentPage:
        page = Paginator.paginate_lines(
            text,
            offset,
            self.deps.settings.server.default_page_lines,
            self.deps.settings.server.hard_page_char_limit,
        )
        next_cursor = None
        if page.next_index is not None:
            next_cursor = CursorCodec.encode(
                {
                    "kind": stream,
                    "result_id": result_id,
                    "offset": page.next_index,
                    "owner_id": owner_id,
                }
            )
        return ContentPage(
            text=page.text,
            start_line=page.start_line,
            end_line=page.end_line,
            returned_lines=page.returned_lines,
            total_lines=page.total_lines,
            truncated_by_page=page.truncated_by_page,
            next_cursor=next_cursor,
        )

    def page_text_with_limit(
        self,
        *,
        owner_id: str,
        result_id: str,
        stream: str,
        text: str,
        offset: int,
        page_lines: int,
        cursor_fields: dict[str, Any] | None = None,
    ) -> ContentPage:
        page = Paginator.paginate_lines(text, offset, page_lines, self.deps.settings.server.hard_page_char_limit)
        next_cursor = None
        if page.next_index is not None:
            cursor_payload = {
                "kind": stream,
                "result_id": result_id,
                "offset": page.next_index,
                "owner_id": owner_id,
            }
            if cursor_fields:
                cursor_payload.update(cursor_fields)
            next_cursor = CursorCodec.encode(cursor_payload)
        return ContentPage(
            text=page.text,
            start_line=page.start_line,
            end_line=page.end_line,
            returned_lines=page.returned_lines,
            total_lines=page.total_lines,
            truncated_by_page=page.truncated_by_page,
            next_cursor=next_cursor,
        )

    def browse_page(self, browse: BrowseResult, *, target_id: str, directory: str, glob: str, limit: int, offset: int) -> BrowseFilesResponse:
        page = Paginator.paginate_items(browse.entries, offset, limit)
        next_cursor = None
        if page.next_index is not None:
            next_cursor = CursorCodec.encode(
                {
                    "kind": "browse",
                    "target_id": target_id,
                    "directory": directory,
                    "glob": glob,
                    "offset": page.next_index,
                }
            )
        return BrowseFilesResponse(
            summary=BrowseSummary(
                target_id=target_id,
                directory=directory,
                glob=glob,
                total_entries=page.total_items,
                returned_entries=len(page.items),
                limit=limit,
            ),
            entries=[FileEntry.model_validate(item.__dict__) for item in page.items],
            next_cursor=next_cursor,
        )

    async def confirm_command(
        self,
        *,
        ctx: Context,
        target_id: str,
        command: str,
        timeout_seconds: int,
        working_dir: str | None = None,
    ) -> None:
        location_line = f"Working directory: {working_dir}\n" if working_dir else ""
        response = await ctx.elicit(
            message=(
                "Approve remote command execution.\n"
                f"Target: {target_id}\n"
                f"Timeout: {timeout_seconds}s\n"
                f"{location_line}"
                "Command:\n"
                f"{command}"
            ),
            schema=ApprovalForm,
        )
        if response.action != "accept" or not response.data.approve:
            raise ToolError("Remote command execution was not approved")

    async def confirm_sensitive_command(self, *, ctx: Context, target_id: str, command: str) -> None:
        token = self.deps.command_policy.confirmation_token(command)
        response = await ctx.elicit(
            message=(
                "This command matches the sensitive command policy.\n"
                f"Target: {target_id}\n"
                f"Confirmation token: {token}\n"
                "Type the exact token to continue."
            ),
            schema=SensitiveApprovalForm,
        )
        if response.action != "accept" or not response.data.approve or response.data.confirmation_token.strip() != token:
            raise ToolError("Sensitive remote command execution was not approved")

    def decode_cursor(self, cursor: str | None, expected_kind: str) -> dict[str, Any] | None:
        if not cursor:
            return None
        payload = CursorCodec.decode(cursor)
        if payload.get("kind") != expected_kind:
            raise ToolError("Cursor does not match this tool")
        return payload


def build_server(
    settings: AppSettings,
    *,
    ssh_service: SSHService | None = None,
    result_store: InMemoryResultStore | None = None,
    redactor: Redactor | None = None,
    command_policy: CommandPolicy | None = None,
    token_verifier: IntrospectionTokenVerifier | None = None,
    session_service: TerminalSessionService | None = None,
) -> FastMCP:
    deps = ServerDependencies(
        settings,
        ssh_service=ssh_service,
        result_store=result_store,
        redactor=redactor,
        command_policy=command_policy,
        token_verifier=token_verifier,
        session_service=session_service,
    )
    runtime = _ServerRuntime(deps)

    server = FastMCP(
        name="sysadmin-mcp-kit",
        instructions="Secure SSH MCP server with paginated remote file reads, confirmed command execution, and implicit per-session terminal context.",
        host=settings.server.host,
        port=settings.server.port,
        streamable_http_path=settings.server.streamable_http_path,
        json_response=settings.server.json_response,
        stateless_http=settings.server.stateless_http,
        auth=settings.oauth.to_auth_settings(),
        token_verifier=deps.token_verifier,
        log_level=settings.server.log_level,
    )
    if settings.server.stateless_http:
        logger.warning("stateless_http=true disables implicit terminal context across run_command calls")

    def build_command_response(
        *,
        owner_id: str,
        target_id: str,
        command: str,
        result,
        sensitive: bool,
    ) -> tuple[str, CommandSummary, ContentPage, ContentPage, StreamRedactionInfo]:
        stdout_text = result.stdout.decode("utf-8", errors="replace")
        stderr_text = result.stderr.decode("utf-8", errors="replace")
        stdout_redaction = deps.redactor.redact_text(stdout_text)
        stderr_redaction = deps.redactor.redact_text(stderr_text)
        stdout_line_count = len((stdout_redaction.text or "").splitlines())
        stderr_line_count = len((stderr_redaction.text or "").splitlines())

        metadata = CommandMetadata(
            target_id=target_id,
            exit_code=result.exit_code,
            duration_seconds=result.duration_seconds,
            timed_out=result.timed_out,
            cancelled=result.cancelled,
            sensitive=sensitive,
            stdout_bytes=len(result.stdout),
            stderr_bytes=len(result.stderr),
            stdout_lines=stdout_line_count,
            stderr_lines=stderr_line_count,
            stdout_redaction=RedactionInfo(
                parser=stdout_redaction.parser,
                replacements=stdout_redaction.replacements,
                binary=False,
                source_truncated=False,
            ),
            stderr_redaction=RedactionInfo(
                parser=stderr_redaction.parser,
                replacements=stderr_redaction.replacements,
                binary=False,
                source_truncated=False,
            ),
            command_hash=_command_hash(command),
        )
        execution_id = deps.result_store.put(
            owner_id,
            "command",
            {
                "stdout": stdout_redaction.text or "",
                "stderr": stderr_redaction.text or "",
            },
            metadata.model_dump(mode="json"),
        )
        stdout_page = runtime.page_text(
            owner_id=owner_id,
            result_id=execution_id,
            stream="stdout",
            text=stdout_redaction.text or "",
            offset=0,
        )
        stderr_page = runtime.page_text(
            owner_id=owner_id,
            result_id=execution_id,
            stream="stderr",
            text=stderr_redaction.text or "",
            offset=0,
        )
        summary = CommandSummary(
            execution_id=execution_id,
            target_id=target_id,
            exit_code=result.exit_code,
            duration_seconds=result.duration_seconds,
            timed_out=result.timed_out,
            cancelled=result.cancelled,
            sensitive=sensitive,
            stdout_bytes=len(result.stdout),
            stderr_bytes=len(result.stderr),
            stdout_lines=stdout_line_count,
            stderr_lines=stderr_line_count,
        )
        redaction = StreamRedactionInfo(
            stdout=metadata.stdout_redaction,
            stderr=metadata.stderr_redaction,
        )
        return execution_id, summary, stdout_page, stderr_page, redaction

    @server.tool(
        title="List configured targets",
        annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False),
    )
    def list_targets() -> ListTargetsResponse:
        return ListTargetsResponse(
            targets=[
                TargetInfo(
                    target_id=target.target_id,
                    ssh_alias=target.ssh_alias,
                    allowed_paths=target.allowed_paths,
                    default_timeout_seconds=target.default_timeout_seconds,
                )
                for target in deps.ssh_service.list_targets()
            ]
        )

    @server.tool(
        title="Browse allowed remote files",
        annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False),
    )
    async def browse_files(
        target_id: str,
        directory: str,
        glob: str = "*",
        cursor: str | None = None,
        limit: int | None = None,
        ctx: Context | None = None,
    ) -> BrowseFilesResponse:
        assert ctx is not None
        await ctx.report_progress(0.05, 1.0, "Listing remote files")
        cursor_payload = runtime.decode_cursor(cursor, "browse")
        offset = 0
        if cursor_payload is not None:
            if cursor_payload.get("target_id") != target_id or cursor_payload.get("directory") != directory or cursor_payload.get("glob") != glob:
                raise ToolError("Cursor does not match the requested target or directory")
            offset = int(cursor_payload.get("offset", 0))

        try:
            browse = await asyncio.to_thread(deps.ssh_service.browse_files, target_id, directory, glob)
        except SSHServiceError as exc:
            raise ToolError(str(exc)) from exc
        await ctx.report_progress(1.0, 1.0, f"Matched {browse.total_entries} entries")
        response = runtime.browse_page(
            browse,
            target_id=target_id,
            directory=directory,
            glob=glob,
            limit=runtime.list_limit(limit),
            offset=offset,
        )
        _audit(
            "browse_files",
            request_id=ctx.request_id,
            client_id=_owner_id(ctx),
            target_id=target_id,
            directory=directory,
            total_entries=browse.total_entries,
        )
        return response

    @server.tool(
        title="Read and redact a remote config file",
        annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False),
    )
    async def read_file(
        target_id: str,
        path: str,
        cursor: str | None = None,
        page_lines: int | None = None,
        ctx: Context | None = None,
    ) -> FileReadResponse:
        assert ctx is not None
        owner_id = _owner_id(ctx)
        cursor_payload = runtime.decode_cursor(cursor, "file")
        page_limit = runtime.page_size(page_lines)

        if cursor_payload is not None:
            result_id = str(cursor_payload.get("result_id"))
            offset = int(cursor_payload.get("offset", 0))
            if cursor_payload.get("target_id") != target_id or cursor_payload.get("path") != path:
                raise ToolError("Cursor does not match the requested file")
            try:
                stored = deps.result_store.get(owner_id, result_id, kind="file")
            except ResultStoreError as exc:
                raise ToolError("Stored file page is no longer available") from exc
            metadata = FileMetadata.model_validate(stored.metadata)
            content = None
            next_cursor = None
            if not metadata.redaction.binary:
                content = runtime.page_text_with_limit(
                    owner_id=owner_id,
                    result_id=result_id,
                    stream="file",
                    text=stored.payload["content"],
                    offset=offset,
                    page_lines=page_limit,
                    cursor_fields={"target_id": target_id, "path": path},
                )
                next_cursor = content.next_cursor
            return FileReadResponse(
                result_id=result_id,
                summary=FileReadSummary(
                    target_id=metadata.target_id,
                    path=metadata.path,
                    size_bytes=metadata.size_bytes,
                    result_id=result_id,
                    binary=metadata.redaction.binary,
                    parser=metadata.redaction.parser,
                    redaction_replacements=metadata.redaction.replacements,
                    source_truncated=metadata.redaction.source_truncated,
                ),
                content=content,
                next_cursor=next_cursor,
                redaction=metadata.redaction,
            )

        await ctx.report_progress(0.05, 1.0, "Fetching remote file")
        try:
            file_result = await asyncio.to_thread(
                deps.ssh_service.read_file_bytes,
                target_id,
                path,
                deps.settings.server.max_file_bytes,
            )
        except SSHServiceError as exc:
            raise ToolError(str(exc)) from exc

        await ctx.report_progress(0.75, 1.0, "Redacting remote file contents")
        redaction = deps.redactor.redact_bytes(
            file_result.data,
            path=file_result.path,
            source_truncated=file_result.source_truncated,
        )
        metadata = FileMetadata(
            target_id=target_id,
            path=file_result.path,
            size_bytes=file_result.size_bytes,
            redaction=RedactionInfo(
                parser=redaction.parser,
                replacements=redaction.replacements,
                binary=redaction.binary,
                source_truncated=redaction.source_truncated,
            ),
        )
        result_id = deps.result_store.put(
            owner_id,
            "file",
            {"content": redaction.text or ""},
            metadata.model_dump(mode="json"),
        )

        content = None
        next_cursor = None
        if redaction.text is not None:
            content = runtime.page_text_with_limit(
                owner_id=owner_id,
                result_id=result_id,
                stream="file",
                text=redaction.text,
                offset=0,
                page_lines=page_limit,
                cursor_fields={"target_id": target_id, "path": file_result.path},
            )
            next_cursor = content.next_cursor
        await ctx.report_progress(1.0, 1.0, "Remote file ready")
        _audit(
            "read_file",
            request_id=ctx.request_id,
            client_id=owner_id,
            target_id=target_id,
            path=file_result.path,
            size_bytes=file_result.size_bytes,
            redactions=redaction.replacements,
            parser=redaction.parser,
            binary=redaction.binary,
        )
        return FileReadResponse(
            result_id=result_id,
            summary=FileReadSummary(
                target_id=target_id,
                path=file_result.path,
                size_bytes=file_result.size_bytes,
                result_id=result_id,
                binary=redaction.binary,
                parser=redaction.parser,
                redaction_replacements=redaction.replacements,
                source_truncated=redaction.source_truncated,
            ),
            content=content,
            next_cursor=next_cursor,
            redaction=metadata.redaction,
        )

    @server.tool(
        title="Run a confirmed remote command",
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=False),
    )
    async def run_command(
        target_id: str,
        command: str,
        timeout_seconds: int | None = None,
        working_dir: str | None = None,
        ctx: Context | None = None,
    ) -> CommandRunResponse:
        assert ctx is not None
        owner_id = _owner_id(ctx)
        target = deps.ssh_service.get_target(target_id)
        timeout = timeout_seconds or target.default_timeout_seconds
        context_session_id = _transport_session_id(ctx) if not settings.server.stateless_http else None
        builtin = parse_builtin_command(command)
        if builtin is not None and context_session_id is None:
            raise ToolError(
                "Terminal context is unavailable for this request. Configure stateless_http=false and reuse the same MCP session."
            )
        if builtin is not None and working_dir is not None:
            raise ToolError("working_dir is not supported for builtin terminal commands")

        decision = deps.command_policy.evaluate(command) if builtin is None else None
        if decision is not None and decision.blocked:
            raise ToolError("Command is blocked by server policy")

        cancel_event = threading.Event()
        reporter = ThreadsafeProgressReporter(ctx)
        current_directory = None
        builtin_used = builtin is not None
        try:
            resolved_working_dir = None
            if working_dir is not None:
                base_directory = target.allowed_paths[0]
                if context_session_id is not None:
                    session = await asyncio.to_thread(
                        deps.session_service.get_or_create_context_session,
                        owner_id,
                        context_session_id,
                        target_id,
                    )
                    base_directory = session.current_directory
                resolved_working_dir = await asyncio.to_thread(
                    deps.ssh_service.resolve_command_directory,
                    target_id,
                    working_dir,
                    base_directory,
                )

            if builtin is None:
                await runtime.confirm_command(
                    ctx=ctx,
                    target_id=target_id,
                    command=command,
                    timeout_seconds=timeout,
                    working_dir=resolved_working_dir,
                )
                if decision is not None and decision.sensitive:
                    await runtime.confirm_sensitive_command(ctx=ctx, target_id=target_id, command=command)

            if context_session_id is not None:
                session_result = await asyncio.to_thread(
                    deps.session_service.execute_context_command,
                    owner_id,
                    context_session_id,
                    target_id,
                    command,
                    timeout_seconds,
                    reporter,
                    cancel_event,
                    resolved_working_dir,
                )
                result = session_result.result
                current_directory = session_result.session.current_directory
                builtin_used = session_result.builtin
            else:
                result = await asyncio.to_thread(
                    deps.ssh_service.run_command,
                    target_id,
                    command,
                    timeout,
                    reporter,
                    cancel_event,
                    resolved_working_dir,
                )
                current_directory = resolved_working_dir
        except SSHServiceError as exc:
            raise ToolError(str(exc)) from exc
        except asyncio.CancelledError:
            cancel_event.set()
            raise

        sensitive = decision.sensitive if decision is not None else False
        execution_id, summary, stdout_page, stderr_page, redaction = build_command_response(
            owner_id=owner_id,
            target_id=target_id,
            command=command,
            result=result,
            sensitive=sensitive,
        )
        _audit(
            "run_command",
            request_id=ctx.request_id,
            client_id=owner_id,
            target_id=target_id,
            command_hash=_command_hash(command),
            exit_code=result.exit_code,
            timed_out=result.timed_out,
            cancelled=result.cancelled,
            duration_seconds=result.duration_seconds,
            stdout_bytes=len(result.stdout),
            stderr_bytes=len(result.stderr),
            sensitive=sensitive,
            builtin=builtin_used,
            current_directory=current_directory,
            requested_working_dir=working_dir,
            transport_session_bound=context_session_id is not None,
        )
        return CommandRunResponse(
            execution_id=execution_id,
            summary=summary,
            stdout=stdout_page,
            stderr=stderr_page,
            redaction=redaction,
            current_directory=current_directory,
            builtin=builtin_used,
        )

    @server.tool(
        title="Read additional command output pages",
        annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False),
    )
    def read_command_output(
        execution_id: str,
        stream: Literal["stdout", "stderr"],
        cursor: str | None = None,
        page_lines: int | None = None,
        ctx: Context | None = None,
    ) -> CommandOutputResponse:
        assert ctx is not None
        owner_id = _owner_id(ctx)
        cursor_payload = runtime.decode_cursor(cursor, stream) if cursor else None
        if cursor_payload is not None and str(cursor_payload.get("result_id")) != execution_id:
            raise ToolError("Cursor does not match the requested execution ID")
        offset = int(cursor_payload.get("offset", 0)) if cursor_payload else 0
        page_limit = runtime.page_size(page_lines)

        try:
            stored = deps.result_store.get(owner_id, execution_id, kind="command")
        except ResultStoreError as exc:
            raise ToolError("Stored command output is no longer available") from exc
        if stream not in stored.payload:
            raise ToolError(f"Unknown output stream: {stream}")
        page = runtime.page_text_with_limit(
            owner_id=owner_id,
            result_id=execution_id,
            stream=stream,
            text=stored.payload[stream],
            offset=offset,
            page_lines=page_limit,
        )
        return CommandOutputResponse(
            execution_id=execution_id,
            stream=stream,
            content=page,
            next_cursor=page.next_cursor,
        )

    return server
