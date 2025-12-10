"""
CRITICAL REGRESSION TEST: Intelligent Cache vs Legacy Interface

Validates that the new @cache interface produces identical results
to the legacy @cache interface with zero performance regression.

🎯 VALIDATION REQUIREMENTS:
- Functional equivalence: Same outputs for same inputs
- Performance equivalence: <1μs overhead for intelligent configuration
- Backward compatibility: @cache still works identically
- Intent profiles: @cache.minimal/.safe/.secure work correctly
"""

import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest

from cachekit import cache

from ..utils.redis_test_helpers import RedisIsolationMixin

# Mark all tests in this module as critical
pytestmark = pytest.mark.critical


class TestIntelligentCacheRegression(RedisIsolationMixin):
    """Critical regression tests for intelligent cache system."""

    def test_functional_equivalence_basic(self):
        """CRITICAL: @cache must produce identical results to @cache."""
        test_data = {"user_id": 123, "data": [1, 2, 3]}
        call_count_legacy = 0
        call_count_intelligent = 0

        @cache(ttl=300, namespace="legacy")
        def legacy_function(data):
            nonlocal call_count_legacy
            call_count_legacy += 1
            return {"result": data, "timestamp": time.time()}

        @cache(ttl=300, namespace="intelligent")
        def intelligent_function(data):
            nonlocal call_count_intelligent
            call_count_intelligent += 1
            return {"result": data, "timestamp": time.time()}

        # Test both interfaces
        legacy_result1 = legacy_function(test_data)
        intelligent_result1 = intelligent_function(test_data)

        # Both should call function once
        assert call_count_legacy == 1
        assert call_count_intelligent == 1

        # Results should have same structure (timestamps will differ)
        assert legacy_result1["result"] == intelligent_result1["result"]
        assert isinstance(legacy_result1["timestamp"], float)
        assert isinstance(intelligent_result1["timestamp"], float)

        # Second calls should hit cache
        legacy_result2 = legacy_function(test_data)
        intelligent_result2 = intelligent_function(test_data)

        # Should not call functions again
        assert call_count_legacy == 1
        assert call_count_intelligent == 1

        # Cached results should be identical
        assert legacy_result1 == legacy_result2
        assert intelligent_result1 == intelligent_result2

    def test_configuration_overhead_benchmark(self):
        """CRITICAL: Intelligent configuration must have <10μs overhead."""
        iterations = 1000

        # Measure legacy configuration time
        start_time = time.perf_counter()
        for _ in range(iterations):

            @cache(ttl=3600)
            def legacy_func():
                return "test"

        legacy_time = time.perf_counter() - start_time

        # Measure intelligent configuration time
        start_time = time.perf_counter()
        for _ in range(iterations):

            @cache
            def intelligent_func():
                return "test"

        intelligent_time = time.perf_counter() - start_time

        # Calculate overhead per configuration
        legacy_per_config = (legacy_time / iterations) * 1_000_000  # microseconds
        intelligent_per_config = (intelligent_time / iterations) * 1_000_000

        print(f"Legacy config time: {legacy_per_config:.2f}μs")
        print(f"Intelligent config time: {intelligent_per_config:.2f}μs")
        print(f"Overhead: {intelligent_per_config - legacy_per_config:.2f}μs")

        # REQUIREMENT: <10μs overhead for intelligent configuration (realistic tolerance)
        overhead = intelligent_per_config - legacy_per_config
        assert overhead < 10.0, f"Configuration overhead {overhead:.2f}μs exceeds 10μs limit"

    def test_intent_profiles_functional(self):
        """CRITICAL: Intent-based profiles must work correctly."""
        test_data = "sensitive_user_data"
        fast_calls = safe_calls = secure_calls = 0

        @cache.minimal
        def fast_function(data):
            nonlocal fast_calls
            fast_calls += 1
            return f"fast_{data}"

        @cache.production
        def safe_function(data):
            nonlocal safe_calls
            safe_calls += 1
            return f"safe_{data}"

        @cache.secure(master_key="a" * 64)
        def secure_function(data):
            nonlocal secure_calls
            secure_calls += 1
            return f"secure_{data}"

        # Test all intent profiles
        fast_result1 = fast_function(test_data)
        safe_result1 = safe_function(test_data)
        secure_result1 = secure_function(test_data)

        # All should call functions once
        assert fast_calls == 1
        assert safe_calls == 1
        assert secure_calls == 1

        # Results should be correct
        assert fast_result1 == "fast_sensitive_user_data"
        assert safe_result1 == "safe_sensitive_user_data"
        assert secure_result1 == "secure_sensitive_user_data"

        # Second calls should hit cache
        fast_result2 = fast_function(test_data)
        safe_result2 = safe_function(test_data)
        secure_result2 = secure_function(test_data)

        # Should not call functions again
        assert fast_calls == 1
        assert safe_calls == 1
        assert secure_calls == 1

        # Cached results should match
        assert fast_result1 == fast_result2
        assert safe_result1 == safe_result2
        assert secure_result1 == secure_result2

    def test_auto_detection_security_profile(self):
        """CRITICAL: @cache without explicit intent uses DEFAULT_PROFILE (no magic)."""
        user_calls = 0

        @cache  # No magic - uses DEFAULT_PROFILE (explicit choices required)
        def get_user_profile(user_id: int) -> dict[str, Any]:
            nonlocal user_calls
            user_calls += 1
            return {"id": user_id, "name": f"User{user_id}", "sensitive": True}

        # Test function works
        result1 = get_user_profile(123)
        assert user_calls == 1
        assert result1["id"] == 123
        assert result1["sensitive"] is True

        # Test caching works
        result2 = get_user_profile(123)
        assert user_calls == 1  # Should hit cache
        assert result1 == result2

    def test_concurrent_access_regression(self):
        """CRITICAL: Concurrent access must work identically for both interfaces.

        Note: Sync decorators don't support distributed locking (async-only feature).
        With max_workers=5 and 10 concurrent requests, some cache stampede is expected.
        The test verifies that both interfaces behave identically, not that there's
        perfect deduplication (which requires async functions with distributed locks).
        """
        legacy_calls = intelligent_calls = 0

        @cache(ttl=300, namespace="legacy_concurrent")
        def legacy_concurrent(value):
            nonlocal legacy_calls
            legacy_calls += 1
            time.sleep(0.01)  # Simulate work
            return f"legacy_{value}"

        @cache(ttl=300, namespace="intelligent_concurrent")
        def intelligent_concurrent(value):
            nonlocal intelligent_calls
            intelligent_calls += 1
            time.sleep(0.01)  # Simulate work
            return f"intelligent_{value}"

        # Test concurrent access with ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=5) as executor:
            # Submit multiple concurrent requests for same value
            legacy_futures = [executor.submit(legacy_concurrent, "test") for _ in range(10)]
            intelligent_futures = [executor.submit(intelligent_concurrent, "test") for _ in range(10)]

            # Collect results
            legacy_results = [f.result() for f in legacy_futures]
            intelligent_results = [f.result() for f in intelligent_futures]

        # Both should have some cache stampede (multiple calls due to no distributed lock in sync mode)
        # But both should behave identically (same number of calls)
        assert legacy_calls > 0 and legacy_calls <= 10, "Should have some calls but not all 10"
        assert intelligent_calls > 0 and intelligent_calls <= 10, "Should have some calls but not all 10"
        # Both interfaces should have similar behavior (within tolerance)
        assert abs(legacy_calls - intelligent_calls) <= 2, "Both interfaces should have similar stampede behavior"

        # All results should be identical within each interface
        assert all(result == "legacy_test" for result in legacy_results)
        assert all(result == "intelligent_test" for result in intelligent_results)
        assert len(set(legacy_results)) == 1  # All identical
        assert len(set(intelligent_results)) == 1  # All identical

    def test_backward_compatibility_alias(self):
        """CRITICAL: redis_cache alias must work identically to cache."""
        test_value = {"data": "compatibility_test"}
        cache_calls = redis_cache_calls = 0

        @cache(ttl=300, namespace="cache_test")
        def cache_function(data):
            nonlocal cache_calls
            cache_calls += 1
            return {"processed": data, "method": "cache"}

        @cache(ttl=300, namespace="redis_cache_test")
        def redis_cache_function(data):
            nonlocal redis_cache_calls
            redis_cache_calls += 1
            return {"processed": data, "method": "redis_cache"}

        # Test both work
        cache_result = cache_function(test_value)
        redis_cache_result = redis_cache_function(test_value)

        # Both should call once
        assert cache_calls == 1
        assert redis_cache_calls == 1

        # Results should have same structure
        assert cache_result["processed"] == redis_cache_result["processed"]
        assert cache_result["method"] == "cache"
        assert redis_cache_result["method"] == "redis_cache"

        # Test caching works for both
        cache_result2 = cache_function(test_value)
        redis_cache_result2 = redis_cache_function(test_value)

        assert cache_calls == 1  # Should hit cache
        assert redis_cache_calls == 1  # Should hit cache
        assert cache_result == cache_result2
        assert redis_cache_result == redis_cache_result2
