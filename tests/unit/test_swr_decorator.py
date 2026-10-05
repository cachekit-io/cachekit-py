"""Decorator-level stale-while-revalidate (LAB-381).

End-to-end behavior through the real decorator + serializer stack against a
fake SWR-capable backend: serve-stale-immediately, background revalidation
(async task / sync daemon thread), single-flight, lease handling, failure
degradation, and decoration-time validation.

Spec: protocol spec/saas-api.md#stale-while-revalidate.
"""

from __future__ import annotations

import asyncio
import faulthandler
import logging
import math
import os
import sys
import threading
import time
from contextlib import asynccontextmanager, suppress
from typing import Any

import pytest

from cachekit import cache, hash_utils
from cachekit.config.decorator import DecoratorConfig
from cachekit.config.validation import ConfigurationError

_CAP = 2_592_000  # 30-day storage cap shared by ttl + stale_ttl


class FakeSWRBackend:
    """SWR-capable backend double: byte store + controllable freshness + lock log."""

    def __init__(self, grant_lock: bool = True) -> None:
        self.store: dict[str, bytes] = {}
        self.stale = False
        self.fresh_for: int | None = None
        self.freshness_reads = 0
        self.grant_lock = grant_lock
        self.set_calls: list[tuple[int | None, int | None]] = []
        self.lock_attempts: list[str] = []

    def get(self, key: str) -> bytes | None:
        return self.store.get(key)

    def get_with_freshness(self, key: str) -> tuple[bytes, bool, int | None] | None:
        self.freshness_reads += 1
        value = self.store.get(key)
        return None if value is None else (value, self.stale, self.fresh_for)

    def set(self, key: str, value: bytes, ttl: int | None = None, stale_ttl: int | None = None) -> None:
        self.store[key] = value
        self.set_calls.append((ttl, stale_ttl))

    def delete(self, key: str) -> bool:
        return self.store.pop(key, None) is not None

    def exists(self, key: str) -> bool:
        return key in self.store

    def health_check(self) -> tuple[bool, dict[str, Any]]:
        return True, {}

    @asynccontextmanager
    async def acquire_lock(self, key: str, timeout: float, blocking_timeout: float | None = None):
        self.lock_attempts.append(key)
        yield self.grant_lock


def _wait_for(predicate, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


async def _await_for(predicate, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.02)
    return predicate()


class TestAsyncSWR:
    async def test_stale_hit_serves_immediately_and_revalidates_in_background(self) -> None:
        backend = FakeSWRBackend()
        calls = {"n": 0}

        @cache(backend=backend, ttl=60, stale_ttl=120, l1_enabled=False)
        async def compute(x: int) -> dict[str, Any]:
            calls["n"] += 1
            return {"x": x, "call": calls["n"]}

        assert (await compute(1))["call"] == 1
        assert backend.set_calls == [(60, 120)]  # miss-path store carries the window

        backend.stale = True
        stale_result = await compute(1)
        assert stale_result["call"] == 1  # stale value served, no synchronous recompute

        assert await _await_for(lambda: len(backend.set_calls) == 2)
        assert backend.set_calls[1] == (60, 120)  # revalidation PUT re-sends the window (spec)
        assert calls["n"] == 2
        assert backend.lock_attempts  # lease attempted

        backend.stale = False
        assert (await compute(1))["call"] == 2  # revalidated value now served fresh

    async def test_contested_lease_serves_stale_without_recompute(self) -> None:
        backend = FakeSWRBackend(grant_lock=False)
        calls = {"n": 0}

        @cache(backend=backend, ttl=60, stale_ttl=120, l1_enabled=False)
        async def compute() -> int:
            calls["n"] += 1
            return calls["n"]

        assert await compute() == 1
        backend.stale = True
        assert await compute() == 1  # stale served

        await asyncio.sleep(0.2)  # give a (wrong) recompute time to happen
        assert calls["n"] == 1  # contested lease: MUST NOT recompute (spec)
        assert len(backend.set_calls) == 1

    async def test_concurrent_stale_hits_single_flight(self) -> None:
        backend = FakeSWRBackend()
        calls = {"n": 0}
        started = asyncio.Event()

        @cache(backend=backend, ttl=60, stale_ttl=120, l1_enabled=False)
        async def compute() -> int:
            calls["n"] += 1
            started.set()
            await asyncio.sleep(0.1)  # keep the first revalidation in flight
            return calls["n"]

        assert await compute() == 1
        backend.stale = True
        results = await asyncio.gather(*(compute() for _ in range(5)))
        assert all(r == 1 for r in results)  # every caller got the stale value instantly

        assert await _await_for(lambda: len(backend.set_calls) == 2)
        await asyncio.sleep(0.15)
        assert calls["n"] == 2  # exactly ONE background recompute for 5 stale hits

    async def test_revalidation_failure_is_silent_and_leaves_entry(self) -> None:
        backend = FakeSWRBackend()
        calls = {"n": 0}

        @cache(backend=backend, ttl=60, stale_ttl=120, l1_enabled=False)
        async def compute() -> int:
            calls["n"] += 1
            if calls["n"] > 1:
                raise RuntimeError("recompute exploded")
            return calls["n"]

        assert await compute() == 1
        backend.stale = True
        assert await compute() == 1  # caller unaffected

        assert await _await_for(lambda: calls["n"] == 2)
        await asyncio.sleep(0.1)
        assert len(backend.set_calls) == 1  # failed revalidation stored nothing
        assert await compute() == 1  # stale keeps being served until evict_at


class TestSyncSWR:
    def test_stale_hit_serves_immediately_and_revalidates_on_daemon_thread(self) -> None:
        backend = FakeSWRBackend()
        calls = {"n": 0}

        @cache(backend=backend, ttl=60, stale_ttl=120, l1_enabled=False)
        def compute(x: int) -> dict[str, Any]:
            calls["n"] += 1
            return {"x": x, "call": calls["n"]}

        assert compute(1)["call"] == 1
        backend.stale = True
        assert compute(1)["call"] == 1  # stale served without blocking

        assert _wait_for(lambda: len(backend.set_calls) == 2)
        assert backend.set_calls[1] == (60, 120)
        assert calls["n"] == 2

    def test_concurrent_stale_hits_dedupe_in_process(self) -> None:
        backend = FakeSWRBackend()
        calls = {"n": 0}
        gate = threading.Event()

        @cache(backend=backend, ttl=60, stale_ttl=120, l1_enabled=False)
        def compute() -> int:
            calls["n"] += 1
            gate.wait(0.2)  # hold the first revalidation in flight
            return calls["n"]

        gate.set()
        assert compute() == 1
        gate.clear()
        backend.stale = True
        results = [compute() for _ in range(5)]
        gate.set()
        assert all(r == 1 for r in results)

        assert _wait_for(lambda: calls["n"] == 2)
        time.sleep(0.2)
        assert calls["n"] == 2  # per-process single-flight


class TestSWRConfig:
    def test_swr_by_default_resolves_stale_ttl_to_ttl(self) -> None:
        """io()-style preset: swr_by_default=True defaults the window to ttl."""
        backend = FakeSWRBackend()
        config = DecoratorConfig(backend=backend, ttl=60, swr_by_default=True)

        @cache(config=config)
        def compute() -> str:
            return "v"

        assert compute() == "v"
        assert backend.set_calls == [(60, 60)]  # window defaulted to ttl

    def test_stale_ttl_zero_opts_out_of_preset_default(self) -> None:
        backend = FakeSWRBackend()
        config = DecoratorConfig(backend=backend, ttl=60, stale_ttl=0, swr_by_default=True)

        @cache(config=config)
        def compute() -> str:
            return "v"

        assert compute() == "v"
        assert backend.set_calls == [(60, None)]  # no window: explicit opt-out

    def test_preset_default_caps_window_to_30_day_total(self) -> None:
        backend = FakeSWRBackend()
        ttl = _CAP - 100  # leaves only 100s of window headroom
        config = DecoratorConfig(backend=backend, ttl=ttl, swr_by_default=True)

        @cache(config=config)
        def compute() -> str:
            return "v"

        assert compute() == "v"
        assert backend.set_calls == [(ttl, 100)]

    def test_preset_default_window_fits_the_cap_after_rounding(self) -> None:
        """A fractional ttl is ceiled on the wire, so the derived window is bounded by the ceiled TTL."""
        backend = FakeSWRBackend()
        ttl = _CAP / 2 + 0.5  # goes out as 1_296_001
        config = DecoratorConfig(backend=backend, ttl=ttl, swr_by_default=True)  # type: ignore[arg-type]

        @cache(config=config)
        def compute() -> str:
            return "v"

        assert compute() == "v"
        assert backend.set_calls == [(ttl, _CAP // 2 - 1)]
        assert math.ceil(ttl) + backend.set_calls[0][1] == _CAP

    def test_stale_ttl_without_ttl_raises(self) -> None:
        with pytest.raises(ConfigurationError, match="requires a positive ttl"):

            @cache(backend=FakeSWRBackend(), stale_ttl=120, l1_enabled=False)
            def f() -> None: ...

    def test_total_over_cap_raises(self) -> None:
        with pytest.raises(ConfigurationError, match="30-day"):

            @cache(backend=FakeSWRBackend(), ttl=2_000_000, stale_ttl=1_000_000, l1_enabled=False)
            def f() -> None: ...

    def test_non_swr_backend_raises(self) -> None:
        class PlainBackend:
            def get(self, k: str) -> None:
                return None

            def set(self, k: str, v: bytes, ttl: int | None = None) -> None: ...

            def delete(self, k: str) -> bool:
                return False

        with pytest.raises(ConfigurationError, match="SWR-capable"):

            @cache(backend=PlainBackend(), ttl=60, stale_ttl=120, l1_enabled=False)
            def f() -> None: ...

    def test_negative_stale_ttl_raises(self) -> None:
        with pytest.raises(ConfigurationError, match="non-negative"):

            @cache(backend=FakeSWRBackend(), ttl=60, stale_ttl=-5, l1_enabled=False)
            def f() -> None: ...

    @pytest.mark.parametrize("bad", [True, False, 0.0, 5.5], ids=["True", "False", "0.0", "5.5"])
    def test_non_integer_stale_ttl_raises(self, bad: Any) -> None:
        """bool/float must be rejected, not coerced (DecoratorConfig is an unvalidated dataclass).

        bool is an int subclass and False == 0 == 0.0: before the type check ran
        ahead of the zero opt-out, True silently meant a 1-second window and
        False/0.0 silently opted out.
        """
        with pytest.raises(ConfigurationError, match="non-negative integer"):

            @cache(backend=FakeSWRBackend(), ttl=60, stale_ttl=bad, l1_enabled=False)
            def f() -> None: ...

    def test_integer_zero_opts_out_without_backend_requirements(self) -> None:
        """stale_ttl=0 is the explicit opt-out: valid even on a non-SWR backend."""

        class PlainBackend:
            def get(self, k: str) -> None:
                return None

            def set(self, k: str, v: bytes, ttl: int | None = None) -> None: ...

            def delete(self, k: str) -> bool:
                return False

        @cache(backend=PlainBackend(), ttl=60, stale_ttl=0, l1_enabled=False)
        def f() -> int:
            return 7

        assert f() == 7


class TestSWRForkIsolation:
    """LAB-506 follow-through: SWR scheduler state must not survive fork().

    The per-wrapper refresh pool (in-flight keys and slots) is parent state:
    the parent threads that would clear it don't survive fork, so without a
    PID guard an inherited in-flight key starves revalidation in the child
    forever, and the inherited pool can carry consumed slots.
    """

    @pytest.mark.skipif(not hasattr(os, "fork"), reason="fork() not available on this platform")
    def test_forked_child_revalidates_key_stuck_inflight_in_parent(self) -> None:
        import multiprocessing

        backend = FakeSWRBackend()
        gate = threading.Event()
        calls: list[int] = []
        parent_pid = os.getpid()

        @cache(backend=backend, ttl=60, stale_ttl=120, l1_enabled=False)
        def compute(x: int) -> int:
            calls.append(os.getpid())
            # Only the parent parks. The child must never touch the gate: the parent's revalidation thread
            # can be forked while it holds the gate's lock on its way into wait(), and the child inherits
            # that lock held, with no thread left to release it.
            if len(calls) > 1 and os.getpid() == parent_pid:
                gate.wait(timeout=30)  # hold the parent's revalidation in flight
            return x + 1

        try:
            assert compute(1) == 2  # miss -> store
            backend.stale = True
            assert compute(1) == 2  # stale hit -> schedules revalidation (blocks on gate)
            # The revalidation daemon thread is now inside compute(), holding the
            # key in the wrapper's in-flight set for the duration of the fork.
            assert _wait_for(lambda: len(calls) == 2)

            ctx = multiprocessing.get_context("fork")
            queue = ctx.Queue()

            def child(q) -> None:
                # A child stuck past 20 s dumps every thread's stack to stderr while the parent still
                # waits, so a hang fails with its own diagnosis instead of a bare queue.Empty.
                faulthandler.dump_traceback_later(20, exit=False, file=sys.__stderr__)
                before = len(calls)
                result = compute(1)  # stale hit; key is stuck in the INHERITED in-flight set
                revalidated = _wait_for(lambda: len(calls) > before, timeout=5.0)
                q.put({"result": result, "revalidated": revalidated})

            # daemon, and killed if still alive: a stuck child fails this test instead of outliving it
            # (a live non-daemon child is joined with no timeout at interpreter exit).
            process = ctx.Process(target=child, args=(queue,), daemon=True)
            process.start()
            try:
                outcome = queue.get(timeout=30)
            finally:
                process.join(timeout=30)
                if process.is_alive():
                    process.kill()
                    process.join()

            assert process.exitcode == 0
            assert outcome["result"] == 2
            assert outcome["revalidated"], "forked child must reset inherited SWR in-flight state and revalidate"
        finally:
            gate.set()  # release the parent's parked revalidation thread

    """Branches the main flows don't reach: L1 refresh, no-lock backends,
    sync failure, slot exhaustion, operation-handler degradation."""

    async def test_no_lock_backend_still_revalidates(self) -> None:
        """Backend WITHOUT acquire_lock: the no-lease branch revalidates directly."""

        class NoLockSWRBackend:
            def __init__(self) -> None:
                self.store: dict[str, bytes] = {}
                self.stale = False
                self.set_calls: list[tuple[int | None, int | None]] = []

            def get(self, key: str) -> bytes | None:
                return self.store.get(key)

            def get_with_freshness(self, key: str) -> tuple[bytes, bool, int | None] | None:
                value = self.store.get(key)
                return None if value is None else (value, self.stale, None)

            def set(self, key: str, value: bytes, ttl: int | None = None, stale_ttl: int | None = None) -> None:
                self.store[key] = value
                self.set_calls.append((ttl, stale_ttl))

            def delete(self, key: str) -> bool:
                return self.store.pop(key, None) is not None

        backend = NoLockSWRBackend()
        assert not hasattr(backend, "acquire_lock")
        calls = {"n": 0}

        @cache(backend=backend, ttl=60, stale_ttl=120, l1_enabled=False)
        async def compute() -> int:
            calls["n"] += 1
            return calls["n"]

        assert await compute() == 1
        backend.stale = True
        assert await compute() == 1  # stale served
        assert await _await_for(lambda: calls["n"] == 2)  # revalidated without a lease
        assert await _await_for(lambda: len(backend.set_calls) == 2)

    async def test_l1_refresh_on_revalidation(self) -> None:
        """With L1 enabled: stale L2 bytes are NOT recorded in L1, and the
        background revalidation refreshes L1 with the new fresh bytes."""
        backend = FakeSWRBackend()
        calls = {"n": 0}

        @cache(backend=backend, ttl=60, stale_ttl=120, namespace="swr-l1-refresh")
        async def compute() -> int:
            calls["n"] += 1
            return calls["n"]

        assert await compute() == 1  # miss -> L2 + L1 store

        # Force the next read to L2: clear L1 via the decorator API, then restore
        # the L2 bytes it also cleared, and flip the entry stale.
        l2_snapshot = dict(backend.store)
        await compute.invalidate_cache()  # type: ignore[attr-defined]  # coroutine for async functions
        backend.store.update(l2_snapshot)
        backend.stale = True

        assert await compute() == 1  # L1 miss -> stale L2 hit -> serve stale
        assert await _await_for(lambda: calls["n"] == 2)  # background recompute ran
        assert await _await_for(lambda: len(backend.set_calls) >= 2)

        # L1 was refreshed with FRESH bytes by the revalidation: with the L2 entry
        # still flagged stale, a pure-L1 hit returns the new value with no recompute.
        backend.stale = False
        assert await compute() == 2
        assert calls["n"] == 2

    def test_sync_revalidation_failure_is_silent(self) -> None:
        backend = FakeSWRBackend()
        calls = {"n": 0}

        @cache(backend=backend, ttl=60, stale_ttl=120, l1_enabled=False)
        def compute() -> int:
            calls["n"] += 1
            if calls["n"] > 1:
                raise RuntimeError("sync recompute exploded")
            return calls["n"]

        assert compute() == 1
        backend.stale = True
        assert compute() == 1  # caller unaffected
        assert _wait_for(lambda: calls["n"] == 2)
        time.sleep(0.1)
        assert len(backend.set_calls) == 1  # nothing stored on failure
        assert compute() == 1  # stale keeps serving

    def test_slot_exhaustion_skips_revalidation(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """With the refresh slot pool at 1, a second distinct stale key skips
        revalidation (stale keeps being served; a later hit retries)."""
        import cachekit.decorators.wrapper as wrapper_mod

        monkeypatch.setattr(wrapper_mod, "_L2_SWR_MAX_CONCURRENT_REFRESHES", 1)
        backend = FakeSWRBackend()
        calls = {"n": 0}
        gate = threading.Event()

        @cache(backend=backend, ttl=60, stale_ttl=120, l1_enabled=False)
        def compute(x: int) -> int:
            calls["n"] += 1
            if calls["n"] > 2:  # only background recomputes block (first two are misses)
                gate.wait(2)
            return calls["n"]

        assert compute(1) == 1
        assert compute(2) == 2
        backend.stale = True
        assert compute(1) == 1  # claims the single slot; recompute blocked on gate
        assert _wait_for(lambda: calls["n"] == 3)  # background recompute started
        assert compute(2) == 2  # slot pool exhausted -> revalidation skipped
        time.sleep(0.15)
        assert calls["n"] == 3  # no second background recompute
        gate.set()
        assert _wait_for(lambda: len(backend.set_calls) == 3)  # first revalidation lands


class TestOperationHandlerFreshnessDegradation:
    """get_cached_value_with_freshness error paths mirror get_cached_value (#159 contract)."""

    def _make_op(self, deserialize_side_effect=None, get_result=(b"bytes", True, None)):
        from unittest import mock

        from cachekit.cache_handler import CacheKeyGenerator, CacheOperationHandler, CacheSerializationHandler

        if deserialize_side_effect is not None:
            serialization = mock.MagicMock(spec=CacheSerializationHandler)
            serialization.deserialize_data.side_effect = deserialize_side_effect
            serialization.encryption_fail_closed = False  # real bool: MagicMock is truthy (LAB-108)
        else:
            serialization = CacheSerializationHandler()
        op = CacheOperationHandler(serialization, CacheKeyGenerator())
        cache_handler = mock.MagicMock()
        cache_handler.get_with_freshness.return_value = get_result
        op.set_cache_handler(cache_handler)
        return op, cache_handler

    def test_no_handler_reads_as_miss(self) -> None:
        from cachekit.cache_handler import CacheKeyGenerator, CacheOperationHandler, CacheSerializationHandler

        op = CacheOperationHandler(CacheSerializationHandler(), CacheKeyGenerator())
        assert op.get_cached_value_with_freshness("k") is None  # RuntimeError -> generic path -> miss

    def test_legacy_two_tuple_handler_degrades_to_no_bound(self) -> None:
        """LAB-557 compat: a custom CacheHandlerStrategy built against the v0.18.0
        2-tuple (bytes, is_stale) signature must read as fresh_for=None (legacy L1
        lifetime), NOT raise a strict-unpack ValueError that the broad except
        swallows into a permanent every-hit-is-a-miss cache bypass (expert-panel
        finding). Third-party 2-tuple BACKENDS are padded upstream by
        StandardCacheHandler (tests/unit/backends/test_cachekitio_swr_transport.py)."""
        from unittest import mock

        from cachekit.cache_handler import CacheHit, CacheKeyGenerator, CacheOperationHandler, CacheSerializationHandler

        serialization = mock.MagicMock(spec=CacheSerializationHandler)
        serialization.deserialize_data.return_value = {"v": 1}
        serialization.encryption_fail_closed = False
        op = CacheOperationHandler(serialization, CacheKeyGenerator())
        cache_handler = mock.MagicMock()
        cache_handler.get_with_freshness.return_value = (b"bytes", False)  # 0.5.x 2-tuple
        op.set_cache_handler(cache_handler)
        assert op.get_cached_value_with_freshness("k") == (CacheHit({"v": 1}, b"bytes", 5), False, None)

    def test_backend_error_reads_as_miss(self) -> None:
        op, cache_handler = self._make_op()
        cache_handler.get_with_freshness.side_effect = ValueError("backend exploded")
        assert op.get_cached_value_with_freshness("k") is None

    def test_poisoned_entry_evicted_and_reads_as_miss(self) -> None:
        from cachekit.serializers.base import SerializationError

        op, cache_handler = self._make_op(deserialize_side_effect=SerializationError("integrity check failed"))
        assert op.get_cached_value_with_freshness("poison:key") is None
        cache_handler.delete.assert_called_once_with("poison:key")

    def test_eviction_failure_never_masks_the_miss(self) -> None:
        from cachekit.serializers.base import SerializationError

        op, cache_handler = self._make_op(deserialize_side_effect=SerializationError("corrupt"))
        cache_handler.delete.side_effect = RuntimeError("delete also broken")
        assert op.get_cached_value_with_freshness("poison:key") is None

    def test_sync_l1_refresh_on_revalidation(self) -> None:
        """Sync twin of the L1-refresh case: the daemon-thread revalidation
        writes the new fresh bytes back into L1."""
        backend = FakeSWRBackend()
        calls = {"n": 0}

        @cache(backend=backend, ttl=60, stale_ttl=120, namespace="swr-l1-sync")
        def compute() -> int:
            calls["n"] += 1
            return calls["n"]

        assert compute() == 1
        l2_snapshot = dict(backend.store)
        compute.invalidate_cache()  # type: ignore[attr-defined]
        backend.store.update(l2_snapshot)
        backend.stale = True

        assert compute() == 1  # L1 miss -> stale L2 hit
        assert _wait_for(lambda: calls["n"] == 2)
        assert _wait_for(lambda: len(backend.set_calls) >= 2)
        backend.stale = False
        assert compute() == 2  # revalidation refreshed L1 with fresh bytes
        assert calls["n"] == 2

    def test_sync_freshness_hit_backfills_l1_unless_stale_or_expired(self) -> None:
        """LAB-348 parity: the sync freshness read backfills L1 exactly as the
        async read does — a fresh hit is recorded (next read is L1, no second
        freshness read), a stale-labelled hit never is, and fresh_for=0 makes
        the backfill a no-op (the remaining-freshness bound reaches the sync
        path). No stale_ttl: a stale hit is served with no revalidation, so
        nothing races the second read."""
        backend = FakeSWRBackend()

        @cache(backend=backend, ttl=60, namespace="swr-l1-sync-backfill")
        def compute() -> int:
            return 1

        assert compute() == 1
        l2_snapshot = dict(backend.store)

        def force_l2() -> None:
            compute.invalidate_cache()  # type: ignore[attr-defined]
            backend.store.update(l2_snapshot)
            backend.freshness_reads = 0

        force_l2()
        assert compute() == 1 and compute() == 1
        assert backend.freshness_reads == 1  # fresh hit backfilled -> second read served by L1

        force_l2()
        backend.stale = True
        assert compute() == 1 and compute() == 1
        assert backend.freshness_reads == 2  # stale hit never recorded in L1

        force_l2()
        backend.stale = False
        backend.fresh_for = 0
        assert compute() == 1 and compute() == 1
        assert backend.freshness_reads == 2  # nothing fresh remains -> L1Cache.put skips the entry


class TestSWRSchedulingHardening:
    """CodeRabbit round-2 regressions: negative default window, arg snapshots,
    slot release when scheduling fails."""

    def test_default_window_off_when_ttl_at_or_above_cap(self) -> None:
        """ttl ≥ the 30-day cap leaves no window headroom: SWR silently off,
        never a negative stale_ttl."""
        backend = FakeSWRBackend()
        config = DecoratorConfig(backend=backend, ttl=_CAP + 100, swr_by_default=True)

        @cache(config=config)
        def compute() -> str:
            return "v"

        assert compute() == "v"
        assert backend.set_calls == [(_CAP + 100, None)]  # no window, not negative

    def test_uncopyable_args_skip_revalidation(self) -> None:
        """Args that can't be deep-copied (e.g. a lock) skip the background
        refresh — stale keeps being served, nothing recomputes with live refs."""
        backend = FakeSWRBackend()
        calls = {"n": 0}

        @cache(backend=backend, ttl=60, stale_ttl=120, l1_enabled=False, key=lambda lock: "fixed-key")
        def compute(lock) -> int:
            calls["n"] += 1
            return calls["n"]

        lock = threading.Lock()  # deepcopy(threading.Lock()) raises TypeError
        assert compute(lock) == 1
        backend.stale = True
        assert compute(lock) == 1  # stale served
        time.sleep(0.2)
        assert calls["n"] == 1  # refresh skipped: args not snapshot-able
        assert len(backend.set_calls) == 1

        # The slot was released: a copyable-args key on the same function can
        # still revalidate (the pool didn't leak).
        assert _wait_for(lambda: calls["n"] == 1)

    def test_thread_start_failure_releases_slot(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Thread.start() raising must not leak the in-flight marker/slot: the
        next stale hit retries and succeeds."""
        import types

        import cachekit.decorators.wrapper as wrapper_mod

        backend = FakeSWRBackend()
        calls = {"n": 0}

        @cache(backend=backend, ttl=60, stale_ttl=120, l1_enabled=False)
        def compute() -> int:
            calls["n"] += 1
            return calls["n"]

        assert compute() == 1
        backend.stale = True

        class _FailingThread:
            def __init__(self, *a, **k) -> None: ...

            def start(self) -> None:
                raise RuntimeError("can't start new thread")

        shim = types.SimpleNamespace(**{name: getattr(threading, name) for name in dir(threading) if not name.startswith("_")})
        shim.Thread = _FailingThread
        monkeypatch.setattr(wrapper_mod, "threading", shim)

        assert compute() == 1  # stale served; scheduling fails silently
        time.sleep(0.1)
        assert calls["n"] == 1

        monkeypatch.setattr(wrapper_mod, "threading", threading)  # restore
        assert compute() == 1  # slot NOT leaked: retry schedules successfully
        assert _wait_for(lambda: calls["n"] == 2)
        assert _wait_for(lambda: len(backend.set_calls) == 2)


async def _resume() -> list[asyncio.Task[Any]]:
    """Let a resumed loop run its other tasks a few steps; return those still pending.

    Every task that finished must have been cancelled: a pruned refresh that raised on the way
    out (say, by giving back a slot it no longer held) fails the test here.
    """
    others = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
    for _ in range(5):
        await asyncio.sleep(0)
    for t in others:
        if t.done():
            assert t.cancelled(), t.exception()
    return [t for t in others if not t.done()]


def _collect_pruned(monkeypatch: pytest.MonkeyPatch) -> None:
    """Collect the refreshes pruned from a closed loop, which nothing holds any more. asyncio logs
    each as destroyed while pending; closing its coroutine must raise nothing."""
    import gc

    unraisable: list[Any] = []
    monkeypatch.setattr(sys, "unraisablehook", unraisable.append)
    gc.collect()
    assert unraisable == []


def _close_loop(loop: asyncio.AbstractEventLoop) -> None:
    if loop.is_closed():
        return
    pending = asyncio.all_tasks(loop)
    for t in pending:
        t.cancel()
    if pending:
        loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
    loop.close()


class TestAsyncSWRStoppedLoop:
    """LAB-7295: a revalidation left pending on an event loop that stopped (sync code calling
    run_until_complete) or closed without cancelling its tasks keeps its slot and key for the
    hold, then the next revalidation attempt releases both and cancels it."""

    @staticmethod
    def _parking_compute(backend: FakeSWRBackend, starts: list[int]) -> Any:
        @cache(backend=backend, ttl=60, stale_ttl=120, l1_enabled=False)
        async def compute(x: int) -> int:
            if backend.stale:  # a revalidation
                starts.append(x)
                await asyncio.sleep(3600)  # never answers: the revalidation stays pending
            return x

        return compute

    @pytest.mark.parametrize("close", [False, True], ids=["stopped", "closed"])
    def test_revalidation_on_stopped_loop_frees_slot_and_key(self, monkeypatch: pytest.MonkeyPatch, close: bool) -> None:
        import cachekit.decorators.wrapper as wrapper_mod

        monkeypatch.setattr(wrapper_mod, "_L2_SWR_MAX_CONCURRENT_REFRESHES", 1)
        monkeypatch.setattr(wrapper_mod, "_SWR_STOPPED_LOOP_HOLD_SECONDS", 3600.0)
        backend = FakeSWRBackend()
        starts: list[int] = []
        compute = self._parking_compute(backend, starts)

        async def hit(x: int) -> None:
            assert await compute(x) == x
            for _ in range(5):
                await asyncio.sleep(0)

        asyncio.run(compute(0))  # misses: store
        asyncio.run(compute(1))
        backend.stale = True
        stopped = asyncio.new_event_loop()
        try:
            stopped.run_until_complete(hit(0))  # key 0's revalidation parks on a loop then left stopped
            if close:
                stopped.close()  # without cancelling it
            asyncio.run(hit(1))  # within the hold the parked revalidation keeps the only slot
            asyncio.run(hit(0))  # and its key
            assert starts == [0]
            monkeypatch.setattr(wrapper_mod, "_SWR_STOPPED_LOOP_HOLD_SECONDS", 0.0)
            asyncio.run(hit(1))  # past the hold it holds no slot
            asyncio.run(hit(0))  # nor its key
            assert starts == [0, 1, 0]
            if close:
                _collect_pruned(monkeypatch)
        finally:
            _close_loop(stopped)

    def test_revalidations_pruned_from_stopped_loops_do_not_resume(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Loops that resume after pausing between run_until_complete calls run no more
        revalidations than the pool holds: the pruned ones were cancelled."""
        import cachekit.decorators.wrapper as wrapper_mod

        monkeypatch.setattr(wrapper_mod, "_L2_SWR_MAX_CONCURRENT_REFRESHES", 2)
        monkeypatch.setattr(wrapper_mod, "_SWR_STOPPED_LOOP_HOLD_SECONDS", 0.0)
        backend = FakeSWRBackend()
        compute = self._parking_compute(backend, [])

        async def hits() -> None:
            for x in (0, 1):
                assert await compute(x) == x
            for _ in range(5):
                await asyncio.sleep(0)

        asyncio.run(compute(0))  # misses: store
        asyncio.run(compute(1))
        backend.stale = True
        loops = [asyncio.new_event_loop() for _ in range(3)]
        try:
            for loop in loops:
                loop.run_until_complete(hits())  # each pauses with its revalidations pending
            assert sum(len(loop.run_until_complete(_resume())) for loop in loops) == 2  # the pool's 2, not 6
        finally:
            for loop in loops:
                _close_loop(loop)

    def test_pruned_revalidation_that_resumes_frees_nothing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A pruned revalidation whose loop runs again neither raises nor gives back the slot or
        the key that a newer revalidation of the same key now holds."""
        import cachekit.decorators.wrapper as wrapper_mod

        monkeypatch.setattr(wrapper_mod, "_L2_SWR_MAX_CONCURRENT_REFRESHES", 1)
        monkeypatch.setattr(wrapper_mod, "_SWR_STOPPED_LOOP_HOLD_SECONDS", 0.0)
        backend = FakeSWRBackend()
        starts: list[int] = []
        compute = self._parking_compute(backend, starts)

        async def hit(x: int) -> None:
            assert await compute(x) == x
            for _ in range(5):
                await asyncio.sleep(0)

        asyncio.run(compute(0))  # misses: store
        asyncio.run(compute(1))
        backend.stale = True
        first, second = asyncio.new_event_loop(), asyncio.new_event_loop()
        try:
            first.run_until_complete(hit(0))  # parks
            second.run_until_complete(hit(0))  # prunes the first, then holds key 0 and the only slot
            assert starts == [0, 0]
            monkeypatch.setattr(wrapper_mod, "_SWR_STOPPED_LOOP_HOLD_SECONDS", 3600.0)
            assert first.run_until_complete(_resume()) == []  # the pruned one ends cancelled
            asyncio.run(hit(0))  # the second still holds key 0
            asyncio.run(hit(1))  # and the only slot
            assert starts == [0, 0]
        finally:
            _close_loop(first)
            _close_loop(second)

    def test_pruned_revalidation_does_not_store_its_late_result(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Cancelling a pruned revalidation does not stop a function that swallows the
        cancellation, so it must not store what it returns: a newer revalidation has already
        stored a later value for the key."""
        import cachekit.decorators.wrapper as wrapper_mod

        monkeypatch.setattr(wrapper_mod, "_L2_SWR_MAX_CONCURRENT_REFRESHES", 1)
        monkeypatch.setattr(wrapper_mod, "_SWR_STOPPED_LOOP_HOLD_SECONDS", 0.0)
        backend = FakeSWRBackend()
        calls: list[str] = []

        @cache(backend=backend, ttl=60, stale_ttl=120, l1_enabled=False)
        async def compute() -> str:
            if len(calls) == 1:  # the first revalidation parks, then swallows its cancellation
                calls.append("parked")
                with suppress(asyncio.CancelledError):
                    await asyncio.sleep(3600)
                calls.append("late")
                return "late"
            calls.append("seed" if not calls else "fresh")
            return calls[-1]

        async def finish() -> None:
            """Run this loop's revalidations to the end: a store goes through a thread, and the
            task is done only once it has landed."""
            others = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
            await asyncio.wait(others, timeout=5)

        async def park() -> None:
            await compute()
            for _ in range(5):
                await asyncio.sleep(0)

        async def hit() -> None:
            await compute()
            await finish()

        asyncio.run(compute())  # miss: stores "seed"
        backend.stale = True
        stopped = asyncio.new_event_loop()
        try:
            stopped.run_until_complete(park())  # the first revalidation parks on a loop then left stopped
            asyncio.run(hit())  # prunes it, then stores "fresh"
            assert calls == ["seed", "parked", "fresh"]
            assert len(backend.set_calls) == 2
            stopped.run_until_complete(finish())  # the pruned one resumes and returns "late"
            assert calls[-1] == "late"
            assert len(backend.set_calls) == 2  # but stores nothing
            backend.stale = False
            assert asyncio.run(compute()) == "fresh"
        finally:
            _close_loop(stopped)


_WRAPPER_LOGGER = "cachekit.decorators.wrapper"


def _warnings(caplog: pytest.LogCaptureFixture, text: str) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING and text in r.getMessage()]


def _assert_key_free(message: str, exc_type: str = "RuntimeError") -> None:
    """The WARNING names the redacted key and the exception type, never the raw key or exception text."""
    assert "<redacted:" in message and f": {exc_type}" in message
    assert "tenant-secret-ns" not in message and "secret-detail" not in message


def _failing_thread_shim() -> Any:
    """A stand-in for the wrapper's threading module whose Thread.start() raises."""
    import types

    class _FailingThread:
        def __init__(self, *a: Any, **k: Any) -> None: ...

        def start(self) -> None:
            raise RuntimeError("can't start new thread")

    shim = types.SimpleNamespace(**{name: getattr(threading, name) for name in dir(threading) if not name.startswith("_")})
    shim.Thread = _FailingThread
    return shim


class TestRevalidationFailureWarnings:
    """A revalidation that fails or never runs reaches a default-level log, throttled per function.

    The caller was served the stale value and must never see the failure, so without a WARNING
    an operator cannot tell that the entry will only be recomputed in the foreground at evict_at.
    """

    async def test_async_failure_warns_with_redacted_key_and_type(self, caplog: pytest.LogCaptureFixture) -> None:
        backend = FakeSWRBackend()
        calls = {"n": 0}

        @cache(backend=backend, ttl=60, stale_ttl=120, l1_enabled=False, namespace="tenant-secret-ns")
        async def compute() -> int:
            calls["n"] += 1
            if calls["n"] > 1:
                raise RuntimeError("secret-detail")
            return 1

        assert await compute() == 1
        backend.stale = True
        with caplog.at_level(logging.WARNING, logger=_WRAPPER_LOGGER):
            assert await compute() == 1  # caller unaffected
            assert await _await_for(lambda: _warnings(caplog, "SWR revalidation failed"))
        (warning,) = _warnings(caplog, "SWR revalidation failed")
        _assert_key_free(warning)
        assert "in function <redacted:" in warning and "(1 since the last warning)" in warning
        assert "compute" not in warning  # the function by its digest only

    def test_sync_failure_warns_with_redacted_key_and_type(self, caplog: pytest.LogCaptureFixture) -> None:
        backend = FakeSWRBackend()
        calls = {"n": 0}

        @cache(backend=backend, ttl=60, stale_ttl=120, l1_enabled=False, namespace="tenant-secret-ns")
        def compute() -> int:
            calls["n"] += 1
            if calls["n"] > 1:
                raise RuntimeError("secret-detail")
            return 1

        assert compute() == 1
        backend.stale = True
        with caplog.at_level(logging.WARNING, logger=_WRAPPER_LOGGER):
            assert compute() == 1
            assert _wait_for(lambda: _warnings(caplog, "SWR revalidation failed"))
        (warning,) = _warnings(caplog, "SWR revalidation failed")
        _assert_key_free(warning)

    def test_uncopyable_args_warn_that_refresh_ahead_cannot_run(self, caplog: pytest.LogCaptureFixture) -> None:
        backend = FakeSWRBackend()

        @cache(backend=backend, ttl=60, stale_ttl=120, l1_enabled=False, namespace="tenant-secret-ns", key=lambda lock: "k")
        def compute(lock: Any) -> int:
            return 1

        lock = threading.Lock()  # deepcopy(threading.Lock()) raises TypeError
        assert compute(lock) == 1
        backend.stale = True
        with caplog.at_level(logging.WARNING, logger=_WRAPPER_LOGGER):
            assert compute(lock) == 1  # stale served; the skip is logged before this returns
        (warning,) = _warnings(caplog, "SWR revalidation skipped")
        assert "arguments not deep-copyable, so refresh-ahead cannot run for this call" in warning
        _assert_key_free(warning, exc_type="TypeError")

    def test_unschedulable_revalidation_warns(self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
        import cachekit.decorators.wrapper as wrapper_mod

        backend = FakeSWRBackend()

        @cache(backend=backend, ttl=60, stale_ttl=120, l1_enabled=False, namespace="tenant-secret-ns")
        def compute() -> int:
            return 1

        assert compute() == 1
        backend.stale = True
        monkeypatch.setattr(wrapper_mod, "threading", _failing_thread_shim())
        with caplog.at_level(logging.WARNING, logger=_WRAPPER_LOGGER):
            assert compute() == 1
        (warning,) = _warnings(caplog, "SWR revalidation could not be scheduled")
        _assert_key_free(warning)

    async def test_failures_in_one_window_warn_once_and_the_next_warning_carries_the_count(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        backend = FakeSWRBackend()
        calls = {"n": 0}

        @cache(backend=backend, ttl=60, stale_ttl=120, l1_enabled=False)
        async def compute() -> int:
            calls["n"] += 1
            if calls["n"] > 1:
                raise RuntimeError("upstream down")
            return 1

        assert await compute() == 1
        backend.stale = True
        n = 5
        with caplog.at_level(logging.DEBUG, logger=_WRAPPER_LOGGER):
            for attempt in range(1, n + 1):
                assert await compute() == 1
                # The recompute raises without awaiting, so its log line and slot release land
                # in the same task step as the count: the next stale hit revalidates again.
                assert await _await_for(lambda attempt=attempt: calls["n"] == attempt + 1)
            monkeypatch.setattr(hash_utils, "_WARN_INTERVAL_SECONDS", 0.0)  # the window elapses
            assert await compute() == 1
            assert await _await_for(lambda: calls["n"] == n + 2)
        warnings = _warnings(caplog, "SWR revalidation failed")
        debugs = [r for r in caplog.records if r.levelno == logging.DEBUG and "SWR revalidation failed" in r.getMessage()]
        assert len(warnings) == 2  # one per window, not one per failure
        assert "(1 since the last warning)" in warnings[0]
        assert len(debugs) == n - 1  # the rest of the first window, still at DEBUG
        assert f"({n} since the last warning)" in warnings[1]  # none lost from the count

    async def test_each_kind_has_its_own_window(self, caplog: pytest.LogCaptureFixture) -> None:
        """A frequent failure must not hide a rarer, permanent skip on the same function."""
        backend = FakeSWRBackend()
        calls = {"n": 0}

        @cache(backend=backend, ttl=60, stale_ttl=120, l1_enabled=False, key=lambda arg: "k")
        async def compute(arg: Any) -> int:
            calls["n"] += 1
            if calls["n"] > 1:
                raise RuntimeError("upstream down")
            return 1

        assert await compute(1) == 1
        backend.stale = True
        with caplog.at_level(logging.WARNING, logger=_WRAPPER_LOGGER):
            assert await compute(1) == 1  # revalidation fails: the failure window opens
            # Logged and its slot released in one task step, so the next hit can revalidate.
            assert await _await_for(lambda: _warnings(caplog, "SWR revalidation failed"))
            assert await compute(threading.Lock()) == 1  # same key, uncopyable argument: skipped
        assert len(_warnings(caplog, "SWR revalidation skipped")) == 1

    @pytest.mark.skipif(not hasattr(os, "fork"), reason="fork() not available on this platform")
    def test_forked_child_starts_with_its_own_throttle(self) -> None:
        """A child does not inherit a held throttle lock, nor the parent's window and count."""
        import multiprocessing

        from cachekit.hash_utils import _WarnThrottle

        throttle = _WarnThrottle()
        assert throttle.claim() == 1  # the parent's window opens
        assert throttle.claim() == 0  # counted into the parent's next WARNING

        ctx = multiprocessing.get_context("fork")
        queue = ctx.Queue()

        def child(q: Any) -> None:
            result: list[int] = []
            claimer = threading.Thread(target=lambda: result.append(throttle.claim()), daemon=True)
            claimer.start()
            claimer.join(5)
            q.put(result[0] if result else None)

        with throttle._lock:  # a parent thread is mid-claim at fork
            process = ctx.Process(target=child, args=(queue,), daemon=True)
            process.start()
        try:
            claimed = queue.get(timeout=30)
        finally:
            process.join(timeout=30)
            if process.is_alive():
                process.kill()
                process.join()
        assert claimed is not None, "a forked child must not block on a throttle lock it inherited held"
        assert claimed == 1, "a forked child must open its own window with its own count"


class TestPanelFollowUps:
    """LAB-381 panel fast-follow regressions (fixes on top of the merged #228)."""

    def test_sync_revalidation_preserves_contextvars_for_encryption(self) -> None:
        """Panel MAJ: the daemon thread must see the caller's contextvars. With
        encryption + ContextVarExtractor (fail-closed on unset var), background
        revalidation must ACTUALLY execute and store — not silently no-op."""
        from cachekit.decorators.tenant_context import ContextVarExtractor

        backend = FakeSWRBackend()
        calls = {"n": 0}

        @cache(
            backend=backend,
            ttl=60,
            stale_ttl=120,
            l1_enabled=False,
            encryption=True,
            master_key="a" * 64,
            tenant_extractor=ContextVarExtractor(),
        )
        def compute() -> dict[str, int]:
            calls["n"] += 1
            return {"call": calls["n"]}

        ContextVarExtractor.set_tenant_id("550e8400-e29b-41d4-a716-446655440000")
        assert compute()["call"] == 1
        backend.stale = True
        assert compute()["call"] == 1  # stale served

        # Without the copy_context() snapshot the daemon thread hits an unset
        # tenant var -> fail-closed ValueError -> swallowed -> no recompute, no store.
        assert _wait_for(lambda: calls["n"] == 2), "background revalidation must run off-request"
        assert _wait_for(lambda: len(backend.set_calls) == 2), "revalidated value must be stored (encrypt succeeded)"

    def test_no_raw_cache_key_in_thread_name_or_debug_logs(self, caplog: pytest.LogCaptureFixture) -> None:
        """Panel MIN (CWE-532): thread names and SWR debug logs carry no raw key.

        The namespace segment of a cache key can carry tenant/user IDs; the
        revalidation thread name must be static and the SWR debug lines must
        emit only the blake2b-redacted form.
        """
        import logging

        backend = FakeSWRBackend()
        seen_thread_names: list[str] = []

        @cache(backend=backend, ttl=60, stale_ttl=120, l1_enabled=False, namespace="tenant-secret-ns")
        def compute() -> int:
            seen_thread_names.append(threading.current_thread().name)
            return 1

        # Happy path: static thread name, no key slice.
        assert compute() == 1
        backend.stale = True
        assert compute() == 1
        assert _wait_for(lambda: len(backend.set_calls) == 2)
        revalidation_threads = [n for n in seen_thread_names if n.startswith("cachekit-swr")]
        assert revalidation_threads == ["cachekit-swr-revalidate"]  # static, no key slice

        # Failure path: force the 'skipped' debug line (uncopyable arg) and prove
        # the raw key never reaches the log — only the <redacted:...> digest does.
        lock = threading.Lock()
        backend3 = FakeSWRBackend()

        @cache(backend=backend3, ttl=60, stale_ttl=120, l1_enabled=False, namespace="tenant-secret-ns", key=lambda lock: "k3")
        def compute3(lock) -> int:
            return 1

        assert compute3(lock) == 1
        backend3.stale = True
        with caplog.at_level(logging.DEBUG):
            assert compute3(lock) == 1  # stale served; deepcopy fails -> skipped debug line
            time.sleep(0.1)

        swr_lines = [r.getMessage() for r in caplog.records if "SWR revalidation" in r.getMessage()]
        assert swr_lines, "expected the 'skipped' SWR debug line to fire"
        for line in swr_lines:
            assert "tenant-secret-ns" not in line  # raw key redacted (CWE-532)
            assert "<redacted:" in line


class TestFreshnessFailClosedPropagation:
    """Panel should-fix 5: a future except-reorder must not demote the freshness
    getters to fail-open with green tests — mirror the get_cached_value_async
    fail-closed contract on both new getters."""

    def _make_op_fail_closed(self):
        from unittest import mock

        from cachekit.cache_handler import CacheKeyGenerator, CacheOperationHandler, CacheSerializationHandler
        from cachekit.serializers.encryption_wrapper import DecryptionAuthenticationError

        serialization = mock.MagicMock(spec=CacheSerializationHandler)
        serialization.deserialize_data.side_effect = DecryptionAuthenticationError("GCM tag mismatch")
        serialization.encryption_fail_closed = True  # fail closed: MUST propagate
        op = CacheOperationHandler(serialization, CacheKeyGenerator())
        cache_handler = mock.MagicMock()
        cache_handler.get_with_freshness.return_value = (b"tampered", True, 0)

        async def _gwfa(key: str):
            return (b"tampered", True, 0)

        cache_handler.get_with_freshness_async.side_effect = _gwfa
        op.set_cache_handler(cache_handler)
        return op, cache_handler

    def test_sync_freshness_getter_propagates_fail_closed(self) -> None:
        from cachekit.serializers.encryption_wrapper import DecryptionAuthenticationError

        op, cache_handler = self._make_op_fail_closed()
        with pytest.raises(DecryptionAuthenticationError):
            op.get_cached_value_with_freshness("k")
        cache_handler.delete.assert_not_called()  # evidence retained when fail-closed

    async def test_async_freshness_getter_propagates_fail_closed(self) -> None:
        from cachekit.serializers.encryption_wrapper import DecryptionAuthenticationError

        op, cache_handler = self._make_op_fail_closed()
        with pytest.raises(DecryptionAuthenticationError):
            await op.get_cached_value_with_freshness_async("k")
        cache_handler.delete_async.assert_not_called()  # evidence retained when fail-closed


class TestFreshForBoundedL1Backfill:
    """LAB-557 (spec/saas-api.md#remaining-freshness): L1 backfill from an L2
    hit is bounded by the server's remaining freshness — an entry read late in
    its freshness window must not be served fresh from L1 past fresh_until."""

    @staticmethod
    def _l1_put_spy():
        """Patch context recording every redis_ttl passed to L1Cache.put."""
        from unittest import mock

        from cachekit.l1_cache import L1Cache

        seen: list[Any] = []
        original = L1Cache.put

        def spy(self: Any, key: str, value: bytes, redis_ttl: Any = None, expires_at: Any = None) -> None:
            seen.append(redis_ttl)
            return original(self, key, value, redis_ttl=redis_ttl, expires_at=expires_at)

        return seen, mock.patch.object(L1Cache, "put", spy)

    @staticmethod
    async def _seed_then_clear_l1(compute: Any, backend: FakeSWRBackend) -> None:
        """First call stores L2 + L1; clear L1 (restoring the L2 bytes) so the
        next read is an L2 hit that backfills."""
        assert await compute() == 1
        l2_snapshot = dict(backend.store)
        await compute.invalidate_cache()  # type: ignore[attr-defined]
        backend.store.update(l2_snapshot)

    async def test_backfill_bounded_to_remaining_freshness(self) -> None:
        backend = FakeSWRBackend()
        calls = {"n": 0}

        @cache(backend=backend, ttl=60, stale_ttl=120, namespace="ff-bound")
        async def compute() -> int:
            calls["n"] += 1
            return calls["n"]

        await self._seed_then_clear_l1(compute, backend)
        backend.fresh_for = 2  # the read lands 2s before the server's fresh_until

        seen, patcher = self._l1_put_spy()
        with patcher:
            assert await compute() == 1  # L2 hit -> bounded L1 backfill
        assert seen == [2]  # min(ttl=60, fresh_for=2) — never the decorator-scale 60

    async def test_absent_signal_keeps_legacy_backfill(self) -> None:
        backend = FakeSWRBackend()
        calls = {"n": 0}

        @cache(backend=backend, ttl=60, stale_ttl=120, namespace="ff-legacy")
        async def compute() -> int:
            calls["n"] += 1
            return calls["n"]

        await self._seed_then_clear_l1(compute, backend)
        backend.fresh_for = None  # pre-signal server

        seen, patcher = self._l1_put_spy()
        with patcher:
            assert await compute() == 1
        assert seen == [60]  # legacy: the decorator ttl, unchanged behavior

    async def test_read_at_freshness_end_is_never_served_fresh_from_l1(self) -> None:
        """The LAB-557 regression: fresh-labelled hit with 0s remaining must not
        be recorded in L1 — the next read goes back to L2 instead of serving a
        locally-resurrected 'fresh' value past the server's freshness end."""
        backend = FakeSWRBackend()
        calls = {"n": 0}

        @cache(backend=backend, ttl=60, stale_ttl=120, namespace="ff-zero")
        async def compute() -> int:
            calls["n"] += 1
            return calls["n"]

        await self._seed_then_clear_l1(compute, backend)
        backend.fresh_for = 0
        reads_before = backend.freshness_reads

        assert await compute() == 1  # fresh hit, 0s remaining -> no L1 record
        assert await compute() == 1  # MUST reach L2 again (unbounded backfill would serve L1)
        assert backend.freshness_reads == reads_before + 2
        assert calls["n"] == 1  # value itself still served from cache, no recompute

    async def test_provider_resolved_backend_gets_the_bound(self) -> None:
        """A provider-backed decorator (no backend= argument, e.g. @cache.production
        with CACHEKIT_API_KEY set) resolves its CachekitIO backend on first call.
        Capability must be read then, not snapshotted at decoration — or every
        such read skips the freshness path and backfills L1 for the full ttl."""
        from unittest import mock

        backend = FakeSWRBackend()
        calls = {"n": 0}
        provider = mock.MagicMock()
        provider.get_backend.return_value = backend

        with mock.patch("cachekit.decorators.wrapper.get_backend_provider", return_value=provider):

            @cache(ttl=60, namespace="ff-provider")  # no backend= -> provider-resolved
            async def compute() -> int:
                calls["n"] += 1
                return calls["n"]

            await self._seed_then_clear_l1(compute, backend)
            backend.fresh_for = 0
            reads_before = backend.freshness_reads

            assert await compute() == 1  # fresh hit, 0s remaining -> no L1 record
            assert await compute() == 1  # MUST reach L2 again
        assert backend.freshness_reads == reads_before + 2
        assert calls["n"] == 1

    def test_provider_resolved_backend_takes_sync_freshness_read(self) -> None:
        """Sync twin: the sync hit path has no L1 backfill, but its stale-label
        detection also depends on the freshness read being taken at all."""
        from unittest import mock

        backend = FakeSWRBackend()
        provider = mock.MagicMock()
        provider.get_backend.return_value = backend

        with mock.patch("cachekit.decorators.wrapper.get_backend_provider", return_value=provider):

            @cache(ttl=60, namespace="ff-provider-sync", l1_enabled=False)
            def compute() -> int:
                return 1

            assert compute() == 1  # miss -> store
            reads_before = backend.freshness_reads
            assert compute() == 1  # L2 hit
        assert backend.freshness_reads == reads_before + 1

    async def test_bound_applies_without_configured_swr(self) -> None:
        """The unbounded backfill predates SWR: a capable backend bounds the
        backfill even when the decorator configures no stale window."""
        backend = FakeSWRBackend()
        calls = {"n": 0}

        @cache(backend=backend, ttl=60, namespace="ff-noswr")  # no stale_ttl
        async def compute() -> int:
            calls["n"] += 1
            return calls["n"]

        await self._seed_then_clear_l1(compute, backend)
        backend.fresh_for = 3

        seen, patcher = self._l1_put_spy()
        with patcher:
            assert await compute() == 1
        assert seen == [3]

    async def test_stale_hit_without_configured_swr_serves_but_never_revalidates(self) -> None:
        """Gate-widening guard: a mixed-reader stale hit on a decorator without
        a stale window is served (spec: never a blocking miss) but must not
        schedule a background revalidation it doesn't own — and must not
        backfill L1."""
        backend = FakeSWRBackend()
        calls = {"n": 0}

        @cache(backend=backend, ttl=60, namespace="ff-mixed")  # no stale_ttl
        async def compute() -> int:
            calls["n"] += 1
            return calls["n"]

        await self._seed_then_clear_l1(compute, backend)
        backend.stale = True
        backend.fresh_for = 0

        seen, patcher = self._l1_put_spy()
        with patcher:
            assert await compute() == 1  # served, not an error, no recompute paid
        assert seen == []  # stale is never recorded in L1
        await asyncio.sleep(0.2)  # grace: a scheduled revalidation would recompute
        assert calls["n"] == 1
        assert len(backend.set_calls) == 1  # only the seeding write — no revalidation PUT

    async def test_no_ttl_backfill_clamps_to_l1_default_never_extends(self) -> None:
        """Panel finding (CWE-613): with ttl=None the legacy L1 lifetime is
        DEFAULT_L1_TTL_SECONDS — a long server remainder must clamp to it, never
        extend local service toward the 30-day cap (DELETE-as-revocation relies
        on that ageout). A short remainder still shortens."""
        from cachekit.l1_cache import DEFAULT_L1_TTL_SECONDS

        backend = FakeSWRBackend()
        calls = {"n": 0}

        @cache(backend=backend, namespace="ff-nottl")  # ttl=None
        async def compute() -> int:
            calls["n"] += 1
            return calls["n"]

        await self._seed_then_clear_l1(compute, backend)
        backend.fresh_for = 2_592_000  # 30-day remainder from the server

        seen, patcher = self._l1_put_spy()
        with patcher:
            assert await compute() == 1
        assert seen == [DEFAULT_L1_TTL_SECONDS]  # clamped, not extended

        l2_snapshot = dict(backend.store)
        await compute.invalidate_cache()  # type: ignore[attr-defined]
        backend.store.update(l2_snapshot)
        backend.fresh_for = 2  # short remainder still shortens below the default

        seen2, patcher2 = self._l1_put_spy()
        with patcher2:
            assert await compute() == 1
        assert seen2 == [2]
