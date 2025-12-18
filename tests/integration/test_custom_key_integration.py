"""Integration tests for custom key= parameter in @cache decorator.

Tests the actual decorator behavior with L1-only mode (no Redis required).
"""

from __future__ import annotations

import hashlib

import pytest

from cachekit import cache


class TestCustomKeyFunctionIntegration:
    """Test custom key function with actual caching behavior."""

    def test_custom_key_function_caches_correctly(self):
        """Custom key function produces cache hits."""
        call_count = 0

        @cache(
            key=lambda x, y: f"{x}:{y}",
            backend=None,  # L1-only mode
            l1_enabled=True,
        )
        def add(x: int, y: int) -> int:
            nonlocal call_count
            call_count += 1
            return x + y

        # First call - cache miss
        result1 = add(1, 2)
        assert result1 == 3
        assert call_count == 1

        # Second call with same args - cache hit
        result2 = add(1, 2)
        assert result2 == 3
        assert call_count == 1  # No additional call

        # Different args - cache miss
        result3 = add(2, 3)
        assert result3 == 5
        assert call_count == 2

    def test_custom_key_function_different_args_same_key(self):
        """Custom key can map different args to same cache entry."""
        call_count = 0

        # Key function ignores second argument - must accept **kwargs
        def user_key(user_id, include_deleted=False, **kwargs):
            return f"user:{user_id}"

        @cache(
            key=user_key,
            backend=None,
            l1_enabled=True,
        )
        def get_user(user_id: int, include_deleted: bool = False) -> dict:
            nonlocal call_count
            call_count += 1
            return {"id": user_id, "name": f"User {user_id}"}

        # First call
        result1 = get_user(123, include_deleted=False)
        assert call_count == 1

        # Same user_id, different include_deleted - should hit cache
        # because key function ignores include_deleted
        result2 = get_user(123, include_deleted=True)
        assert call_count == 1  # Still 1 - cache hit
        assert result1 == result2

    def test_custom_key_with_numpy_array(self):
        """Custom key enables numpy arrays as arguments."""
        np = pytest.importorskip("numpy")
        call_count = 0

        def array_key(arr):
            return hashlib.blake2b(arr.tobytes(), digest_size=16).hexdigest()

        @cache(
            key=array_key,
            backend=None,
            l1_enabled=True,
        )
        def sum_array(arr) -> float:
            nonlocal call_count
            call_count += 1
            return float(arr.sum())

        arr1 = np.array([1.0, 2.0, 3.0])
        arr2 = np.array([1.0, 2.0, 3.0])  # Same content
        arr3 = np.array([4.0, 5.0, 6.0])  # Different content

        # First call
        result1 = sum_array(arr1)
        assert result1 == 6.0
        assert call_count == 1

        # Same content array - cache hit
        result2 = sum_array(arr2)
        assert result2 == 6.0
        assert call_count == 1

        # Different content - cache miss
        result3 = sum_array(arr3)
        assert result3 == 15.0
        assert call_count == 2

    def test_custom_key_wrong_return_type_falls_through(self):
        """Key function returning non-string falls through to function execution."""
        call_count = 0

        @cache(
            key=lambda x: x,  # Returns int, not str - will fail
            backend=None,
            l1_enabled=True,
        )
        def process(x: int) -> int:
            nonlocal call_count
            call_count += 1
            return x * 2

        # Should execute function despite key error (graceful degradation)
        result = process(42)
        assert result == 84
        assert call_count == 1

        # Second call also falls through (no caching due to key error)
        result2 = process(42)
        assert result2 == 84
        assert call_count == 2  # Called again - no cache

    def test_custom_key_error_executes_function(self):
        """Key function error falls through to function execution."""
        call_count = 0

        def bad_key(*args):
            raise ValueError("Key generation failed")

        @cache(
            key=bad_key,
            backend=None,
            l1_enabled=True,
        )
        def add(x: int, y: int) -> int:
            nonlocal call_count
            call_count += 1
            return x + y

        # Should execute function despite key error
        # (errors in key generation fall through to function)
        result = add(1, 2)
        assert result == 3
        assert call_count == 1


class TestCustomKeyFunctionAsync:
    """Test custom key function with async functions."""

    @pytest.mark.asyncio
    async def test_async_custom_key_function(self):
        """Custom key works with async functions."""
        call_count = 0

        @cache(
            key=lambda x: f"async:{x}",
            backend=None,
            l1_enabled=True,
        )
        async def async_double(x: int) -> int:
            nonlocal call_count
            call_count += 1
            return x * 2

        # First call - cache miss
        result1 = await async_double(5)
        assert result1 == 10
        assert call_count == 1

        # Second call - cache hit
        result2 = await async_double(5)
        assert result2 == 10
        assert call_count == 1

    @pytest.mark.asyncio
    async def test_async_custom_key_wrong_return_type_falls_through(self):
        """Async: Key function returning non-string falls through to function execution."""
        call_count = 0

        @cache(
            key=lambda x: 123,  # Returns int, not str - will fail
            backend=None,
            l1_enabled=True,
        )
        async def process(x: int) -> int:
            nonlocal call_count
            call_count += 1
            return x * 2

        # Should execute function despite key error (graceful degradation)
        result = await process(42)
        assert result == 84
        assert call_count == 1
