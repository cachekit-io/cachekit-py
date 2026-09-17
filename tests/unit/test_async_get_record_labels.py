"""Async hit-record stats parity (LAB-3765).

The sync wrapper records an L1 hit as ``operation="get", serializer="l1_memory",
hit=True`` and an L2 hit as ``serializer="rust", hit=True``, both with the served
payload size. The async wrapper's two hit sites recorded ``get`` with none of
those, so on a hit-heavy async workload most ``get`` traffic filed under
``serializer="unknown"``. Both async tiers are pinned here through the real
decorator stack: a miss primes L2 (and L1 via backfill); clearing L1 before the
second call forces it past L1 to the L2 site.
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
    """Override the root conftest's Redis isolation: the backend is injected, no Redis needed.

    Keep L1 clear between cases — both share a cache key, and a leaked L1 entry would
    turn the L2 case's priming miss into an L1 hit.
    """
    get_l1_cache_manager().clear_all()
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
    assert gets[0].get("size_bytes", 0) > 0
