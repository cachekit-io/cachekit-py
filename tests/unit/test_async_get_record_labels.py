"""Async hit-record stats parity.

The sync wrapper records an L1 hit as ``serializer="l1_memory"`` and an L2
hit as ``serializer="rust"``, both with the served size; the async L1 and
uncontended L2 hit sites recorded none of those, so async hits filed under
``serializer="unknown"``. A miss primes L2 (and L1 via the miss-store); clearing L1
before the second call forces it past L1 to the L2 site.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

from cachekit import cache
from cachekit.l1_cache import get_l1_cache, get_l1_cache_manager
from cachekit.reliability.async_metrics import AsyncMetricsCollector


class _ByteStore:
    """Plain in-memory byte store standing in for L2."""

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


@pytest.fixture(autouse=True)
def setup_di_for_redis_isolation() -> Iterator[None]:
    """L1 hygiene between the two cases: tests/unit/conftest.py's no-op override of the root
    fixture dropped its clear_all(), and a leaked entry would turn the L2 case's priming
    miss into an L1 hit."""
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
@pytest.mark.parametrize("tier", ["l1_memory", "rust"], ids=["l1-hit", "l2-hit"])
async def test_async_get_hit_records_sync_labels(recorded: list[dict[str, Any]], tier: str) -> None:
    backend = _ByteStore()

    @cache(backend=backend, ttl=60, namespace="async-get-labels")
    async def compute() -> dict[str, int]:
        return {"answer": 42}

    assert await compute() == {"answer": 42}  # miss: primes L2, and L1 via the miss-store
    assert backend.store
    if tier == "rust":
        get_l1_cache_manager().clear_all()  # force the hit past L1 to the L2 site
    recorded.clear()

    assert await compute() == {"answer": 42}

    assert len(recorded) == 1  # one hit, one record
    assert (recorded[0]["operation"], recorded[0].get("serializer")) == ("get", tier)  # what the sync hit sites pass
    # Exact served size, not just positive: each hit site records the length of the bytes it read,
    # the L2 envelope for an L2 hit and the L1 entry for an L1 hit. This one's L1 entry is its
    # verified MessagePack, shorter than the envelope (CacheSerializationHandler.l1_entry). A `> 0`
    # assertion would pass on a wrong constant (e.g. size_bytes=1); this pins the real value.
    ((key, envelope),) = backend.store.items()
    l1_entry = get_l1_cache("async-get-labels").peek(key)
    assert l1_entry is not None and len(l1_entry) < len(envelope)
    expected_size = len(envelope) if tier == "rust" else len(l1_entry)
    assert recorded[0].get("size_bytes") == expected_size
