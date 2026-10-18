"""``cache_info().misses`` counts a backed miss once, sync and async (LAB-8298).

A miss is a call that runs the wrapped function because no cached value was found.
The async wrapper's backed miss path never counted one, on either the locked or the
no-lock branch, so an async function's hit ratio read 100%. A locked caller whose
double-check finds a value another caller stored did not run the function: it counts
as an L2 hit, not a miss.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from typing import Any

import pytest

from cachekit import cache
from cachekit.l1_cache import get_l1_cache_manager

VALUE = {"answer": 42}


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
    """Byte store with a lock; ``lock_acquired`` False takes the lock-timeout branch."""

    def __init__(self, *, lock_acquired: bool = True) -> None:
        super().__init__()
        self._lock_acquired = lock_acquired

    @asynccontextmanager
    async def acquire_lock(self, key: str, timeout: float, blocking_timeout: float | None = None) -> AsyncIterator[bool]:
        yield self._lock_acquired


@pytest.fixture(autouse=True)
def setup_di_for_redis_isolation() -> Iterator[None]:
    """Override the root conftest's Redis isolation: the backend is injected, no Redis needed.

    Keep L1 clear so a parametrized case never starts on an L1 hit.
    """
    yield
    get_l1_cache_manager().clear_all()


def _deltas(before: Any, after: Any) -> tuple[int, int, int]:
    return (after.misses - before.misses, after.l1_hits - before.l1_hits, after.l2_hits - before.l2_hits)


@pytest.mark.unit
def test_sync_miss_l1_hit_l2_hit_each_count_once() -> None:
    backend = _ByteStore()

    @cache(backend=backend, ttl=60, namespace="miss-count-sync")
    def compute() -> dict[str, int]:
        return dict(VALUE)

    before = compute.cache_info()  # type: ignore[attr-defined]  # stats are per function, shared across runs
    assert compute() == VALUE  # miss that stores
    assert compute() == VALUE  # L1 hit
    l2_snapshot = dict(backend.store)
    compute.invalidate_cache()  # type: ignore[attr-defined]
    backend.store.update(l2_snapshot)
    assert compute() == VALUE  # L2 hit

    assert _deltas(before, compute.cache_info()) == (1, 1, 1)  # type: ignore[attr-defined]


@pytest.mark.unit
@pytest.mark.parametrize("backend_cls", [_ByteStore, _LockableByteStore], ids=["no-lock", "locked"])
async def test_async_miss_l1_hit_l2_hit_each_count_once(backend_cls: type[_ByteStore]) -> None:
    backend = backend_cls()

    @cache(backend=backend, ttl=60, namespace="miss-count-async")
    async def compute() -> dict[str, int]:
        return dict(VALUE)

    before = compute.cache_info()  # type: ignore[attr-defined]
    assert await compute() == VALUE  # miss that stores
    assert backend.store
    assert await compute() == VALUE  # L1 hit
    l2_snapshot = dict(backend.store)
    await compute.invalidate_cache()  # type: ignore[attr-defined]
    backend.store.update(l2_snapshot)
    assert await compute() == VALUE  # L2 hit

    assert _deltas(before, compute.cache_info()) == (1, 1, 1)  # type: ignore[attr-defined]


@pytest.mark.unit
@pytest.mark.parametrize("lock_acquired", [True, False], ids=["lock-acquired", "lock-timeout"])
async def test_async_double_check_hit_is_an_l2_hit_not_a_miss(lock_acquired: bool) -> None:
    backend = _LockableByteStore(lock_acquired=lock_acquired)
    calls = 0

    @cache(backend=backend, ttl=60, namespace="miss-count-double-check", l1_enabled=False)
    async def compute() -> dict[str, int]:
        nonlocal calls
        calls += 1
        return dict(VALUE)

    assert await compute() == VALUE  # primes L2 with the real envelope
    assert calls == 1

    # The pre-lock read misses once; the double-check read finds the primed value,
    # standing in for another caller that stored it while this one waited.
    real_get = backend.get
    gets = 0

    def patched_get(key: str) -> bytes | None:
        nonlocal gets
        gets += 1
        return None if gets == 1 else real_get(key)

    backend.get = patched_get  # type: ignore[method-assign]
    before = compute.cache_info()  # type: ignore[attr-defined]

    assert await compute() == VALUE
    assert (calls, gets) == (1, 2)  # served by the double-check, function not run
    assert _deltas(before, compute.cache_info()) == (0, 0, 1)  # type: ignore[attr-defined]
