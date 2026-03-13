import time

import pytest

from sysadmin_mcp_kit.result_store import InMemoryResultStore, ResultStoreError


def test_result_store_enforces_owner() -> None:
    store = InMemoryResultStore(ttl_seconds=60)
    result_id = store.put("owner-a", "file", {"content": "x"}, {"path": "/etc/app.conf"})

    assert store.get("owner-a", result_id).payload["content"] == "x"
    with pytest.raises(ResultStoreError):
        store.get("owner-b", result_id)


def test_result_store_expires_items() -> None:
    store = InMemoryResultStore(ttl_seconds=0)
    result_id = store.put("owner-a", "file", {"content": "x"}, {"path": "/etc/app.conf"})
    time.sleep(0.01)
    with pytest.raises(ResultStoreError):
        store.get("owner-a", result_id)
