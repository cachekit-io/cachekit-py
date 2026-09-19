"""Async lock double-check L2 hit-record parity (LAB-3769).

The uncontended async L2 hit site records get/serializer="rust"/hit=True telemetry
(LAB-3765); the two post-lock ``_l2_double_check`` hit returns did not — so a
thundering-herd hit, filled by another worker while this one waited on the
distributed lock, was invisible to ``cache_operations_total`` and ``cache_info()``
L2 stats. That is exactly the traffic the lock exists to absorb.

Reproduces contention by patching the backend's ``get`` to miss once (forcing the
wrapper past the pre-lock check into the lock path) then hit on the next call —
the double-check read standing in for "another request filled the cache while we
waited". Parametrised over the lock outcome so both hit returns are covered: the
lock-acquired branch and the lock-timeout branch, which record via the same
``_record_l2_hit_async`` helper but are reached by different control flow.

``_LockableByteStore`` is defined locally rather than imported from
tests/unit/test_async_set_record_labels.py (cachekit-py#295): that file does not
exist on `main` yet. Hoist to a shared fixture once #295 merges.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import pytest

from cachekit import cache
from cachekit.decorators.orchestrator import FeatureOrchestrator


class _LockableByteStore:
    """In-memory byte store implementing LockableBackend.

    ``lock_acquired`` selects which double-check hit return is exercised: True
    takes the lock-acquired branch, False the lock-timeout branch.
    """

    def __init__(self, *, lock_acquired: bool = True) -> None:
        self.store: dict[str, bytes] = {}
        self._lock_acquired = lock_acquired

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

    @asynccontextmanager
    async def acquire_lock(self, key: str, timeout: float, blocking_timeout: float | None = None) -> AsyncIterator[bool]:
        yield self._lock_acquired


@pytest.fixture
def recorded(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Capture the wrapper's explicit features.record_cache_operation(...) calls.

    Patched at the orchestrator, not the collector: record_success() also forwards
    an operation-context record to the collector, which would shadow the labels
    under test (same rationale as test_async_get_record_labels.py).
    """
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(FeatureOrchestrator, "record_cache_operation", lambda self, **kw: calls.append(kw))
    return calls


@pytest.mark.unit
@pytest.mark.parametrize("lock_acquired", [True, False], ids=["lock-acquired", "lock-timeout"])
async def test_async_l2_double_check_hit_records_get(recorded: list[dict[str, Any]], lock_acquired: bool) -> None:
    backend = _LockableByteStore(lock_acquired=lock_acquired)

    @cache(backend=backend, ttl=60, namespace="async-dc-labels", l1_enabled=False)
    async def compute() -> dict[str, int]:
        return {"answer": 42}

    assert await compute() == {"answer": 42}  # miss: primes L2 with the real envelope
    assert backend.store
    expected_size = len(next(iter(backend.store.values())))

    # Make the pre-lock L2 check miss exactly once so the wrapper falls into the
    # lock/double-check path; the primed value is still in the real store, so the
    # double-check read inside the lock finds it — the contended-hit scenario.
    real_get = backend.get
    call_count = 0

    def patched_get(key: str) -> bytes | None:
        nonlocal call_count
        call_count += 1
        return None if call_count == 1 else real_get(key)

    backend.get = patched_get  # type: ignore[method-assign]
    recorded.clear()

    assert await compute() == {"answer": 42}
    assert call_count >= 2  # pre-lock miss, then the double-check hit

    gets = [c for c in recorded if c["operation"] == "get"]
    assert len(gets) == 1
    assert (gets[0].get("serializer"), gets[0].get("hit")) == ("rust", True)
    assert gets[0].get("size_bytes") == expected_size
    # The double-check read is timed on its own window, so a duration is always
    # recorded — a regression that drops it would still pass the label asserts.
    assert isinstance(gets[0].get("duration_ms"), float)
    assert gets[0]["duration_ms"] >= 0.0
