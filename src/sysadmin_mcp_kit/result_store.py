from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any, Literal


class ResultStoreError(KeyError):
    """Raised when a stored result is missing or unavailable to a client."""


@dataclass
class StoredResult:
    result_id: str
    owner_id: str
    kind: Literal["file", "command"]
    payload: dict[str, str]
    metadata: dict[str, Any]
    created_at: float


class InMemoryResultStore:
    def __init__(self, ttl_seconds: int):
        self._ttl_seconds = ttl_seconds
        self._items: dict[str, StoredResult] = {}
        self._lock = threading.Lock()

    def _purge_expired_locked(self) -> None:
        threshold = time.time() - self._ttl_seconds
        expired = [key for key, item in self._items.items() if item.created_at < threshold]
        for key in expired:
            self._items.pop(key, None)

    def put(self, owner_id: str, kind: Literal["file", "command"], payload: dict[str, str], metadata: dict[str, Any]) -> str:
        result_id = uuid.uuid4().hex
        with self._lock:
            self._purge_expired_locked()
            self._items[result_id] = StoredResult(
                result_id=result_id,
                owner_id=owner_id,
                kind=kind,
                payload=payload,
                metadata=metadata,
                created_at=time.time(),
            )
        return result_id

    def get(self, owner_id: str, result_id: str, kind: Literal["file", "command"] | None = None) -> StoredResult:
        with self._lock:
            self._purge_expired_locked()
            result = self._items.get(result_id)
            if result is None:
                raise ResultStoreError(result_id)
            if result.owner_id != owner_id:
                raise ResultStoreError(result_id)
            if kind is not None and result.kind != kind:
                raise ResultStoreError(result_id)
            return result
