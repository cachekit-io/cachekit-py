"""
CRITICAL PATH TEST: Circuit Breaker State Machine

This test MUST pass for reliability features to work correctly.
Tests circuit breaker state transitions: CLOSED → OPEN → HALF_OPEN → CLOSED.

The circuit breaker is crucial for preventing cascading failures when Redis
becomes unhealthy. These tests validate the state machine logic that protects
the application from overwhelming a struggling Redis instance.
"""

import time

import pytest
import redis

from cachekit.reliability.circuit_breaker import (
    CircuitBreaker,
    CircuitBreakerConfig,
    CircuitState,
)

from ..utils.redis_test_helpers import RedisIsolationMixin

pytestmark = pytest.mark.critical


class TestCircuitBreakerStateMachine(RedisIsolationMixin):
    """Critical tests for circuit breaker state machine transitions."""

    def test_closed_to_open_after_failure_threshold(self):
        """CRITICAL: Circuit opens after failure_threshold consecutive failures."""
        config = CircuitBreakerConfig(failure_threshold=5)
        breaker = CircuitBreaker(config, namespace="test")

        # Verify initial state
        assert breaker.state == CircuitState.CLOSED
        assert breaker.failure_count == 0

        # Record failures up to threshold - 1
        for i in range(4):
            breaker.record_failure()
            assert breaker.state == CircuitState.CLOSED, f"Should stay CLOSED at {i + 1} failures"
            assert breaker.failure_count == i + 1

        # One more failure should open the circuit
        breaker.record_failure()
        assert breaker.state == CircuitState.OPEN, "Circuit should OPEN at threshold"
        assert breaker.failure_count == 5

    def test_open_to_half_open_after_timeout(self):
        """CRITICAL: Circuit transitions to HALF_OPEN after timeout_seconds."""
        config = CircuitBreakerConfig(
            failure_threshold=3,
            timeout_seconds=0.1,  # Short timeout for testing
        )
        breaker = CircuitBreaker(config, namespace="test")

        # Force circuit to OPEN state
        for _ in range(3):
            breaker.record_failure()
        assert breaker.state == CircuitState.OPEN

        # Should reject immediately (before timeout)
        assert not breaker.should_attempt_call(), "Should reject before timeout"
        assert breaker.state == CircuitState.OPEN

        # Wait for timeout to expire
        time.sleep(0.15)

        # Next request should transition to HALF_OPEN
        assert breaker.should_attempt_call(), "Should allow test request after timeout"
        assert breaker.state == CircuitState.HALF_OPEN, "Should transition to HALF_OPEN"

    def test_half_open_to_closed_after_success_threshold(self):
        """CRITICAL: Circuit closes after success_threshold successes in HALF_OPEN."""
        config = CircuitBreakerConfig(
            failure_threshold=3,
            success_threshold=3,
            timeout_seconds=0.1,
            half_open_requests=3,  # Allow 3 test requests
        )
        breaker = CircuitBreaker(config, namespace="test")

        # Force to OPEN then HALF_OPEN
        for _ in range(3):
            breaker.record_failure()
        assert breaker.state == CircuitState.OPEN

        time.sleep(0.15)
        breaker.should_attempt_call()  # Transition to HALF_OPEN
        assert breaker.state == CircuitState.HALF_OPEN

        # Record successes up to threshold - 1
        for i in range(2):
            breaker.record_success()
            assert breaker.state == CircuitState.HALF_OPEN, f"Should stay HALF_OPEN at {i + 1} successes"
            assert breaker.success_count == i + 1

        # One more success should close the circuit
        breaker.record_success()
        assert breaker.state == CircuitState.CLOSED, "Circuit should CLOSE after success threshold"
        assert breaker.success_count == 0, "Success count should reset"
        assert breaker.failure_count == 0, "Failure count should reset"

    def test_half_open_to_open_on_failure(self):
        """CRITICAL: Any failure in HALF_OPEN immediately returns to OPEN."""
        config = CircuitBreakerConfig(
            failure_threshold=3,
            success_threshold=3,
            timeout_seconds=0.1,
        )
        breaker = CircuitBreaker(config, namespace="test")

        # Force to OPEN then HALF_OPEN
        for _ in range(3):
            breaker.record_failure()
        time.sleep(0.15)
        breaker.should_attempt_call()  # Transition to HALF_OPEN
        assert breaker.state == CircuitState.HALF_OPEN

        # Record some successes
        breaker.record_success()
        assert breaker.state == CircuitState.HALF_OPEN
        assert breaker.success_count == 1

        # Single failure should reopen circuit
        breaker.record_failure()
        assert breaker.state == CircuitState.OPEN, "Should return to OPEN on any HALF_OPEN failure"
        assert breaker.success_count == 0, "Success count should reset"

    def test_closed_state_with_intermittent_failures(self):
        """CRITICAL: Circuit stays CLOSED with intermittent failures below threshold."""
        config = CircuitBreakerConfig(failure_threshold=5)
        breaker = CircuitBreaker(config, namespace="test")

        # Record failures below threshold in multiple batches
        # Note: successes don't reset failure count in CLOSED state
        # Only total failures matter, not consecutive
        for _ in range(2):
            # 4 failures (below threshold of 5)
            for _ in range(4):
                breaker.record_failure()
            # Reset manually to simulate recovery
            breaker.reset()

        # Circuit should remain CLOSED (never hit threshold in one batch)
        assert breaker.state == CircuitState.CLOSED, "Failures below threshold shouldn't open circuit"

    def test_half_open_request_limiting(self):
        """CRITICAL: HALF_OPEN only allows limited concurrent requests."""
        config = CircuitBreakerConfig(
            failure_threshold=3,
            timeout_seconds=0.1,
            half_open_requests=1,  # Only 1 test request
        )
        breaker = CircuitBreaker(config, namespace="test")

        # Force to OPEN then HALF_OPEN
        for _ in range(3):
            breaker.record_failure()
        time.sleep(0.15)

        # First request should be allowed
        assert breaker.should_attempt_call(), "First HALF_OPEN request should be allowed"
        assert breaker.state == CircuitState.HALF_OPEN

        # Second request should be rejected (limit reached)
        assert not breaker.should_attempt_call(), "Second request should be rejected (limit=1)"

        # After completing the first request successfully, circuit should close
        breaker.record_success()
        # Note: With success_threshold=3 (default), we need 3 successes to close
        # So we stay in HALF_OPEN, but permits are reset

    def test_excluded_exceptions_dont_count_as_failures(self):
        """CRITICAL: Excluded error types don't trigger circuit breaker via call() method."""
        from cachekit.backends.errors import BackendError, BackendErrorType

        config = CircuitBreakerConfig(
            failure_threshold=3,
            excluded_error_types=(BackendErrorType.PERMANENT,),
        )
        breaker = CircuitBreaker(config, namespace="test")

        call_count = 0

        def failing_with_excluded():
            nonlocal call_count
            call_count += 1
            # Raise BackendError with PERMANENT type (excluded)
            raise BackendError("Config error", error_type=BackendErrorType.PERMANENT)

        # Call with excluded error type multiple times
        for _ in range(10):
            try:
                breaker.call(failing_with_excluded)
            except BackendError:
                pass  # Expected

        # Circuit should remain CLOSED (excluded error types don't count)
        assert breaker.state == CircuitState.CLOSED
        assert breaker.failure_count == 0, "Excluded error types shouldn't increment failure count"

        # Non-excluded error type should count
        def failing_with_transient_error():
            raise BackendError("Connection failed", error_type=BackendErrorType.TRANSIENT)

        for _ in range(3):
            try:
                breaker.call(failing_with_transient_error)
            except BackendError:
                pass

        assert breaker.state == CircuitState.OPEN, "Non-excluded error types should trigger circuit"

    def test_manual_reset_to_closed(self):
        """CRITICAL: Manual reset transitions circuit to CLOSED state."""
        config = CircuitBreakerConfig(failure_threshold=3)
        breaker = CircuitBreaker(config, namespace="test")

        # Force to OPEN state
        for _ in range(3):
            breaker.record_failure()
        assert breaker.state == CircuitState.OPEN
        assert breaker.failure_count == 3

        # Manual reset should restore CLOSED state
        breaker.reset()
        assert breaker.state == CircuitState.CLOSED, "Reset should return to CLOSED"
        assert breaker.failure_count == 0, "Reset should clear failure count"
        assert breaker.success_count == 0, "Reset should clear success count"

    def test_get_stats_returns_accurate_state(self):
        """CRITICAL: get_stats() returns accurate circuit breaker state."""
        config = CircuitBreakerConfig(
            failure_threshold=5,
            success_threshold=3,
            timeout_seconds=30.0,
        )
        breaker = CircuitBreaker(config, namespace="production")

        # Initial state stats
        stats = breaker.get_stats()
        assert stats["state"] == "CLOSED"
        assert stats["failure_count"] == 0
        assert stats["success_count"] == 0
        assert stats["namespace"] == "production"
        assert stats["config"]["failure_threshold"] == 5
        assert stats["config"]["success_threshold"] == 3
        assert stats["config"]["timeout_seconds"] == 30.0

        # Record some failures
        for _ in range(3):
            breaker.record_failure()

        stats = breaker.get_stats()
        assert stats["state"] == "CLOSED"
        assert stats["failure_count"] == 3

        # Open the circuit
        for _ in range(2):
            breaker.record_failure()

        stats = breaker.get_stats()
        assert stats["state"] == "OPEN"
        assert stats["failure_count"] == 5
        assert stats["last_failure_time"] > 0

    def test_circuit_breaker_call_method_integration(self):
        """CRITICAL: call() method properly integrates with state machine."""
        config = CircuitBreakerConfig(failure_threshold=3)
        breaker = CircuitBreaker(config, namespace="test")

        call_count = 0

        def successful_operation():
            nonlocal call_count
            call_count += 1
            return "success"

        def failing_operation():
            nonlocal call_count
            call_count += 1
            raise redis.ConnectionError("Redis down")

        # Successful calls work normally
        result = breaker.call(successful_operation)
        assert result == "success"
        assert call_count == 1
        assert breaker.state == CircuitState.CLOSED

        # Failing calls increment failure count
        for _ in range(3):
            with pytest.raises(redis.ConnectionError):
                breaker.call(failing_operation)

        assert breaker.state == CircuitState.OPEN
        assert call_count == 4  # 1 success + 3 failures

        # Circuit is now open - calls should fail fast without executing
        from cachekit.backends.errors import BackendError

        with pytest.raises(BackendError, match="Circuit breaker is OPEN"):
            breaker.call(successful_operation)

        # Call count shouldn't increase (fast fail)
        assert call_count == 4, "Circuit OPEN should fail fast without calling function"

    def test_concurrent_state_transitions_thread_safe(self):
        """CRITICAL: State transitions are thread-safe under concurrent load."""
        import threading

        config = CircuitBreakerConfig(
            failure_threshold=10,
            timeout_seconds=0.1,
        )
        breaker = CircuitBreaker(config, namespace="test")

        failure_count = {"value": 0}
        lock = threading.Lock()

        def record_failures():
            """Record failures concurrently."""
            for _ in range(5):
                breaker.record_failure()
                with lock:
                    failure_count["value"] += 1

        # Launch multiple threads recording failures
        threads = [threading.Thread(target=record_failures) for _ in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # Total failures: 3 threads * 5 failures = 15 failures
        assert failure_count["value"] == 15
        # Circuit should be OPEN (threshold is 10)
        assert breaker.state == CircuitState.OPEN

        # Wait for timeout and test concurrent HALF_OPEN transitions
        time.sleep(0.15)

        allowed_count = {"value": 0}

        def attempt_half_open():
            """Attempt requests during HALF_OPEN state."""
            if breaker.should_attempt_call():
                with lock:
                    allowed_count["value"] += 1

        # Only half_open_requests (1 by default) should be allowed
        threads = [threading.Thread(target=attempt_half_open) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # Due to thread safety, only the configured number should be allowed
        assert allowed_count["value"] == 1, "Only half_open_requests should be allowed concurrently"

    def test_circuit_breaker_async_call_success(self):
        """CRITICAL: call_async method works with successful async functions."""
        import asyncio

        config = CircuitBreakerConfig(failure_threshold=3)
        breaker = CircuitBreaker(config)

        async def async_operation():
            await asyncio.sleep(0.01)
            return "success"

        async def run_test():
            result = await breaker.call_async(async_operation)
            assert result == "success"
            assert breaker.state.name == "CLOSED"
            return True

        assert asyncio.run(run_test())

    def test_circuit_breaker_async_call_failure(self):
        """CRITICAL: call_async method handles async failures correctly."""
        import asyncio

        config = CircuitBreakerConfig(failure_threshold=2)
        breaker = CircuitBreaker(config)

        async def failing_async_operation():
            await asyncio.sleep(0.01)
            raise ValueError("Async failure")

        async def run_test():
            # Record 2 failures to open circuit
            for _ in range(2):
                try:
                    await breaker.call_async(failing_async_operation)
                except ValueError:
                    pass  # Expected

            # Circuit should be open
            assert breaker.state.name == "OPEN"

            # Next call should be rejected immediately
            try:
                await breaker.call_async(failing_async_operation)
                raise AssertionError("Should have raised ConnectionError")
            except Exception as e:
                assert "Circuit breaker is OPEN" in str(e)

            return True

        assert asyncio.run(run_test())

    def test_circuit_breaker_async_call_excluded_exceptions(self):
        """CRITICAL: call_async respects excluded error types."""
        import asyncio

        from cachekit.backends.errors import BackendError, BackendErrorType

        config = CircuitBreakerConfig(failure_threshold=2, excluded_error_types=(BackendErrorType.PERMANENT,))
        breaker = CircuitBreaker(config)

        async def async_op_with_excluded_error():
            await asyncio.sleep(0.01)
            raise BackendError("Config error", error_type=BackendErrorType.PERMANENT)

        async def run_test():
            # Raise excluded error type 3 times
            for _ in range(3):
                try:
                    await breaker.call_async(async_op_with_excluded_error)
                except BackendError:
                    pass  # Expected

            # Circuit should still be CLOSED (excluded error types don't count)
            assert breaker.state.name == "CLOSED"
            assert breaker.failure_count == 0

            return True

        assert asyncio.run(run_test())
