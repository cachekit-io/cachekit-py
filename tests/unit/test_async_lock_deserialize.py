"""Test async distributed locking deserialize_data paths in wrapper.py.

Targets the two uncovered lines where cache_key=cache_key was added:
- Line ~1106: Lock acquired, double-check finds cache populated
- Line ~1131: Lock timeout, cache populated while waiting

These are the "thundering herd" protection paths — when multiple concurrent
requests miss cache simultaneously, only one executes the function while
others wait and then find the cache populated.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import pytest

from cachekit import cache


class LockableTestBackend:
    """Backend with LockableBackend protocol for testing lock paths.

    Controls lock acquisition result and simulates cache population
    during lock wait.
    """

    def __init__(self, *, lock_acquired: bool = True):
        self._store: dict[str, bytes] = {}
        self._lock_acquired = lock_acquired
        self._get_call_count = 0

    def get(self, key: str) -> bytes | None:
        return self._store.get(key)

    def set(self, key: str, value: bytes, ttl: int | None = None) -> None:
        self._store[key] = value

    def delete(self, key: str) -> bool:
        return self._store.pop(key, None) is not None

    def exists(self, key: str) -> bool:
        return key in self._store

    def health_check(self) -> tuple[bool, dict[str, Any]]:
        return True, {"backend_type": "lockable_test"}

    @asynccontextmanager
    async def acquire_lock(
        self,
        key: str,
        timeout: float = 10.0,
        blocking_timeout: float | None = None,
    ) -> AsyncIterator[bool]:
        yield self._lock_acquired


@pytest.fixture(autouse=True)
def setup_di_for_redis_isolation():
    """Override root conftest's Redis isolation."""
    yield


@pytest.mark.asyncio
class TestAsyncLockDeserializePaths:
    """Test the two async lock paths where deserialize_data gets cache_key.

    Strategy: Patch the backend's get() to return None on the first call
    (triggering lock acquisition) and real cached data on the second call
    (inside the lock, simulating another request populating the cache).
    """

    async def test_lock_acquired_cache_populated_during_wait(self):
        """Line ~1106: Lock acquired, double-check finds cache populated.

        Flow: L2 miss → acquire lock → check L2 again → find data → deserialize(cache_key=)
        """
        backend = LockableTestBackend(lock_acquired=True)

        @cache(backend=backend, ttl=300, l1_enabled=False)
        async def expensive_fn(x: int) -> dict:
            return {"result": x * 2}

        # First call populates the cache normally
        result1 = await expensive_fn(42)
        assert result1["result"] == 84

        # Manipulate the backend to return None on first get (cache miss → triggers
        # lock acquisition), then real data on second get (inside lock → deserialize
        # with cache_key, hitting the target coverage line).
        call_count = 0
        original_get = backend.get

        def patched_get(key: str) -> bytes | None:
            nonlocal call_count
            call_count += 1
            if call_count <= 1:
                return None  # First L2 check: miss → triggers lock
            return original_get(key)  # Inside lock: hit → deserialize with cache_key

        backend.get = patched_get

        result2 = await expensive_fn(42)
        assert result2["result"] == 84
        # Should have gone through the lock-acquired path
        assert call_count >= 2

    async def test_lock_timeout_cache_populated_during_wait(self):
        """Line ~1131: Lock timeout, cache populated while waiting.

        Flow: L2 miss → lock timeout (not acquired) → check L2 again → find data → deserialize(cache_key=)
        """
        backend = LockableTestBackend(lock_acquired=False)  # Lock will timeout

        @cache(backend=backend, ttl=300, l1_enabled=False)
        async def expensive_fn(x: int) -> dict:
            return {"result": x * 2}

        # First call populates the cache normally
        result1 = await expensive_fn(42)
        assert result1["result"] == 84

        # Now make the outer L2 check miss, but the lock-timeout check find data
        call_count = 0
        original_get = backend.get

        def patched_get(key: str) -> bytes | None:
            nonlocal call_count
            call_count += 1
            if call_count <= 1:
                return None  # First L2 check: miss → triggers lock attempt
            return original_get(key)  # Lock timeout check: hit → deserialize with cache_key

        backend.get = patched_get

        result2 = await expensive_fn(42)
        assert result2["result"] == 84
        assert call_count >= 2
