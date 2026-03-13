from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from typing import Any


class CursorError(ValueError):
    """Raised when a cursor cannot be decoded or does not match expectations."""


class CursorCodec:
    @staticmethod
    def encode(payload: dict[str, Any]) -> str:
        raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    @staticmethod
    def decode(cursor: str) -> dict[str, Any]:
        padding = "=" * (-len(cursor) % 4)
        try:
            raw = base64.urlsafe_b64decode(cursor + padding)
            payload = json.loads(raw.decode("utf-8"))
        except Exception as exc:  # pragma: no cover - defensive wrapper
            raise CursorError("Invalid cursor") from exc
        if not isinstance(payload, dict):
            raise CursorError("Cursor payload must be an object")
        return payload


@dataclass(frozen=True)
class TextPage:
    text: str
    start_line: int
    end_line: int
    returned_lines: int
    total_lines: int
    next_index: int | None
    truncated_by_page: bool


@dataclass(frozen=True)
class ListPage[T]:
    items: list[T]
    next_index: int | None
    total_items: int


class Paginator:
    @staticmethod
    def paginate_lines(text: str, start_line: int, page_lines: int, char_limit: int) -> TextPage:
        lines = text.splitlines()
        total_lines = len(lines)
        selected: list[str] = []
        current_chars = 0
        index = max(0, start_line)

        while index < total_lines and len(selected) < page_lines:
            line = lines[index]
            line_chars = len(line) + (1 if selected else 0)
            if selected and current_chars + line_chars > char_limit:
                break
            if not selected and len(line) > char_limit:
                selected.append(line[:char_limit])
                current_chars = len(selected[0])
                index += 1
                break
            selected.append(line)
            current_chars += line_chars
            index += 1

        text_page = "\n".join(selected)
        next_index = index if index < total_lines else None
        return TextPage(
            text=text_page,
            start_line=start_line,
            end_line=start_line + len(selected),
            returned_lines=len(selected),
            total_lines=total_lines,
            next_index=next_index,
            truncated_by_page=next_index is not None,
        )

    @staticmethod
    def paginate_items(items: list[T], start_index: int, limit: int) -> ListPage[T]:
        offset = max(0, start_index)
        window = items[offset : offset + limit]
        next_index = offset + limit if offset + limit < len(items) else None
        return ListPage(items=window, next_index=next_index, total_items=len(items))
