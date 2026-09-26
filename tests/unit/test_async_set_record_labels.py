"""Async miss-store stats parity (LAB-3755).

The sync wrapper records its miss-store as ``operation="set", serializer="rust"``.
The async wrapper's two miss-store sites — under the distributed lock and on the
no-lock fallback — recorded ``set`` without the label, so the Prometheus sink filed
async sets under ``serializer="unknown"``. Both async paths are pinned here through the real decorator stack; the
backend decides which path runs (``acquire_lock`` present → locked).
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from typing import Any

import pytest

from cachekit import cache
from cachekit.l1_cache import get_l1_cache_manager
from cachekit.reliability.async_metrics import AsyncMetricsCollector


class _ByteStore:
    """Plain byte store without ``acquire_lock``: drives the no-lock miss path."""

    def __init__(self) -> None:
        self.store: dict[str, bytes] = {}

    def get(self, key: str) -> bytes | None:
        return self.store.get(key)

    def set(self, key: str, value: bytes, ttl: int | None = None) -> None:
        self.store[key] = value

    def delete(self, key: str) -> bool:
        return self.store.pop(key, None) is not None

    def exists(self, key: str) -> bool:
        return key in self.store

    def health_check(self) -> tuple[bool, dict[str, Any]]:
        return True, {}


class _LockableByteStore(_ByteStore):
    """Byte store whose lock always acquires: drives the locked miss path."""

    @asynccontextmanager
    async def acquire_lock(self, key: str, timeout: float, blocking_timeout: float | None = None) -> AsyncIterator[bool]:
        yield True


@pytest.fixture(autouse=True)
def setup_di_for_redis_isolation() -> Iterator[None]:
    """Override the root conftest's Redis isolation: the backend is injected, no Redis needed.

    Keep its L1 clear — both parametrized cases share a cache key, and a leaked L1 entry
    turns the second case into an L1 hit that never reaches the miss-store site.
    """
    yield
    get_l1_cache_manager().clear_all()


@pytest.fixture
def recorded(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Capture every cache-operation metric record, keyword args as passed.

    Patched at the collector, the metrics sink: every record that reaches Prometheus passes here.
    """
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(AsyncMetricsCollector, "record_cache_operation", lambda self, **kw: calls.append(kw))
    return calls


@pytest.mark.unit
@pytest.mark.parametrize("backend_cls", [_ByteStore, _LockableByteStore], ids=["no-lock", "locked"])
async def test_async_miss_store_records_sync_set_labels(recorded: list[dict[str, Any]], backend_cls: type[_ByteStore]) -> None:
    backend = backend_cls()

    @cache(backend=backend, ttl=60, namespace="async-set-labels")
    async def compute() -> dict[str, int]:
        return {"answer": 42}

    assert await compute() == {"answer": 42}
    assert backend.store  # the miss reached L2, so a set record must exist

    assert len(recorded) == 1  # one miss, one record: the set
    assert (recorded[0]["operation"], recorded[0].get("serializer")) == ("set", "rust")  # what the sync miss-store passes
