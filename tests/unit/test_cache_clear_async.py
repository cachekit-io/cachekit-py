"""Bug #49/#76: cache_clear() behavior for async-decorated functions.

History:
- #49: cache_clear() on async created an unawaited coroutine (fixed by raising TypeError)
- #76: TypeError is unnecessary in L1-only mode (no backend I/O needed)

Current behavior:
- L1-only mode (backend=None): cache_clear() works synchronously for both sync and async
- With backend: cache_clear() raises TypeError for async (must use ainvalidate_cache)
"""

import asyncio
from unittest.mock import MagicMock, patch

import pytest

from cachekit.decorators import cache


class TestCacheClearAsyncL1Only:
    """cache_clear() works synchronously in L1-only mode (#76 fix)."""

    def test_cache_clear_works_for_async_l1_only(self):
        """cache_clear() on async + backend=None must NOT raise.

        In L1-only mode, invalidation is synchronous (no backend I/O),
        so cache_clear() can safely clear without awaiting.
        """

        @cache(backend=None)
        async def async_func(x: int) -> int:
            return x * 2

        # Should NOT raise TypeError
        async_func.cache_clear()

    def test_cache_clear_actually_clears_async_l1_only(self):
        """cache_clear() must actually clear cached entries for async L1-only."""
        call_count = 0

        @cache(backend=None, ttl=300, namespace="test_clear_async_l1")
        async def async_func(x: int) -> int:
            nonlocal call_count
            call_count += 1
            return x * 2

        async def run():
            nonlocal call_count

            await async_func(5)
            assert call_count == 1

            await async_func(5)  # cached
            assert call_count == 1

            async_func.cache_clear()

            await async_func(5)  # recomputed
            assert call_count == 2

        asyncio.run(run())


class TestCacheClearAsyncWithBackend:
    """cache_clear() raises TypeError when a backend is involved."""

    def test_cache_clear_raises_type_error_with_backend(self):
        """cache_clear() on async with a real backend must raise TypeError.

        When a backend exists, invalidation requires async I/O (delete from Redis).
        cache_clear() is sync, so it cannot safely invalidate L2.
        """
        mock_backend = MagicMock()

        @cache(backend=mock_backend)
        async def async_func(x: int) -> int:
            return x * 2

        with pytest.raises(TypeError, match="cache_clear\\(\\) cannot clear cache for async functions with a backend"):
            async_func.cache_clear()


class TestCacheClearSync:
    """cache_clear() always works for sync functions (no regression)."""

    def test_cache_clear_does_not_raise_for_sync_function(self):
        """Sync cache_clear() must NOT raise TypeError."""
        with patch("cachekit.decorators.wrapper.get_backend_provider") as mock_provider:
            mock_provider.return_value.get_backend.side_effect = RuntimeError("Should not be called!")

            @cache(backend=None)
            def sync_func(x: int) -> int:
                return x * 2

            sync_func.cache_clear()  # No exception = pass


class TestAsyncInvalidateCacheStillWorks:
    """ainvalidate_cache() remains the recommended path for async+backend."""

    def test_async_ainvalidate_cache_works(self):
        """The recommended path (ainvalidate_cache) must still work for async."""
        call_count = 0

        @cache(backend=None, namespace="test_ainvalidate_works")
        async def async_func(x: int) -> int:
            nonlocal call_count
            call_count += 1
            return x * 2

        async def run_test():
            nonlocal call_count

            result1 = await async_func(5)
            assert result1 == 10
            assert call_count == 1

            result2 = await async_func(5)
            assert result2 == 10
            assert call_count == 1

            await async_func.ainvalidate_cache(5)

            result3 = await async_func(5)
            assert result3 == 10
            assert call_count == 2

        asyncio.run(run_test())
