"""Async hit-record stats parity (LAB-3765).

The sync wrapper records an L1 hit as ``serializer="l1_memory", hit=True`` and an L2
hit as ``serializer="rust", hit=True``, both with the served size; the async L1 and
uncontended L2 hit sites recorded none of those, so async hits filed under
``serializer="unknown"``. A miss primes L2 (and L1 via the miss-store); clearing L1
before the second call forces it past L1 to the L2 site.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

from cachekit import cache
from cachekit.decorators.orchestrator import FeatureOrchestrator
from cachekit.l1_cache import get_l1_cache_manager


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
    """Capture the wrapper's explicit features.record_cache_operation(...) calls, keyword args as passed.

    Patched at the orchestrator, not the collector: record_success() also forwards an
    operation-context record to the collector, which would shadow the labels under test.
    """
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(FeatureOrchestrator, "record_cache_operation", lambda self, **kw: calls.append(kw))
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

    gets = [c for c in recorded if c["operation"] == "get"]
    assert len(gets) == 1
    assert (gets[0].get("serializer"), gets[0].get("hit")) == (tier, True)  # what the sync hit sites pass
    # Exact served size, not just positive: both hit sites record the raw serialized
    # envelope's length — the L1 backfill stores the same bytes L2 returned, so the
    # single L2 store value is the ground truth for either tier. A `> 0` assertion
    # would pass on a wrong constant (e.g. size_bytes=1); this pins the real value.
    expected_size = len(next(iter(backend.store.values())))
    assert gets[0].get("size_bytes") == expected_size
