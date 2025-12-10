"""TDD-based cache implementation tests against real Redis."""

import asyncio
import time

import pytest

from cachekit import cache


@pytest.mark.integration
class TestTDDSyncCacheImplementation:
    """TDD tests for synchronous cache implementation."""

    @pytest.fixture(autouse=True)
    def setup_test_env(self, skip_if_no_redis):
        """Ensure Redis is available and clean test environment."""
        import os

        os.environ["REDIS_POOL_DB"] = "15"

        yield

        # Cleanup test database
        try:
            import redis

            client = redis.Redis(host="localhost", port=6379, db=15)
            client.flushdb()
            client.close()
        except Exception:
            pass

    def test_sync_cache_different_arguments(self):
        """Different arguments should create separate cache entries."""
        call_count = 0

        @cache(ttl=300)
        def compute_square(n):
            nonlocal call_count
            call_count += 1
            return n * n

        # Different arguments should each execute once
        result1 = compute_square(5)
        result2 = compute_square(6)
        result3 = compute_square(5)  # Should hit cache

        assert result1 == 25
        assert result2 == 36
        assert result3 == 25  # Same as first call
        assert call_count == 2  # Only two unique calls

    def test_sync_cache_with_kwargs(self):
        """Cache should handle keyword arguments correctly."""
        call_count = 0

        @cache(ttl=300)
        def format_message(message, prefix="", suffix=""):
            nonlocal call_count
            call_count += 1
            return f"{prefix}{message}{suffix}"

        # Different kwarg combinations should cache separately
        result1 = format_message("hello", prefix=">> ")
        result2 = format_message("hello", suffix=" <<")
        result3 = format_message("hello", prefix=">> ")  # Cache hit

        assert result1 == ">> hello"
        assert result2 == "hello <<"
        assert result3 == ">> hello"
        assert call_count == 2

    def test_sync_cache_complex_return_types(self):
        """Cache should handle complex data structures."""
        call_count = 0

        @cache(ttl=300)
        def get_user_data(user_id):
            nonlocal call_count
            call_count += 1
            return {
                "id": user_id,
                "profile": {"name": f"User {user_id}", "active": True},
                "permissions": ["read", "write"],
                "metadata": {"created": "2024-01-01", "logins": 42},
            }

        # First call
        data1 = get_user_data(123)
        assert data1["id"] == 123
        assert data1["profile"]["name"] == "User 123"
        assert call_count == 1

        # Cache hit
        data2 = get_user_data(123)
        assert data2 == data1  # Exact same structure
        assert call_count == 1

    def test_sync_cache_ttl_expiration(self):
        """Cache should expire after TTL period."""
        call_count = 0

        @cache(ttl=1)  # 1 second TTL
        def time_sensitive_func():
            nonlocal call_count
            call_count += 1
            return f"result_{call_count}_{time.time()}"

        # First call
        result1 = time_sensitive_func()
        assert call_count == 1

        # Immediate second call - cache hit
        result2 = time_sensitive_func()
        assert result2 == result1
        assert call_count == 1

        # Wait for expiration
        time.sleep(1.5)

        # Third call - cache expired, function executes
        result3 = time_sensitive_func()
        assert result3 != result1
        assert call_count == 2

    def test_sync_cache_invalidation(self):
        """Manual cache invalidation should work."""
        call_count = 0

        @cache(ttl=3600)  # Long TTL
        def cacheable_computation(value):
            nonlocal call_count
            call_count += 1
            return value**2 + call_count

        # Cache initial result
        result1 = cacheable_computation(10)
        assert result1 == 101  # 100 + 1
        assert call_count == 1

        # Verify cache hit
        result2 = cacheable_computation(10)
        assert result2 == 101
        assert call_count == 1

        # Invalidate cache
        cacheable_computation.invalidate_cache(10)

        # Should execute function again
        result3 = cacheable_computation(10)
        assert result3 == 102  # 100 + 2
        assert call_count == 2


@pytest.mark.integration
class TestTDDAsyncCacheImplementation:
    """TDD tests for asynchronous cache implementation."""

    @pytest.fixture(autouse=True)
    def setup_test_env(self, skip_if_no_redis):
        """Ensure Redis is available and clean test environment."""
        import os

        os.environ["REDIS_POOL_DB"] = "15"

        yield

        # Cleanup test database
        try:
            import redis

            client = redis.Redis(host="localhost", port=6379, db=15)
            client.flushdb()
            client.close()
        except Exception:
            pass

    def test_async_cache_concurrent_calls(self):
        """Concurrent async calls with same args should not cause race conditions."""
        call_count = 0

        @cache(ttl=300)
        async def slow_async_func(value):
            nonlocal call_count
            call_count += 1
            await asyncio.sleep(0.1)  # Simulate slow operation
            return f"result_{value}_{call_count}"

        async def run_concurrent_test():
            # Start multiple concurrent calls with same arguments
            tasks = [
                slow_async_func("test"),
                slow_async_func("test"),
                slow_async_func("test"),
            ]

            results = await asyncio.gather(*tasks)

            # All should get the same result
            assert len(set(results)) == 1
            # Function should only be called once
            assert call_count == 1

        asyncio.run(run_concurrent_test())

    def test_async_cache_different_arguments(self):
        """Different async arguments should create separate cache entries."""
        call_count = 0

        @cache(ttl=300)
        async def async_multiply(a, b):
            nonlocal call_count
            call_count += 1
            await asyncio.sleep(0.01)
            return a * b

        async def run_test():
            # Different arguments should each execute
            result1 = await async_multiply(3, 4)
            result2 = await async_multiply(5, 6)
            result3 = await async_multiply(3, 4)  # Cache hit

            assert result1 == 12
            assert result2 == 30
            assert result3 == 12  # Same as first
            assert call_count == 2  # Only two unique calls

        asyncio.run(run_test())

    def test_async_cache_complex_async_operations(self):
        """Cache should work with complex async operations."""
        call_count = 0

        @cache(ttl=300)
        async def complex_async_operation(operation_id):
            nonlocal call_count
            call_count += 1

            # Simulate multiple async steps
            await asyncio.sleep(0.01)
            intermediate = f"step1_{operation_id}"

            await asyncio.sleep(0.01)
            result = f"final_{intermediate}_{call_count}"

            return {
                "operation_id": operation_id,
                "result": result,
                "processing_time": 0.02,
                "call_number": call_count,
            }

        async def run_test():
            # First call
            data1 = await complex_async_operation("op123")
            assert data1["operation_id"] == "op123"
            assert data1["call_number"] == 1
            assert call_count == 1

            # Cache hit
            data2 = await complex_async_operation("op123")
            assert data2 == data1  # Exact same result
            assert call_count == 1

        asyncio.run(run_test())

    def test_async_cache_invalidation(self):
        """Async cache invalidation should work correctly."""
        call_count = 0

        @cache(ttl=3600)
        async def async_cached_func(key):
            nonlocal call_count
            call_count += 1
            await asyncio.sleep(0.01)
            return f"value_{key}_{call_count}"

        async def run_test():
            # Cache initial result
            result1 = await async_cached_func("mykey")
            assert result1 == "value_mykey_1"
            assert call_count == 1

            # Verify cache hit
            result2 = await async_cached_func("mykey")
            assert result2 == "value_mykey_1"
            assert call_count == 1

            # Invalidate cache (await the async method)
            await async_cached_func.invalidate_cache("mykey")

            # Should execute function again
            result3 = await async_cached_func("mykey")
            assert result3 == "value_mykey_2"
            assert call_count == 2

        asyncio.run(run_test())
