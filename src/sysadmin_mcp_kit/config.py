from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from mcp.server.auth.settings import AuthSettings
from pydantic import AnyHttpUrl, BaseModel, Field, field_validator, model_validator

DEFAULT_SENSITIVE_KEYS = [
    r"password",
    r"passphrase",
    r"secret",
    r"token",
    r"api[_-]?key",
    r"access[_-]?key",
    r"client[_-]?secret",
    r"private[_-]?key",
    r"credential",
]

DEFAULT_TEXT_PATTERNS = [
    r"(?is)-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?-----END [A-Z0-9 ]*PRIVATE KEY-----",
    r"(?i)(bearer\s+)([A-Za-z0-9._~+/=-]+)",
    r"(?im)(^\s*(?:password|passphrase|secret|token|api[_-]?key|client[_-]?secret|private[_-]?key)\s*[:=]\s*)(.+)$",
    r"([a-z]+://[^\s:/@]+:)([^\s/@]+)(@)",
]


def _normalize_http_url(url: str) -> str:
    parsed = urlparse(url)
    return parsed.hostname or ""


class ServerSettings(BaseModel):
    host: str = "127.0.0.1"
    port: int = 8000
    streamable_http_path: str = "/mcp"
    json_response: bool = False
    stateless_http: bool = False
    list_limit: int = 50
    default_page_lines: int = 200
    max_page_lines: int = 500
    hard_page_char_limit: int = 16_000
    cache_ttl_seconds: int = 900
    max_file_bytes: int = 1_048_576
    progress_report_interval_seconds: float = 1.0
    log_level: str = "INFO"

    @field_validator("streamable_http_path")
    @classmethod
    def validate_streamable_http_path(cls, value: str) -> str:
        if not value.startswith("/"):
            raise ValueError("streamable_http_path must start with '/'")
        return value.rstrip("/") or "/mcp"

    @field_validator("max_page_lines")
    @classmethod
    def validate_max_page_lines(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("max_page_lines must be positive")
        return value

    @model_validator(mode="after")
    def validate_page_defaults(self) -> "ServerSettings":
        if self.default_page_lines <= 0:
            raise ValueError("default_page_lines must be positive")
        if self.default_page_lines > self.max_page_lines:
            raise ValueError("default_page_lines must not exceed max_page_lines")
        return self


class OAuthSettings(BaseModel):
    issuer_url: AnyHttpUrl
    introspection_endpoint: AnyHttpUrl
    resource_server_url: AnyHttpUrl
    client_id: str
    client_secret: str
    required_scope: str
    allow_insecure_transport: bool = False

    @model_validator(mode="after")
    def validate_https(self) -> "OAuthSettings":
        urls = [self.issuer_url, self.introspection_endpoint, self.resource_server_url]
        for url in urls:
            parsed = urlparse(str(url))
            host = parsed.hostname or ""
            is_local = host in {"127.0.0.1", "localhost", "::1"}
            if parsed.scheme != "https" and not (self.allow_insecure_transport or is_local):
                raise ValueError(f"OAuth URL must use HTTPS outside localhost: {url}")
        return self

    def to_auth_settings(self) -> AuthSettings:
        return AuthSettings(
            issuer_url=self.issuer_url,
            resource_server_url=self.resource_server_url,
            required_scopes=[self.required_scope],
        )


class TargetSettings(BaseModel):
    target_id: str
    ssh_alias: str
    allowed_paths: list[str] = Field(default_factory=list)
    default_timeout_seconds: int = 300
    connect_timeout_seconds: int = 10

    @field_validator("allowed_paths")
    @classmethod
    def validate_allowed_paths(cls, value: list[str]) -> list[str]:
        normalized: list[str] = []
        for item in value:
            if not item.startswith("/"):
                raise ValueError("allowed_paths entries must be absolute POSIX paths")
            clean = item.rstrip("/") or "/"
            normalized.append(clean)
        if not normalized:
            raise ValueError("allowed_paths must not be empty")
        return normalized


class CommandPolicySettings(BaseModel):
    sensitive_patterns: list[str] = Field(default_factory=list)
    blocked_patterns: list[str] = Field(default_factory=list)
    confirmation_token_length: int = 8

    @field_validator("confirmation_token_length")
    @classmethod
    def validate_confirmation_token_length(cls, value: int) -> int:
        if value < 4 or value > 32:
            raise ValueError("confirmation_token_length must be between 4 and 32")
        return value


class RedactionSettings(BaseModel):
    sensitive_key_patterns: list[str] = Field(default_factory=lambda: list(DEFAULT_SENSITIVE_KEYS))
    text_patterns: list[str] = Field(default_factory=lambda: list(DEFAULT_TEXT_PATTERNS))


class AppSettings(BaseModel):
    server: ServerSettings = Field(default_factory=ServerSettings)
    oauth: OAuthSettings
    targets: list[TargetSettings]
    command_policy: CommandPolicySettings = Field(default_factory=CommandPolicySettings)
    redaction: RedactionSettings = Field(default_factory=RedactionSettings)
    ssh_config_path: Path = Field(default_factory=lambda: Path.home() / ".ssh" / "config")

    @model_validator(mode="after")
    def validate_targets(self) -> "AppSettings":
        seen: set[str] = set()
        for target in self.targets:
            if target.target_id in seen:
                raise ValueError(f"Duplicate target_id: {target.target_id}")
            seen.add(target.target_id)
        if self.server.json_response:
            raise ValueError(
                "server.json_response=true is incompatible with confirmed commands because MCP elicitation "
                "requires streamable SSE responses. Set server.json_response=false."
            )
        return self

    def target_by_id(self, target_id: str) -> TargetSettings:
        for target in self.targets:
            if target.target_id == target_id:
                return target
        raise KeyError(target_id)


DEFAULT_CONFIG_PATH = Path("config") / "server.toml"
ENV_CONFIG_PATH = "SYSADMIN_MCP_CONFIG"


def _resolve_config_path(path: str | Path | None = None) -> Path:
    raw_value = path or os.getenv(ENV_CONFIG_PATH) or DEFAULT_CONFIG_PATH
    candidate = Path(raw_value).expanduser()
    if candidate.is_absolute():
        if candidate.exists():
            return candidate
        raise FileNotFoundError(f"Config file not found: {candidate}")

    search_roots: list[Path] = [Path.cwd().resolve(), *Path.cwd().resolve().parents]
    package_root = Path(__file__).resolve().parents[2]
    if package_root not in search_roots:
        search_roots.append(package_root)

    attempted: list[Path] = []
    for root in search_roots:
        resolved = root / candidate
        attempted.append(resolved)
        if resolved.exists():
            return resolved

    attempted_text = ", ".join(str(item) for item in attempted)
    raise FileNotFoundError(
        f"Config file not found. Tried: {attempted_text}. "
        f"Pass --config or set {ENV_CONFIG_PATH}."
    )


def load_settings(path: str | Path | None = None) -> AppSettings:
    config_path = _resolve_config_path(path)
    with config_path.open("rb") as handle:
        data = tomllib.load(handle)
    return AppSettings.model_validate(data)


def example_config_dict() -> dict[str, Any]:
    return {
        "server": ServerSettings().model_dump(mode="json"),
        "oauth": {
            "issuer_url": "https://auth.example.com",
            "introspection_endpoint": "https://auth.example.com/oauth/introspect",
            "resource_server_url": "https://mcp.example.com/mcp",
            "client_id": "sysadmin-mcp",
            "client_secret": "replace-me",
            "required_scope": "sysadmin:mcp",
            "allow_insecure_transport": False,
        },
        "targets": [
            {
                "target_id": "cheetan",
                "ssh_alias": "cheetan",
                "allowed_paths": ["/etc", "/opt/app/config"],
                "default_timeout_seconds": 300,
                "connect_timeout_seconds": 10,
            }
        ],
        "command_policy": CommandPolicySettings(
            sensitive_patterns=[r"(?i)\\bsudo\\b", r"(?i)\\bsystemctl\\s+(restart|stop)\\b"],
            blocked_patterns=[r"(?i)rm\\s+-rf\\s+/\\s*$"],
        ).model_dump(mode="json"),
        "redaction": RedactionSettings().model_dump(mode="json"),
        "ssh_config_path": str(Path.home() / ".ssh" / "config"),
    }
