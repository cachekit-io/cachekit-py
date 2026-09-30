"""
Test for #59: invalidate_cache() / ainvalidate_cache() with no args on parameterized functions.

Bug: When invalidate_cache() is called with no arguments on a function that HAS parameters,
it generates a cache key for the zero-argument call (which was never cached) and invalidates
that non-existent key. All cached entries for real argument combinations survive.

Expected: calling invalidate_cache() with no args on a parameterized function should clear
ALL cached entries for that function (namespace-level invalidation).
"""

from __future__ import annotations

import contextvars
import logging
from typing import Any, Optional

import pytest

from cachekit import cache
from cachekit.backends.errors import BackendError
from cachekit.backends.file import FileBackend, FileBackendConfig


@pytest.mark.unit
class TestInvalidateNoArgs:
    """Reproduce #59: invalidate_cache() no-op on parameterized functions."""

    def test_sync_invalidate_no_args_clears_all_entries(self):
        """invalidate_cache() with no args should clear all cached entries."""
        call_count = 0

        @cache(backend=None, ttl=300, namespace="test_sync_invalidate_no_args")
        def expensive(query: str) -> str:
            nonlocal call_count
            call_count += 1
            return f"result_{call_count}"

        # Populate cache with two different argument combinations
        result1 = expensive("hello")
        result2 = expensive("world")
        assert call_count == 2

        # Verify cache hits
        assert expensive("hello") == result1
        assert expensive("world") == result2
        assert call_count == 2  # no new calls

        # Invalidate with no args — should clear ALL entries
        expensive.invalidate_cache()

        # Both entries should be gone — function must be called again
        expensive("hello")
        expensive("world")
        assert call_count == 4, (
            f"Expected 4 calls after invalidation, got {call_count}. "
            "invalidate_cache() with no args did not clear cached entries."
        )

    def test_sync_invalidate_with_args_clears_single_entry(self):
        """invalidate_cache(specific_args) should only clear that one entry."""
        call_count = 0

        @cache(backend=None, ttl=300, namespace="test_sync_invalidate_with_args")
        def expensive(query: str) -> str:
            nonlocal call_count
            call_count += 1
            return f"result_{call_count}"

        expensive("hello")
        expensive("world")
        assert call_count == 2

        # Invalidate only "hello"
        expensive.invalidate_cache("hello")

        # "hello" should miss, "world" should still hit
        expensive("hello")
        assert call_count == 3
        expensive("world")
        assert call_count == 3  # still cached

    def test_sync_no_param_function_invalidate_still_works(self):
        """invalidate_cache() on a zero-param function should still clear its entry."""
        call_count = 0

        @cache(backend=None, ttl=300, namespace="test_sync_no_param")
        def no_params() -> str:
            nonlocal call_count
            call_count += 1
            return f"result_{call_count}"

        no_params()
        assert call_count == 1
        no_params()
        assert call_count == 1  # cached

        no_params.invalidate_cache()

        no_params()
        assert call_count == 2  # cache was cleared

    @pytest.mark.asyncio
    async def test_async_invalidate_no_args_clears_all_entries(self):
        """ainvalidate_cache() with no args should clear all cached entries."""
        call_count = 0

        @cache(backend=None, ttl=300, namespace="test_async_invalidate_no_args")
        async def expensive(query: str) -> str:
            nonlocal call_count
            call_count += 1
            return f"result_{call_count}"

        result1 = await expensive("hello")
        result2 = await expensive("world")
        assert call_count == 2

        # Verify cache hits
        assert await expensive("hello") == result1
        assert await expensive("world") == result2
        assert call_count == 2

        # Invalidate with no args
        await expensive.ainvalidate_cache()

        # Both should be recalculated
        await expensive("hello")
        await expensive("world")
        assert call_count == 4, (
            f"Expected 4 calls after invalidation, got {call_count}. "
            "ainvalidate_cache() with no args did not clear cached entries."
        )

    def test_cache_clear_clears_all_entries(self):
        """cache_clear() should clear all cached entries for parameterized functions."""
        call_count = 0

        @cache(backend=None, ttl=300, namespace="test_cache_clear_all")
        def expensive(query: str) -> str:
            nonlocal call_count
            call_count += 1
            return f"result_{call_count}"

        expensive("hello")
        expensive("world")
        assert call_count == 2

        expensive.cache_clear()

        expensive("hello")
        expensive("world")
        assert call_count == 4, (
            f"Expected 4 calls after cache_clear(), got {call_count}. "
            "cache_clear() did not clear cached entries for parameterized function."
        )


@pytest.mark.unit
class TestInvalidateNoArgsWithL2Backend:
    """Exercise the L2 (backend) mass-invalidation path using FileBackend."""

    def test_file_backend_invalidate_no_args_clears_l2(self, tmp_path):
        """invalidate_cache() with no args should delete entries from both L1 and L2."""
        call_count = 0
        backend = FileBackend(FileBackendConfig(cache_dir=str(tmp_path), max_size_mb=256))

        @cache(backend=backend, ttl=300, namespace="test_file_l2_invalidate")
        def expensive(query: str) -> str:
            nonlocal call_count
            call_count += 1
            return f"result_{call_count}"

        # Populate L1 + L2
        result1 = expensive("hello")
        result2 = expensive("world")
        assert call_count == 2

        # Verify cache hits (served from L1)
        assert expensive("hello") == result1
        assert expensive("world") == result2
        assert call_count == 2

        # Invalidate all — should clear both L1 and L2
        expensive.invalidate_cache()

        # Both should miss and recompute
        expensive("hello")
        expensive("world")
        assert call_count == 4, (
            f"Expected 4 calls after invalidation, got {call_count}. L2 entries survived invalidate_cache() with no args."
        )

    def test_file_backend_partial_failure_retains_keys(self, tmp_path):
        """If L2 delete fails, the key stays in _cached_keys for retry."""
        from unittest.mock import patch

        call_count = 0
        backend = FileBackend(FileBackendConfig(cache_dir=str(tmp_path), max_size_mb=256))

        @cache(backend=backend, ttl=300, namespace="test_file_partial_fail")
        def expensive(query: str) -> str:
            nonlocal call_count
            call_count += 1
            return f"result_{call_count}"

        expensive("hello")
        expensive("world")
        assert call_count == 2

        # Make L2 delete fail for all keys
        with patch.object(backend, "delete", side_effect=Exception("disk error")):
            expensive.invalidate_cache()

        # L1 was cleared (invalidate always succeeds for L1), but L2 keys
        # should still be tracked. We can't easily check _cached_keys directly,
        # but we can verify a second invalidation attempt works when the backend
        # is healthy again.
        expensive.invalidate_cache()

        # Now both L1 and L2 should be clear
        expensive("hello")
        expensive("world")
        assert call_count == 4

    @pytest.mark.asyncio
    async def test_async_file_backend_partial_failure_retains_keys(self, tmp_path):
        """Async: if L2 delete fails, the key stays tracked for retry."""
        from unittest.mock import patch

        call_count = 0
        backend = FileBackend(FileBackendConfig(cache_dir=str(tmp_path), max_size_mb=256))

        @cache(backend=backend, ttl=300, namespace="test_async_file_partial_fail")
        async def expensive(query: str) -> str:
            nonlocal call_count
            call_count += 1
            return f"result_{call_count}"

        await expensive("hello")
        assert call_count == 1

        with patch.object(backend, "delete", side_effect=Exception("disk error")):
            await expensive.ainvalidate_cache()

        # L2 delete failed → key still tracked. Second attempt with healthy backend:
        await expensive.ainvalidate_cache()

        await expensive("hello")
        assert call_count == 2

    @pytest.mark.asyncio
    async def test_async_file_backend_invalidate_with_specific_args(self, tmp_path):
        """Async: ainvalidate_cache(specific_args) clears only that entry from L2."""
        call_count = 0
        backend = FileBackend(FileBackendConfig(cache_dir=str(tmp_path), max_size_mb=256))

        @cache(backend=backend, ttl=300, namespace="test_async_file_specific_args")
        async def expensive(query: str) -> str:
            nonlocal call_count
            call_count += 1
            return f"result_{call_count}"

        await expensive("hello")
        await expensive("world")
        assert call_count == 2

        await expensive.ainvalidate_cache("hello")

        await expensive("hello")
        assert call_count == 3  # recalculated
        await expensive("world")
        assert call_count == 3  # still cached

    @pytest.mark.asyncio
    async def test_async_file_backend_invalidate_no_args_clears_l2(self, tmp_path):
        """Async ainvalidate_cache() with no args should clear L2 entries via FileBackend."""
        call_count = 0
        backend = FileBackend(FileBackendConfig(cache_dir=str(tmp_path), max_size_mb=256))

        @cache(backend=backend, ttl=300, namespace="test_async_file_l2_invalidate")
        async def expensive(query: str) -> str:
            nonlocal call_count
            call_count += 1
            return f"result_{call_count}"

        await expensive("hello")
        await expensive("world")
        assert call_count == 2

        await expensive.ainvalidate_cache()

        await expensive("hello")
        await expensive("world")
        assert call_count == 4, (
            f"Expected 4 calls after async invalidation, got {call_count}. L2 entries survived ainvalidate_cache() with no args."
        )


@pytest.mark.unit
class TestInvalidateNoArgsCrossFunctionIsolation:
    """Ensure invalidation doesn't leak across functions."""

    def test_invalidate_does_not_affect_other_functions_same_namespace(self):
        """Invalidating fn_a should not affect fn_b even if they share a namespace."""
        a_count = 0
        b_count = 0
        ns = "test_cross_function_isolation"

        @cache(backend=None, ttl=300, namespace=ns)
        def fn_a(x: int) -> str:
            nonlocal a_count
            a_count += 1
            return f"a_{a_count}"

        @cache(backend=None, ttl=300, namespace=ns)
        def fn_b(x: int) -> str:
            nonlocal b_count
            b_count += 1
            return f"b_{b_count}"

        # Populate both
        fn_a(1)
        fn_b(1)
        assert a_count == 1
        assert b_count == 1

        # Invalidate only fn_a
        fn_a.invalidate_cache()

        # fn_a should miss, fn_b should still hit
        fn_a(1)
        assert a_count == 2  # recalculated
        fn_b(1)
        assert b_count == 1  # still cached


_tenant: contextvars.ContextVar[str] = contextvars.ContextVar("_tenant", default="a")
_ERROR_TEXT = "detail naming the raw key"


class FlakyBackend:
    """In-memory L2 whose delete raises ``delete_error`` while it is set. No key registry."""

    def __init__(self, delete_error: Optional[BaseException] = None) -> None:
        self.store: dict[str, bytes] = {}
        self.delete_error = delete_error

    def get(self, key: str) -> Optional[bytes]:
        return self.store.get(key)

    def set(self, key: str, value: bytes, ttl: Optional[int] = None) -> None:
        self.store[key] = value

    def delete(self, key: str) -> bool:
        if self.delete_error is not None:
            raise self.delete_error
        return self.store.pop(key, None) is not None

    def exists(self, key: str) -> bool:
        return key in self.store

    def health_check(self) -> tuple[bool, dict[str, Any]]:
        return True, {"backend_type": "fake", "latency_ms": 0.0}


class ScopedFlakyBackend(FlakyBackend):
    """FlakyBackend under a per-context tenant prefix, like a tenant-scoped backend."""

    @property
    def key_prefix(self) -> str:
        return f"t:{_tenant.get()}:"

    def get(self, key: str) -> Optional[bytes]:
        return super().get(self.key_prefix + key)

    def set(self, key: str, value: bytes, ttl: Optional[int] = None) -> None:
        super().set(self.key_prefix + key, value, ttl)

    def delete(self, key: str) -> bool:
        return super().delete(self.key_prefix + key)


class DrainFailingBackend(FlakyBackend):
    """Has a key registry whose drain always fails, so no-args falls back to the local sweep."""

    def track_key(self, registry_id: str, key: str) -> None:
        pass

    def drain_tracked(self, registry_id: str, local_keys: Any) -> set[str]:
        raise BackendError("drain failed")


def _failed_delete_records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    """Visible (WARNING+) failed-delete records on a cachekit logger."""
    return [
        r
        for r in caplog.records
        if r.levelno >= logging.WARNING and r.name.startswith("cachekit") and "Failed to delete" in r.getMessage()
    ]


FAILURES = [BackendError(_ERROR_TEXT), OSError(_ERROR_TEXT)]


@pytest.mark.unit
class TestInvalidateFailedDeleteVisibility:
    """A failed L2 delete never raises, and a failing no-args sweep logs ONE visible counted record."""

    @pytest.mark.parametrize("error", FAILURES, ids=lambda e: type(e).__name__)
    @pytest.mark.parametrize("with_args", [True, False], ids=["args", "no_args"])
    def test_sync_never_raises(self, error: BaseException, with_args: bool) -> None:
        backend = FlakyBackend()

        @cache(backend=backend, ttl=60, namespace=f"visible_sync_raise_{with_args}")
        def f(x: int) -> int:
            return x

        f(1)
        backend.delete_error = error
        assert (f.invalidate_cache(1) if with_args else f.invalidate_cache()) is None

    @pytest.mark.asyncio
    @pytest.mark.parametrize("error", FAILURES, ids=lambda e: type(e).__name__)
    @pytest.mark.parametrize("with_args", [True, False], ids=["args", "no_args"])
    async def test_async_never_raises(self, error: BaseException, with_args: bool) -> None:
        backend = FlakyBackend()

        @cache(backend=backend, ttl=60, namespace=f"visible_async_raise_{with_args}")
        async def f(x: int) -> int:
            return x

        await f(1)
        backend.delete_error = error
        assert (await f.ainvalidate_cache(1) if with_args else await f.ainvalidate_cache()) is None

    @pytest.mark.parametrize("error", FAILURES, ids=lambda e: type(e).__name__)
    def test_sync_sweep_logs_one_counted_record(self, caplog: pytest.LogCaptureFixture, error: BaseException) -> None:
        backend = FlakyBackend()

        @cache(backend=backend, ttl=60, namespace="visible_sync_count")
        def f(x: int) -> int:
            return x

        for i in range(5000):
            f(i)
        raw_keys = list(backend.store)
        backend.delete_error = error
        with caplog.at_level(logging.DEBUG, logger="cachekit"):
            f.invalidate_cache()

        records = _failed_delete_records(caplog)
        assert len(records) == 1
        message = records[0].getMessage()
        assert "delete 5000 L2 key" in message
        assert _ERROR_TEXT not in message
        assert not any(key in message for key in raw_keys)

    @pytest.mark.asyncio
    async def test_async_sweep_logs_one_counted_record(self, caplog: pytest.LogCaptureFixture) -> None:
        backend = FlakyBackend()

        @cache(backend=backend, ttl=60, namespace="visible_async_count")
        async def f(x: int) -> int:
            return x

        for i in range(3):
            await f(i)
        raw_keys = list(backend.store)
        backend.delete_error = BackendError(_ERROR_TEXT)
        with caplog.at_level(logging.DEBUG, logger="cachekit"):
            await f.ainvalidate_cache()

        records = _failed_delete_records(caplog)
        assert len(records) == 1
        message = records[0].getMessage()
        assert "delete 3 L2 key" in message
        assert _ERROR_TEXT not in message
        assert not any(key in message for key in raw_keys)

    def test_drain_fallback_logs_one_record_beside_drain_warning(self, caplog: pytest.LogCaptureFixture) -> None:
        backend = DrainFailingBackend()

        @cache(backend=backend, ttl=60, namespace="visible_drain_fallback")
        def f(x: int) -> int:
            return x

        f(1)
        f(2)
        backend.delete_error = OSError(_ERROR_TEXT)
        with caplog.at_level(logging.DEBUG, logger="cachekit"):
            f.invalidate_cache()

        records = _failed_delete_records(caplog)
        assert len(records) == 1
        assert "delete 2 L2 key" in records[0].getMessage()
        assert sum("Key registry drain failed" in r.getMessage() for r in caplog.records) == 1

    @pytest.mark.asyncio
    async def test_async_drain_fallback_logs_one_record(self, caplog: pytest.LogCaptureFixture) -> None:
        backend = DrainFailingBackend()

        @cache(backend=backend, ttl=60, namespace="visible_async_drain_fallback")
        async def f(x: int) -> int:
            return x

        await f(1)
        backend.delete_error = BackendError(_ERROR_TEXT)
        with caplog.at_level(logging.DEBUG, logger="cachekit"):
            await f.ainvalidate_cache()

        assert len(_failed_delete_records(caplog)) == 1

    def test_successful_sweep_logs_nothing(self, caplog: pytest.LogCaptureFixture) -> None:
        backend = FlakyBackend()

        @cache(backend=backend, ttl=60, namespace="visible_success")
        def f(x: int) -> int:
            return x

        f(1)
        f(2)
        with caplog.at_level(logging.DEBUG, logger="cachekit"):
            f.invalidate_cache()

        assert _failed_delete_records(caplog) == []
        assert backend.store == {}

    def test_l1_only_logs_nothing(self, caplog: pytest.LogCaptureFixture) -> None:
        @cache(backend=None, ttl=60, namespace="visible_l1_only")
        def f(x: int) -> int:
            return x

        f(1)
        with caplog.at_level(logging.DEBUG, logger="cachekit"):
            f.invalidate_cache()

        assert _failed_delete_records(caplog) == []

    def test_other_tenant_entries_are_not_counted(self, caplog: pytest.LogCaptureFixture) -> None:
        backend = ScopedFlakyBackend()

        @cache(backend=backend, ttl=60, namespace="visible_tenants")
        def f(x: int) -> int:
            return x

        token = _tenant.set("b")
        try:
            f(1)
            f(2)  # tenant b's two entries: skipped by tenant a's sweep, never counted
        finally:
            _tenant.reset(token)
        backend.delete_error = BackendError(_ERROR_TEXT)

        with caplog.at_level(logging.DEBUG, logger="cachekit"):
            f.invalidate_cache()  # tenant a owns nothing
        assert _failed_delete_records(caplog) == []

        f(3)  # tenant a's one entry
        caplog.clear()
        with caplog.at_level(logging.DEBUG, logger="cachekit"):
            f.invalidate_cache()
        records = _failed_delete_records(caplog)
        assert len(records) == 1
        assert "delete 1 L2 key" in records[0].getMessage()

    def test_failed_keys_are_retried_once_backend_recovers(self, caplog: pytest.LogCaptureFixture) -> None:
        backend = FlakyBackend()

        @cache(backend=backend, ttl=60, namespace="visible_retry")
        def f(x: int) -> int:
            return x

        f(1)
        f(2)
        backend.delete_error = BackendError(_ERROR_TEXT)
        f.invalidate_cache()
        assert len(backend.store) == 2  # still in L2, still tracked

        backend.delete_error = None
        caplog.clear()
        with caplog.at_level(logging.DEBUG, logger="cachekit"):
            f.invalidate_cache()
        assert backend.store == {}
        assert _failed_delete_records(caplog) == []


class MultiDeleteBackend(FlakyBackend):
    """FlakyBackend with the internal multi-key delete: counts calls, fails chosen keys.

    ``_delete_many`` reports every key in ``fail_keys`` as not deleted, raises
    ``batch_error`` as a whole while it is set, and calls ``during_batch`` (if set) after
    applying a batch, before returning to the sweep.
    """

    def __init__(self) -> None:
        super().__init__()
        self.batches: list[list[str]] = []
        self.single_deletes: list[str] = []
        self.fail_keys: set[str] = set()
        self.batch_error: Optional[BaseException] = None
        self.during_batch: Optional[Any] = None

    def delete(self, key: str) -> bool:
        self.single_deletes.append(key)
        return super().delete(key)

    def _delete_many(self, keys: list[str]) -> set[str]:
        self.batches.append(list(keys))
        if self.batch_error is not None:
            raise self.batch_error
        failed = {k for k in keys if k in self.fail_keys}
        for k in keys:
            if k not in failed:
                self.store.pop(k, None)
        if self.during_batch is not None:
            self.during_batch(keys)
        return failed


class ScopedMultiDeleteBackend(MultiDeleteBackend):
    """MultiDeleteBackend under a per-context tenant prefix; records every L2 read."""

    def __init__(self) -> None:
        super().__init__()
        self.reads: list[str] = []

    @property
    def key_prefix(self) -> str:
        return f"t:{_tenant.get()}:"

    def get(self, key: str) -> Optional[bytes]:
        self.reads.append(self.key_prefix + key)
        return super().get(self.key_prefix + key)

    def set(self, key: str, value: bytes, ttl: Optional[int] = None) -> None:
        super().set(self.key_prefix + key, value, ttl)

    def _delete_many(self, keys: list[str]) -> set[str]:
        return {k[len(self.key_prefix) :] for k in super()._delete_many([self.key_prefix + k for k in keys])}


def _tracked(fn: Any) -> set[tuple[str, str]]:
    from tests.unit.test_key_registry import _closure_cell

    return _closure_cell(fn, "_cached_keys").cell_contents


@pytest.mark.unit
class TestInvalidateNoArgsMultiDelete:
    """No-args invalidation batches L2 deletes on a backend with the internal multi-key delete."""

    def test_capability_is_internal_and_class_level(self, tmp_path: Any) -> None:
        import cachekit.backends as backends
        from cachekit.cache_handler import _supports_multi_delete

        assert _supports_multi_delete(MultiDeleteBackend())
        assert not _supports_multi_delete(FlakyBackend())
        assert not _supports_multi_delete(FileBackend(FileBackendConfig(cache_dir=str(tmp_path))))

        class Dynamic(FlakyBackend):
            def __getattr__(self, name: str) -> Any:
                return lambda *a, **k: set()

        assert not _supports_multi_delete(Dynamic())  # instance-level attributes do not count
        assert not any("delete_many" in name.lower() or "multi" in name.lower() for name in backends.__all__)

    def test_subclass_overriding_delete_loses_inherited_capability(self) -> None:
        from cachekit.backends.memcached.backend import MemcachedBackend
        from cachekit.backends.redis.backend import RedisBackend
        from cachekit.cache_handler import _supports_multi_delete

        class PrefixedRedis(RedisBackend):
            def delete(self, key: str) -> bool:
                return super().delete("app:" + key)

        class TunedRedis(RedisBackend):  # does not touch delete: keeps the batch path
            pass

        class PrefixedWithBatch(PrefixedRedis):  # re-declares a matching batch: keeps it
            def _delete_many(self, keys: list[str]) -> set[str]:
                return super()._delete_many(["app:" + k for k in keys])

        for cls in (RedisBackend, MemcachedBackend, TunedRedis, PrefixedWithBatch):
            assert _supports_multi_delete(object.__new__(cls)), cls
        assert not _supports_multi_delete(object.__new__(PrefixedRedis))

    def test_dynamic_or_instance_delete_loses_capability(self) -> None:
        from cachekit.cache_handler import _supports_multi_delete

        patched = MultiDeleteBackend()
        patched.delete = lambda key: True  # type: ignore[method-assign]
        assert not _supports_multi_delete(patched)

        batch_patched = MultiDeleteBackend()
        batch_patched._delete_many = lambda keys: set()  # type: ignore[method-assign]
        assert not _supports_multi_delete(batch_patched)

        class Dispatching(MultiDeleteBackend):
            def __getattribute__(self, name: str) -> Any:
                return object.__getattribute__(self, name)

        assert not _supports_multi_delete(Dispatching())
        assert _supports_multi_delete(MultiDeleteBackend())

    def test_slots_backend_with_getattr_does_not_break_the_guard(self) -> None:
        """A __slots__ backend has no instance __dict__; its __getattr__ must not answer for one."""
        from cachekit.cache_handler import _supports_multi_delete

        class Slotted:
            __slots__ = ("store",)

            def __init__(self) -> None:
                self.store: dict[str, bytes] = {}

            def __getattr__(self, name: str) -> Any:
                if name == "__dict__":  # reached only through a plain getattr: slots leave no __dict__
                    return lambda *a, **k: None  # not a container: `in` on it would raise TypeError
                raise AttributeError(name)

            def get(self, key: str) -> Optional[bytes]:
                return self.store.get(key)

            def set(self, key: str, value: bytes, ttl: Optional[int] = None) -> None:
                self.store[key] = value

            def delete(self, key: str) -> bool:
                return self.store.pop(key, None) is not None

            def _delete_many(self, keys: list[str]) -> set[str]:
                for k in keys:
                    self.store.pop(k, None)
                return set()

            def exists(self, key: str) -> bool:
                return key in self.store

            def health_check(self) -> tuple[bool, dict[str, Any]]:
                return True, {"backend_type": "fake", "latency_ms": 0.0}

        backend = Slotted()
        assert _supports_multi_delete(backend)

        @cache(backend=backend, ttl=60, namespace="multi_slots_getattr")
        def f(x: int) -> int:
            return x

        f(1)
        f(2)
        assert f.invalidate_cache() is None
        assert backend.store == {}
        assert _tracked(f) == set()

    def test_descriptor_backed_delete_loses_capability(self) -> None:
        """A slot or property delete can differ per instance: no batch path, even beside _delete_many."""
        from cachekit.cache_handler import _supports_multi_delete

        class SlottedDelete:
            __slots__ = ("delete",)

            def _delete_many(self, keys: list[str]) -> set[str]:
                return set()

        slotted = SlottedDelete()
        slotted.delete = lambda key: True  # type: ignore[method-assign]
        assert not _supports_multi_delete(slotted)

        class PropertyDelete(MultiDeleteBackend):
            @property
            def delete(self) -> Any:  # type: ignore[override]
                return lambda key: True

            def _delete_many(self, keys: list[str]) -> set[str]:
                return set()

        assert not _supports_multi_delete(PropertyDelete())

        class SlottedBatch:
            __slots__ = ("_delete_many",)

            def delete(self, key: str) -> bool:
                return True

        assert not _supports_multi_delete(SlottedBatch())

    def test_slotted_prefixed_delete_sweeps_through_it(self) -> None:
        """Kody's case end to end: a per-instance prefixing delete in a slot, beside a raw batch."""
        from collections.abc import Callable

        class Slotted:
            __slots__ = ("store", "delete", "batches")

            def __init__(self) -> None:
                self.store: dict[str, bytes] = {}
                self.batches: list[list[str]] = []
                self.delete: Callable[[str], bool] = lambda key: self.store.pop("app:" + key, None) is not None

            def get(self, key: str) -> Optional[bytes]:
                return self.store.get("app:" + key)

            def set(self, key: str, value: bytes, ttl: Optional[int] = None) -> None:
                self.store["app:" + key] = value

            def _delete_many(self, keys: list[str]) -> set[str]:  # raw keys: wrong for this backend
                self.batches.append(list(keys))
                for k in keys:
                    self.store.pop(k, None)
                return set()

            def exists(self, key: str) -> bool:
                return "app:" + key in self.store

            def health_check(self) -> tuple[bool, dict[str, Any]]:
                return True, {"backend_type": "fake", "latency_ms": 0.0}

        backend = Slotted()

        @cache(backend=backend, ttl=60, namespace="multi_slotted_prefix")
        def f(x: int) -> int:
            return x

        for i in range(3):
            f(i)
        f.invalidate_cache()
        assert backend.batches == []
        assert backend.store == {}
        assert _tracked(f) == set()

    def test_shadowed_instance_dict_delete_sweeps_through_it(self) -> None:
        """A __dict__ property hides the real instance dict: the guard must still see its delete."""
        from cachekit.cache_handler import _supports_multi_delete

        class Shadowed(MultiDeleteBackend):
            @property
            def __dict__(self) -> dict[str, Any]:  # type: ignore[override]
                return {}

            def get(self, key: str) -> Optional[bytes]:
                return super().get("app:" + key)

            def set(self, key: str, value: bytes, ttl: Optional[int] = None) -> None:
                super().set("app:" + key, value, ttl)

        backend = Shadowed()
        # Write into the real instance dict through the base class's own __dict__ descriptor.
        base_dict = next(c.__dict__["__dict__"] for c in type(backend).__mro__[1:] if "__dict__" in c.__dict__)
        parent = MultiDeleteBackend.delete.__get__(backend)
        base_dict.__get__(backend)["delete"] = lambda key: parent("app:" + key)
        assert backend.__dict__ == {}  # the shadow hides it
        assert not _supports_multi_delete(backend)

        @cache(backend=backend, ttl=60, namespace="multi_shadowed_dict")
        def f(x: int) -> int:
            return x

        for i in range(3):
            f(i)
        f.invalidate_cache()
        assert backend.batches == []
        assert backend.store == {}
        assert _tracked(f) == set()

    def test_bound_to_another_instance_loses_capability(self) -> None:
        from cachekit.cache_handler import _supports_multi_delete

        a, b = MultiDeleteBackend(), MultiDeleteBackend()
        b.delete = a.delete  # type: ignore[method-assign]  # right function, wrong self
        assert not _supports_multi_delete(b)
        assert _supports_multi_delete(a)

    def test_dynamically_prefixed_delete_sweeps_through_it(self) -> None:
        """An instance whose delete() rewrites keys: the sweep must not batch-delete the raw keys."""

        class Prefixed(MultiDeleteBackend):
            def get(self, key: str) -> Optional[bytes]:
                return super().get("app:" + key)

            def set(self, key: str, value: bytes, ttl: Optional[int] = None) -> None:
                super().set("app:" + key, value, ttl)

            def __getattribute__(self, name: str) -> Any:
                if name == "delete":
                    parent = MultiDeleteBackend.delete.__get__(self)
                    return lambda key: parent("app:" + key)
                return object.__getattribute__(self, name)

        backend = Prefixed()

        @cache(backend=backend, ttl=60, namespace="multi_dynamic_prefix")
        def f(x: int) -> int:
            return x

        for i in range(3):
            f(i)
        f.invalidate_cache()
        assert backend.batches == []
        assert backend.store == {}
        assert _tracked(f) == set()

    def test_subclass_overriding_delete_sweeps_through_its_delete(self) -> None:
        class Prefixed(MultiDeleteBackend):
            def get(self, key: str) -> Optional[bytes]:
                return super().get("app:" + key)

            def set(self, key: str, value: bytes, ttl: Optional[int] = None) -> None:
                super().set("app:" + key, value, ttl)

            def delete(self, key: str) -> bool:
                return super().delete("app:" + key)

        backend = Prefixed()

        @cache(backend=backend, ttl=60, namespace="multi_prefixed_subclass")
        def f(x: int) -> int:
            return x

        for i in range(3):
            f(i)
        f.invalidate_cache()
        assert backend.batches == []  # the parent's batch would delete unprefixed keys
        assert backend.store == {}
        assert _tracked(f) == set()

    def test_custom_backend_without_capability_keeps_per_key_loop(self) -> None:
        deletes: list[str] = []

        class Minimal(FlakyBackend):
            def delete(self, key: str) -> bool:
                deletes.append(key)
                return super().delete(key)

        backend = Minimal()

        @cache(backend=backend, ttl=60, namespace="multi_minimal")
        def f(x: int) -> int:
            return x

        for i in range(5):
            f(i)
        f.invalidate_cache()
        assert len(deletes) == 5
        assert backend.store == {}

    @pytest.mark.parametrize(("n", "calls"), [(5000, 1), (12_345, 2)])
    def test_round_trips_bounded(self, n: int, calls: int) -> None:
        backend = MultiDeleteBackend()

        @cache(backend=backend, ttl=60, namespace=f"multi_bounded_{n}")
        def f(x: int) -> int:
            return x

        for i in range(n):
            f(i)
        f.invalidate_cache()

        assert len(backend.batches) == calls
        assert all(len(b) <= 10_000 for b in backend.batches)
        assert sum(len(b) for b in backend.batches) == n
        assert backend.single_deletes == []
        assert backend.store == {}
        assert _tracked(f) == set()

    def test_reported_failures_stay_tracked_and_are_counted(self, caplog: pytest.LogCaptureFixture) -> None:
        backend = MultiDeleteBackend()

        @cache(backend=backend, ttl=60, namespace="multi_partial")
        def f(x: int) -> int:
            return x

        for i in range(10):
            f(i)
        failing = set(list(backend.store)[:2])
        backend.fail_keys = failing
        with caplog.at_level(logging.DEBUG, logger="cachekit"):
            f.invalidate_cache()

        assert {key for _, key in _tracked(f)} == failing
        assert set(backend.store) == failing
        records = _failed_delete_records(caplog)
        assert len(records) == 1
        assert "delete 2 L2 key" in records[0].getMessage()

        backend.fail_keys = set()
        backend.batches.clear()
        f.invalidate_cache()  # the retry sends exactly the two failed keys
        assert [set(b) for b in backend.batches] == [failing]
        assert backend.store == {}

    def test_batch_that_raises_falls_back_per_key(self, caplog: pytest.LogCaptureFixture) -> None:
        backend = MultiDeleteBackend()

        @cache(backend=backend, ttl=60, namespace="multi_raise")
        def f(x: int) -> int:
            return x

        for i in range(4):
            f(i)
        backend.batch_error = BackendError(_ERROR_TEXT)
        with caplog.at_level(logging.DEBUG, logger="cachekit"):
            f.invalidate_cache()
        warnings = [r for r in caplog.records if "Multi-key L2 delete failed" in r.getMessage()]
        assert len(warnings) == 1 and warnings[0].levelno == logging.WARNING
        assert _ERROR_TEXT not in warnings[0].getMessage()
        assert _failed_delete_records(caplog) == []  # the fallback succeeded
        caplog.clear()
        assert len(backend.batches) == 1
        assert len(backend.single_deletes) == 4
        assert backend.store == {}
        assert _tracked(f) == set()

        for i in range(4):
            f(i)
        backend.delete_error = OSError(_ERROR_TEXT)  # the per-key fallback fails too
        with caplog.at_level(logging.DEBUG, logger="cachekit"):
            f.invalidate_cache()
        records = _failed_delete_records(caplog)
        assert len(records) == 1
        assert "delete 4 L2 key" in records[0].getMessage()
        assert len(_tracked(f)) == 4

    def test_l2_delete_precedes_trim_precedes_l1_eviction(self) -> None:
        backend = MultiDeleteBackend()
        calls = 0

        @cache(backend=backend, ttl=60, namespace="multi_order")
        def f(x: int) -> int:
            nonlocal calls
            calls += 1
            return x

        f(1)
        f(2)
        seen: dict[str, Any] = {}

        def check(keys: list[str]) -> None:
            seen["tracked"] = len(_tracked(f))
            before = calls
            f(1)  # still an L1 hit: L1 is evicted only after the batch returns
            seen["l1_hit"] = calls == before

        backend.during_batch = check
        f.invalidate_cache()
        assert seen == {"tracked": 2, "l1_hit": True}
        backend.during_batch = None
        f(1)
        assert calls == 3  # L1 evicted after the batch

    def test_rewrite_during_batch_stays_recorded(self) -> None:
        backend = MultiDeleteBackend()

        @cache(backend=backend, ttl=60, namespace="multi_rerecord", l1_enabled=False)
        def f(x: int) -> int:
            return x

        f(1)
        f(2)
        backend.during_batch = lambda keys: f(1)  # a concurrent miss rewrites k after its delete
        f.invalidate_cache()

        assert len(_tracked(f)) == 1
        (entry,) = _tracked(f)
        assert entry[1] in backend.store
        backend.during_batch = None
        f.invalidate_cache()
        assert backend.store == {}

    def test_only_calling_tenants_keys_go_to_the_batch(self) -> None:
        backend = ScopedMultiDeleteBackend()
        calls = 0

        @cache(backend=backend, ttl=60, namespace="multi_tenants")
        def f(x: int) -> int:
            nonlocal calls
            calls += 1
            return x

        token = _tenant.set("b")
        try:
            f(1)
            f(2)
        finally:
            _tenant.reset(token)
        f(3)  # tenant a
        b_keys = {k for k in backend.store if k.startswith("t:b:")}

        f.invalidate_cache()  # as tenant a

        assert len(backend.batches) == 1
        assert all(k.startswith("t:a:") for k in backend.batches[0]) and len(backend.batches[0]) == 1
        assert set(backend.store) == b_keys  # tenant b's L2 untouched
        assert {scope for scope, _ in _tracked(f)} == {"t:b:"}  # and still tracked
        backend.reads.clear()
        token = _tenant.set("b")
        try:
            assert f(1) == 1
        finally:
            _tenant.reset(token)
        assert calls == 3  # served from tenant b's surviving L2 value, not recomputed
        assert backend.reads  # ...after an L1 miss: only tenant b's L1 copy was evicted
