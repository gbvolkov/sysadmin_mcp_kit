from __future__ import annotations

import configparser
import json
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from .config import DEFAULT_TEXT_PATTERNS, RedactionSettings

REDACTED = "<REDACTED>"
BUILTIN_PATTERN_SET = set(DEFAULT_TEXT_PATTERNS)
ENV_REFERENCE_PATTERNS = [
    re.compile(r"^\$[A-Za-z_][A-Za-z0-9_]*$"),
    re.compile(r"^\$\{[A-Za-z_][A-Za-z0-9_]*(?::[-=?+][^}]*)?\}$"),
    re.compile(r"^%[A-Za-z_][A-Za-z0-9_]*%$"),
    re.compile(r"^(?:env|ENV)\([A-Za-z_][A-Za-z0-9_]*\)$"),
    re.compile(r"^[A-Z_][A-Z0-9_]*$"),
    re.compile(r"^[A-Za-z_][A-Za-z0-9_]*_[A-Za-z0-9_]*$"),
]


@dataclass(frozen=True)
class RedactionResult:
    text: str | None
    parser: str
    replacements: int
    binary: bool
    source_truncated: bool = False


class Redactor:
    def __init__(self, settings: RedactionSettings):
        self._settings = settings
        self._key_patterns = [re.compile(pattern, re.IGNORECASE) for pattern in settings.sensitive_key_patterns]
        self._extra_patterns = [re.compile(pattern, re.MULTILINE | re.DOTALL) for pattern in settings.text_patterns if pattern not in BUILTIN_PATTERN_SET]
        self._pem_pattern = re.compile(DEFAULT_TEXT_PATTERNS[0])
        self._bearer_pattern = re.compile(DEFAULT_TEXT_PATTERNS[1])
        self._assignment_pattern = re.compile(DEFAULT_TEXT_PATTERNS[2])
        self._url_secret_pattern = re.compile(DEFAULT_TEXT_PATTERNS[3])

    def redact_bytes(self, data: bytes, *, path: str | None = None, source_truncated: bool = False) -> RedactionResult:
        if self._is_binary(data):
            return RedactionResult(
                text=None,
                parser="binary",
                replacements=0,
                binary=True,
                source_truncated=source_truncated,
            )

        text = data.decode("utf-8", errors="replace")
        suffix = (Path(path).suffix.lower() if path else "")
        name = (Path(path).name.lower() if path else "")

        for parser_name, parser in self._structured_parsers(suffix=suffix, name=name):
            try:
                rendered, replacements = parser(text, data)
            except Exception:
                continue
            sanitized, extra = self._apply_text_redactions(rendered)
            return RedactionResult(
                text=sanitized,
                parser=parser_name,
                replacements=replacements + extra,
                binary=False,
                source_truncated=source_truncated,
            )

        sanitized, replacements = self._apply_text_redactions(text)
        return RedactionResult(
            text=sanitized,
            parser="text",
            replacements=replacements,
            binary=False,
            source_truncated=source_truncated,
        )

    def redact_text(self, text: str, *, source_truncated: bool = False) -> RedactionResult:
        sanitized, replacements = self._apply_text_redactions(text)
        return RedactionResult(
            text=sanitized,
            parser="text",
            replacements=replacements,
            binary=False,
            source_truncated=source_truncated,
        )

    def _structured_parsers(self, *, suffix: str, name: str):
        if suffix == ".json":
            yield "json", self._parse_json
        if suffix in {".yaml", ".yml"}:
            yield "yaml", self._parse_yaml
        if suffix == ".toml":
            yield "toml", self._parse_toml
        if suffix in {".ini", ".cfg"}:
            yield "ini", self._parse_ini
        if suffix == ".env" or name.startswith(".env"):
            yield "dotenv", self._parse_dotenv

    def _is_sensitive_key(self, key: str) -> bool:
        return any(pattern.search(key) for pattern in self._key_patterns)

    @staticmethod
    def _strip_wrapping_quotes(value: str) -> str:
        stripped = value.strip()
        if len(stripped) >= 2 and stripped[0] == stripped[-1] and stripped[0] in {"'", '"'}:
            return stripped[1:-1].strip()
        return stripped

    def _is_env_reference_value(self, value: Any) -> bool:
        if not isinstance(value, str):
            return False
        stripped = self._strip_wrapping_quotes(value)
        return any(pattern.fullmatch(stripped) for pattern in ENV_REFERENCE_PATTERNS)

    def _redact_sensitive_value(self, value: Any) -> tuple[Any, int]:
        if self._is_env_reference_value(value):
            return value, 0
        return REDACTED, 1

    def _redact_value(self, value: Any) -> tuple[Any, int]:
        if isinstance(value, dict):
            total = 0
            output: dict[str, Any] = {}
            for key, item in value.items():
                if self._is_sensitive_key(str(key)):
                    redacted_value, count = self._redact_sensitive_value(item)
                    output[str(key)] = redacted_value
                    total += count
                else:
                    redacted, count = self._redact_value(item)
                    output[str(key)] = redacted
                    total += count
            return output, total
        if isinstance(value, list):
            total = 0
            items: list[Any] = []
            for item in value:
                redacted, count = self._redact_value(item)
                items.append(redacted)
                total += count
            return items, total
        return value, 0

    def _parse_json(self, text: str, _: bytes) -> tuple[str, int]:
        data = json.loads(text)
        redacted, count = self._redact_value(data)
        return json.dumps(redacted, indent=2, sort_keys=True), count

    def _parse_yaml(self, text: str, _: bytes) -> tuple[str, int]:
        data = yaml.safe_load(text)
        redacted, count = self._redact_value(data)
        return yaml.safe_dump(redacted, sort_keys=False), count

    def _parse_toml(self, _: str, raw: bytes) -> tuple[str, int]:
        data = tomllib.loads(raw.decode("utf-8", errors="replace"))
        redacted, count = self._redact_value(data)
        return self._dump_toml(redacted), count

    def _parse_ini(self, text: str, _: bytes) -> tuple[str, int]:
        parser = configparser.ConfigParser()
        parser.optionxform = str
        parser.read_string(text)
        replacements = 0
        for section in parser.sections():
            for option in list(parser[section]):
                if self._is_sensitive_key(option):
                    value = parser[section][option]
                    if not self._is_env_reference_value(value):
                        parser[section][option] = REDACTED
                        replacements += 1
        from io import StringIO

        handle = StringIO()
        parser.write(handle)
        return handle.getvalue(), replacements

    def _parse_dotenv(self, text: str, _: bytes) -> tuple[str, int]:
        replacements = 0
        lines: list[str] = []
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in line:
                lines.append(line)
                continue
            key, value = line.split("=", 1)
            if self._is_sensitive_key(key.strip()):
                if self._is_env_reference_value(value):
                    lines.append(line)
                else:
                    lines.append(f"{key}={REDACTED}")
                    replacements += 1
            else:
                lines.append(line)
        return "\n".join(lines), replacements

    def _dump_toml(self, data: dict[str, Any]) -> str:
        lines: list[str] = []

        def render_scalar(value: Any) -> str:
            if isinstance(value, bool):
                return "true" if value else "false"
            if isinstance(value, (int, float)):
                return str(value)
            if isinstance(value, str):
                escaped = value.replace('"', '\\"')
                return f'"{escaped}"'
            if isinstance(value, list):
                return "[" + ", ".join(render_scalar(item) for item in value) + "]"
            return f'"{str(value)}"'

        def walk(prefix: list[str], value: dict[str, Any]) -> None:
            scalars: dict[str, Any] = {}
            tables: dict[str, Any] = {}
            for key, item in value.items():
                if isinstance(item, dict):
                    tables[key] = item
                else:
                    scalars[key] = item

            if prefix:
                lines.append(f"[{'.'.join(prefix)}]")
            for key, item in scalars.items():
                lines.append(f"{key} = {render_scalar(item)}")
            if scalars and tables:
                lines.append("")
            for index, (key, item) in enumerate(tables.items()):
                walk(prefix + [key], item)
                if index < len(tables) - 1:
                    lines.append("")

        walk([], data)
        return "\n".join(line for line in lines if line is not None).strip() + "\n"

    def _apply_text_redactions(self, text: str) -> tuple[str, int]:
        total = 0

        text, count = self._pem_pattern.subn(REDACTED, text)
        total += count

        def bearer_replacement(match: re.Match[str]) -> str:
            return f"{match.group(1)}{REDACTED}"

        text, count = self._bearer_pattern.subn(bearer_replacement, text)
        total += count

        assignment_count = 0

        def assignment_replacement(match: re.Match[str]) -> str:
            nonlocal assignment_count
            value = match.group(2)
            if self._is_env_reference_value(value):
                return match.group(0)
            if self._strip_wrapping_quotes(value) == REDACTED:
                return match.group(0)
            assignment_count += 1
            return f"{match.group(1)}{REDACTED}"

        text = self._assignment_pattern.sub(assignment_replacement, text)
        total += assignment_count

        def url_replacement(match: re.Match[str]) -> str:
            return f"{match.group(1)}{REDACTED}{match.group(3)}"

        text, count = self._url_secret_pattern.subn(url_replacement, text)
        total += count

        for pattern in self._extra_patterns:
            text, count = pattern.subn(REDACTED, text)
            total += count

        return text, total

    @staticmethod
    def _is_binary(data: bytes) -> bool:
        if not data:
            return False
        sample = data[:1024]
        if b"\x00" in sample:
            return True
        try:
            sample.decode("utf-8")
            return False
        except UnicodeDecodeError:
            return True
