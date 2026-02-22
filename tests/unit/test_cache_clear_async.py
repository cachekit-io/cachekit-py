"""Bug #49: cache_clear() broken for async-decorated functions.

Symptom: Calling cache_clear() on an async-decorated function creates
an unawaited coroutine (ainvalidate_cache()) that gets GC'd silently.
The cache is never cleared and Python emits RuntimeWarning.

Fix: cache_clear() is sync -- it cannot await. Raise TypeError telling
the user to use 'await fn.ainvalidate_cache()' instead.
"""

import asyncio
from unittest.mock import patch

import pytest

from cachekit.decorators import cache


class TestCacheClearAsyncBug:
    """Regression tests for GitHub Issue #49."""

    def test_cache_clear_raises_type_error_for_async_function(self):
        """cache_clear() on an async function must raise TypeError.

        BUG REPRODUCTION: Previously, cache_clear() called ainvalidate_cache()
        without awaiting, creating a dangling coroutine that was silently GC'd.
        """
        with patch("cachekit.decorators.wrapper.get_backend_provider") as mock_provider:
            mock_provider.return_value.get_backend.side_effect = RuntimeError("Should not be called!")

            @cache(backend=None)
            async def async_func(x: int) -> int:
                return x * 2

            with pytest.raises(TypeError, match="cache_clear\\(\\) cannot clear cache for async functions"):
                async_func.cache_clear()

    def test_cache_clear_error_message_suggests_ainvalidate(self):
        """TypeError message must tell the user what to use instead."""
        with patch("cachekit.decorators.wrapper.get_backend_provider") as mock_provider:
            mock_provider.return_value.get_backend.side_effect = RuntimeError("Should not be called!")

            @cache(backend=None)
            async def async_func(x: int) -> int:
                return x * 2

            with pytest.raises(TypeError, match="await fn.ainvalidate_cache\\(\\)"):
                async_func.cache_clear()

    def test_cache_clear_does_not_raise_for_sync_function(self):
        """Sync cache_clear() must NOT raise TypeError -- no regression.

        This test verifies that the async fix does not break sync cache_clear().
        We only verify it runs without raising, not full invalidation behavior
        (which depends on key generation with no args -- a separate concern).
        """
        with patch("cachekit.decorators.wrapper.get_backend_provider") as mock_provider:
            mock_provider.return_value.get_backend.side_effect = RuntimeError("Should not be called!")

            @cache(backend=None)
            def sync_func(x: int) -> int:
                return x * 2

            # cache_clear() should NOT raise TypeError for sync functions
            sync_func.cache_clear()  # No exception = pass

    def test_async_ainvalidate_cache_still_works(self):
        """The recommended path (ainvalidate_cache) must still work for async."""
        with patch("cachekit.decorators.wrapper.get_backend_provider") as mock_provider:
            mock_provider.return_value.get_backend.side_effect = RuntimeError("Should not be called!")

            call_count = 0

            @cache(backend=None)
            async def async_func(x: int) -> int:
                nonlocal call_count
                call_count += 1
                return x * 2

            async def run_test():
                nonlocal call_count

                # Populate cache
                result1 = await async_func(5)
                assert result1 == 10
                assert call_count == 1

                # Cached hit
                result2 = await async_func(5)
                assert result2 == 10
                assert call_count == 1

                # Use the correct async invalidation path
                await async_func.ainvalidate_cache(5)

                # After invalidation, function should re-execute
                result3 = await async_func(5)
                assert result3 == 10
                assert call_count == 2

            asyncio.run(run_test())
