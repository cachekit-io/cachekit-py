"""L1 memory-bound guarantees, especially the oversized-single-entry vector.

A cached value larger than the entire L1 budget must NOT be stored (it would push L1
permanently over its own limit and, for multi-GB DataFrame envelopes, become an OOM
vector that also evicts every other useful entry). Such values still live in L2.
"""

from __future__ import annotations

import ast
import logging
import os
import random
import select
import signal
import sys
import threading
import time
from collections.abc import Callable
from typing import NoReturn

import pytest

from cachekit.l1_cache import CacheEntry, L1Cache, L1CacheManager

MB = 1024 * 1024


@pytest.mark.unit
class TestOversizedEntryRejection:
    def test_entry_larger_than_budget_is_not_stored(self):
        cache = L1Cache(max_memory_mb=1)
        cache.put("big", b"\x00" * (2 * MB), redis_ttl=300)

        found, _ = cache.get("big")
        assert found is False
        assert cache._state.memory_bytes == 0

    def test_rejected_oversized_put_does_not_evict_existing_entries(self):
        """A doomed oversized put must not evict good entries on its way to failing."""
        cache = L1Cache(max_memory_mb=1)
        cache.put("keep", b"\x00" * (512 * 1024), redis_ttl=300)  # fits

        cache.put("toobig", b"\x00" * (5 * MB), redis_ttl=300)  # cannot ever fit

        assert cache.get("keep")[0] is True  # survivor
        assert cache.get("toobig")[0] is False
        assert cache._state.memory_bytes <= cache.max_memory_bytes

    def test_oversized_update_drops_stale_smaller_entry(self):
        """An oversized put for an EXISTING key must drop the stale value, not serve it."""
        cache = L1Cache(max_memory_mb=1)
        cache.put("k", b"\x00" * (256 * 1024), redis_ttl=300)  # fits
        assert cache.get("k")[0] is True

        cache.put("k", b"\x00" * (5 * MB), redis_ttl=300)  # same key, now oversized

        assert cache.get("k")[0] is False  # stale smaller value evicted, not served
        assert cache._state.memory_bytes == 0

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
        assert cache._state.memory_bytes <= cache.max_memory_bytes


def _held_bytes(cache: L1Cache) -> int:
    return sum(entry.size_bytes for entry in cache._state.cache.values())


@pytest.mark.unit
class TestUpdateUnderPressure:
    """LAB-6897: updating a key whose old entry is LRU must not count its bytes twice."""

    def test_lru_first_update_keeps_count_and_budget(self):
        cache = L1Cache(max_memory_mb=1)
        cache.put("A", b"\x00" * 512_000, redis_ttl=300)
        cache.put("B", b"\x00" * 512_000, redis_ttl=300)
        cache.put("A", b"\x00" * 614_400, redis_ttl=300)  # A is LRU: eviction walks past it first
        assert _consistent(cache)
        assert _held_bytes(cache) <= cache.max_memory_bytes

    @pytest.mark.parametrize(
        ("keys", "min_size", "max_size"),
        [(200, 10 * 1024, 60 * 1024), (8, 100 * 1024, 300 * 1024)],
        ids=["200-keys-10-60KB", "8-keys-100-300KB"],
    )
    def test_random_update_heavy_load_keeps_count_and_budget(self, keys, min_size, max_size):
        rng = random.Random(6897)
        cache = L1Cache(max_memory_mb=1)
        for i in range(5000):
            cache.put(f"k{rng.randrange(keys)}", b"\x00" * rng.randint(min_size, max_size), redis_ttl=300)
            assert _consistent(cache), f"count drifted at put {i}"
            assert cache._state.memory_bytes >= 0, f"count negative at put {i}"
            assert _held_bytes(cache) <= cache.max_memory_bytes, f"over budget at put {i}"


@pytest.mark.unit
class TestL1NonFiniteTtl:
    """A non-finite TTL (NaN/inf) must never produce an immortal L1 entry (#158)."""

    @pytest.mark.parametrize("bad_ttl", [float("nan"), float("inf"), float("-inf")])
    def test_non_finite_redis_ttl_not_stored(self, bad_ttl):
        cache = L1Cache(max_memory_mb=10)
        cache.put("k", b"value", redis_ttl=bad_ttl)
        assert cache.get("k")[0] is False
        assert cache._state.memory_bytes == 0

    @pytest.mark.parametrize("bad_ttl", [float("nan"), float("inf"), float("-inf")])
    def test_non_finite_expires_at_not_stored(self, bad_ttl):
        cache = L1Cache(max_memory_mb=10)
        cache.put("k", b"value", expires_at=bad_ttl)
        assert cache.get("k")[0] is False
        assert cache._state.memory_bytes == 0


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

        assert cache._state.memory_bytes <= 2 * MB
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


def _report(w: int, outcome: object) -> NoReturn:
    """End a child forked with os.fork() with an outcome for its parent; never return into pytest."""
    try:
        os.write(w, repr(outcome).encode())  # literals only: ast.literal_eval reads it
    finally:
        os._exit(0)


def _child_outcome(pid: int, r: int, timeout: float = 20.0) -> object:
    """What the child at pid reported on the pipe r; a hung child is killed."""
    assert pid > 0, "no child was forked"  # os.kill(0, ...) would signal pytest's whole process group
    data = os.read(r, 65536) if select.select([r], [], [], timeout)[0] else b""
    if not data:
        os.kill(pid, signal.SIGKILL)
    os.waitpid(pid, 0)
    os.close(r)
    return ast.literal_eval(data.decode()) if data else "no outcome: the child hung or died"


def _on_new_thread(fn: Callable[[], object], timeout: float = 5.0) -> object:
    """fn's result from a new thread, or "hung" if it holds the thread past timeout."""
    out: list[object] = []
    thread = threading.Thread(target=lambda: out.append(fn()), daemon=True)
    thread.start()
    thread.join(timeout)
    return out[0] if out else "hung"


def _consistent(cache: L1Cache) -> bool:
    s = cache._state
    return s.memory_bytes == sum(entry.size_bytes for entry in s.cache.values())


def _as_if_forked(manager: L1CacheManager, parent_ran_cleanup: bool) -> None:
    """Leave the manager in the state fork() hands a child: dead thread, foreign owner."""
    manager._cleanup_thread = threading.Thread(target=lambda: None) if parent_ran_cleanup else None
    manager._owner_pid = -1


def _as_if_hookless(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make this process a child the at-fork hook never reached (uWSGI without --py-call-osafterfork)."""
    from cachekit import l1_cache

    monkeypatch.setattr(l1_cache, "_import_pid", -1)
    monkeypatch.setattr(l1_cache, "_hooked_pid", None)


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
    @pytest.mark.parametrize("get_first", [False, True], ids=["take-over-put-first", "hook-get-first"])
    def test_forked_child_survives_cache_lock_held_at_fork(self, monkeypatch, put_from_new_thread, get_first):
        import multiprocessing
        import queue as queue_mod
        import weakref

        from cachekit import l1_cache

        manager = L1CacheManager(default_max_memory_mb=10)
        if not get_first:  # hide the manager from the at-fork hook, as a fork without hooks (uWSGI) does
            monkeypatch.setattr(l1_cache, "_managers", weakref.WeakSet())
        cache = manager.get_cache("held-ns")
        cache.put("pre-fork", b"v")
        manager.start_background_cleanup(interval_seconds=0.05)
        held, release = threading.Event(), threading.Event()

        def hold() -> None:
            with cache._state.lock:
                held.set()
                release.wait()

        holder = threading.Thread(target=hold, daemon=True)
        holder.start()
        assert held.wait(5)
        try:
            ctx = multiprocessing.get_context("fork")
            queue = ctx.Queue()

            def child(q) -> None:
                if not get_first:
                    l1_cache._hooked_pid = None  # and undo the mark the hook still left on this process

                def put() -> None:
                    if get_first:  # a decorator's first call: get() before any put() runs the take-over
                        assert cache.get("pre-fork") == (False, None)  # dropped: the holder may have torn it
                    cache.put("live", b"v")
                    cache.put("short", b"v", redis_ttl=1.2)  # minus the 1s ttl buffer: expires in ~0.2s

                if put_from_new_thread:  # the child's first new thread reuses the dead holder's ident
                    t = threading.Thread(target=put)
                    t.start()
                    t.join()
                else:
                    put()
                found = cache.get("live")[0]  # from the forking thread, never the holder's ident
                if get_first:
                    q.put({"found": found, "swept": _wait_for(lambda: cache.get_stats()["expired_evictions"] == 1)})
                else:  # no hook ran, so the take-over started no cleanup thread (LAB-7271)
                    q.put({"found": found, "cleanup_off": manager._cleanup_thread is None})

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

            assert outcome == ({"found": True, "swept": True} if get_first else {"found": True, "cleanup_off": True})
            assert process.exitcode == 0
        finally:
            release.set()
            holder.join(5)
            manager.stop_background_cleanup()

    @pytest.mark.skipif(not hasattr(os, "fork"), reason="fork() not available on this platform")
    def test_take_over_leaves_a_child_threads_cache_lock_alone(self):
        import multiprocessing

        manager = L1CacheManager(default_max_memory_mb=10)
        busy = manager.get_cache("busy-ns")
        other = manager.get_cache("other-ns")
        busy.put("pre-fork", b"v")
        manager.start_background_cleanup(interval_seconds=30)
        try:
            ctx = multiprocessing.get_context("fork")
            queue = ctx.Queue()

            def child(q) -> None:
                busy._state.cache["child-entry"] = CacheEntry(b"v", time.time() + 60, 1)  # no put: no take-over yet
                busy._state.memory_bytes += 1
                lock = busy._state.lock  # the hook's fresh state
                held, release = threading.Event(), threading.Event()

                def hold() -> None:
                    with busy._state.lock:
                        held.set()
                        release.wait(10)

                holder = threading.Thread(target=hold)
                holder.start()
                assert held.wait(5)  # else the take-over below never meets a live holder and the test proves nothing
                other.put("k", b"v")  # first put runs the take-over while a live child thread holds busy-ns
                release.set()
                holder.join(5)
                q.put({"same_lock": busy._state.lock is lock, "found": busy.get("child-entry")[0]})

            process = ctx.Process(target=child, args=(queue,))
            process.start()
            try:
                outcome = queue.get(timeout=20)
            finally:
                process.join(timeout=10)
                if process.is_alive():  # a hung child would otherwise block pytest's exit
                    process.kill()

            assert outcome == {"same_lock": True, "found": True}  # not cleared under its holder
            assert process.exitcode == 0
        finally:
            manager.stop_background_cleanup()

    @pytest.mark.skipif(not hasattr(os, "fork"), reason="fork() not available on this platform")
    @pytest.mark.parametrize("two_managers", [False, True], ids=["one-manager", "two-managers"])
    def test_forked_child_starts_with_an_empty_l1(self, two_managers):
        """Every cache is empty in the child: one whose lock was free at fork, and one a parent thread held."""
        import multiprocessing
        import queue as queue_mod

        from cachekit import l1_cache

        first = L1CacheManager(default_max_memory_mb=10)
        second = L1CacheManager(default_max_memory_mb=10) if two_managers else first
        free, held_ns = first.get_cache("free-at-fork-ns"), second.get_cache("held-at-fork-ns")
        caches = [free, held_ns]
        for cache in caches:
            cache.put("pre-fork", b"v", redis_ttl=60)
        held, release = threading.Event(), threading.Event()

        def hold() -> None:
            with held_ns._state.lock:  # a parent thread mid-critical-section when the fork lands
                held.set()
                release.wait()

        holder = threading.Thread(target=hold, daemon=True)
        holder.start()
        assert held.wait(5)
        try:
            ctx = multiprocessing.get_context("fork")
            queue = ctx.Queue()

            def child(q) -> None:
                # From the child's main thread: a lock the hook left orphaned hangs here for good.
                outcome = {
                    "found": [cache.get("pre-fork")[0] for cache in caches],
                    "bytes": [cache._state.memory_bytes for cache in caches],
                    "hooked": l1_cache._hooked_pid == os.getpid(),
                }
                free.put("child", b"v")
                outcome["child_put"] = free.get("child")
                q.put(outcome)

            process = ctx.Process(target=child, args=(queue,))
            process.start()
            try:
                outcome = queue.get(timeout=20)
            except queue_mod.Empty:
                outcome = "child hung on a cache lock the at-fork hook left orphaned"
            finally:
                process.join(timeout=10)
                if process.is_alive():  # a hung child would otherwise block pytest's exit
                    process.kill()

            assert outcome == {"found": [False, False], "bytes": [0, 0], "hooked": True, "child_put": (True, b"v")}
            assert process.exitcode == 0
            assert free.get("pre-fork") == (True, b"v")  # the parent keeps its own entries
        finally:
            release.set()
            holder.join(5)

    def test_at_fork_hook_publishes_fresh_states_without_taking_a_lock(self, monkeypatch):
        """The hook replaces each state in one store: no clear in place, no lock acquired."""
        import weakref

        from cachekit import l1_cache

        manager = L1CacheManager(default_max_memory_mb=10)
        monkeypatch.setattr(l1_cache, "_managers", weakref.WeakSet([manager]))  # leave the global manager alone
        monkeypatch.setattr(l1_cache, "_hooked_pid", None)  # restored after the in-process hook call
        monkeypatch.setattr(l1_cache, "_inherited_states", [])
        caches = [manager.get_cache("free-ns"), manager.get_cache("held-ns")]
        for cache in caches:
            cache.put("pre-fork", b"v")
        old = [cache._state for cache in caches]
        held, release = threading.Event(), threading.Event()

        def hold() -> None:
            with old[1].lock:  # another thread's hold: acquiring it would block the hook
                held.set()
                release.wait(10)

        holder = threading.Thread(target=hold, daemon=True)
        holder.start()
        assert held.wait(5)
        try:
            assert _on_new_thread(l1_cache._empty_caches_after_fork, timeout=5) is None  # returned, never blocked
        finally:
            release.set()
            holder.join(5)

        assert [cache._state is state for cache, state in zip(caches, old, strict=True)] == [False, False]
        assert [list(state.cache) for state in old] == [["pre-fork"], ["pre-fork"]]  # a holder finishes on its own
        # Kept, not freed inside fork(): freeing would copy the parent's L1 pages into the child.
        assert l1_cache._inherited_states == old
        assert [cache.get("pre-fork")[0] for cache in caches] == [False, False]
        assert l1_cache._hooked_pid == os.getpid()

    def test_release_frees_inherited_states_and_leaves_one_in_use_alone(self, monkeypatch):
        """A thread that forked inside a state's critical section still runs on it: that one is only dropped."""
        from cachekit import l1_cache

        free_state, held_state = l1_cache._L1State(), l1_cache._L1State()
        for state in (free_state, held_state):
            for i in range(2500):  # more than one release batch
                state.cache[f"k{i}"] = CacheEntry(b"v", time.time() + 60, 1)
            state.memory_bytes = 2500
        monkeypatch.setattr(l1_cache, "_inherited_states", [held_state, free_state])
        held, release = threading.Event(), threading.Event()

        def hold() -> None:
            with held_state.lock:
                held.set()
                release.wait(10)

        holder = threading.Thread(target=hold, daemon=True)
        holder.start()
        assert held.wait(5)
        try:
            l1_cache._release_inherited_states()
        finally:
            release.set()
            holder.join(5)

        assert l1_cache._inherited_states == []
        assert len(free_state.cache) == 0 and free_state.memory_bytes == 0
        assert len(held_state.cache) == 2500  # its holder finishes on it, untouched

    def test_cleanup_thread_frees_inherited_states_when_it_starts(self, monkeypatch):
        from cachekit import l1_cache

        state = l1_cache._L1State()
        state.cache["k"] = CacheEntry(b"v", time.time() + 60, 1)
        monkeypatch.setattr(l1_cache, "_inherited_states", [state])
        manager = L1CacheManager(default_max_memory_mb=10)
        manager.start_background_cleanup(interval_seconds=600)  # freed at start, not after a sweep interval
        try:
            assert _wait_for(lambda: not l1_cache._inherited_states)
            assert len(state.cache) == 0
        finally:
            manager.stop_background_cleanup()

    def test_cleanup_stopped_in_parent_stays_stopped(self):
        manager = L1CacheManager(default_max_memory_mb=10)
        cache = manager.get_cache("stopped-ns")
        _as_if_forked(manager, parent_ran_cleanup=False)

        cache.put("k", b"v")

        assert manager._cleanup_thread is None
        assert manager._owner_pid == os.getpid()

    def test_orphaned_cache_lock_reset_leaves_the_old_state_alone_and_logs_nothing(self, monkeypatch, caplog):
        manager = L1CacheManager(default_max_memory_mb=10)
        cache = manager.get_cache("orphan-ns")
        cache.put("pre-fork", b"v")
        old = cache._state
        held_bytes = old.memory_bytes
        _as_if_forked(manager, parent_ran_cleanup=False)
        _as_if_hookless(monkeypatch)
        old.lock.acquire()  # _is_owned(): how a child thread reusing the dead holder's ident sees the hold
        try:
            with caplog.at_level(logging.WARNING, logger="cachekit.l1_cache"):
                cache.put("k", b"v")
        finally:
            old.lock.release()

        assert cache._state is not old
        # A holder still inside a critical section finds its state as it left it.
        assert list(old.cache) == ["pre-fork"] and old.memory_bytes == held_bytes
        assert not cache.get("pre-fork")[0] and cache.get("k")[0]
        assert caplog.records == []  # logging's own locks may be orphaned in this child

    @pytest.mark.skipif(not hasattr(os, "fork"), reason="fork() not available on this platform")
    def test_fork_inside_a_critical_section_lets_the_child_finish_it(self, monkeypatch):
        """A fork from a signal handler or finalizer inside get(): the child returns into it."""
        manager = L1CacheManager(default_max_memory_mb=10)
        cache = manager.get_cache("fork-in-get-ns")
        cache.put("k", b"v")
        real = CacheEntry.is_expired
        parent, (r, w) = os.getpid(), os.pipe()
        child = 0

        def fork_here(entry: CacheEntry) -> bool:  # runs inside get()'s critical section
            nonlocal child
            monkeypatch.setattr(CacheEntry, "is_expired", real)
            child = os.fork()
            return real(entry)

        monkeypatch.setattr(CacheEntry, "is_expired", fork_here)
        try:
            found = cache.get("k")
        except BaseException as e:
            if os.getpid() != parent:
                _report(w, {"error": repr(e)})  # the reset emptied the dict under the in-flight get()
            raise
        if os.getpid() != parent:
            try:
                outcome = {
                    "found": found,
                    "consistent": _consistent(cache),
                    "new_thread": _on_new_thread(lambda: (cache.put("n", b"v"), cache.get("n"))[1]),
                }
            except BaseException as e:
                outcome = {"error": repr(e)}
            _report(w, outcome)
        os.close(w)

        assert child, "get() no longer calls CacheEntry.is_expired"
        assert _child_outcome(child, r) == {"found": (True, b"v"), "consistent": True, "new_thread": (True, b"v")}

    @pytest.mark.skipif(not hasattr(os, "fork"), reason="fork() not available on this platform")
    def test_fork_inside_a_critical_section_the_child_never_leaves(self, monkeypatch):
        """multiprocessing's child runs its target, then os._exit()s without unwinding to the critical section."""
        manager = L1CacheManager(default_max_memory_mb=10)
        cache = manager.get_cache("fork-never-returns-ns")
        cache.put("pre-fork", b"v")
        real = CacheEntry.is_expired
        r, w = os.pipe()
        child = 0

        def fork_here(entry: CacheEntry) -> bool:
            nonlocal child
            monkeypatch.setattr(CacheEntry, "is_expired", real)
            child = os.fork()
            if child == 0:
                try:  # this thread holds the old lock for good, so new threads must not need it
                    outcome = {
                        "pre_fork": _on_new_thread(lambda: cache.get("pre-fork")),
                        "new_thread": _on_new_thread(lambda: (cache.put("n", b"v"), cache.get("n"))[1]),
                    }
                except BaseException as e:
                    outcome = {"error": repr(e)}
                _report(w, outcome)
            return real(entry)

        monkeypatch.setattr(CacheEntry, "is_expired", fork_here)
        assert cache.get("pre-fork") == (True, b"v")
        os.close(w)
        assert child, "get() no longer calls CacheEntry.is_expired"

        # The fork dropped the namespace's entries; L2 still has them.
        assert _child_outcome(child, r) == {"pre_fork": (False, None), "new_thread": (True, b"v")}

    @pytest.mark.skipif(not hasattr(os, "fork"), reason="fork() not available on this platform")
    def test_hookless_take_over_past_a_live_holder_leaves_it_unharmed(self, monkeypatch):
        """Without hooks (uWSGI), a child thread stalled inside get() past the 1 s probe looks orphaned."""
        import weakref

        from cachekit import l1_cache

        manager = L1CacheManager(default_max_memory_mb=10)
        monkeypatch.setattr(l1_cache, "_managers", weakref.WeakSet())  # hide it from the at-fork hook
        cache = manager.get_cache("slow-holder-ns")
        cache.put("pre-fork", b"v")
        r, w = os.pipe()
        child = os.fork()
        if child == 0:
            try:
                l1_cache._hooked_pid = None  # and undo the mark the hook still left on this process
                stalled, resume = threading.Event(), threading.Event()
                real = CacheEntry.is_expired

                def stall(entry: CacheEntry) -> bool:
                    if threading.current_thread().name == "holder":
                        stalled.set()
                        resume.wait(10)
                    return real(entry)

                CacheEntry.is_expired = stall  # child-only, so nothing to restore
                got: list[object] = []

                def hold() -> None:
                    try:
                        got.append(cache.get("pre-fork"))
                    except BaseException as e:  # the reset emptied the dict under the stalled get()
                        got.append(repr(e))

                holder = threading.Thread(target=hold, name="holder")
                holder.start()
                assert stalled.wait(5)
                cache.put("k", b"v")  # the first put: the take-over probes for 1 s, then resets
                resume.set()
                holder.join(5)
                outcome = {"holder": got, "found": cache.get("k"), "consistent": _consistent(cache)}
            except BaseException as e:
                outcome = {"error": repr(e)}
            _report(w, outcome)
        os.close(w)

        assert _child_outcome(child, r) == {"holder": [(True, b"v")], "found": (True, b"v"), "consistent": True}

    def test_invalidation_queued_behind_a_replaced_lock_reaches_the_fresh_state(self):
        cache = L1Cache(namespace="requeue-ns")
        old = cache._state
        real_lock = old.lock
        held, waiting, release = threading.Event(), threading.Event(), threading.Event()

        class SignalOnWait:  # the old lock, announcing when the invalidating thread queues on it
            def __enter__(self) -> None:
                waiting.set()
                real_lock.acquire()

            def __exit__(self, *exc: object) -> None:
                real_lock.release()

        def hold() -> None:
            with real_lock:
                held.set()
                release.wait(5)

        holder = threading.Thread(target=hold)
        holder.start()
        assert held.wait(5)
        old.lock = SignalOnWait()  # type: ignore[assignment]
        invalidator = threading.Thread(target=cache.invalidate, args=("k",))
        invalidator.start()
        assert waiting.wait(5)
        old.lock = real_lock  # the reset probes it directly
        cache._reset_lock_after_fork(timeout=0)  # a take-over judging the live hold orphaned
        cache.put("k", b"stale")  # read from L2 before the invalidation, stored after the reset
        release.set()
        holder.join(5)
        invalidator.join(5)

        assert not invalidator.is_alive()
        assert cache.get("k") == (False, None)

    def test_hookless_child_starts_no_cleanup_thread(self, monkeypatch, caplog):
        """LAB-7271: a fork from C skips CPython's own after-fork repair, so no thread may start there."""
        from cachekit import l1_cache

        manager = L1CacheManager(default_max_memory_mb=10)
        cache = manager.get_cache("hookless-ns")
        _as_if_forked(manager, parent_ran_cleanup=True)
        _as_if_hookless(monkeypatch)
        starts: list[threading.Thread] = []
        monkeypatch.setattr(threading.Thread, "start", lambda self: starts.append(self))
        with caplog.at_level(logging.WARNING, logger="cachekit.l1_cache"):
            cache.put("k", b"v")
            manager.start_background_cleanup(interval_seconds=60)
            born = L1CacheManager(default_max_memory_mb=10)  # first built in the child: no take-over runs
            born.start_background_cleanup(interval_seconds=60)

        assert starts == [] and manager._cleanup_thread is None and born._cleanup_thread is None
        assert cache.get("k")[0]
        assert caplog.records == []  # logging's own locks may be orphaned in this child

        monkeypatch.setattr(l1_cache, "_hooked_pid", os.getpid())  # its own child, forked with hooks
        _as_if_forked(manager, parent_ran_cleanup=False)
        manager.start_background_cleanup(interval_seconds=60)
        assert len(starts) == 1

    @pytest.mark.skipif(not sys.platform.startswith("linux"), reason="needs glibc fork() via ctypes")
    def test_child_of_a_c_fork_requests_no_thread_start(self):
        """A real fork from C: the at-fork hook does not run, and neither take-over nor start may start a thread.

        Parent threads hold a cache lock and a logging handler's lock at fork, which a fork from C leaves
        held in the child: the take-over's lock reset and the refusal must not log, or the child's first
        put() hangs. A manager first built in the child, as a worker's first get_l1_cache() builds the
        global one, must refuse a thread too.
        """
        import ctypes

        from cachekit import l1_cache

        manager = L1CacheManager(default_max_memory_mb=10)
        cache = manager.get_cache("c-fork-ns")  # captured pre-fork, like a decorator's _l1_cache
        cache.put("pre-fork", b"v")
        l1_logger = logging.getLogger("cachekit.l1_cache")
        old_level = l1_logger.level
        handler = logging.StreamHandler(open(os.devnull, "w"))  # noqa: SIM115 - closed below
        held, release = [threading.Event(), threading.Event()], threading.Event()

        def hold(lock: threading.RLock, done: threading.Event) -> None:
            with lock:  # a parent thread mid-critical-section when the fork lands
                done.set()
                release.wait(10)

        locks = (handler.lock, cache._state.lock)
        holders = [threading.Thread(target=hold, args=pair, daemon=True) for pair in zip(locks, held, strict=True)]
        try:
            manager.start_background_cleanup(interval_seconds=60)
            l1_logger.addHandler(handler)
            l1_logger.setLevel(logging.DEBUG)
            for holder in holders:
                holder.start()
            assert all(event.wait(5) for event in held)
            libc_fork = ctypes.PyDLL(None).fork  # PyDLL keeps the GIL through the call
            r, w = os.pipe()
            child = libc_fork()
            if child == 0:
                try:
                    signal.alarm(5)  # a log call on the inherited handler lock hangs; end the child instead
                    os.close(r)
                    requested: list[str] = []

                    class _RecordingThread(threading.Thread):  # keeps the child single-threaded
                        def start(self) -> None:
                            requested.append(self.name)

                    threading.Thread = _RecordingThread  # the child's copy only; it never returns to pytest
                    cache.put("k", b"v")  # the take-over probes the held cache lock for 1 s, then resets it
                    manager.start_background_cleanup(interval_seconds=60)
                    l1_cache._global_l1_manager = None
                    born = l1_cache.get_l1_cache_manager()  # starts its cleanup unless refused
                    _report(
                        w,
                        {
                            "requested": requested,
                            "thread": manager._cleanup_thread,
                            "born_thread": born._cleanup_thread,
                            "found": cache.get("k")[0],
                            "pre_fork": cache.get("pre-fork")[0],
                        },
                    )
                finally:
                    os._exit(1)
            os.close(w)
            assert _child_outcome(child, r) == {
                "requested": [],
                "thread": None,
                "born_thread": None,
                "found": True,
                "pre_fork": False,  # dropped with the orphaned lock; L2 still has it
            }
        finally:
            release.set()
            for holder in holders:
                if holder.is_alive():  # join() raises on a thread that never started
                    holder.join(5)
            l1_logger.removeHandler(handler)
            l1_logger.setLevel(old_level)
            handler.stream.close()
            manager.stop_background_cleanup()

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
