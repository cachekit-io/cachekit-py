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
