"""
CRITICAL PATH TEST: Basic Cache Functionality

This test MUST pass for the library to be usable.
Tests core @cache decorator with real Redis.

🧪 TDD Approach: Write failing test first, then implement to make it pass.
"""

import time

import pytest

from cachekit import cache

from ..utils.redis_test_helpers import RedisIsolationMixin

# Mark all tests in this module as critical
pytestmark = pytest.mark.critical


class TestBasicCacheWorks(RedisIsolationMixin):
    """Critical tests that must pass for basic cache functionality."""

    def test_cache_decorator_basic_usage(self):
        """CRITICAL: @cache must work for basic usage."""
        call_count = 0

        @cache(ttl=300)
        def get_user_data(user_id):
            nonlocal call_count
            call_count += 1
            return {"id": user_id, "name": f"User {user_id}", "call_number": call_count}

        # First call should execute the function
        result1 = get_user_data(123)
        assert result1["id"] == 123
        assert result1["name"] == "User 123"
        assert result1["call_number"] == 1
        assert call_count == 1

        # Second call should hit cache - exact same result
        result2 = get_user_data(123)
        assert result2 == result1  # Exact same data from cache
        assert result2["call_number"] == 1  # Should still be 1 (cached)
        assert call_count == 1  # Function not called again

    def test_cache_with_different_arguments(self):
        """CRITICAL: Different arguments should create separate cache entries."""
        call_count = 0

        @cache(ttl=300)
        def calculate_value(multiplier, base=10):
            nonlocal call_count
            call_count += 1
            return multiplier * base + call_count

        # Different arguments should each execute
        result1 = calculate_value(2, base=5)  # 2 * 5 + 1 = 11
        result2 = calculate_value(3, base=5)  # 3 * 5 + 2 = 17
        result3 = calculate_value(2, base=5)  # Should hit cache, return 11

        assert result1 == 11
        assert result2 == 17
        assert result3 == 11  # Same as first call (cached)
        assert call_count == 2  # Only two unique calls

    def test_cache_invalidation_works(self):
        """CRITICAL: Cache invalidation must work."""
        call_count = 0

        @cache(ttl=3600)  # Long TTL
        def get_config(config_key):
            nonlocal call_count
            call_count += 1
            return f"config_value_{config_key}_{call_count}"

        # Cache initial result
        result1 = get_config("database_url")
        assert result1 == "config_value_database_url_1"
        assert call_count == 1

        # Verify cache hit
        result2 = get_config("database_url")
        assert result2 == result1
        assert call_count == 1

        # Invalidate cache
        get_config.invalidate_cache("database_url")

        # Should execute function again after invalidation
        result3 = get_config("database_url")
        assert result3 == "config_value_database_url_2"
        assert call_count == 2

    def test_async_cache_basic_functionality(self):
        """CRITICAL: Async functions must be cacheable."""
        import asyncio

        call_count = 0

        @cache(ttl=300)
        async def async_get_data(data_id):
            nonlocal call_count
            call_count += 1
            await asyncio.sleep(0.001)  # Simulate async work
            return {"data_id": data_id, "processed": True, "call_number": call_count}

        async def run_async_test():
            # First call
            result1 = await async_get_data("async_123")
            assert result1["data_id"] == "async_123"
            assert result1["call_number"] == 1
            assert call_count == 1

            # Second call should hit cache
            result2 = await async_get_data("async_123")
            assert result2 == result1  # Exact same cached data
            assert call_count == 1  # Function not called again

            return True

        # Run the async test
        assert asyncio.run(run_async_test())

    def test_complex_data_types_work(self):
        """CRITICAL: Complex data types must be cacheable."""
        from datetime import datetime

        @cache(ttl=300)
        def get_complex_data():
            return {
                "strings": ["hello", "world"],
                "numbers": [1, 2, 3.14, 42],
                "nested": {
                    "inner": {"deep": "value"},
                    "list": [{"item": i} for i in range(3)],
                },
                "mixed": [1, "string", {"nested": True}],
                "timestamp": datetime.now().isoformat(),
            }

        # First call
        result1 = get_complex_data()
        assert isinstance(result1, dict)
        assert result1["strings"] == ["hello", "world"]
        assert result1["numbers"] == [1, 2, 3.14, 42]
        assert result1["nested"]["inner"]["deep"] == "value"
        assert len(result1["nested"]["list"]) == 3

        # Second call should return identical cached data
        result2 = get_complex_data()
        assert result2 == result1
        # Timestamp should be identical (proving it's cached)
        assert result2["timestamp"] == result1["timestamp"]

    def test_ttl_expiration_basic(self):
        """CRITICAL: TTL expiration must work."""
        call_count = 0

        @cache(ttl=1)  # 1 second TTL
        def time_sensitive_data():
            nonlocal call_count
            call_count += 1
            return f"time_data_{call_count}_{time.time()}"

        # First call
        result1 = time_sensitive_data()
        assert call_count == 1

        # Immediate second call should hit cache
        result2 = time_sensitive_data()
        assert result2 == result1
        assert call_count == 1

        # Wait for TTL to expire
        time.sleep(1.5)

        # Third call should execute function again (cache expired)
        result3 = time_sensitive_data()
        assert result3 != result1  # Different result after expiration
        assert call_count == 2

    def test_namespace_isolation(self):
        """CRITICAL: Namespaces must isolate cache entries."""
        call_count_a = 0
        call_count_b = 0

        @cache(namespace="service_a", ttl=300)
        def service_a_function(key):
            nonlocal call_count_a
            call_count_a += 1
            return f"service_a_result_{key}_{call_count_a}"

        @cache(namespace="service_b", ttl=300)
        def service_b_function(key):
            nonlocal call_count_b
            call_count_b += 1
            return f"service_b_result_{key}_{call_count_b}"

        # Same key, different namespaces should not interfere
        result_a = service_a_function("shared_key")
        result_b = service_b_function("shared_key")

        assert result_a != result_b  # Different results (no collision)
        assert call_count_a == 1
        assert call_count_b == 1

        # Verify independent caching
        result_a2 = service_a_function("shared_key")
        result_b2 = service_b_function("shared_key")

        assert result_a2 == result_a  # Service A cached
        assert result_b2 == result_b  # Service B cached
        assert call_count_a == 1  # A not called again
        assert call_count_b == 1  # B not called again
