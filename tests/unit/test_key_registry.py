"""Key registry (KeyTrackableBackend) wiring in the decorator.

Whole-function invalidate_cache() on a tracking backend drains a server-side set of every
key any process wrote, instead of deleting only this process's _cached_keys. These tests pin
the wrapper's side of that contract against an in-memory tracking backend; the Redis
implementation is covered in tests/integration/test_key_registry_redis.py.
"""

from __future__ import annotations

import ast
import asyncio
import logging
import threading
import time
from pathlib import Path
from typing import Any, Optional

import pytest

from cachekit import cache
from cachekit.backends.errors import BackendError
from cachekit.cache_handler import supports_key_tracking
from cachekit.config.validation import ConfigurationError
from cachekit.l1_cache import L1Cache


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
            monkeypatch.setattr(wrapper_module, "_TRACK_WARN_INTERVAL_SECONDS", 0.0)  # the window elapses
            assert f(50) == 50
        warnings = [r.getMessage() for r in caplog.records if "Key tracking failed" in r.getMessage()]
        # A registry outage is one WARNING per window, not one per write, and none is lost from the count.
        assert len(warnings) == 2
        assert "since the last warning: 1)" in warnings[0]
        assert "since the last warning: 50)" in warnings[1]

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
        adds = [
            n
            for n in ast.walk(put_l1)
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and n.func.attr == "add"
            and isinstance(n.func.value, ast.Name)
            and n.func.value.id == "_cached_keys"
        ]
        assert len(adds) == 1
        assert adds[0].lineno > puts[0].lineno


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
        assert l1._current_memory_bytes == 1

    def test_invalidate_many_releases_the_lock_between_batches(self) -> None:
        l1 = L1Cache(namespace="inv_many_batches")
        l1.put("k2499", b"v", redis_ttl=60)
        real_lock, acquisitions = l1._lock, 0

        class CountingLock:
            def __enter__(self) -> None:
                nonlocal acquisitions
                acquisitions += 1
                real_lock.acquire()

            def __exit__(self, *exc: object) -> None:
                real_lock.release()

        l1._lock = CountingLock()  # type: ignore[assignment]
        l1.invalidate_many(f"k{i}" for i in range(2_500))  # a one-shot iterable, like a drain result
        l1._lock = real_lock
        assert acquisitions == 3  # 1 000-key batches: a large drain never holds every get/put off at once
        assert l1.get("k2499") == (False, None)
