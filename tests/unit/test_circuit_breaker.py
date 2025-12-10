"""Unit tests for circuit breaker components."""

import threading
import time
from unittest.mock import MagicMock, patch

import pytest
import redis

from cachekit.reliability.circuit_breaker import (
    CacheOperationMetrics,
    CircuitBreaker,
    CircuitBreakerConfig,
    CircuitState,
)


class TestCircuitBreakerConfig:
    """Test circuit breaker configuration."""

    def test_default_configuration(self):
        """Test default configuration values."""
        config = CircuitBreakerConfig()

        assert config.failure_threshold == 5
        assert config.success_threshold == 3
        assert config.timeout_seconds == 30.0
        assert config.half_open_requests == 1
        # No error types excluded by default
        assert len(config.excluded_error_types) == 0

    def test_custom_configuration(self):
        """Test custom configuration values."""
        from cachekit.backends.errors import BackendErrorType

        config = CircuitBreakerConfig(
            failure_threshold=10,
            success_threshold=5,
            timeout_seconds=60.0,
            half_open_requests=2,
            excluded_error_types=(BackendErrorType.PERMANENT,),
        )

        assert config.failure_threshold == 10
        assert config.success_threshold == 5
        assert config.timeout_seconds == 60.0
        assert config.half_open_requests == 2
        # Should include custom error type
        assert BackendErrorType.PERMANENT in config.excluded_error_types
        assert len(config.excluded_error_types) == 1

    def test_post_init_adds_lock_error(self):
        """Test that configuration accepts empty excluded error types."""

        config = CircuitBreakerConfig(excluded_error_types=())

        # Empty tuple is valid
        assert len(config.excluded_error_types) == 0


class TestCacheOperationMetrics:
    """Test cache operation metrics calculations."""

    def test_initial_metrics(self):
        """Test initial metric values."""
        metrics = CacheOperationMetrics()

        assert metrics.total_operations == 0
        assert metrics.cache_hits == 0
        assert metrics.cache_misses == 0
        assert metrics.errors == 0
        assert metrics.fallbacks == 0
        assert metrics.circuit_opens == 0
        assert metrics.hit_rate == 0.0
        assert metrics.error_rate == 0.0

    def test_hit_rate_calculation(self):
        """Test hit rate calculation."""
        metrics = CacheOperationMetrics(total_operations=100, cache_hits=80)

        assert metrics.hit_rate == 0.8

    def test_error_rate_calculation(self):
        """Test error rate calculation."""
        metrics = CacheOperationMetrics(total_operations=100, errors=5)

        assert metrics.error_rate == 0.05

    def test_zero_operations_rates(self):
        """Test rates when no operations have been performed."""
        metrics = CacheOperationMetrics()

        assert metrics.hit_rate == 0.0
        assert metrics.error_rate == 0.0


class TestCircuitBreaker:
    """Test enhanced circuit breaker functionality."""

    def test_initial_state(self):
        """Test circuit breaker starts in CLOSED state."""
        config = CircuitBreakerConfig()
        breaker = CircuitBreaker(config, namespace="test")

        assert breaker.state == CircuitState.CLOSED
        assert breaker.failure_count == 0
        assert breaker.success_count == 0

    def test_successful_operation_closed_state(self):
        """Test successful operation in CLOSED state."""
        config = CircuitBreakerConfig()
        breaker = CircuitBreaker(config, namespace="test")

        def mock_operation():
            return "success"

        result = breaker.call(mock_operation)

        assert result == "success"
        assert breaker.state == CircuitState.CLOSED
        assert breaker.failure_count == 0

    def test_failure_counting_closed_state(self):
        """Test failure counting in CLOSED state."""
        config = CircuitBreakerConfig(failure_threshold=3)
        breaker = CircuitBreaker(config, namespace="test")

        def failing_operation():
            raise redis.ConnectionError("Connection failed")

        # Fail less than threshold
        for _ in range(2):
            with pytest.raises(redis.ConnectionError):
                breaker.call(failing_operation)

        assert breaker.state == CircuitState.CLOSED
        assert breaker.failure_count == 2

        # One more failure should open circuit
        with pytest.raises(redis.ConnectionError):
            breaker.call(failing_operation)

        assert breaker.state == CircuitState.OPEN
        assert breaker.failure_count == 3

    def test_excluded_exceptions_dont_count_as_failures(self):
        """Test that excluded error types don't count as failures."""
        from cachekit.backends.errors import BackendError, BackendErrorType

        config = CircuitBreakerConfig(failure_threshold=2, excluded_error_types=(BackendErrorType.PERMANENT,))
        breaker = CircuitBreaker(config, namespace="test")

        def permanent_error_operation():
            raise BackendError("Config error", error_type=BackendErrorType.PERMANENT)

        # Raise PERMANENT error multiple times - should not open circuit
        for _ in range(5):
            with pytest.raises(BackendError):
                breaker.call(permanent_error_operation)

        assert breaker.state == CircuitState.CLOSED
        assert breaker.failure_count == 0

    def test_circuit_opens_after_threshold(self):
        """Test circuit opens after failure threshold."""
        from cachekit.backends.errors import BackendError, BackendErrorType

        config = CircuitBreakerConfig(failure_threshold=2)
        breaker = CircuitBreaker(config, namespace="test")

        def failing_operation():
            raise BackendError("Connection failed", error_type=BackendErrorType.TRANSIENT)

        # First failure
        with pytest.raises(BackendError):
            breaker.call(failing_operation)
        assert breaker.state == CircuitState.CLOSED

        # Second failure should open circuit
        with pytest.raises(BackendError):
            breaker.call(failing_operation)
        assert breaker.state == CircuitState.OPEN

    def test_open_state_fails_fast(self):
        """Test that OPEN state fails fast without calling function."""
        from cachekit.backends.errors import BackendError, BackendErrorType

        config = CircuitBreakerConfig(failure_threshold=1)
        breaker = CircuitBreaker(config, namespace="test")

        # Open the circuit
        def failing_operation():
            raise BackendError("Connection failed", error_type=BackendErrorType.TRANSIENT)

        with pytest.raises(BackendError):
            breaker.call(failing_operation)

        assert breaker.state == CircuitState.OPEN

        # Now it should fail fast without calling the function
        mock_operation = MagicMock()
        with pytest.raises(BackendError) as exc_info:
            breaker.call(mock_operation)

        assert "Circuit breaker is OPEN" in str(exc_info.value)
        mock_operation.assert_not_called()

    def test_half_open_transition_after_timeout(self):
        """Test transition to HALF_OPEN after timeout."""
        from cachekit.backends.errors import BackendError, BackendErrorType

        config = CircuitBreakerConfig(failure_threshold=1, timeout_seconds=0.1)
        breaker = CircuitBreaker(config, namespace="test")

        # Open the circuit
        def failing_operation():
            raise BackendError("Connection failed", error_type=BackendErrorType.TRANSIENT)

        with pytest.raises(BackendError):
            breaker.call(failing_operation)

        assert breaker.state == CircuitState.OPEN

        # Wait for timeout
        time.sleep(0.15)

        # Next request should transition to HALF_OPEN and be allowed
        def successful_operation():
            return "success"

        result = breaker.call(successful_operation)
        assert result == "success"
        assert breaker.state == CircuitState.HALF_OPEN

    def test_half_open_to_closed_after_successes(self):
        """Test transition from HALF_OPEN to CLOSED after success threshold."""
        from cachekit.backends.errors import BackendError, BackendErrorType

        config = CircuitBreakerConfig(
            failure_threshold=1,
            success_threshold=2,
            timeout_seconds=0.1,
            half_open_requests=2,  # Allow 2 requests during HALF_OPEN to meet success threshold
        )
        breaker = CircuitBreaker(config, namespace="test")

        # Open the circuit
        def failing_operation():
            raise BackendError("Connection failed", error_type=BackendErrorType.TRANSIENT)

        with pytest.raises(BackendError):
            breaker.call(failing_operation)

        # Wait and transition to HALF_OPEN
        time.sleep(0.15)

        def successful_operation():
            return "success"

        # First success in HALF_OPEN
        result = breaker.call(successful_operation)
        assert result == "success"
        assert breaker.state == CircuitState.HALF_OPEN
        assert breaker.success_count == 1

        # Second success should close circuit
        result = breaker.call(successful_operation)
        assert result == "success"
        assert breaker.state == CircuitState.CLOSED

    def test_half_open_to_open_on_failure(self):
        """Test transition from HALF_OPEN back to OPEN on failure."""
        from cachekit.backends.errors import BackendError, BackendErrorType

        config = CircuitBreakerConfig(failure_threshold=1, timeout_seconds=0.1)
        breaker = CircuitBreaker(config, namespace="test")

        # Open the circuit
        def failing_operation():
            raise BackendError("Connection failed", error_type=BackendErrorType.TRANSIENT)

        with pytest.raises(BackendError):
            breaker.call(failing_operation)

        # Wait and transition to HALF_OPEN
        time.sleep(0.15)

        # Any failure in HALF_OPEN should immediately reopen circuit
        with pytest.raises(BackendError):
            breaker.call(failing_operation)

        assert breaker.state == CircuitState.OPEN

    def test_half_open_permit_limiting(self):
        """Test that HALF_OPEN state limits concurrent requests."""
        from cachekit.backends.errors import BackendError, BackendErrorType

        config = CircuitBreakerConfig(failure_threshold=1, timeout_seconds=0.1, half_open_requests=1)
        breaker = CircuitBreaker(config, namespace="test")

        # Open the circuit
        def failing_operation():
            raise BackendError("Connection failed", error_type=BackendErrorType.TRANSIENT)

        with pytest.raises(BackendError):
            breaker.call(failing_operation)

        # Wait for timeout
        time.sleep(0.15)

        # First request should be allowed (transitions to HALF_OPEN)
        slow_operation = MagicMock(return_value="success")
        result = breaker.call(slow_operation)
        assert result == "success"
        assert breaker.state == CircuitState.HALF_OPEN

        # Additional requests should be rejected (no more permits)
        fast_operation = MagicMock()
        with pytest.raises(BackendError) as exc_info:
            breaker.call(fast_operation)

        assert "Circuit breaker is OPEN" in str(exc_info.value)
        fast_operation.assert_not_called()

    @pytest.mark.asyncio
    async def test_async_call_functionality(self):
        """Test async call functionality."""
        config = CircuitBreakerConfig()
        breaker = CircuitBreaker(config, namespace="test")

        async def async_operation():
            return "async_success"

        result = await breaker.call_async(async_operation)
        assert result == "async_success"
        assert breaker.state == CircuitState.CLOSED

    @pytest.mark.asyncio
    async def test_async_call_with_failure(self):
        """Test async call with failures."""
        from cachekit.backends.errors import BackendError, BackendErrorType

        config = CircuitBreakerConfig(failure_threshold=1)
        breaker = CircuitBreaker(config, namespace="test")

        async def failing_async_operation():
            raise BackendError("Async connection failed", error_type=BackendErrorType.TRANSIENT)

        # Should fail and open circuit
        with pytest.raises(BackendError):
            await breaker.call_async(failing_async_operation)

        assert breaker.state == CircuitState.OPEN

    def test_thread_safety_concurrent_requests(self):
        """Test thread safety with concurrent requests."""
        config = CircuitBreakerConfig(failure_threshold=5)
        breaker = CircuitBreaker(config, namespace="test")

        results = []
        errors = []

        def thread_operation(thread_id):
            try:

                def operation():
                    return f"success_{thread_id}"

                result = breaker.call(operation)
                results.append(result)
            except Exception as e:
                errors.append(e)

        # Start multiple threads
        threads = []
        for i in range(10):
            thread = threading.Thread(target=thread_operation, args=(i,))
            threads.append(thread)
            thread.start()

        # Wait for all threads to complete
        for thread in threads:
            thread.join()

        # All should succeed
        assert len(results) == 10
        assert len(errors) == 0
        assert breaker.state == CircuitState.CLOSED

    def test_thread_safety_state_transitions(self):
        """Test thread safety during state transitions."""
        config = CircuitBreakerConfig(failure_threshold=1, timeout_seconds=0.1)
        breaker = CircuitBreaker(config, namespace="test")

        # Open the circuit first
        def failing_operation():
            raise redis.ConnectionError("Connection failed")

        with pytest.raises(redis.ConnectionError):
            breaker.call(failing_operation)

        assert breaker.state == CircuitState.OPEN

        # Wait for timeout
        time.sleep(0.15)

        results = []
        errors = []

        def thread_operation():
            try:

                def operation():
                    return "success"

                result = breaker.call(operation)
                results.append(result)
            except Exception as e:
                errors.append(e)

        # Start multiple threads - only one should get through in HALF_OPEN
        threads = []
        for _ in range(5):
            thread = threading.Thread(target=thread_operation)
            threads.append(thread)
            thread.start()

        # Wait for all threads to complete
        for thread in threads:
            thread.join()

        # Exactly one should succeed (the one that got the permit)
        # Others should fail with circuit breaker error
        assert len(results) == 1
        assert results[0] == "success"
        assert len(errors) == 4

    def test_reset_functionality(self):
        """Test manual reset functionality."""
        config = CircuitBreakerConfig(failure_threshold=1)
        breaker = CircuitBreaker(config, namespace="test")

        # Open the circuit
        def failing_operation():
            raise redis.ConnectionError("Connection failed")

        with pytest.raises(redis.ConnectionError):
            breaker.call(failing_operation)

        assert breaker.state == CircuitState.OPEN
        assert breaker.failure_count == 1

        # Reset circuit
        breaker.reset()

        assert breaker.state == CircuitState.CLOSED
        assert breaker.failure_count == 0

    def test_get_stats(self):
        """Test statistics retrieval."""
        config = CircuitBreakerConfig(failure_threshold=2, success_threshold=1)
        breaker = CircuitBreaker(config, namespace="test_stats")

        stats = breaker.get_stats()

        assert stats["state"] == "CLOSED"
        assert stats["failure_count"] == 0
        assert stats["success_count"] == 0
        assert stats["namespace"] == "test_stats"
        assert stats["config"]["failure_threshold"] == 2
        assert stats["config"]["success_threshold"] == 1

    @patch("cachekit.reliability.circuit_breaker.cache_operations")
    def test_metrics_integration(self, mock_counter):
        """Test integration with Prometheus metrics."""
        from cachekit.backends.errors import BackendError, BackendErrorType

        config = CircuitBreakerConfig(failure_threshold=1)
        breaker = CircuitBreaker(config, namespace="test")

        # Open the circuit
        def failing_operation():
            raise BackendError("Connection failed", error_type=BackendErrorType.TRANSIENT)

        with pytest.raises(BackendError):
            breaker.call(failing_operation)

        # Try to call while circuit is open
        with pytest.raises(BackendError):
            breaker.call(failing_operation)

        # Should have recorded rejection metric
        mock_counter.labels.assert_called_with(
            operation="circuit_breaker_open", status="rejected", serializer="", namespace="test"
        )
        mock_counter.labels().inc.assert_called()

    def test_properties_thread_safety(self):
        """Test that property accessors are thread-safe."""
        config = CircuitBreakerConfig()
        breaker = CircuitBreaker(config, namespace="test")

        def access_properties():
            _ = breaker.state
            _ = breaker.failure_count
            _ = breaker.success_count
            _ = breaker.get_stats()

        # Access properties from multiple threads simultaneously
        threads = []
        for _ in range(10):
            thread = threading.Thread(target=access_properties)
            threads.append(thread)
            thread.start()

        for thread in threads:
            thread.join()

        # Should not raise any exceptions or cause deadlocks
        assert breaker.state == CircuitState.CLOSED
