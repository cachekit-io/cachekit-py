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
import copy
import logging
import threading
import time
from collections.abc import Callable, Coroutine
from typing import Any

import pytest

from cachekit import cache
from cachekit.config import L1CacheConfig
from tests.unit.test_swr_decorator import _close_loop, _collect_pruned, _resume


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
class TestL1OnlyDisabled:
    """backend=None + L1CacheConfig(enabled=False) must not cache at all."""

    def test_sync_no_caching_when_l1_disabled(self):
        calls = 0

        @cache(ttl=60, backend=None, l1=L1CacheConfig(enabled=False))
        def fn():
            nonlocal calls
            calls += 1
            return calls

        assert fn() == 1
        assert fn() == 2  # every call executes — nothing was cached
        assert calls == 2

    async def test_async_no_caching_when_l1_disabled(self):
        calls = 0

        @cache(ttl=60, backend=None, l1=L1CacheConfig(enabled=False))
        async def fn():
            nonlocal calls
            calls += 1
            return calls

        assert await fn() == 1
        assert await fn() == 2
        assert calls == 2


@pytest.mark.unit
class TestL1OnlySWRArgumentSnapshot:
    """The background refresh must see the arguments as they were at call time.

    The cache key is computed before the refresh is scheduled; if the caller
    mutates an argument after receiving the stale value, an un-snapshotted
    refresh would compute from the new state and store it under the old key.
    """

    async def test_async_refresh_uses_snapshot_not_live_args(self):
        seen: list[dict] = []

        @cache(ttl=2, backend=None, l1=L1CacheConfig(swr_enabled=True, swr_threshold_ratio=0.2))
        async def fn(payload: dict):
            seen.append(copy.deepcopy(payload))
            return dict(payload)

        payload = {"v": 1}
        assert await fn(payload) == {"v": 1}  # miss -> executes
        await asyncio.sleep(0.6)
        assert await fn(payload) == {"v": 1}  # stale hit -> refresh scheduled (snapshot taken)
        payload["v"] = 999  # caller mutates BEFORE the refresh task first runs

        deadline = time.monotonic() + 2.0
        while len(seen) < 2 and time.monotonic() < deadline:
            await asyncio.sleep(0.02)
        assert len(seen) == 2, "no background refresh happened"
        assert seen[1] == {"v": 1}, f"refresh saw the caller's mutation: {seen[1]}"

    def test_sync_refresh_uses_snapshot_not_live_args(self):
        seen: list[dict] = []
        release = threading.Event()

        @cache(ttl=2, backend=None, l1=L1CacheConfig(swr_enabled=True, swr_threshold_ratio=0.2))
        def fn(payload: dict):
            if seen:  # only the refresh call waits, so the mutation happens first
                release.wait(timeout=2.0)
            seen.append(copy.deepcopy(payload))
            return dict(payload)

        payload = {"v": 1}
        assert fn(payload) == {"v": 1}
        time.sleep(1.0)
        assert fn(payload) == {"v": 1}  # snapshot taken synchronously before this returns
        payload["v"] = 999
        release.set()  # now let the refresh thread read its (copied) argument

        deadline = time.monotonic() + 2.0
        while len(seen) < 2 and time.monotonic() < deadline:
            time.sleep(0.02)
        assert len(seen) == 2, "no background refresh happened"
        assert seen[1] == {"v": 1}, f"refresh saw the caller's mutation: {seen[1]}"


@pytest.mark.unit
class TestL1OnlySWRBoundedConcurrency:
    """Background refresh concurrency is capped (32 per wrapped function).

    Per-key suppression alone would spawn one task per distinct stale key. At
    capacity the refresh is skipped (stale keeps being served) and the per-key
    marker is released so a later hit retries.
    """

    async def test_async_refreshes_capped_at_32_distinct_stale_keys(self):
        n_keys = 40
        refresh_calls = 0

        @cache(ttl=2, backend=None, l1=L1CacheConfig(swr_enabled=True, swr_threshold_ratio=0.2))
        async def fn(i: int):
            nonlocal refresh_calls
            refresh_calls += 1
            return i

        for i in range(n_keys):  # seed
            assert await fn(i) == i
        assert refresh_calls == n_keys

        await asyncio.sleep(1.0)  # everything stale, nothing hard-expired

        # The wrapper's hit path has no await points, so all 40 stale hits
        # reserve slots before any refresh task gets to run: exactly 32 slots
        # grant, 8 are rejected (marker released for a later retry).
        for i in range(n_keys):
            assert await fn(i) == i  # stale value served either way

        deadline = time.monotonic() + 3.0
        while refresh_calls < n_keys + 32 and time.monotonic() < deadline:
            await asyncio.sleep(0.02)
        await asyncio.sleep(0.1)  # settle: catch any over-cap stragglers
        assert refresh_calls == n_keys + 32, f"expected exactly 32 refreshes, got {refresh_calls - n_keys}"


@pytest.mark.unit
class TestL1OnlySWRStoppedLoop:
    """LAB-7295: an async refresh left pending on an event loop that stopped (sync code calling
    run_until_complete) or closed without cancelling its tasks keeps its slot and key for the
    hold; the next refresh attempt on any key then releases both and cancels it."""

    _L1 = L1CacheConfig(swr_enabled=True, swr_threshold_ratio=0.01)  # stale after ~0.1 s of ttl=10

    def _seeded_fn(self, n_keys: int, starts: list[int], park: Callable[[], bool]) -> Any:
        """An L1-only async function with n_keys entries, all stale; a refresh logs its key in
        starts and, while park() says so, never answers."""
        seeded: set[int] = set()

        @cache(ttl=10, backend=None, l1=self._L1)
        async def fn(x: int) -> int:
            if x in seeded:  # a refresh
                starts.append(x)
                if park():
                    await asyncio.sleep(3600)
            seeded.add(x)
            return x

        for x in range(n_keys):
            asyncio.run(fn(x))  # misses: seed
        time.sleep(0.2)  # past ttl * ratio, with jitter
        return fn

    @staticmethod
    def _hit(fn: Any, *keys: int) -> Coroutine[Any, Any, None]:
        async def hit() -> None:
            for x in keys:
                assert await fn(x) == x
            for _ in range(5):
                await asyncio.sleep(0)

        return hit()

    @pytest.mark.parametrize("close", [False, True], ids=["stopped", "closed"])
    def test_refresh_on_stopped_loop_frees_slot_and_key(self, monkeypatch: pytest.MonkeyPatch, close: bool) -> None:
        import cachekit.decorators.wrapper as wrapper_mod

        monkeypatch.setattr(wrapper_mod, "_L1_SWR_MAX_CONCURRENT_REFRESHES", 1)
        monkeypatch.setattr(wrapper_mod, "_SWR_STOPPED_LOOP_HOLD_SECONDS", 3600.0)
        starts: list[int] = []
        fn = self._seeded_fn(2, starts, park=lambda: len(starts) == 1)  # only the first refresh parks
        stopped = asyncio.new_event_loop()
        try:
            stopped.run_until_complete(self._hit(fn, 0))  # key 0's refresh parks on a loop then left stopped
            if close:
                stopped.close()  # without cancelling it
            asyncio.run(self._hit(fn, 1))  # within the hold the parked refresh keeps the only slot
            assert starts == [0]
            monkeypatch.setattr(wrapper_mod, "_SWR_STOPPED_LOOP_HOLD_SECONDS", 0.0)
            asyncio.run(self._hit(fn, 1))  # past the hold it holds no slot
            asyncio.run(self._hit(fn, 0))  # and the prune released key 0's in-flight marker
            assert starts == [0, 1, 0]
            if close:
                _collect_pruned(monkeypatch)
        finally:
            _close_loop(stopped)

    def test_refreshes_pruned_from_stopped_loops_do_not_resume(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Loops that resume after pausing between run_until_complete calls run no more
        refreshes than the pool holds: the pruned ones were cancelled."""
        import cachekit.decorators.wrapper as wrapper_mod

        monkeypatch.setattr(wrapper_mod, "_L1_SWR_MAX_CONCURRENT_REFRESHES", 2)
        monkeypatch.setattr(wrapper_mod, "_SWR_STOPPED_LOOP_HOLD_SECONDS", 0.0)
        fn = self._seeded_fn(6, [], park=lambda: True)
        loops = [asyncio.new_event_loop() for _ in range(3)]
        try:
            for i, loop in enumerate(loops):
                loop.run_until_complete(self._hit(fn, 2 * i, 2 * i + 1))  # each pauses with its refreshes pending
            assert sum(len(loop.run_until_complete(_resume())) for loop in loops) == 2  # the pool's 2, not 6
        finally:
            for loop in loops:
                _close_loop(loop)

    def test_pruned_refresh_that_resumes_frees_nothing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A pruned refresh whose loop runs again neither raises nor settles key state: a newer
        refresh of the same entry shares its version, so failing its own refresh would release
        the newer one's marker. Nor does it give back the slot the newer one holds."""
        import cachekit.decorators.wrapper as wrapper_mod
        from cachekit.object_cache import ObjectCache

        monkeypatch.setattr(wrapper_mod, "_L1_SWR_MAX_CONCURRENT_REFRESHES", 1)
        monkeypatch.setattr(wrapper_mod, "_SWR_STOPPED_LOOP_HOLD_SECONDS", 0.0)
        settled: list[str] = []

        def spy(name: str) -> Callable[..., Any]:
            real = getattr(ObjectCache, name)

            def record(self: ObjectCache, *args: Any, **kwargs: Any) -> Any:
                settled.append(name)
                return real(self, *args, **kwargs)

            return record

        for name in ("fail_refresh", "complete_refresh"):
            monkeypatch.setattr(ObjectCache, name, spy(name))
        starts: list[int] = []
        fn = self._seeded_fn(2, starts, park=lambda: True)
        loop_a, loop_b, loop_c = (asyncio.new_event_loop() for _ in range(3))
        try:
            loop_a.run_until_complete(self._hit(fn, 0))  # A parks on key 0
            loop_b.run_until_complete(self._hit(fn, 1))  # prunes A (key 0 released); B parks on key 1
            loop_c.run_until_complete(self._hit(fn, 0))  # prunes B; C refreshes key 0 at A's version
            assert starts == [0, 1, 0]
            monkeypatch.setattr(wrapper_mod, "_SWR_STOPPED_LOOP_HOLD_SECONDS", 3600.0)
            assert loop_a.run_until_complete(_resume()) == []  # A and B resume and end cancelled
            assert loop_b.run_until_complete(_resume()) == []
            assert settled == []  # without touching C's marker
            asyncio.run(self._hit(fn, 1))  # C still holds the only slot
            assert starts == [0, 1, 0]
        finally:
            for loop in (loop_a, loop_b, loop_c):
                _close_loop(loop)


@pytest.mark.unit
class TestL1OnlySWRFailureWarnings:
    """A refresh that fails or never runs reaches a default-level log: the caller never sees it.

    Without the WARNING, a function whose upstream is down, or whose arguments cannot be
    snapshotted, silently serves the cached value until ttl and then recomputes in the foreground.
    """

    _L1 = L1CacheConfig(swr_enabled=True, swr_threshold_ratio=0.01)  # stale after ~0.1 s of ttl=10

    async def test_async_failed_refresh_warns_with_redacted_key_and_type(self, caplog):
        from tests.unit.test_swr_decorator import _assert_key_free, _await_for, _warnings

        calls = 0

        @cache(ttl=10, backend=None, namespace="tenant-secret-ns", l1=self._L1)
        async def fn():
            nonlocal calls
            calls += 1
            if calls > 1:
                raise RuntimeError("secret-detail")
            return calls

        assert await fn() == 1
        await asyncio.sleep(0.15)
        with caplog.at_level(logging.WARNING, logger="cachekit.decorators.wrapper"):
            assert await fn() == 1  # stale served; the refresh fails in the background
            assert await _await_for(lambda: _warnings(caplog, "L1-only SWR refresh failed"))
        (warning,) = _warnings(caplog, "L1-only SWR refresh failed")
        _assert_key_free(warning)

    def test_sync_failed_refresh_warns_with_redacted_key_and_type(self, caplog):
        from tests.unit.test_swr_decorator import _assert_key_free, _wait_for, _warnings

        calls = 0

        @cache(ttl=10, backend=None, namespace="tenant-secret-ns", l1=self._L1)
        def fn():
            nonlocal calls
            calls += 1
            if calls > 1:
                raise RuntimeError("secret-detail")
            return calls

        assert fn() == 1
        time.sleep(0.15)
        with caplog.at_level(logging.WARNING, logger="cachekit.decorators.wrapper"):
            assert fn() == 1
            assert _wait_for(lambda: _warnings(caplog, "L1-only SWR refresh failed"))
        (warning,) = _warnings(caplog, "L1-only SWR refresh failed")
        _assert_key_free(warning)

    def test_function_metadata_never_reaches_the_warning(self, caplog):
        """A dynamically created function's __qualname__ and __name__ can carry caller data.

        The WARNING names the function by the digest of its module.qualname, so it stays
        correlatable, and the record's thread name is static: a log format with
        %(threadName)s must not leak the function name either.
        """
        from cachekit.hash_utils import redact_cache_key
        from tests.unit.test_swr_decorator import _wait_for, _warnings

        calls = 0

        def source():
            nonlocal calls
            calls += 1
            if calls > 1:
                raise RuntimeError("down")
            return calls

        source.__qualname__ = "factory.<locals>.tenant-customer-42.fetch"
        source.__name__ = "fetch_tenant-customer-42"
        fn = cache(ttl=10, backend=None, l1=self._L1)(source)

        assert fn() == 1
        time.sleep(0.15)
        with caplog.at_level(logging.WARNING, logger="cachekit.decorators.wrapper"):
            assert fn() == 1
            assert _wait_for(lambda: _warnings(caplog, "L1-only SWR refresh failed"))
        (record,) = [r for r in caplog.records if "L1-only SWR refresh failed" in r.getMessage()]
        assert "tenant-customer-42" not in record.getMessage()
        assert "tenant-customer-42" not in record.threadName
        assert f"in function {redact_cache_key(f'{__name__}.factory.<locals>.tenant-customer-42.fetch')}" in record.getMessage()

    def test_uncopyable_args_warn_that_refresh_ahead_cannot_run(self, caplog):
        from tests.unit.test_swr_decorator import _assert_key_free, _warnings

        @cache(ttl=10, backend=None, namespace="tenant-secret-ns", key=lambda lock: "k", l1=self._L1)
        def fn(lock):
            return 1

        lock = threading.Lock()  # deepcopy(threading.Lock()) raises TypeError
        assert fn(lock) == 1
        time.sleep(0.15)
        with caplog.at_level(logging.WARNING, logger="cachekit.decorators.wrapper"):
            assert fn(lock) == 1  # stale served; the skip is logged before this returns
        (warning,) = _warnings(caplog, "L1-only SWR refresh skipped")
        assert "arguments not deep-copyable, so refresh-ahead cannot run for this call" in warning
        _assert_key_free(warning, exc_type="TypeError")

    def test_thread_start_failure_warns(self, monkeypatch, caplog):
        import cachekit.decorators.wrapper as wrapper_mod
        from tests.unit.test_swr_decorator import _assert_key_free, _failing_thread_shim, _warnings

        @cache(ttl=10, backend=None, namespace="tenant-secret-ns", l1=self._L1)
        def fn():
            return 1

        assert fn() == 1
        time.sleep(0.15)
        monkeypatch.setattr(wrapper_mod, "threading", _failing_thread_shim())
        with caplog.at_level(logging.WARNING, logger="cachekit.decorators.wrapper"):
            assert fn() == 1
        (warning,) = _warnings(caplog, "L1-only SWR refresh could not be started")
        _assert_key_free(warning)


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


@pytest.mark.unit
class TestL1OnlySWRRetryBackoff:
    """A failed L1-only refresh is not retried on every read (swr_retry_interval).

    The ObjectCache clock is faked so the band/back-off/expiry boundaries are exact;
    the refresh itself still runs on a real thread or task.
    """

    @staticmethod
    def _fake_clock(monkeypatch, start: float = 1000.0):
        import types

        fake_time = types.SimpleNamespace(monotonic=lambda: start)
        monkeypatch.setattr("cachekit.object_cache.time", fake_time)
        return fake_time

    @staticmethod
    def _wait_sync(get_calls, expected: int, timeout: float = 2.0) -> None:
        deadline = time.monotonic() + timeout
        while get_calls() < expected and time.monotonic() < deadline:
            time.sleep(0.01)
        time.sleep(0.05)  # let the refresh thread record its failure

    def test_sync_failed_refresh_backs_off_then_retries_once(self, monkeypatch):
        fake = self._fake_clock(monkeypatch)
        calls = 0

        @cache(ttl=100, backend=None, l1=L1CacheConfig(swr_threshold_ratio=0.5, swr_retry_interval=20))
        def fn():
            nonlocal calls
            calls += 1
            if calls > 1:
                raise RuntimeError("upstream down")
            return "held"

        assert fn() == "held"
        fake.monotonic = lambda: 1060.0  # in the refresh band
        assert fn() == "held"  # schedules the refresh that fails
        self._wait_sync(lambda: calls, 2)
        assert calls == 2

        for i in range(12):
            fake.monotonic = lambda i=i: 1061.0 + i  # inside the 20 s back-off
            assert fn() == "held"
        time.sleep(0.1)
        assert calls == 2, f"refresh retried during back-off (calls={calls})"

        fake.monotonic = lambda: 1080.0  # back-off over
        assert fn() == "held"
        assert fn() == "held"
        self._wait_sync(lambda: calls, 3)
        assert calls == 3, f"expected exactly one retry after the interval (calls={calls})"

    def test_sync_past_ttl_caller_sees_exception(self, monkeypatch):
        fake = self._fake_clock(monkeypatch)
        calls = 0

        @cache(ttl=100, backend=None, l1=L1CacheConfig(swr_threshold_ratio=0.5, swr_retry_interval=1000))
        def fn():
            nonlocal calls
            calls += 1
            if calls > 1:
                raise RuntimeError("upstream down")
            return "held"

        assert fn() == "held"
        fake.monotonic = lambda: 1060.0
        assert fn() == "held"
        self._wait_sync(lambda: calls, 2)

        fake.monotonic = lambda: 1100.0  # hard expiry, back-off still running
        with pytest.raises(RuntimeError, match="upstream down"):
            fn()
        assert calls == 3

    def test_zero_interval_restores_retry_on_every_stale_read(self, monkeypatch):
        fake = self._fake_clock(monkeypatch)
        calls = 0

        @cache(ttl=100, backend=None, l1=L1CacheConfig(swr_threshold_ratio=0.5, swr_retry_interval=0))
        def fn():
            nonlocal calls
            calls += 1
            if calls > 1:
                raise RuntimeError("upstream down")
            return "held"

        assert fn() == "held"
        fake.monotonic = lambda: 1060.0
        for expected in (2, 3, 4):
            assert fn() == "held"
            self._wait_sync(lambda: calls, expected)
        assert calls == 4

    async def test_async_failed_refresh_backs_off(self, monkeypatch):
        fake = self._fake_clock(monkeypatch)
        calls = 0

        @cache(ttl=100, backend=None, l1=L1CacheConfig(swr_threshold_ratio=0.5, swr_retry_interval=20))
        async def fn():
            nonlocal calls
            calls += 1
            if calls > 1:
                raise RuntimeError("upstream down")
            return "held"

        assert await fn() == "held"
        fake.monotonic = lambda: 1060.0
        assert await fn() == "held"
        await _wait_for_calls(lambda: calls, 2)
        await asyncio.sleep(0.05)  # let the failed task finish

        for i in range(12):
            fake.monotonic = lambda i=i: 1061.0 + i
            assert await fn() == "held"
        await asyncio.sleep(0.1)
        assert calls == 2

        fake.monotonic = lambda: 1080.0
        assert await fn() == "held"
        await _wait_for_calls(lambda: calls, 3)
        assert calls == 3

    async def test_async_upstream_cancelled_error_backs_off(self, monkeypatch):
        """An upstream that raises CancelledError counts as a failed attempt, not a skip."""
        fake = self._fake_clock(monkeypatch)
        calls = 0

        @cache(ttl=100, backend=None, l1=L1CacheConfig(swr_threshold_ratio=0.5, swr_retry_interval=20))
        async def fn():
            nonlocal calls
            calls += 1
            if calls > 1:
                raise asyncio.CancelledError
            return "held"

        assert await fn() == "held"
        fake.monotonic = lambda: 1060.0
        assert await fn() == "held"
        await _wait_for_calls(lambda: calls, 2)
        await asyncio.sleep(0.05)

        for i in range(12):
            fake.monotonic = lambda i=i: 1061.0 + i
            assert await fn() == "held"
        await asyncio.sleep(0.1)
        assert calls == 2, f"refresh retried during back-off (calls={calls})"
