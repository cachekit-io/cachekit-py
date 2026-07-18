"""L1-only mode (backend=None) honors L1CacheConfig SWR + size config.

Regression tests for cachekit-py#207: with backend=None the decorator used to
route through an ObjectCache that ignored every L1CacheConfig field — SWR never
scheduled a background refresh and max_size_mb was dead (the store was
entry-count-bounded). These tests pin the fixed behavior:

- swr_enabled=True + ttl schedules a non-blocking background refresh past
  ttl * swr_threshold_ratio (asyncio task for async functions, daemon thread
  for sync functions)
- max_size_mb bounds bytes, not entry count

No Redis or external services required.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from cachekit import cache
from cachekit.config import L1CacheConfig


async def _wait_for_calls(get_calls, expected: int, timeout: float = 2.0) -> None:
    """Poll until the call counter reaches ``expected`` (refresh is fire-and-forget)."""
    deadline = time.monotonic() + timeout
    while get_calls() < expected and time.monotonic() < deadline:
        await asyncio.sleep(0.02)


@pytest.mark.unit
class TestL1OnlySWRAsync:
    """SWR background refresh for async functions in L1-only mode."""

    async def test_issue_207_repro_background_refresh_happens(self):
        """Exact repro from #207: call count reaches 2 after the SWR window passes."""
        calls = 0

        @cache(ttl=2, backend=None, l1=L1CacheConfig(swr_enabled=True, swr_threshold_ratio=0.2))
        async def fn():
            nonlocal calls
            calls += 1
            return calls

        assert await fn() == 1  # miss -> executes
        await asyncio.sleep(1.0)  # past 20% (±10% jitter) of ttl=2
        assert await fn() == 1  # serves cached value, schedules background refresh
        await _wait_for_calls(lambda: calls, 2)
        assert calls == 2, f"no background refresh happened (calls={calls})"

    async def test_stale_serve_does_not_block_caller(self):
        """The hit that triggers a refresh returns the stale value without awaiting it."""
        calls = 0

        @cache(ttl=2, backend=None, l1=L1CacheConfig(swr_enabled=True, swr_threshold_ratio=0.2))
        async def fn():
            nonlocal calls
            calls += 1
            if calls > 1:
                await asyncio.sleep(0.5)  # slow refresh must not delay the caller
            return calls

        assert await fn() == 1
        await asyncio.sleep(0.6)

        start = time.perf_counter()
        result = await fn()
        elapsed = time.perf_counter() - start

        assert result == 1  # stale value served
        assert elapsed < 0.25, f"caller blocked on refresh ({elapsed:.3f}s)"
        await _wait_for_calls(lambda: calls, 2)
        assert calls == 2

    async def test_refreshed_value_served_after_refresh_completes(self):
        """Once the background refresh lands, subsequent hits serve the new value."""
        calls = 0

        @cache(ttl=2, backend=None, l1=L1CacheConfig(swr_enabled=True, swr_threshold_ratio=0.2))
        async def fn():
            nonlocal calls
            calls += 1
            return calls

        assert await fn() == 1
        await asyncio.sleep(0.6)
        assert await fn() == 1  # stale served, refresh scheduled
        await _wait_for_calls(lambda: calls, 2)
        assert await fn() == 2  # refreshed value now served from cache
        assert calls == 2  # ... without another execution

    async def test_swr_disabled_no_background_refresh(self):
        """swr_enabled=False must never schedule a refresh."""
        calls = 0

        @cache(ttl=2, backend=None, l1=L1CacheConfig(swr_enabled=False))
        async def fn():
            nonlocal calls
            calls += 1
            return calls

        assert await fn() == 1
        await asyncio.sleep(1.2)  # well past any threshold, before hard expiry
        assert await fn() == 1
        await asyncio.sleep(0.2)
        assert calls == 1

    async def test_swr_without_ttl_serves_cached_without_refresh(self):
        """SWR needs a ttl — with ttl=None entries never go stale, so no refresh."""
        calls = 0

        @cache(backend=None, l1=L1CacheConfig(swr_enabled=True, swr_threshold_ratio=0.2))
        async def fn():
            nonlocal calls
            calls += 1
            return calls

        assert await fn() == 1
        await asyncio.sleep(0.3)
        assert await fn() == 1
        await asyncio.sleep(0.2)
        assert calls == 1

    async def test_failing_refresh_keeps_serving_stale_value(self):
        """A refresh that raises is swallowed (logged) and the stale value survives."""
        calls = 0

        @cache(ttl=5, backend=None, l1=L1CacheConfig(swr_enabled=True, swr_threshold_ratio=0.1))
        async def fn():
            nonlocal calls
            calls += 1
            if calls > 1:
                raise RuntimeError("refresh boom")
            return calls

        assert await fn() == 1
        await asyncio.sleep(0.7)
        assert await fn() == 1  # triggers a refresh that will fail
        await _wait_for_calls(lambda: calls, 2)
        assert calls == 2
        await asyncio.sleep(0.05)  # let the failed task's done-callback run
        assert await fn() == 1  # stale value still served, caller unaffected


@pytest.mark.unit
class TestL1OnlySWRSync:
    """SWR background refresh for sync functions in L1-only mode (daemon thread)."""

    def test_sync_function_background_refresh_via_thread(self):
        """Sync functions get SWR too — refreshed on a daemon thread, not an error."""
        calls = 0

        @cache(ttl=2, backend=None, l1=L1CacheConfig(swr_enabled=True, swr_threshold_ratio=0.2))
        def fn():
            nonlocal calls
            calls += 1
            return calls

        assert fn() == 1
        time.sleep(1.0)
        assert fn() == 1  # stale served, refresh scheduled on a thread

        deadline = time.monotonic() + 2.0
        while calls < 2 and time.monotonic() < deadline:
            time.sleep(0.02)
        assert calls == 2, f"no background refresh happened (calls={calls})"

    def test_sync_failing_refresh_is_swallowed(self):
        """A failing sync refresh must not propagate into any caller."""
        calls = 0

        @cache(ttl=5, backend=None, l1=L1CacheConfig(swr_enabled=True, swr_threshold_ratio=0.1))
        def fn():
            nonlocal calls
            calls += 1
            if calls > 1:
                raise RuntimeError("refresh boom")
            return calls

        assert fn() == 1
        time.sleep(0.7)
        assert fn() == 1  # triggers failing refresh

        deadline = time.monotonic() + 2.0
        while calls < 2 and time.monotonic() < deadline:
            time.sleep(0.02)
        assert calls == 2
        assert fn() == 1  # stale value still served


@pytest.mark.unit
class TestL1OnlySizeBound:
    """max_size_mb is a byte bound in L1-only mode, not an entry count."""

    def test_max_size_mb_bounds_bytes_not_entry_count(self):
        """Two ~700KB values under max_size_mb=1 evict by byte pressure at 2 entries."""
        calls = 0

        @cache(ttl=60, backend=None, l1=L1CacheConfig(max_size_mb=1, swr_enabled=False))
        def fn(i: int) -> str:
            nonlocal calls
            calls += 1
            return "x" * (700 * 1024)

        fn(1)  # cached (~700KB)
        fn(2)  # ~1.4MB total > 1MB -> LRU-evicts the i=1 entry
        assert calls == 2
        fn(2)  # MRU entry survived the eviction
        assert calls == 2
        fn(1)  # evicted at only 2 entries (far below any entry-count bound) -> re-executes
        assert calls == 3

    def test_oversized_value_returned_but_never_cached(self):
        """A single value larger than max_size_mb is returned but not stored."""
        calls = 0

        @cache(ttl=60, backend=None, l1=L1CacheConfig(max_size_mb=1, swr_enabled=False))
        def fn() -> str:
            nonlocal calls
            calls += 1
            return "x" * (2 * 1024 * 1024)

        assert len(fn()) == 2 * 1024 * 1024
        assert len(fn()) == 2 * 1024 * 1024
        assert calls == 2  # never cached — every call executes

    def test_small_values_cached_normally_under_byte_bound(self):
        """Values comfortably within the budget still hit as before."""
        calls = 0

        @cache(ttl=60, backend=None, l1=L1CacheConfig(max_size_mb=1, swr_enabled=False))
        def fn(i: int) -> str:
            nonlocal calls
            calls += 1
            return f"value-{i}"

        assert fn(1) == "value-1"
        assert fn(1) == "value-1"
        assert calls == 1
