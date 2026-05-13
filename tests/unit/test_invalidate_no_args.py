"""
Test for #59: invalidate_cache() / ainvalidate_cache() with no args on parameterized functions.

Bug: When invalidate_cache() is called with no arguments on a function that HAS parameters,
it generates a cache key for the zero-argument call (which was never cached) and invalidates
that non-existent key. All cached entries for real argument combinations survive.

Expected: calling invalidate_cache() with no args on a parameterized function should clear
ALL cached entries for that function (namespace-level invalidation).
"""

from __future__ import annotations

import pytest

from cachekit import cache
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
