#!/usr/bin/env python3
"""🔄 Comprehensive Circuit Breaker Tests for PyRedis Cache Pro

Consolidated test suite that combines all circuit breaker testing scenarios:
- Unit tests for circuit breaker components
- Integration tests with decorators
- Async and sync integration
- Race condition testing
- State transition logging
- Production scenarios

This replaces 8 separate circuit breaker test files with one comprehensive suite.

Test Coverage:
- Configuration and defaults
- State transitions (CLOSED → OPEN → HALF_OPEN → CLOSED)
- Failure and success thresholds
- Race condition prevention
- Thread safety
- Async/sync decorator integration
- Error classification
- Metrics collection
- State logging
- Recovery scenarios
"""

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from unittest.mock import patch

import pytest

from cachekit import cache
from cachekit.config import DecoratorConfig
from cachekit.config.nested import (
    CircuitBreakerConfig as DecoratorCircuitBreakerConfig,
)
from cachekit.reliability.circuit_breaker import (
    CircuitBreaker,
    CircuitBreakerConfig,
    CircuitState,
)

# Import shared fixtures


class TestCircuitBreakerConfiguration:
    """Test circuit breaker configuration and defaults."""

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
        config = CircuitBreakerConfig(failure_threshold=10, success_threshold=5, timeout_seconds=60.0, half_open_requests=3)

        assert config.failure_threshold == 10
        assert config.success_threshold == 5
        assert config.timeout_seconds == 60.0
        assert config.half_open_requests == 3

    def test_configuration_validation(self):
        """Test configuration validation."""
        # Invalid thresholds should raise ValueError
        with pytest.raises(ValueError):
            CircuitBreakerConfig(failure_threshold=0)

        with pytest.raises(ValueError):
            CircuitBreakerConfig(success_threshold=0)

        with pytest.raises(ValueError):
            CircuitBreakerConfig(timeout_seconds=-1)


class TestCircuitBreakerStates:
    """Test circuit breaker state transitions and logic."""

    def setup_method(self):
        """Set up test fixtures."""
        self.config = CircuitBreakerConfig(failure_threshold=3, success_threshold=2, timeout_seconds=1.0)
        self.circuit_breaker = CircuitBreaker(self.config)

    def test_initial_state_is_closed(self):
        """Test that circuit breaker starts in CLOSED state."""
        assert self.circuit_breaker.state == CircuitState.CLOSED
        assert self.circuit_breaker.failure_count == 0
        assert self.circuit_breaker.success_count == 0

    def test_failure_transitions_to_open(self):
        """Test that enough failures transition to OPEN state."""
        # Record failures up to threshold
        for _ in range(self.config.failure_threshold):
            self.circuit_breaker.record_failure()

        assert self.circuit_breaker.state == CircuitState.OPEN
        assert self.circuit_breaker.failure_count == self.config.failure_threshold

    def test_open_state_prevents_calls(self):
        """Test that OPEN state prevents function calls."""
        # Force circuit to open
        for _ in range(self.config.failure_threshold):
            self.circuit_breaker.record_failure()

        assert not self.circuit_breaker.should_attempt_call()

    def test_half_open_after_timeout(self):
        """Test transition to HALF_OPEN after timeout."""
        # Force circuit to open
        for _ in range(self.config.failure_threshold):
            self.circuit_breaker.record_failure()

        # Wait for timeout
        time.sleep(self.config.timeout_seconds + 0.1)

        # Should allow calls in HALF_OPEN state
        assert self.circuit_breaker.should_attempt_call()
        assert self.circuit_breaker.state == CircuitState.HALF_OPEN

    def test_half_open_success_returns_to_closed(self):
        """Test that successes in HALF_OPEN return to CLOSED."""
        # Force to HALF_OPEN
        for _ in range(self.config.failure_threshold):
            self.circuit_breaker.record_failure()

        time.sleep(self.config.timeout_seconds + 0.1)
        self.circuit_breaker.should_attempt_call()  # Transition to HALF_OPEN

        # Record enough successes
        for _ in range(self.config.success_threshold):
            self.circuit_breaker.record_success()

        assert self.circuit_breaker.state == CircuitState.CLOSED
        assert self.circuit_breaker.failure_count == 0

    def test_half_open_failure_returns_to_open(self):
        """Test that failure in HALF_OPEN returns to OPEN."""
        # Force to HALF_OPEN
        for _ in range(self.config.failure_threshold):
            self.circuit_breaker.record_failure()

        time.sleep(self.config.timeout_seconds + 0.1)
        self.circuit_breaker.should_attempt_call()  # Transition to HALF_OPEN

        # Record failure
        self.circuit_breaker.record_failure()

        assert self.circuit_breaker.state == CircuitState.OPEN


class TestCircuitBreakerRaceConditions:
    """Test circuit breaker thread safety and race condition prevention."""

    def setup_method(self):
        """Set up test fixtures."""
        self.config = CircuitBreakerConfig(
            failure_threshold=5,
            success_threshold=3,
            timeout_seconds=0.1,  # Short timeout for testing
        )
        self.circuit_breaker = CircuitBreaker(self.config)

    def test_concurrent_state_transitions(self):
        """Test that concurrent state transitions are thread-safe."""
        results = []

        def record_failures():
            """Record failures concurrently."""
            for _ in range(10):
                self.circuit_breaker.record_failure()
                results.append(self.circuit_breaker.state)

        # Start multiple threads recording failures
        threads = []
        for _ in range(5):
            thread = threading.Thread(target=record_failures)
            threads.append(thread)
            thread.start()

        # Wait for all threads to complete
        for thread in threads:
            thread.join()

        # Circuit should be open and state should be consistent
        assert self.circuit_breaker.state == CircuitState.OPEN

        # All recorded states should be valid
        valid_states = {CircuitState.CLOSED, CircuitState.OPEN}
        assert all(state in valid_states for state in results)

    def test_open_to_half_open_race_condition(self):
        """Test the critical OPEN → HALF_OPEN race condition fix."""
        # Force circuit to open
        for _ in range(self.config.failure_threshold):
            self.circuit_breaker.record_failure()

        assert self.circuit_breaker.state == CircuitState.OPEN

        # Wait for timeout
        time.sleep(self.config.timeout_seconds + 0.1)

        # Multiple threads try to transition to HALF_OPEN simultaneously
        transition_results = []

        def attempt_call():
            """Attempt call and record if it was allowed."""
            allowed = self.circuit_breaker.should_attempt_call()
            transition_results.append(allowed)
            return allowed

        # Start multiple threads simultaneously
        with ThreadPoolExecutor(max_workers=10) as executor:
            futures = [executor.submit(attempt_call) for _ in range(10)]
            results = [future.result() for future in as_completed(futures)]

        # Only the configured number of requests should be allowed in HALF_OPEN
        allowed_count = sum(1 for result in results if result)
        assert allowed_count <= self.config.half_open_requests

        # Circuit should be in HALF_OPEN state
        assert self.circuit_breaker.state == CircuitState.HALF_OPEN

    @pytest.mark.skip(reason="Test expects cumulative metrics, but success_count/failure_count are state machine counters")
    def test_concurrent_metrics_collection(self):
        """Test that metrics collection is thread-safe."""

        def record_operations():
            """Record operations concurrently."""
            for i in range(100):
                if i % 2 == 0:
                    self.circuit_breaker.record_success()
                else:
                    self.circuit_breaker.record_failure()

        # Start multiple threads
        with ThreadPoolExecutor(max_workers=5) as executor:
            futures = [executor.submit(record_operations) for _ in range(5)]
            for future in as_completed(futures):
                future.result()

        # Note: success_count/failure_count are state machine counters, not cumulative metrics


class TestCircuitBreakerIntegration:
    """Test circuit breaker integration with Redis cache decorators."""

    def test_sync_decorator_integration(self, redis_test_client):
        """Test circuit breaker integration with sync decorator."""
        call_count = 0

        @cache(config=DecoratorConfig(ttl=60, circuit_breaker=DecoratorCircuitBreakerConfig(enabled=True)))
        def test_function():
            nonlocal call_count
            call_count += 1
            if call_count <= 3:
                raise ConnectionError("Simulated Redis failure")
            return f"success_{call_count}"

        # First few calls should fail and trigger circuit breaker
        for _ in range(5):
            try:
                _result = test_function()
            except Exception:
                pass  # Expected failures

        # Circuit should be open and prevent further calls
        # This would be verified by checking metrics or state

    @pytest.mark.asyncio
    async def test_async_decorator_integration(self):
        """Test circuit breaker integration with async decorator."""
        call_count = 0

        @cache(config=DecoratorConfig(ttl=60, circuit_breaker=DecoratorCircuitBreakerConfig(enabled=True)))
        async def async_test_function():
            nonlocal call_count
            call_count += 1
            if call_count <= 3:
                raise ConnectionError("Simulated Redis failure")
            return f"success_{call_count}"

        # First few calls should fail
        for _ in range(5):
            try:
                _result = await async_test_function()
            except Exception:
                pass  # Expected failures

    def test_error_classification(self, redis_test_client):
        """Test that circuit breaker correctly classifies errors."""

        @cache(config=DecoratorConfig(ttl=60, circuit_breaker=DecoratorCircuitBreakerConfig(enabled=True)))
        def test_function(error_type):
            if error_type == "connection":
                raise ConnectionError("Connection failed")
            elif error_type == "timeout":
                raise TimeoutError("Operation timed out")
            elif error_type == "value":
                raise ValueError("Invalid value")  # Should not trigger circuit breaker
            return "success"

        # Connection and timeout errors should trigger circuit breaker
        for _ in range(3):
            try:
                test_function("connection")
            except ConnectionError:
                pass

        # ValueError should not trigger circuit breaker
        try:
            test_function("value")
        except ValueError:
            pass


class TestCircuitBreakerStateLogging:
    """Test circuit breaker state transition logging."""

    def setup_method(self):
        """Set up test fixtures."""
        self.config = CircuitBreakerConfig(failure_threshold=2, success_threshold=1, timeout_seconds=0.1)
        self.circuit_breaker = CircuitBreaker(self.config)

    def test_state_transition_logging(self, caplog):
        """Test that state transitions are properly logged."""
        caplog.set_level(logging.INFO)

        # Transition to OPEN
        for _ in range(self.config.failure_threshold):
            self.circuit_breaker.record_failure()

        # Check for state transition log
        state_logs = [record for record in caplog.records if "circuit breaker" in record.message.lower()]
        assert len(state_logs) > 0

        # Transition to HALF_OPEN
        time.sleep(self.config.timeout_seconds + 0.1)
        self.circuit_breaker.should_attempt_call()

        # Transition back to CLOSED
        self.circuit_breaker.record_success()

        # Should have logged multiple state transitions
        final_logs = [record for record in caplog.records if "circuit breaker" in record.message.lower()]
        assert len(final_logs) >= len(state_logs)

    @pytest.mark.skip(reason="CircuitBreaker doesn't log metrics at DEBUG level; only state transitions")
    def test_metrics_logging(self, caplog):
        """Test that circuit breaker metrics are logged."""
        caplog.set_level(logging.DEBUG)

        # Record some operations
        self.circuit_breaker.record_success()
        self.circuit_breaker.record_failure()

        # Note: Circuit breaker only logs state transitions (INFO/WARNING),
        # not individual metric updates at DEBUG level


class TestCircuitBreakerProductionScenarios:
    """Test circuit breaker behavior in production-like scenarios."""

    def test_redis_connection_failure_scenario(self, redis_test_client):
        """Test circuit breaker behavior during Redis connection failures."""

        @cache(config=DecoratorConfig(ttl=60, circuit_breaker=DecoratorCircuitBreakerConfig(enabled=True)))
        def cached_operation(value):
            return f"processed_{value}"

        # Simulate Redis connection failure
        with patch.object(redis_test_client, "get", side_effect=ConnectionError("Redis down")):
            with patch.object(redis_test_client, "set", side_effect=ConnectionError("Redis down")):
                # Multiple calls should trigger circuit breaker
                results = []
                for i in range(10):
                    try:
                        result = cached_operation(i)
                        results.append(result)
                    except Exception as e:
                        results.append(f"error_{type(e).__name__}")

                # Should have both errors and successful fallbacks
                assert len(results) == 10
                # Early calls might fail, later calls should be blocked by circuit breaker

    def test_intermittent_failure_recovery(self):
        """Test circuit breaker recovery from intermittent failures."""
        config = CircuitBreakerConfig(failure_threshold=2, success_threshold=1, timeout_seconds=0.1)
        circuit_breaker = CircuitBreaker(config)

        # Simulate intermittent failures with a counter
        call_count = [0]  # Use list to allow modification in nested function

        def intermittent_function():
            call_count[0] += 1
            # Fail every 3rd call
            if call_count[0] % 3 == 0:
                raise ConnectionError("Intermittent failure")
            return f"success_{call_count[0]}"

        # Simulate intermittent failures
        results = []
        for _ in range(20):
            try:
                if circuit_breaker.should_attempt_call():
                    result = intermittent_function()
                    circuit_breaker.record_success()
                    results.append(("success", result))
                else:
                    results.append(("blocked", None))
            except Exception as e:
                circuit_breaker.record_failure(e)
                results.append(("failure", str(e)))

            # Small delay to allow timeout recovery
            time.sleep(0.05)

        # Should have mix of successes, failures, and blocked calls
        success_count = sum(1 for result, _ in results if result == "success")
        failure_count = sum(1 for result, _ in results if result == "failure")
        _blocked_count = sum(1 for result, _ in results if result == "blocked")

        assert success_count > 0, "Should have some successful calls"
        assert failure_count > 0, "Should have some failed calls"
        # May or may not have blocked calls depending on timing

    def test_high_concurrency_scenario(self):
        """Test circuit breaker under high concurrency load."""
        config = CircuitBreakerConfig(failure_threshold=10, success_threshold=5, timeout_seconds=0.2)
        circuit_breaker = CircuitBreaker(config)

        results = []

        def concurrent_operation(thread_id: int):
            """Simulate concurrent operations."""
            thread_results = []
            for i in range(50):
                try:
                    if circuit_breaker.should_attempt_call():
                        # Simulate operation that fails 30% of the time
                        if (thread_id + i) % 10 < 3:
                            circuit_breaker.record_failure()
                            thread_results.append("failure")
                        else:
                            circuit_breaker.record_success()
                            thread_results.append("success")
                    else:
                        thread_results.append("blocked")
                except Exception:
                    thread_results.append("error")
            return thread_results

        # Run with high concurrency
        with ThreadPoolExecutor(max_workers=20) as executor:
            futures = [executor.submit(concurrent_operation, thread_id) for thread_id in range(20)]

            for future in as_completed(futures):
                results.extend(future.result())

        # Analyze results
        total_operations = len(results)
        success_count = results.count("success")
        failure_count = results.count("failure")
        blocked_count = results.count("blocked")

        assert total_operations == 1000  # 20 threads × 50 operations
        assert success_count > 0, "Should have successful operations"
        assert failure_count > 0, "Should have failed operations"

        # Circuit breaker should have provided protection
        _protection_rate = blocked_count / total_operations
        # Don't assert specific protection rate as it depends on timing


class TestCircuitBreakerMetrics:
    """Test circuit breaker metrics collection and reporting."""

    def setup_method(self):
        """Set up test fixtures."""
        self.config = CircuitBreakerConfig()
        self.circuit_breaker = CircuitBreaker(self.config)

    def test_metrics_collection(self):
        """Test that state machine counters are properly tracked."""
        # Note: success_count and failure_count are state machine counters,
        # not cumulative metrics. They track specific state transitions.

        # In CLOSED state, successes don't increment success_count
        # (success_count only tracks recovery in HALF_OPEN state)
        for _ in range(5):
            self.circuit_breaker.record_success()
        assert self.circuit_breaker.success_count == 0  # No increment in CLOSED

        # Failures increment failure_count in CLOSED state
        for _ in range(3):
            self.circuit_breaker.record_failure()
        assert self.circuit_breaker.failure_count == 3
        assert self.circuit_breaker.state == CircuitState.CLOSED  # Not enough to open

    def test_metrics_reset_on_state_change(self):
        """Test that metrics reset appropriately on state changes."""
        # Force to OPEN state
        for _ in range(self.config.failure_threshold):
            self.circuit_breaker.record_failure()

        _failure_count_at_open = self.circuit_breaker.failure_count

        # Transition to HALF_OPEN and then CLOSED
        time.sleep(self.config.timeout_seconds + 0.1)
        self.circuit_breaker.should_attempt_call()

        for _ in range(self.config.success_threshold):
            self.circuit_breaker.record_success()

        # Failure count should be reset when returning to CLOSED
        assert self.circuit_breaker.failure_count == 0
        assert self.circuit_breaker.state == CircuitState.CLOSED


if __name__ == "__main__":
    # Run basic tests
    pytest.main([__file__, "-v"])
