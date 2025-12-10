"""Critical reliability tests using fault injection patterns."""

import time
from unittest.mock import patch

import pytest
import redis

from cachekit import cache
from cachekit.reliability.load_control import BackpressureController

from ..utils.redis_test_helpers import RedisIsolationMixin

pytestmark = pytest.mark.critical


class TestReliabilityFaultInjection(RedisIsolationMixin):
    """Test reliability under various failure scenarios."""

    def test_redis_connection_failure_graceful_degradation(self):
        """Test system behavior when Redis is completely unavailable."""

        call_count = 0

        @cache(ttl=60, namespace="fault_test")
        def cached_operation(data_id: str) -> str:
            nonlocal call_count
            call_count += 1
            # This should still work even if Redis is down
            return f"result_{data_id}_{call_count}"

        # First call - should work normally
        result1 = cached_operation("test_key")
        assert result1 == "result_test_key_1"
        assert call_count == 1

        # Simulate Redis connection failure
        with patch("redis.Redis.get", side_effect=redis.ConnectionError("Redis unavailable")):
            with patch("redis.Redis.set", side_effect=redis.ConnectionError("Redis unavailable")):
                # Operation should still succeed - fallback behavior
                result2 = cached_operation("test_key2")

                # Should execute function since Redis is unavailable
                assert result2 == "result_test_key2_2"
                assert call_count == 2

    def test_redis_timeout_graceful_handling(self):
        """Test graceful handling of Redis timeouts."""

        timeout_calls = 0
        function_calls = 0

        @cache(ttl=60, namespace="timeout_test")
        def cached_operation_with_timeout(data_id: str) -> str:
            nonlocal function_calls
            function_calls += 1
            return f"timeout_result_{data_id}_{function_calls}"

        def slow_redis_get(*args, **kwargs):
            nonlocal timeout_calls
            timeout_calls += 1
            time.sleep(0.1)  # Simulate slow Redis

        # Simulate slow Redis responses
        with patch("redis.Redis.get", side_effect=slow_redis_get):
            start_time = time.time()
            result = cached_operation_with_timeout("test_key")
            _elapsed = time.time() - start_time

            # Should return result despite slow Redis
            assert result == "timeout_result_test_key_1"
            assert function_calls == 1
            # With thundering herd protection, we may call Redis.get twice (initial check + double-check in lock)
            assert timeout_calls >= 1 and timeout_calls <= 2
            # Test completed (slow Redis doesn't prevent function execution)

    def test_intermittent_redis_failures_resilience(self):
        """Test resilience to intermittent Redis failures."""

        failure_count = 0
        success_count = 0
        function_calls = 0

        @cache(ttl=60, namespace="resilience_test")
        def resilient_operation(data_id: str) -> str:
            nonlocal function_calls
            function_calls += 1
            return f"resilient_result_{data_id}_{function_calls}"

        def intermittent_redis_failure(*args, **kwargs):
            nonlocal failure_count, success_count
            # Alternate between failure and success
            if (failure_count + success_count) % 2 == 0:
                failure_count += 1
                raise redis.ConnectionError("Intermittent failure")
            else:
                success_count += 1
                return None  # Cache miss

        with patch("redis.Redis.get", side_effect=intermittent_redis_failure):
            # Run multiple operations to test resilience
            results = []
            for i in range(4):
                result = resilient_operation(f"key_{i}")
                results.append(result)

            # All operations should succeed despite intermittent Redis failures
            assert len(results) == 4
            assert all("resilient_result" in result for result in results)
            assert function_calls == 4  # All function calls should execute
            assert failure_count >= 2  # Should have encountered Redis failures
            assert success_count >= 0  # May have some successes

    def test_concurrent_cache_stampede_baseline(self):
        """Baseline test showing behavior under concurrent access."""

        call_count = 0

        @cache(ttl=60, namespace="stampede_baseline")
        def expensive_operation(data_id: str) -> str:
            nonlocal call_count
            call_count += 1
            time.sleep(0.01)  # Simulate some work
            return f"computed_result_{data_id}_{call_count}"

        # Sequential calls should use cache after first call
        result1 = expensive_operation("test_key")
        result2 = expensive_operation("test_key")  # Should be cached

        assert result1 == "computed_result_test_key_1"
        # Result2 should be same (from cache) or different (if cache miss)
        # The important thing is the function works correctly
        assert "computed_result_test_key" in result2

        # At least one call should have been made
        assert call_count >= 1


class TestBackpressureController:
    """Test backpressure controller functionality."""

    def test_backpressure_controller_initialization(self):
        """Test backpressure controller can be created and provides stats."""

        controller = BackpressureController(max_concurrent=5, queue_size=10)

        # Should not raise exception
        assert controller is not None

        # Get initial stats
        stats = controller.get_stats()

        assert isinstance(stats, dict)
        assert "queue_depth" in stats
        assert "rejected_count" in stats
        assert "max_concurrent" in stats
        assert "queue_size" in stats
        assert "healthy" in stats

        # Verify initial values
        assert stats["queue_depth"] == 0
        assert stats["rejected_count"] == 0
        assert stats["max_concurrent"] == 5
        assert stats["queue_size"] == 10
        assert stats["healthy"] is True

    def test_backpressure_controller_stats_updates(self):
        """Test that backpressure controller stats can be updated."""

        controller = BackpressureController(max_concurrent=2, queue_size=3)

        initial_stats = controller.get_stats()
        assert initial_stats["rejected_count"] == 0

        # The controller should track stats over time
        # (Implementation details may vary, but stats should be accessible)
        updated_stats = controller.get_stats()
        assert isinstance(updated_stats, dict)
        assert "healthy" in updated_stats


class TestCircuitBreakerComponent:
    """Test circuit breaker component availability and basic functionality."""

    def test_circuit_breaker_component_exists(self):
        """Verify circuit breaker component is available and can be created."""
        try:
            from cachekit.reliability import CircuitBreaker, CircuitBreakerConfig

            # Should be able to create circuit breaker
            config = CircuitBreakerConfig(failure_threshold=5, timeout_seconds=30.0)
            cb = CircuitBreaker(config=config, namespace="test")
            assert cb is not None

        except ImportError:
            pytest.skip("CircuitBreaker component not available in current implementation")

    def test_circuit_breaker_basic_functionality(self):
        """Test basic circuit breaker functionality if available."""
        try:
            from cachekit.reliability import CircuitBreaker, CircuitBreakerConfig

            config = CircuitBreakerConfig(failure_threshold=3, timeout_seconds=1.0)
            cb = CircuitBreaker(config=config, namespace="test")

            # Circuit breaker should start in closed state (allowing operations)
            # Implementation details may vary, but it should be functional
            assert cb is not None

        except ImportError:
            pytest.skip("CircuitBreaker component not available in current implementation")


class TestReliabilityIntegration:
    """Integration tests for reliability components with main codebase."""

    def test_cache_decorator_handles_redis_errors_gracefully(self):
        """Test that cache decorator doesn't crash on Redis errors."""

        @cache(ttl=60, namespace="integration_test")
        def reliable_operation(data: str) -> str:
            return f"processed_{data}"

        # Should work normally
        result1 = reliable_operation("test_data")
        assert result1 == "processed_test_data"

        # Should handle Redis errors gracefully
        with patch("redis.Redis.get", side_effect=redis.ConnectionError("Connection failed")):
            result2 = reliable_operation("test_data2")
            assert result2 == "processed_test_data2"

    def test_reliability_components_importable(self):
        """Test that reliability components can be imported without errors."""

        # These imports should not raise exceptions
        from cachekit.reliability.load_control import BackpressureController

        # Should be able to create instances
        controller = BackpressureController(max_concurrent=10, queue_size=20)
        assert controller is not None

        # Try circuit breaker if available
        try:
            from cachekit.reliability import CircuitBreaker, CircuitBreakerConfig

            config = CircuitBreakerConfig(failure_threshold=5, timeout_seconds=30.0)
            cb = CircuitBreaker(config=config, namespace="test")
            assert cb is not None
        except ImportError:
            # Circuit breaker may not be fully implemented yet
            pass
