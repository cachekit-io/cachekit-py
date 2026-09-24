"""L1 memory-bound guarantees, especially the oversized-single-entry vector.

A cached value larger than the entire L1 budget must NOT be stored (it would push L1
permanently over its own limit and, for multi-GB DataFrame envelopes, become an OOM
vector that also evicts every other useful entry). Such values still live in L2.
"""

from __future__ import annotations

import logging
import os
import threading
import time

import pytest

from cachekit.l1_cache import L1Cache, L1CacheManager

MB = 1024 * 1024


@pytest.mark.unit
class TestOversizedEntryRejection:
    def test_entry_larger_than_budget_is_not_stored(self):
        cache = L1Cache(max_memory_mb=1)
        cache.put("big", b"\x00" * (2 * MB), redis_ttl=300)

        found, _ = cache.get("big")
        assert found is False
        assert cache._current_memory_bytes == 0

    def test_rejected_oversized_put_does_not_evict_existing_entries(self):
        """A doomed oversized put must not evict good entries on its way to failing."""
        cache = L1Cache(max_memory_mb=1)
        cache.put("keep", b"\x00" * (512 * 1024), redis_ttl=300)  # fits

        cache.put("toobig", b"\x00" * (5 * MB), redis_ttl=300)  # cannot ever fit

        assert cache.get("keep")[0] is True  # survivor
        assert cache.get("toobig")[0] is False
        assert cache._current_memory_bytes <= cache.max_memory_bytes

    def test_oversized_update_drops_stale_smaller_entry(self):
        """An oversized put for an EXISTING key must drop the stale value, not serve it."""
        cache = L1Cache(max_memory_mb=1)
        cache.put("k", b"\x00" * (256 * 1024), redis_ttl=300)  # fits
        assert cache.get("k")[0] is True

        cache.put("k", b"\x00" * (5 * MB), redis_ttl=300)  # same key, now oversized

        assert cache.get("k")[0] is False  # stale smaller value evicted, not served
        assert cache._current_memory_bytes == 0

    def test_entry_equal_to_budget_is_stored(self):
        cache = L1Cache(max_memory_mb=1)
        cache.put("exact", b"\x00" * (1 * MB), redis_ttl=300)
        assert cache.get("exact")[0] is True

    def test_normal_entry_still_stored(self):
        cache = L1Cache(max_memory_mb=10)
        cache.put("k", b"value", redis_ttl=300)
        assert cache.get("k") == (True, b"value")

    def test_memory_never_exceeds_budget_under_mixed_load(self):
        cache = L1Cache(max_memory_mb=2)
        for i in range(20):
            cache.put(f"k{i}", b"\x00" * (300 * 1024), redis_ttl=300)  # 300KB each
        cache.put("huge", b"\x00" * (50 * MB), redis_ttl=300)  # rejected
        assert cache._current_memory_bytes <= cache.max_memory_bytes


@pytest.mark.unit
class TestL1NonFiniteTtl:
    """A non-finite TTL (NaN/inf) must never produce an immortal L1 entry (#158)."""

    @pytest.mark.parametrize("bad_ttl", [float("nan"), float("inf"), float("-inf")])
    def test_non_finite_redis_ttl_not_stored(self, bad_ttl):
        cache = L1Cache(max_memory_mb=10)
        cache.put("k", b"value", redis_ttl=bad_ttl)
        assert cache.get("k")[0] is False
        assert cache._current_memory_bytes == 0

    @pytest.mark.parametrize("bad_ttl", [float("nan"), float("inf"), float("-inf")])
    def test_non_finite_expires_at_not_stored(self, bad_ttl):
        cache = L1Cache(max_memory_mb=10)
        cache.put("k", b"value", expires_at=bad_ttl)
        assert cache.get("k")[0] is False
        assert cache._current_memory_bytes == 0


@pytest.mark.unit
class TestNonBytesRejection:
    """#171 blocker C belt-and-suspenders: L1 stores raw bytes ONLY.

    An mmap-backed memoryview must never reach L1 — it would pin the mapped file's inode for the
    whole L1 TTL (silent staleness on POSIX, write failures on Windows, RSS blowup under hot keys).
    The mmap read path confines the view to the deserialize frame, but a future refactor could
    regress; this guard makes that regression a loud TypeError instead of a silent alias. bytearray
    is rejected too (a mutable buffer could change underneath the cache).
    """

    def test_put_rejects_memoryview(self):
        cache = L1Cache(max_memory_mb=10)
        with pytest.raises(TypeError):
            cache.put("k", memoryview(b"data"), redis_ttl=300)  # type: ignore[arg-type]
        assert cache.get("k")[0] is False

    def test_put_rejects_bytearray(self):
        cache = L1Cache(max_memory_mb=10)
        with pytest.raises(TypeError):
            cache.put("k", bytearray(b"data"), redis_ttl=300)  # type: ignore[arg-type]

    def test_put_accepts_bytes(self):
        cache = L1Cache(max_memory_mb=10)
        cache.put("k", b"data", redis_ttl=300)
        assert cache.get("k")[0] is True


@pytest.mark.unit
class TestConfiguredBudgetWiring:
    """Issue #163: l1_max_size_mb / L1CacheConfig.max_size_mb must reach L1Cache.

    Before the fix, the wrapper called get_l1_cache(namespace) and the manager
    hardcoded default_max_memory_mb=100 — configuring the budget was silently
    ignored. These tests pin the wiring at every layer.
    """

    def test_configured_budget_enforced_with_eviction(self):
        """Filling past a configured (non-default) 2MB budget evicts LRU entries."""
        cache = L1Cache(max_memory_mb=2)
        for i in range(5):  # 5 x 512KB = 2.5MB > 2MB budget
            cache.put(f"k{i}", b"\x00" * (512 * 1024), redis_ttl=300)

        assert cache._current_memory_bytes <= 2 * MB
        assert cache.get("k0")[0] is False  # oldest evicted
        assert cache.get("k4")[0] is True  # newest survives
        assert cache._evictions > 0

    def test_manager_default_reads_settings(self, monkeypatch):
        """L1CacheManager() with no explicit default uses CACHEKIT_L1_MAX_SIZE_MB."""
        from cachekit.config.singleton import reset_settings
        from cachekit.l1_cache import L1CacheManager

        monkeypatch.setenv("CACHEKIT_L1_MAX_SIZE_MB", "7")
        reset_settings()
        try:
            manager = L1CacheManager()
            cache = manager.get_cache("settings-budget-ns")
            assert cache.max_memory_bytes == 7 * MB
        finally:
            reset_settings()

    def test_manager_explicit_default_wins_over_settings(self):
        from cachekit.l1_cache import L1CacheManager

        manager = L1CacheManager(default_max_memory_mb=3)
        assert manager.get_cache("explicit-default-ns").max_memory_bytes == 3 * MB

    def test_get_cache_per_namespace_override(self):
        from cachekit.l1_cache import L1CacheManager

        manager = L1CacheManager(default_max_memory_mb=100)
        cache = manager.get_cache("override-ns", max_size_mb=5)
        assert cache.max_memory_bytes == 5 * MB

    def test_first_configuration_wins_per_namespace(self, caplog):
        """A second, conflicting budget for an existing namespace is ignored loudly."""
        import logging

        from cachekit.l1_cache import L1CacheManager

        manager = L1CacheManager(default_max_memory_mb=100)
        first = manager.get_cache("conflict-ns", max_size_mb=5)
        with caplog.at_level(logging.WARNING, logger="cachekit.l1_cache"):
            second = manager.get_cache("conflict-ns", max_size_mb=50)

        assert second is first
        assert second.max_memory_bytes == 5 * MB
        assert any("first configuration wins" in r.message for r in caplog.records)

    def test_decorator_config_budget_reaches_l1_cache(self):
        """End-to-end: DecoratorConfig(l1=L1CacheConfig(max_size_mb=N)) sizes the L1Cache."""
        import uuid

        from cachekit.config import DecoratorConfig
        from cachekit.config.nested import L1CacheConfig
        from cachekit.decorators.wrapper import create_cache_wrapper
        from cachekit.l1_cache import get_l1_cache

        namespace = f"budget-wiring-{uuid.uuid4().hex[:8]}"
        config = DecoratorConfig(namespace=namespace, l1=L1CacheConfig(max_size_mb=9))

        def fn(x: int) -> int:
            return x * 2

        create_cache_wrapper(fn, config=config)

        assert get_l1_cache(namespace).max_memory_bytes == 9 * MB

    @pytest.mark.parametrize("preset", ["minimal", "production", "dev", "test", "secure", "io"])
    def test_presets_do_not_pin_l1_budget(self, preset, monkeypatch):
        """Regression guard (issue #163): every intent preset must leave max_size_mb=None so
        CACHEKIT_L1_MAX_SIZE_MB is honored. The prior concrete default (100) was passed
        explicitly by the wrapper and silently shadowed the settings-derived budget on
        every @cache.* decorator. Combined with test_manager_default_reads_settings
        (None -> settings), this proves the env var reaches presets end-to-end.
        """
        from cachekit.config import DecoratorConfig

        if preset == "secure":
            config = DecoratorConfig.secure(master_key="a" * 64)
        elif preset == "io":
            monkeypatch.setenv("CACHEKIT_API_KEY", "ck_test_key")
            config = DecoratorConfig.io()
        else:
            config = getattr(DecoratorConfig, preset)()

        assert config.l1.max_size_mb is None


def _wait_for(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


def _as_if_forked(manager: L1CacheManager, parent_ran_cleanup: bool) -> None:
    """Leave the manager in the state fork() hands a child: dead thread, foreign owner."""
    manager._cleanup_thread = threading.Thread(target=lambda: None) if parent_ran_cleanup else None
    manager._owner_pid = -1


@pytest.mark.unit
class TestCleanupThreadAfterFork:
    """LAB-4772: a prefork child must run its own L1 cleanup thread."""

    @pytest.mark.skipif(not hasattr(os, "fork"), reason="fork() not available on this platform")
    def test_forked_child_restarts_cleanup_on_first_put(self):
        import multiprocessing

        manager = L1CacheManager(default_max_memory_mb=10)
        cache = manager.get_cache("fork-ns")  # captured pre-fork, like a decorator's _l1_cache
        manager.start_background_cleanup(interval_seconds=0.05)
        try:
            ctx = multiprocessing.get_context("fork")
            queue = ctx.Queue()

            def child(q) -> None:
                cache.put("k", b"v", redis_ttl=1.2)  # minus the 1s ttl buffer: expires in ~0.2s
                thread = manager._cleanup_thread
                # No get(): only the background sweep can count this eviction.
                swept = _wait_for(lambda: cache.get_stats()["expired_evictions"] == 1)
                q.put({"alive": thread is not None and thread.is_alive(), "swept": swept})

            process = ctx.Process(target=child, args=(queue,))
            process.start()
            try:
                outcome = queue.get(timeout=30)
            finally:
                process.join(timeout=30)
                if process.is_alive():  # a hung child would otherwise block pytest's exit
                    process.kill()

            assert process.exitcode == 0
            assert outcome == {"alive": True, "swept": True}  # fork leaves the inherited thread dead
        finally:
            manager.stop_background_cleanup()

    @pytest.mark.skipif(not hasattr(os, "fork"), reason="fork() not available on this platform")
    @pytest.mark.parametrize("put_from_new_thread", [False, True], ids=["forking-thread", "reused-ident"])
    def test_forked_child_survives_cache_lock_held_at_fork(self, put_from_new_thread):
        import multiprocessing
        import queue as queue_mod

        manager = L1CacheManager(default_max_memory_mb=10)
        cache = manager.get_cache("held-ns")
        cache.put("pre-fork", b"v")
        manager.start_background_cleanup(interval_seconds=0.05)
        held, release = threading.Event(), threading.Event()

        def hold() -> None:
            with cache._lock:
                held.set()
                release.wait()

        holder = threading.Thread(target=hold, daemon=True)
        holder.start()
        assert held.wait(5)
        try:
            ctx = multiprocessing.get_context("fork")
            queue = ctx.Queue()

            def child(q) -> None:
                def put() -> None:
                    cache.put("live", b"v")
                    cache.put("short", b"v", redis_ttl=1.2)  # minus the 1s ttl buffer: expires in ~0.2s

                if put_from_new_thread:  # the child's first new thread reuses the dead holder's ident
                    t = threading.Thread(target=put)
                    t.start()
                    t.join()
                else:
                    put()
                found = cache.get("live")[0]  # from the forking thread, never the holder's ident
                swept = _wait_for(lambda: cache.get_stats()["expired_evictions"] == 1)
                q.put({"found": found, "swept": swept})

            process = ctx.Process(target=child, args=(queue,))
            process.start()
            try:
                outcome = queue.get(timeout=20)
            except queue_mod.Empty:
                outcome = "child deadlocked on the lock held at fork"
            finally:
                process.join(timeout=10)
                if process.is_alive():  # a hung child would otherwise block pytest's exit
                    process.kill()

            assert outcome == {"found": True, "swept": True}
            assert process.exitcode == 0
        finally:
            release.set()
            holder.join(5)
            manager.stop_background_cleanup()

    def test_cleanup_stopped_in_parent_stays_stopped(self):
        manager = L1CacheManager(default_max_memory_mb=10)
        cache = manager.get_cache("stopped-ns")
        _as_if_forked(manager, parent_ran_cleanup=False)

        cache.put("k", b"v")

        assert manager._cleanup_thread is None
        assert manager._owner_pid == os.getpid()

    def test_restart_failure_is_logged_once_and_put_still_stores(self, monkeypatch, caplog):
        manager = L1CacheManager(default_max_memory_mb=10)
        cache = manager.get_cache("refused-ns")
        _as_if_forked(manager, parent_ran_cleanup=True)

        def refuse(self):
            raise RuntimeError("can't start new thread")

        monkeypatch.setattr(threading.Thread, "start", refuse)
        with caplog.at_level(logging.WARNING, logger="cachekit.l1_cache"):
            cache.put("k1", b"v")
            cache.put("k2", b"v")

        assert cache.get("k1")[0] and cache.get("k2")[0]
        assert sum("restart after fork failed" in r.message for r in caplog.records) == 1
        assert manager._cleanup_thread is None  # the dead inherited thread, not kept as if running

    def test_unexpected_restart_error_propagates_and_next_put_retries(self, monkeypatch):
        class BugError(Exception):  # not TypeError: that is put()'s own documented refusal
            pass

        manager = L1CacheManager(default_max_memory_mb=10)
        cache = manager.get_cache("bug-ns")
        _as_if_forked(manager, parent_ran_cleanup=True)

        def broken(self):
            raise BugError

        monkeypatch.setattr(threading.Thread, "start", broken)
        with pytest.raises(BugError):
            cache.put("k", b"v")

        monkeypatch.undo()
        cache.put("k", b"v")
        try:
            assert manager._cleanup_thread is not None and manager._cleanup_thread.is_alive()
        finally:
            manager.stop_background_cleanup()

    def test_start_replaces_dead_thread_instead_of_noop(self):
        manager = L1CacheManager(default_max_memory_mb=10)
        manager._cleanup_thread = threading.Thread(target=lambda: None)  # never started: dead

        manager.start_background_cleanup(interval_seconds=60)
        try:
            assert manager._cleanup_thread.is_alive()
        finally:
            manager.stop_background_cleanup()

    def test_racing_first_puts_restart_cleanup_once(self):
        """Smoke test under the GIL; on the free-threaded lane, dropping the take-over lock fails it."""
        duplicated = 0
        for _ in range(200):
            manager = L1CacheManager(default_max_memory_mb=10)
            cache = manager.get_cache("hammer-ns")
            _as_if_forked(manager, parent_ran_cleanup=True)
            spawns = []
            spawn = manager._spawn_cleanup_thread
            manager._spawn_cleanup_thread = lambda interval, spawn=spawn, spawns=spawns: (spawns.append(1), spawn(interval))
            barrier = threading.Barrier(32)
            workers = [
                threading.Thread(target=lambda b=barrier, c=cache: (b.wait(), c.put("k", b"v")), daemon=True) for _ in range(32)
            ]
            for t in workers:
                t.start()
            for t in workers:
                t.join(timeout=10)
            assert not any(t.is_alive() for t in workers), "take-over deadlocked"
            manager.stop_background_cleanup()
            duplicated += len(spawns) != 1

        assert duplicated == 0, f"{duplicated}/200 forked children restarted cleanup more than once"
