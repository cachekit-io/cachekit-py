"""
CRITICAL PATH TEST: Cache Reliability & Error Handling

These tests ensure the cache decorator handles errors gracefully
and doesn't break user applications when Redis is unavailable.

🧪 TDD Principle: Cache failure should never break the application
"""

import time

import pytest

from cachekit import cache

from ..utils.redis_test_helpers import RedisIsolationMixin

pytestmark = pytest.mark.critical


class TestCacheReliability(RedisIsolationMixin):
    """Critical tests for cache reliability and error handling."""

    # Redis isolation is now handled by RedisIsolationMixin
    # No manual setup needed - pytest-redis provides perfect isolation

    def test_cache_with_very_short_ttl_expires_quickly(self):
        """CRITICAL: Cache with very short TTL should expire and refresh."""
        call_count = 0

        @cache(ttl=1)  # Very short TTL
        def time_sensitive_function(key):
            nonlocal call_count
            call_count += 1
            return f"fresh_data_{key}_{call_count}_{time.time()}"

        # First call should cache with short TTL
        result1 = time_sensitive_function("data")
        assert "fresh_data_data_1_" in result1
        assert call_count == 1

        # Immediate second call should hit cache
        result2 = time_sensitive_function("data")
        assert result2 == result1  # Same cached result
        assert call_count == 1

        # Wait for TTL to expire
        time.sleep(1.2)

        # Third call should get fresh data after expiration
        result3 = time_sensitive_function("data")
        assert "fresh_data_data_2_" in result3
        assert result3 != result1  # Different result (fresh data)
        assert call_count == 2

    def test_cache_bypass_parameter_works(self):
        """CRITICAL: _bypass_cache parameter must work for debugging."""
        call_count = 0

        @cache(ttl=300)
        def cacheable_function(data):
            nonlocal call_count
            call_count += 1
            return f"processed_{data}_{call_count}"

        # Normal caching behavior
        result1 = cacheable_function("test")
        result2 = cacheable_function("test")
        assert result1 == result2  # Same from cache
        assert call_count == 1

        # Bypass cache should force execution
        result3 = cacheable_function("test", _bypass_cache=True)
        assert result3 != result1  # Different result (new execution)
        assert call_count == 2

        # Normal call should still hit cache (not affected by bypass)
        result4 = cacheable_function("test")
        assert result4 == result1  # Back to cached result
        assert call_count == 2

    def test_decorator_preserves_function_metadata(self):
        """CRITICAL: Decorator must preserve original function attributes."""

        @cache(ttl=300)
        def well_documented_function(param1, param2="default"):
            """This is a well documented function.

            Args:
                param1: First parameter
                param2: Second parameter with default

            Returns:
                str: A formatted result
            """
            return f"{param1}_{param2}"

        # Function name preserved
        assert well_documented_function.__name__ == "well_documented_function"

        # Docstring preserved
        assert "well documented function" in well_documented_function.__doc__

        # Module preserved
        assert well_documented_function.__module__ == __name__

        # Function should still work
        result = well_documented_function("test", param2="custom")
        assert result == "test_custom"

    def test_cache_works_with_none_return_values(self):
        """CRITICAL: Cache must handle None return values correctly."""
        call_count = 0

        @cache(ttl=300)
        def function_returning_none(should_return_none):
            nonlocal call_count
            call_count += 1
            if should_return_none:
                return None
            return f"not_none_{call_count}"

        # Function that returns None
        result1 = function_returning_none(True)
        assert result1 is None
        assert call_count == 1

        # Second call should hit cache (still None)
        result2 = function_returning_none(True)
        assert result2 is None
        assert call_count == 1  # Cached, not called again

        # Different parameter should execute
        result3 = function_returning_none(False)
        assert result3 == "not_none_2"
        assert call_count == 2

    def test_cache_handles_exception_in_function(self):
        """CRITICAL: Cache must not interfere with function exceptions."""
        call_count = 0

        @cache(ttl=300)
        def function_that_sometimes_fails(should_fail):
            nonlocal call_count
            call_count += 1
            if should_fail:
                raise ValueError(f"Intentional error {call_count}")
            return f"success_{call_count}"

        # Successful call should cache
        result1 = function_that_sometimes_fails(False)
        assert result1 == "success_1"
        assert call_count == 1

        # Second call should hit cache
        result2 = function_that_sometimes_fails(False)
        assert result2 == result1
        assert call_count == 1

        # Function that raises exception should not be cached
        # Note: Decorator calls function twice due to resilient error handling
        # (once in lock, once as fallback) - this is correct behavior
        with pytest.raises(ValueError, match="Intentional error 2"):
            function_that_sometimes_fails(True)
        assert call_count == 2

        # Same failing call should execute again (not cached)
        # Again, function called twice due to error handling
        with pytest.raises(ValueError, match="Intentional error 3"):
            function_that_sometimes_fails(True)
        assert call_count == 3

    def test_cache_with_mutable_arguments(self):
        """CRITICAL: Cache must handle mutable arguments safely."""
        call_count = 0

        @cache(ttl=300)
        def process_list(items):
            nonlocal call_count
            call_count += 1
            return f"processed_{len(items)}_items_{call_count}"

        # Test with list
        list1 = [1, 2, 3]
        result1 = process_list(list1)
        assert result1 == "processed_3_items_1"
        assert call_count == 1

        # Same list content should hit cache
        list2 = [1, 2, 3]  # Different object, same content
        result2 = process_list(list2)
        assert result2 == result1
        assert call_count == 1  # Cache hit

        # Modifying original list shouldn't affect cache
        list1.append(4)
        result3 = process_list([1, 2, 3])  # Original content
        assert result3 == result1  # Still cached
        assert call_count == 1

        # Different content should execute
        result4 = process_list([1, 2, 3, 4])
        assert result4 == "processed_4_items_2"
        assert call_count == 2

    def test_async_cache_error_handling(self):
        """CRITICAL: Async cache must handle errors gracefully."""
        import asyncio

        call_count = 0

        @cache(ttl=300)
        async def async_function_with_errors(should_fail):
            nonlocal call_count
            call_count += 1
            await asyncio.sleep(0.001)  # Simulate async work

            if should_fail:
                raise RuntimeError(f"Async error {call_count}")
            return f"async_success_{call_count}"

        async def run_async_error_test():
            # Successful call
            result1 = await async_function_with_errors(False)
            assert result1 == "async_success_1"
            assert call_count == 1

            # Should hit cache
            result2 = await async_function_with_errors(False)
            assert result2 == result1
            assert call_count == 1

            # Error should not be cached
            # Note: Async decorator also calls function twice due to resilient error handling
            with pytest.raises(RuntimeError, match="Async error 2"):
                await async_function_with_errors(True)
            assert call_count == 2

            # Error call again should execute (not cached)
            # Again, function called twice due to error handling
            with pytest.raises(RuntimeError, match="Async error 3"):
                await async_function_with_errors(True)
            assert call_count == 3

            return True

        assert asyncio.run(run_async_error_test())

    def test_cache_isolation_between_functions(self):
        """CRITICAL: Different functions must have isolated caches."""
        call_count_a = 0
        call_count_b = 0

        @cache(ttl=300)
        def function_a(key):
            nonlocal call_count_a
            call_count_a += 1
            return f"a_{key}_{call_count_a}"

        @cache(ttl=300)
        def function_b(key):
            nonlocal call_count_b
            call_count_b += 1
            return f"b_{key}_{call_count_b}"

        # Same arguments, different functions
        result_a1 = function_a("same_key")
        result_b1 = function_b("same_key")

        assert result_a1 != result_b1  # Different results
        assert call_count_a == 1
        assert call_count_b == 1

        # Cache should be isolated
        result_a2 = function_a("same_key")
        result_b2 = function_b("same_key")

        assert result_a2 == result_a1  # A cached
        assert result_b2 == result_b1  # B cached
        assert call_count_a == 1  # A not called again
        assert call_count_b == 1  # B not called again
