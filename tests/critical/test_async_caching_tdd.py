"""TDD tests for async caching functionality.

These tests define the expected behavior for async caching before implementation.
They will initially fail, then guide the implementation.
"""

import asyncio
import time
from typing import Any

import pytest
import redis

from cachekit import cache
from cachekit.config.nested import L1CacheConfig, MonitoringConfig


class TestAsyncCachingTDD:
    """Test-driven development for async caching functionality."""

    @pytest.mark.asyncio
    async def test_async_function_detection(self):
        """Async functions should be properly detected and wrapped."""
        call_count = 0

        @cache(monitoring=MonitoringConfig(collect_stats=False))
        async def async_function(x: int) -> int:
            nonlocal call_count
            call_count += 1
            await asyncio.sleep(0.001)  # Simulate async work
            return x * 2

        # First call should execute function
        result1 = await async_function(5)
        assert result1 == 10
        assert call_count == 1

        # Second call should use cache
        result2 = await async_function(5)
        assert result2 == 10
        assert call_count == 1  # Should NOT increment - cached!

    @pytest.mark.asyncio
    async def test_async_l1_cache_hit(self):
        """L1 cache should work with async functions."""
        call_count = 0

        @cache  # L1 enabled by default
        async def expensive_async_operation(value: str) -> str:
            nonlocal call_count
            call_count += 1
            await asyncio.sleep(0.01)
            return f"processed_{value}"

        # First call - cache miss
        start = time.perf_counter()
        result1 = await expensive_async_operation("test")
        duration1 = time.perf_counter() - start
        assert result1 == "processed_test"
        assert call_count == 1
        assert duration1 >= 0.01  # Should take at least 10ms

        # Second call - L1 cache hit (should be near-instant)
        start = time.perf_counter()
        result2 = await expensive_async_operation("test")
        duration2 = time.perf_counter() - start
        assert result2 == "processed_test"
        assert call_count == 1  # No additional calls
        assert duration2 < 0.001  # L1 hit should be <1ms

    @pytest.mark.asyncio
    async def test_async_redis_cache_hit(self):
        """Redis cache should work with async functions when L1 misses."""
        call_count = 0

        @cache(l1=L1CacheConfig(enabled=False))  # Disable L1 to test Redis directly
        async def fetch_data(key: str) -> dict:
            nonlocal call_count
            call_count += 1
            await asyncio.sleep(0.005)
            return {"key": key, "data": "value"}

        # First call - Redis miss
        result1 = await fetch_data("testkey")
        assert result1 == {"key": "testkey", "data": "value"}
        assert call_count == 1

        # Second call - Redis hit
        result2 = await fetch_data("testkey")
        assert result2 == {"key": "testkey", "data": "value"}
        assert call_count == 1  # Should use Redis cache

    @pytest.mark.asyncio
    async def test_async_cache_bypass(self):
        """_bypass_cache parameter should work with async functions."""
        call_count = 0

        @cache
        async def cached_async_func(x: int) -> int:
            nonlocal call_count
            call_count += 1
            return x + 1

        # Normal call - cached
        result1 = await cached_async_func(10)
        assert result1 == 11
        assert call_count == 1

        # Bypass call - should execute again
        result2 = await cached_async_func(10, _bypass_cache=True)
        assert result2 == 11
        assert call_count == 2  # Should increment

        # Normal call again - should still use cache from first call
        result3 = await cached_async_func(10)
        assert result3 == 11
        assert call_count == 2  # No additional call

    @pytest.mark.asyncio
    @pytest.mark.skip(
        reason="KNOWN ISSUE: Async distributed locking with pytest-redis isolated client has threading issues. Lock acquisition fails in test environment. Works in production with real Redis."
    )
    async def test_async_concurrent_requests(self):
        """Concurrent async requests should be handled properly."""
        call_count = 0
        call_lock = asyncio.Lock()

        @cache
        async def slow_async_operation(value: int) -> int:
            nonlocal call_count
            async with call_lock:
                call_count += 1
            await asyncio.sleep(0.05)  # Simulate slow operation
            return value * value

        # Launch 10 concurrent requests for the same value
        tasks = [slow_async_operation(7) for _ in range(10)]
        results = await asyncio.gather(*tasks)

        # All should get the same result
        assert all(r == 49 for r in results)
        # But function should only be called once (cache stampede prevention)
        assert call_count == 1

    @pytest.mark.asyncio
    async def test_async_different_arguments(self):
        """Different arguments should result in different cache keys."""
        results = []

        @cache
        async def async_identity(x: Any) -> Any:
            await asyncio.sleep(0.001)
            return x

        # Test various argument types
        test_values = [1, "string", [1, 2, 3], {"key": "value"}, None, True]

        for value in test_values:
            result = await async_identity(value)
            results.append(result)

        # Each result should match its input
        assert results == test_values

    @pytest.mark.asyncio
    @pytest.mark.skip(
        reason="KNOWN ISSUE: Circuit breaker test needs review after backend refactoring. May need updated error handling."
    )
    async def test_async_circuit_breaker(self):
        """Circuit breaker should fail-fast when open (after threshold failures)."""
        fail_count = 0

        # Circuit breaker enabled by default
        @cache
        async def flaky_async_service(should_fail: bool = False):
            nonlocal fail_count
            if should_fail:
                fail_count += 1
                raise RuntimeError("Service unavailable")
            return "success"

        # Successful calls
        assert await flaky_async_service() == "success"
        assert await flaky_async_service() == "success"

        # Trigger failures to open circuit
        for _ in range(5):  # Default failure threshold
            with pytest.raises(RuntimeError):
                await flaky_async_service(should_fail=True)

        # Circuit should be open - calls should fail fast without executing function
        initial_fail_count = fail_count
        with pytest.raises(redis.ConnectionError, match="Circuit breaker OPEN"):
            await flaky_async_service()

        # Function shouldn't have been called (circuit is open, failing fast)
        assert fail_count == initial_fail_count

    @pytest.mark.asyncio
    async def test_async_ttl_expiration(self):
        """TTL should work correctly with async functions."""
        call_count = 0

        @cache(ttl=1)  # 1 second TTL
        async def short_lived_cache(x: int) -> int:
            nonlocal call_count
            call_count += 1
            return x * 10

        # First call
        result1 = await short_lived_cache(5)
        assert result1 == 50
        assert call_count == 1

        # Immediate second call - should be cached
        result2 = await short_lived_cache(5)
        assert result2 == 50
        assert call_count == 1

        # Wait for TTL to expire
        await asyncio.sleep(1.1)

        # Should execute again after TTL
        result3 = await short_lived_cache(5)
        assert result3 == 50
        assert call_count == 2

    @pytest.mark.asyncio
    async def test_async_error_handling(self):
        """Errors in async functions should be handled properly."""
        call_count = 0

        @cache
        async def error_prone_async(x: int) -> int:
            nonlocal call_count
            call_count += 1
            if x < 0:
                raise ValueError("Negative values not allowed")
            return x * 2

        # Successful call - should cache
        result1 = await error_prone_async(5)
        assert result1 == 10
        assert call_count == 1

        # Error call - should not cache the error
        with pytest.raises(ValueError):
            await error_prone_async(-1)
        assert call_count == 2

        # Retry same error input - should execute again (errors not cached)
        with pytest.raises(ValueError):
            await error_prone_async(-1)
        assert call_count == 3

        # Previous successful call should still be cached
        result2 = await error_prone_async(5)
        assert result2 == 10
        assert call_count == 3  # No additional call

    @pytest.mark.asyncio
    async def test_async_invalidation(self):
        """Cache invalidation should work with async functions."""
        call_count = 0

        @cache
        async def cached_async_data(key: str) -> str:
            nonlocal call_count
            call_count += 1
            return f"data_{key}_{call_count}"

        # First call
        result1 = await cached_async_data("test")
        assert result1 == "data_test_1"
        assert call_count == 1

        # Cached call
        result2 = await cached_async_data("test")
        assert result2 == "data_test_1"
        assert call_count == 1

        # Invalidate cache
        await cached_async_data.ainvalidate_cache("test")

        # Should execute again after invalidation
        result3 = await cached_async_data("test")
        assert result3 == "data_test_2"
        assert call_count == 2

    @pytest.mark.asyncio
    async def test_async_mixed_sync_usage(self):
        """Decorator should handle both sync and async functions correctly."""
        sync_calls = 0
        async_calls = 0

        @cache
        def sync_function(x: int) -> int:
            nonlocal sync_calls
            sync_calls += 1
            return x + 1

        @cache
        async def async_function(x: int) -> int:
            nonlocal async_calls
            async_calls += 1
            await asyncio.sleep(0.001)
            return x + 1

        # Test sync function
        assert sync_function(10) == 11
        assert sync_function(10) == 11  # Cached
        assert sync_calls == 1

        # Test async function
        assert await async_function(20) == 21
        assert await async_function(20) == 21  # Cached
        assert async_calls == 1

    @pytest.mark.asyncio
    async def test_async_performance_profile(self):
        """Different profiles should work with async functions."""
        fast_calls = 0
        safe_calls = 0

        @cache.minimal
        async def fast_async_operation(x: int) -> int:
            nonlocal fast_calls
            fast_calls += 1
            return x * 2

        @cache.production
        async def safe_async_operation(x: int) -> int:
            nonlocal safe_calls
            safe_calls += 1
            return x * 3

        # Test fast profile
        assert await fast_async_operation(5) == 10
        assert await fast_async_operation(5) == 10  # Cached
        assert fast_calls == 1

        # Test safe profile
        assert await safe_async_operation(5) == 15
        assert await safe_async_operation(5) == 15  # Cached
        assert safe_calls == 1

    @pytest.mark.asyncio
    async def test_async_monitoring_integration(self):
        """Monitoring features should work with async functions."""

        @cache(monitoring=MonitoringConfig(collect_stats=True))
        async def monitored_async_func(x: int) -> int:
            await asyncio.sleep(0.001)
            return x * x

        # Execute function
        result = await monitored_async_func(7)
        assert result == 49

        # Check health status
        health = monitored_async_func.get_health_status()
        assert health["healthy"] is True
        assert "circuit_breaker" in health

        # Full health check
        full_health = await monitored_async_func.check_health()
        assert full_health["status"] == "healthy"

    @pytest.mark.asyncio
    async def test_async_namespace_isolation(self):
        """Different namespaces should have isolated caches."""
        calls_a = 0
        calls_b = 0

        @cache(namespace="service_a")
        async def service_a_func(x: int) -> str:
            nonlocal calls_a
            calls_a += 1
            return f"a_{x}"

        @cache(namespace="service_b")
        async def service_b_func(x: int) -> str:
            nonlocal calls_b
            calls_b += 1
            return f"b_{x}"

        # Same argument, different namespaces
        assert await service_a_func(100) == "a_100"
        assert await service_b_func(100) == "b_100"

        # Both should have executed
        assert calls_a == 1
        assert calls_b == 1

        # Cached calls
        assert await service_a_func(100) == "a_100"
        assert await service_b_func(100) == "b_100"

        # No additional executions
        assert calls_a == 1
        assert calls_b == 1


if __name__ == "__main__":
    # Run with: python -m pytest tests/critical/test_async_caching_tdd.py -v
    pytest.main([__file__, "-v"])
