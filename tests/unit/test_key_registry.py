"""Key registry (KeyTrackableBackend) wiring in the decorator.

Whole-function invalidate_cache() on a tracking backend drains a server-side set of every
key any process wrote, instead of deleting only this process's _cached_keys. These tests pin
the wrapper's side of that contract against an in-memory tracking backend; the Redis
implementation is covered in tests/integration/test_key_registry_redis.py.
"""

from __future__ import annotations

import ast
import asyncio
import contextvars
import logging
import os
import sys
import threading
import time
from enum import Enum
from pathlib import Path
from typing import Any, Optional

import pytest

from cachekit import cache
from cachekit.backends.errors import BackendError
from cachekit.cache_handler import supports_key_tracking
from cachekit.config.validation import ConfigurationError
from cachekit.l1_cache import L1Cache


def _closure_cell(fn: Any, name: str) -> Any:
    """The closure cell ``name`` reachable from ``fn`` through nested closures."""
    seen: set[int] = set()
    stack = [fn]
    while stack:
        f = stack.pop()
        if id(f) in seen or not hasattr(f, "__code__"):
            continue
        seen.add(id(f))
        for var, cell in zip(f.__code__.co_freevars, f.__closure__ or (), strict=True):
            if var == name:
                return cell
            try:
                stack.append(cell.cell_contents)
            except ValueError:  # an empty cell
                pass
    raise LookupError(name)


class TrackingBackend:
    """In-memory L2 with a key registry: sets of raw keys per registry id."""

    def __init__(self) -> None:
        self.store: dict[str, bytes] = {}
        self.sets: dict[str, set[str]] = {}
        self.track_calls: list[tuple[str, str]] = []
        self.drain_calls: list[tuple[str, set[str]]] = []
        self.deleted: list[str] = []
        self.fail_set = False
        self.fail_track = False
        self.fail_drain = False
        self.stall: Optional[threading.Event] = None

    def get(self, key: str) -> Optional[bytes]:
        return self.store.get(key)

    def set(self, key: str, value: bytes, ttl: Optional[int] = None) -> None:
        if self.fail_set:
            raise BackendError("set failed")
        self.store[key] = value

    def delete(self, key: str) -> bool:
        self.deleted.append(key)
        return self.store.pop(key, None) is not None

    def exists(self, key: str) -> bool:
        return key in self.store

    def health_check(self) -> tuple[bool, dict[str, Any]]:
        return True, {"backend_type": "fake", "latency_ms": 0.0}

    def track_key(self, registry_id: str, key: str) -> None:
        if self.stall is not None:
            self.stall.wait(5)
        self.track_calls.append((registry_id, key))
        if self.fail_track:
            raise BackendError("track failed")
        self.sets.setdefault(registry_id, set()).add(key)

    def drain_tracked(self, registry_id: str, local_keys: Any) -> set[str]:
        if self.stall is not None:
            self.stall.wait(5)
        local = set(local_keys)
        self.drain_calls.append((registry_id, local))
        if self.fail_drain:
            raise BackendError("drain detail naming t:tenant:key")
        out = self.sets.pop(registry_id, set()) | local
        for key in out:
            self.store.pop(key, None)
        return out


class PlainBackend(TrackingBackend):
    """Same storage, no registry: the class does not define the tracking methods."""

    track_key = None  # type: ignore[assignment]
    drain_tracked = None  # type: ignore[assignment]


_tenant: contextvars.ContextVar[str] = contextvars.ContextVar("_tenant", default="a")


class ScopedBackend(TrackingBackend):
    """TrackingBackend under a per-context tenant prefix, like the tenant-scoped Redis backend."""

    @property
    def key_prefix(self) -> str:
        return f"t:{_tenant.get()}:"

    def get(self, key: str) -> Optional[bytes]:
        return super().get(self.key_prefix + key)

    def set(self, key: str, value: bytes, ttl: Optional[int] = None) -> None:
        super().set(self.key_prefix + key, value, ttl)

    def delete(self, key: str) -> bool:
        return super().delete(self.key_prefix + key)

    def track_key(self, registry_id: str, key: str) -> None:
        super().track_key(self.key_prefix + registry_id, self.key_prefix + key)

    def drain_tracked(self, registry_id: str, local_keys: Any) -> set[str]:
        out = super().drain_tracked(self.key_prefix + registry_id, {self.key_prefix + k for k in local_keys})
        return {k.removeprefix(self.key_prefix) for k in out}


def _registry_ids(backend: TrackingBackend) -> set[str]:
    return {rid for rid, _ in backend.track_calls}


@pytest.mark.unit
class TestRegistryId:
    def test_registry_id_deterministic(self) -> None:
        backend = TrackingBackend()

        def f(x: int) -> int:
            return x

        cache(backend=backend, ttl=60, namespace="reg_det")(f)(1)
        cache(backend=backend, ttl=60, namespace="reg_det")(f)(2)
        (rid,) = _registry_ids(backend)
        assert rid.startswith("ck:reg:reg_det:")
        assert len(rid.rsplit(":", 1)[1]) == 16  # 64-bit blake3

    def test_registry_id_namespace_isolation(self) -> None:
        backend = TrackingBackend()

        def f(x: int) -> int:
            return x

        cache(backend=backend, ttl=60, namespace="reg_a")(f)(1)
        cache(backend=backend, ttl=60, namespace="reg_b")(f)(1)
        assert len(_registry_ids(backend)) == 2

    def test_registry_id_none_vs_default_distinct(self) -> None:
        backend = TrackingBackend()

        def f(x: int) -> int:
            return x

        cache(backend=backend, ttl=60)(f)(1)
        cache(backend=backend, ttl=60, namespace="default")(f)(1)
        ids = _registry_ids(backend)
        assert len(ids) == 2
        assert any(rid.startswith("ck:reg::") for rid in ids)
        assert any(rid.startswith("ck:reg:default:") for rid in ids)

    @pytest.mark.parametrize("namespace", ["ck", "ck:x", "ck:reg"])
    def test_namespace_ck_rejected(self, namespace: str) -> None:
        with pytest.raises(ConfigurationError, match="reserved"):

            @cache(backend=TrackingBackend(), ttl=60, namespace=namespace)
            def f(x: int) -> int:
                return x

    def test_namespace_resembling_ck_allowed(self) -> None:
        @cache(backend=TrackingBackend(), ttl=60, namespace="cko")
        def f(x: int) -> int:
            return x

        assert f(1) == 1


@pytest.mark.unit
class TestTrackingSites:
    def test_miss_tracks_after_l2_write(self) -> None:
        backend = TrackingBackend()

        @cache(backend=backend, ttl=60, namespace="track_miss")
        def f(x: int) -> int:
            return x

        f(1)
        assert len(backend.track_calls) == 1
        _, key = backend.track_calls[0]
        assert key in backend.store  # the tracked key is the raw key the wrapper wrote

    def test_track_only_after_l2_success(self) -> None:
        backend = TrackingBackend()
        backend.fail_set = True

        @cache(backend=backend, ttl=60, namespace="track_fail_set")
        def f(x: int) -> int:
            return x

        assert f(1) == 1
        assert backend.track_calls == []

    @pytest.mark.asyncio
    async def test_track_only_after_l2_success_async(self) -> None:
        backend = TrackingBackend()
        backend.fail_set = True

        @cache(backend=backend, ttl=60, namespace="track_fail_set_async")
        async def f(x: int) -> int:
            return x

        assert await f(1) == 1
        assert backend.track_calls == []

    def test_tracking_only_on_l2_writes(self) -> None:
        backend = TrackingBackend()

        @cache(backend=backend, ttl=60, namespace="track_hit")
        def writer(x: int) -> int:
            return x

        writer(1)
        assert len(backend.track_calls) == 1

        # Evict this namespace's L1 so the next call takes the L2-hit backfill path.
        from cachekit.l1_cache import get_l1_cache

        get_l1_cache("track_hit").clear()
        assert writer(1) == 1  # L2 hit + L1 backfill
        assert len(backend.track_calls) == 1

    def test_track_failure_doesnt_break_cache_write(self, caplog: pytest.LogCaptureFixture) -> None:
        backend = TrackingBackend()
        backend.fail_track = True
        calls = 0

        @cache(backend=backend, ttl=60, namespace="track_raises")
        def f(x: int) -> int:
            nonlocal calls
            calls += 1
            return x

        with caplog.at_level(logging.WARNING, logger="cachekit.decorators.wrapper"):
            assert f(1) == 1
            assert f(1) == 1
        assert calls == 1  # written to L2/L1 despite the tracking failure
        # Other processes' drains cannot see the key, so the failure must reach production logs.
        (warning,) = [r for r in caplog.records if "Key tracking failed" in r.getMessage()]
        assert warning.levelno == logging.WARNING
        assert "track_raises" not in warning.getMessage()  # key and registry id are redacted

    def test_track_failure_warning_is_throttled_per_function(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        import cachekit.decorators.wrapper as wrapper_module

        backend = TrackingBackend()
        backend.fail_track = True

        @cache(backend=backend, ttl=60, namespace="track_flood")
        def f(x: int) -> int:
            return x

        with caplog.at_level(logging.WARNING, logger="cachekit.decorators.wrapper"):
            for i in range(50):
                assert f(i) == i
            monkeypatch.setattr(wrapper_module, "_WARN_INTERVAL_SECONDS", 0.0)  # the window elapses
            assert f(50) == 50
        warnings = [r.getMessage() for r in caplog.records if "Key tracking failed" in r.getMessage()]
        # A registry outage is one WARNING per window, not one per write, and none is lost from the count.
        assert len(warnings) == 2
        assert "since the last warning: 1)" in warnings[0]
        assert "since the last warning: 50)" in warnings[1]

    def test_concurrent_track_failures_emit_one_warning(self, caplog: pytest.LogCaptureFixture) -> None:
        n = 32
        barrier = threading.Barrier(n, timeout=10)

        class BarrierBackend(TrackingBackend):
            def track_key(self, registry_id: str, key: str) -> None:
                barrier.wait()  # every writer fails at once
                raise BackendError("track failed")

        class SlowHandler(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                time.sleep(0.02)  # a slow sink widens any check-to-claim gap

        @cache(backend=BarrierBackend(), ttl=60, namespace="track_burst")
        def f(x: int) -> int:
            return x

        slow = SlowHandler(logging.WARNING)
        wrapper_logger = logging.getLogger("cachekit.decorators.wrapper")
        wrapper_logger.addHandler(slow)
        try:
            with caplog.at_level(logging.WARNING, logger="cachekit.decorators.wrapper"):
                threads = [threading.Thread(target=f, args=(i,)) for i in range(n)]
                for t in threads:
                    t.start()
                for t in threads:
                    t.join(10)
        finally:
            wrapper_logger.removeHandler(slow)
        assert not barrier.broken
        warnings = [r for r in caplog.records if "Key tracking failed" in r.getMessage()]
        assert len(warnings) == 1  # the window is claimed atomically, not after logging returns

    @pytest.mark.skipif(not hasattr(os, "fork"), reason="fork() not available on this platform")
    def test_forked_child_does_not_inherit_a_held_warning_lock(self) -> None:
        import multiprocessing

        backend = TrackingBackend()
        backend.fail_track = True

        @cache(backend=backend, ttl=60, namespace="track_fork")
        def f(x: int) -> int:
            return x

        ctx = multiprocessing.get_context("fork")
        queue = ctx.Queue()

        def child(q: Any) -> None:
            writer = threading.Thread(target=f, args=(1,), daemon=True)
            writer.start()
            writer.join(5)
            q.put(not writer.is_alive())

        with _closure_cell(f, "_track_warn").cell_contents._lock:  # a parent writer is mid-claim at fork
            process = ctx.Process(target=child, args=(queue,))
            process.start()
        try:
            returned = queue.get(timeout=30)
        finally:
            process.join(timeout=30)
        assert returned, "a forked child must not block on a warning lock it inherited held"

    def test_fallback_to_local_when_not_trackable(self) -> None:
        backend = PlainBackend()
        assert not supports_key_tracking(backend)

        @cache(backend=backend, ttl=60, namespace="track_plain")
        def f(x: int) -> int:
            return x

        f(1)
        f(2)
        assert len(backend.store) == 2
        f.invalidate_cache()
        assert backend.store == {}
        assert backend.track_calls == [] and backend.drain_calls == []

    def test_mock_backend_is_not_trackable(self) -> None:
        from unittest.mock import Mock

        assert not supports_key_tracking(Mock())


@pytest.mark.unit
class TestDrain:
    def test_drain_deletes_peer_keys(self) -> None:
        """Keys another process wrote (present only in the registry) are deleted too."""
        backend = TrackingBackend()

        @cache(backend=backend, ttl=60, namespace="drain_peer")
        def f(x: int) -> int:
            return x

        f(1)
        (rid, _) = backend.track_calls[0]
        backend.store["peer-key"] = b"x"
        backend.sets[rid].add("peer-key")

        f.invalidate_cache()
        assert backend.store == {}
        assert rid not in backend.sets

    def test_fresh_process_drains(self) -> None:
        """A wrapper that never wrote (a new process) still drains via the registry."""
        backend = TrackingBackend()

        def f(x: int) -> int:
            return x

        writer = cache(backend=backend, ttl=60, namespace="drain_fresh")(f)
        writer(1)
        writer(2)

        fresh = cache(backend=backend, ttl=60, namespace="drain_fresh")(f)
        fresh.invalidate_cache()
        assert backend.store == {}
        assert backend.drain_calls[0][1] == set()  # it knew no keys itself

    def test_fresh_process_drains_custom_keys(self) -> None:
        """The registry tracks the key the write path wrote, custom key= included (LAB-4387)."""
        backend = TrackingBackend()

        def f(x: int) -> int:
            return x

        writer = cache(backend=backend, ttl=60, namespace="drain_custom", key=lambda x: f"user:{x}")(f)
        writer(1)
        writer(2)
        assert set(backend.store) == {key for _, key in backend.track_calls}

        fresh = cache(backend=backend, ttl=60, namespace="drain_custom", key=lambda x: f"user:{x}")(f)
        fresh.invalidate_cache()
        assert backend.store == {}

    def test_untracked_key_removed_by_next_drain(self) -> None:
        """A key whose track_key failed is gone from L2 and L1 after a drain."""
        backend = TrackingBackend()
        backend.fail_track = True
        calls = 0

        @cache(backend=backend, ttl=60, namespace="drain_untracked")
        def f(x: int) -> int:
            nonlocal calls
            calls += 1
            return x

        f(1)
        assert backend.sets == {}  # never tracked
        backend.fail_track = False

        f.invalidate_cache()
        assert backend.store == {}
        f(1)
        assert calls == 2  # L1 was evicted too

    def test_same_key_rewrite_during_drain_stays_recorded(self) -> None:
        """A key re-recorded while a drain is in flight survives that drain's trim.

        The drain snapshots k and unlinks its old value; a concurrent miss then rewrites k
        and its track_key fails. The new L2 value is in no registry, so the local record is
        the only thing that can reach it: the next drain must still delete it."""
        backend = TrackingBackend()

        @cache(backend=backend, ttl=60, namespace="drain_rerecord", l1_enabled=False)
        def f(x: int) -> int:
            return x

        f(1)  # k recorded and tracked
        (key,) = backend.store
        backend.fail_track = True
        unlinked, rewritten = threading.Event(), threading.Event()
        real_drain = backend.drain_tracked

        def drain_then_wait(registry_id: str, local_keys: Any) -> set[str]:
            out = real_drain(registry_id, local_keys)  # old value of k unlinked
            unlinked.set()
            assert rewritten.wait(5)
            return out

        backend.drain_tracked = drain_then_wait  # type: ignore[method-assign]
        drainer = threading.Thread(target=f.invalidate_cache)
        drainer.start()
        assert unlinked.wait(5)
        f(1)  # concurrent miss: L2 set succeeds, track_key fails
        rewritten.set()
        drainer.join(5)
        assert not drainer.is_alive()

        assert key in backend.store and backend.sets == {}  # the rewrite is in no registry
        assert ("", key) in _closure_cell(f, "_cached_keys").cell_contents  # (L2 scope, key); unscoped backend
        backend.drain_tracked = real_drain  # type: ignore[method-assign]
        f.invalidate_cache()
        assert backend.store == {}

    def test_same_key_rewrite_during_local_invalidate_stays_recorded(self) -> None:
        """The per-key loop (no registry, or a failed drain) has the same race between a
        key's L2 delete and its discard: a rewrite landing there must stay recorded."""
        backend = PlainBackend()

        @cache(backend=backend, ttl=60, namespace="local_rerecord", l1_enabled=False)
        def f(x: int) -> int:
            return x

        f(1)
        (key,) = backend.store
        deleted, rewritten = threading.Event(), threading.Event()
        real_delete = backend.delete

        def delete_then_wait(k: str) -> bool:
            out = real_delete(k)
            deleted.set()
            assert rewritten.wait(5)
            return out

        backend.delete = delete_then_wait  # type: ignore[method-assign]
        invalidator = threading.Thread(target=f.invalidate_cache)
        invalidator.start()
        assert deleted.wait(5)
        f(1)  # concurrent miss rewrites k after its delete
        rewritten.set()
        invalidator.join(5)
        assert not invalidator.is_alive()

        assert key in backend.store
        assert ("", key) in _closure_cell(f, "_cached_keys").cell_contents  # (L2 scope, key); unscoped backend
        backend.delete = real_delete  # type: ignore[method-assign]
        f.invalidate_cache()
        assert backend.store == {}

    @pytest.mark.parametrize("error", [RuntimeError, KeyboardInterrupt], ids=["Exception", "BaseException"])
    def test_single_key_delete_that_raises_keeps_key_tracked(
        self, error: type[BaseException], caplog: pytest.LogCaptureFixture
    ) -> None:
        """invalidate_cache(args) untracks before its L2 delete. A delete that raises anything,
        not only an Exception, re-tracks the key so a later no-args call retries it, and the L1
        copy is still evicted. Only an Exception is caught and logged; the rest propagates."""
        backend = PlainBackend()
        calls = 0

        @cache(backend=backend, ttl=60, namespace="single_key_raise")
        def f(x: int) -> int:
            nonlocal calls
            calls += 1
            return x

        f(1)
        (key,) = backend.store
        real_delete = backend.delete

        def delete_raises(k: str) -> bool:
            raise error("delete interrupted")

        backend.delete = delete_raises  # type: ignore[method-assign]
        with caplog.at_level(logging.ERROR, logger="cachekit.decorators.wrapper"):
            if issubclass(error, Exception):
                f.invalidate_cache(1)
            else:
                with pytest.raises(error):
                    f.invalidate_cache(1)
        backend.delete = real_delete  # type: ignore[method-assign]

        errors = [r for r in caplog.records if "Failed to delete L2 key" in r.getMessage()]
        assert len(errors) == (1 if issubclass(error, Exception) else 0)
        assert ("", key) in _closure_cell(f, "_cached_keys").cell_contents  # (L2 scope, key); unscoped backend
        value = backend.store.pop(key)
        f(1)  # L1 evicted and L2 emptied: the function runs again
        assert calls == 2
        backend.store[key] = value
        f.invalidate_cache()  # no-args retry reaches the re-tracked key
        assert backend.store == {}

    def test_other_tenant_rewrite_during_drain_keeps_only_its_own_entry(self) -> None:
        """Watches hold (scope, key): tenant b rewriting the same key during tenant a's drain
        keeps b's entry recorded and does not keep a's drained entry alive."""
        backend = ScopedBackend()

        @cache(backend=backend, ttl=60, namespace="drain_tenants", l1_enabled=False)
        def f(x: int) -> int:
            return x

        f(1)  # tenant a
        (a_key,) = backend.store
        key = a_key.removeprefix("t:a:")
        unlinked, rewritten = threading.Event(), threading.Event()
        real_drain = backend.drain_tracked

        def drain_then_wait(registry_id: str, local_keys: Any) -> set[str]:
            out = real_drain(registry_id, local_keys)
            unlinked.set()
            assert rewritten.wait(5)
            return out

        backend.drain_tracked = drain_then_wait  # type: ignore[method-assign]
        drainer = threading.Thread(target=contextvars.copy_context().run, args=(f.invalidate_cache,))
        drainer.start()
        assert unlinked.wait(5)
        token = _tenant.set("b")
        try:
            f(1)  # tenant b writes the same cache key under its own prefix
        finally:
            _tenant.reset(token)
        rewritten.set()
        drainer.join(5)
        assert not drainer.is_alive()

        assert set(backend.store) == {f"t:b:{key}"}
        assert _closure_cell(f, "_cached_keys").cell_contents == {("t:b:", key)}

    def test_drain_leaves_no_watch_behind(self) -> None:
        """A drain unregisters its watch whether it succeeds or fails, and drops watches a
        forked child inherited from a parent drain that never finished there."""
        backend = TrackingBackend()

        @cache(backend=backend, ttl=60, namespace="drain_watches")
        def f(x: int) -> int:
            return x

        f(1)
        watches = _closure_cell(f, "_drain_watches").cell_contents
        watches[(-1, object())] = {("", "orphan")}  # another pid's drain, in flight at fork
        f.invalidate_cache()
        assert watches == {}
        backend.fail_drain = True
        f.invalidate_cache()
        assert watches == {}

    def test_drain_failure_falls_back_to_local_with_l2_delete(self, caplog: pytest.LogCaptureFixture) -> None:
        backend = TrackingBackend()
        calls = 0

        @cache(backend=backend, ttl=60, namespace="drain_fail")
        def f(x: int) -> int:
            nonlocal calls
            calls += 1
            return x

        f(1)
        f(2)
        backend.fail_drain = True
        with caplog.at_level("WARNING"):
            f.invalidate_cache()
        assert "BackendError(unknown)" in caplog.text  # rendered by type, never by message
        assert "t:tenant:key" not in caplog.text
        assert "Key registry drain failed" in caplog.text
        assert backend.store == {}  # local loop deleted from L2
        assert len(backend.deleted) == 2
        f(1)
        assert calls == 3  # and from L1

    def test_local_invalidate_keeps_key_whose_delete_failed(self) -> None:
        backend = PlainBackend()

        @cache(backend=backend, ttl=60, namespace="local_delete_fail")
        def f(x: int) -> int:
            return x

        f(1)
        original = backend.delete

        def failing_delete(key: str) -> bool:
            raise BackendError("delete failed")

        backend.delete = failing_delete  # type: ignore[method-assign]
        f.invalidate_cache()
        assert len(backend.store) == 1
        backend.delete = original  # type: ignore[method-assign]
        f.invalidate_cache()  # retried: the key stayed recorded
        assert backend.store == {}

    @pytest.mark.asyncio
    async def test_async_drain(self) -> None:
        backend = TrackingBackend()

        @cache(backend=backend, ttl=60, namespace="drain_async")
        async def f(x: int) -> int:
            return x

        await f(1)
        assert len(backend.track_calls) == 1
        await f.ainvalidate_cache()
        assert backend.store == {} and backend.sets == {}


@pytest.mark.unit
class TestAsyncPathsDoNotBlockLoop:
    @staticmethod
    async def _ticks_while(coro: Any) -> int:
        ticks = 0
        done = False

        async def ticker() -> None:
            nonlocal ticks
            while not done:
                ticks += 1
                await asyncio.sleep(0.01)

        t = asyncio.create_task(ticker())
        try:
            await coro
        finally:
            done = True
            await t
        return ticks

    @pytest.mark.asyncio
    async def test_async_paths_do_not_block_loop(self) -> None:
        """A stalled track_key / drain_tracked leaves the event loop responsive."""
        backend = TrackingBackend()

        @cache(backend=backend, ttl=60, namespace="async_no_block")
        async def f(x: int) -> int:
            return x

        backend.stall = threading.Event()
        releaser = threading.Timer(0.3, backend.stall.set)
        releaser.start()
        assert await self._ticks_while(f(1)) >= 5
        assert len(backend.track_calls) == 1

        backend.stall = threading.Event()
        releaser = threading.Timer(0.3, backend.stall.set)
        releaser.start()
        start = time.monotonic()
        assert await self._ticks_while(f.ainvalidate_cache()) >= 5
        assert time.monotonic() - start >= 0.25
        assert backend.store == {}


@pytest.mark.unit
class TestSingleL1WriteSite:
    def test_put_l1_is_the_only_l1_write(self) -> None:
        """Every L1 write goes through _put_l1, which records the key after the put —
        so "in L1 => in _cached_keys" holds by construction."""
        import cachekit.decorators.wrapper as wrapper_module

        tree = ast.parse(Path(wrapper_module.__file__).read_text())
        puts: list[ast.Call] = []
        owners: list[str] = []

        class Visitor(ast.NodeVisitor):
            def __init__(self) -> None:
                self.stack: list[str] = []

            def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
                self.stack.append(node.name)
                self.generic_visit(node)
                self.stack.pop()

            visit_AsyncFunctionDef = visit_FunctionDef  # type: ignore[assignment]  # noqa: N815 - ast.NodeVisitor API

            def visit_Call(self, node: ast.Call) -> None:
                f = node.func
                if (
                    isinstance(f, ast.Attribute)
                    and f.attr == "put"
                    and isinstance(f.value, ast.Name)
                    and f.value.id == "_l1_cache"
                ):
                    puts.append(node)
                    owners.append(self.stack[-1])
                self.generic_visit(node)

        Visitor().visit(tree)
        assert owners == ["_put_l1"]

        put_l1 = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "_put_l1")
        records = [
            n for n in ast.walk(put_l1) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "_record"
        ]
        assert len(records) == 1
        assert records[0].lineno > puts[0].lineno


@pytest.mark.unit
class TestL1InvalidateMany:
    def test_invalidate_many_removes_only_named_keys(self) -> None:
        l1 = L1Cache(namespace="inv_many")
        for k in ("a", "b", "c"):
            l1.put(k, b"v", redis_ttl=60)
        l1.invalidate_many({"a", "b", "missing"})
        assert l1.get("a") == (False, None)
        assert l1.get("b") == (False, None)
        assert l1.get("c") == (True, b"v")
        assert l1._state.memory_bytes == 1

    def test_invalidate_many_releases_the_lock_between_batches(self) -> None:
        l1 = L1Cache(namespace="inv_many_batches")
        l1.put("k2499", b"v", redis_ttl=60)
        real_lock, acquisitions = l1._state.lock, 0

        class CountingLock:
            def __enter__(self) -> None:
                nonlocal acquisitions
                acquisitions += 1
                real_lock.acquire()

            def __exit__(self, *exc: object) -> None:
                real_lock.release()

        l1._state.lock = CountingLock()  # type: ignore[assignment]
        l1.invalidate_many(f"k{i}" for i in range(2_500))  # a one-shot iterable, like a drain result
        l1._state.lock = real_lock
        assert acquisitions == 3  # 1 000-key batches: a large drain never holds every get/put off at once
        assert l1.get("k2499") == (False, None)


class _NS(str, Enum):
    USERS = "users"


class _StrEnumNS(str, Enum):
    """enum.StrEnum's rendering (3.11+), spelled out so the test also runs on 3.10."""

    USERS = "users"
    __str__ = str.__str__
    __format__ = str.__format__


class _FormatsAs(str):
    """A str whose __format__ lies: f-strings render ``rendered``, "".join the real value."""

    rendered = "ck:reg:users"

    def __format__(self, spec: str) -> str:
        return self.rendered


class _LegacyFormat(_FormatsAs):
    rendered = "legacy"


class _HidesCk(str):
    """A str that claims not to be "ck" and not to start with it."""

    def __eq__(self, other: object) -> bool:
        return False

    __hash__ = str.__hash__

    def startswith(self, *args: Any, **kwargs: Any) -> bool:  # type: ignore[override]
        return False


@pytest.mark.unit
class TestNamespaceExactStr:
    """A str-subclass namespace keys and names its registry set by its underlying str (LAB-6197)."""

    def test_str_enum_registry_id_uses_value(self) -> None:
        backend = TrackingBackend()

        @cache(backend=backend, ttl=60, namespace=_NS.USERS, l1_enabled=False)
        def f(x: int) -> int:
            return x

        f(1)
        (rid,) = _registry_ids(backend)
        assert rid.startswith("ck:reg:users:")

    def test_str_enum_custom_key_uses_value(self) -> None:
        backend = TrackingBackend()

        @cache(backend=backend, ttl=60, namespace=_NS.USERS, key=lambda *a, **kw: "k", l1_enabled=False)
        def f(x: int) -> int:
            return x

        f(1)
        assert set(backend.store) == {"users:k"}
        (rid,) = _registry_ids(backend)
        assert rid.startswith("ck:reg:users:")

    def test_format_override_cannot_forge_registry_shape(self) -> None:
        backend = TrackingBackend()

        @cache(backend=backend, ttl=60, namespace=_FormatsAs("x"), key=lambda *a, **kw: "k", l1_enabled=False)
        def f(x: int) -> int:
            return x

        f(1)
        assert set(backend.store) == {"x:k"}
        (rid,) = _registry_ids(backend)
        assert rid.startswith("ck:reg:x:")

    @pytest.mark.parametrize("value", ["ck", "ck:reg"])
    def test_eq_and_startswith_override_cannot_bypass_ck_reservation(self, value: str) -> None:
        ns = _HidesCk(value)
        assert not ns == "ck" and not ns.startswith("ck:")  # the overrides the old check trusted
        with pytest.raises(ConfigurationError, match="reserved"):

            @cache(backend=TrackingBackend(), ttl=60, namespace=ns)
            def f(x: int) -> int:
                return x

    def test_drain_also_empties_pre_fix_registry_set(self) -> None:
        """Entries tracked under the pre-fix f-string registry id go on a no-args drain."""
        backend = TrackingBackend()

        @cache(backend=backend, ttl=60, namespace=_LegacyFormat("x"), l1_enabled=False)
        def f(x: int) -> int:
            return x

        f(1)
        (rid,) = _registry_ids(backend)
        legacy_rid = "ck:reg:legacy:" + rid.rsplit(":", 1)[1]
        backend.store["pre-upgrade-key"] = b"x"  # written and tracked by pre-fix code
        backend.sets[legacy_rid] = {"pre-upgrade-key"}

        f.invalidate_cache()
        assert backend.store == {}
        assert [r for r, _ in backend.drain_calls] == [rid, legacy_rid]

    def test_legacy_drain_failure_still_applies_primary_drain(self, caplog: pytest.LogCaptureFixture) -> None:
        """A failed legacy drain must not discard what the primary drain deleted: another
        wrapper's shared-L1 copy of a drained key is still evicted, and the old set is kept."""

        class LegacyFails(TrackingBackend):
            def drain_tracked(self, registry_id: str, local_keys: Any) -> set[str]:
                if registry_id.startswith("ck:reg:legacy:"):
                    raise BackendError("legacy drain failed")
                return super().drain_tracked(registry_id, local_keys)

        backend = LegacyFails()
        calls: list[int] = []

        def f(x: int) -> int:
            calls.append(x)
            return x

        ns = _LegacyFormat("legacy_fail")
        writer = cache(backend=backend, ttl=60, namespace=ns)(f)
        writer(1)  # this wrapper's L1 now holds the entry
        (rid,) = _registry_ids(backend)
        legacy_rid = "ck:reg:legacy:" + rid.rsplit(":", 1)[1]
        backend.sets[legacy_rid] = {"pre-upgrade-key"}

        fresh = cache(backend=backend, ttl=60, namespace=ns)(f)  # knows no keys itself
        with caplog.at_level(logging.WARNING):
            fresh.invalidate_cache()

        assert "Legacy key registry drain failed" in caplog.text
        assert "invalidating local keys only" not in caplog.text
        assert legacy_rid in backend.sets  # retried by the next drain
        writer(1)
        assert calls == [1, 1]  # the shared L1 copy was evicted, so it recomputed

    @pytest.mark.parametrize(
        ("namespace", "interop", "legacy"),
        [
            ("users", None, None),
            (_StrEnumNS.USERS, None, None),
            (None, None, None),
            # f"{member}" is "_NS.USERS" only from 3.11; on 3.10 the set name never moved.
            (_NS.USERS, None, "_NS.USERS" if sys.version_info >= (3, 11) else None),
            # 0.20.0 shipped the registry with interop's exact-str rebind: no pre-fix set exists.
            (_NS.USERS, "get_user", None),
        ],
    )
    def test_no_args_drain_ids(self, namespace: Optional[str], interop: Optional[str], legacy: Optional[str]) -> None:
        """Only a namespace whose set name actually moved gets a second drain."""
        backend = TrackingBackend()

        @cache(backend=backend, ttl=60, namespace=namespace, interop=interop, l1_enabled=False)
        def f(x: int) -> int:
            return x

        f(1)
        (rid,) = _registry_ids(backend)
        assert rid.startswith(f"ck:reg:{'users' if namespace is not None else ''}:")
        f.invalidate_cache()
        expected = [rid] if legacy is None else [rid, f"ck:reg:{legacy}:{rid.rsplit(':', 1)[1]}"]
        assert [r for r, _ in backend.drain_calls] == expected
