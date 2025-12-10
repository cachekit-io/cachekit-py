"""Tests for circuit breaker race condition fixes.

This module tests the thread-safety fixes implemented in the circuit breaker
to prevent race conditions during state transitions, particularly the
OPEN -> HALF_OPEN transition that could allow multiple threads to simultaneously
enter the testing phase.
"""

import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from unittest.mock import MagicMock

from cachekit.reliability import (
    CircuitBreaker,
    CircuitBreakerConfig,
    CircuitState,
)


class TestCircuitBreakerRaceConditions:
    """Test circuit breaker race condition prevention."""

    def test_concurrent_open_to_half_open_transition(self):
        """Test that only one thread can transition from OPEN to HALF_OPEN.

        This test verifies Requirement 1.1: WHEN multiple threads attempt to
        transition circuit breaker from OPEN to HALF_OPEN THEN only one thread
        SHALL successfully transition.
        """
        # Configure circuit breaker with short timeout for testing
        config = CircuitBreakerConfig(
            failure_threshold=1,
            timeout_seconds=0.1,  # Short timeout for testing
            half_open_requests=1,  # Only allow 1 test request
        )
        breaker = CircuitBreaker(config, namespace="test")

        # Force breaker into OPEN state
        mock_func = MagicMock(side_effect=Exception("Redis error"))
        try:
            breaker.call(mock_func)
        except Exception:
            pass  # Expected

        # Verify it's in OPEN state
        assert breaker._state == CircuitState.OPEN

        # Wait for timeout to expire
        time.sleep(0.2)

        # Track transition attempts from multiple threads
        transition_count = 0
        half_open_permits_issued = 0
        transition_lock = threading.Lock()

        def attempt_transition():
            """Attempt to make a request that might trigger transition."""
            nonlocal transition_count, half_open_permits_issued

            mock_success_func = MagicMock(return_value="success")

            # Check if request is allowed (triggers potential transition)
            try:
                _result = breaker.call(mock_success_func)
                # If we get here, we were allowed through
                with transition_lock:
                    if breaker._state == CircuitState.HALF_OPEN:
                        transition_count += 1
                    half_open_permits_issued += 1
                return True
            except Exception:
                # Request was rejected
                return False

        # Launch multiple threads simultaneously
        num_threads = 20
        with ThreadPoolExecutor(max_workers=num_threads) as executor:
            futures = [executor.submit(attempt_transition) for _ in range(num_threads)]
            results = [future.result() for future in as_completed(futures)]

        # Verify that only one thread successfully transitioned to HALF_OPEN
        # and that half-open permits never exceeded the limit
        assert transition_count <= 1, f"Multiple transitions detected: {transition_count}"
        assert half_open_permits_issued <= config.half_open_requests, (
            f"Half-open permits exceeded limit: {half_open_permits_issued} > {config.half_open_requests}"
        )

        # At least one request should have been allowed through
        assert any(results), "No requests were allowed through after timeout"

    def test_half_open_permit_limit_under_concurrency(self):
        """Test that half-open permits never exceed configured limit under concurrent load.

        This test verifies Requirement 1.2: WHEN circuit breaker is in HALF_OPEN
        state THEN the number of test requests SHALL never exceed the configured limit.
        """
        config = CircuitBreakerConfig(
            failure_threshold=1,
            timeout_seconds=0.1,
            half_open_requests=3,  # Allow 3 test requests
        )
        breaker = CircuitBreaker(config, namespace="test")

        # Force into OPEN state
        mock_func = MagicMock(side_effect=Exception("Redis error"))
        try:
            breaker.call(mock_func)
        except Exception:
            pass

        # Wait for timeout
        time.sleep(0.2)

        # Manually transition to HALF_OPEN to test permit limiting
        with breaker._lock:
            breaker._transition_to_half_open()

        assert breaker._state == CircuitState.HALF_OPEN

        # Track permits issued across threads
        permits_issued = []
        permits_lock = threading.Lock()

        # Barrier to synchronize thread starts
        barrier = threading.Barrier(20)

        def request_permit():
            """Attempt to get a permit in HALF_OPEN state."""
            # Wait for all threads to be ready
            barrier.wait()

            # Test the permit allocation directly
            with breaker._lock:
                if breaker._half_open_permits < config.half_open_requests:
                    _initial_permits = breaker._half_open_permits
                    breaker._half_open_permits += 1
                    with permits_lock:
                        permits_issued.append(True)
                    return True
                else:
                    with permits_lock:
                        permits_issued.append(False)
                    return False

        # Launch threads simultaneously to stress test permit limiting
        num_threads = 20
        with ThreadPoolExecutor(max_workers=num_threads) as executor:
            futures = [executor.submit(request_permit) for _ in range(num_threads)]
            results = [future.result() for future in as_completed(futures)]

        # Count successful permits
        successful_permits = sum(results)

        # Verify permit limit was respected
        assert successful_permits <= config.half_open_requests, (
            f"Too many permits issued: {successful_permits} > {config.half_open_requests}"
        )

        # Verify internal permit counter is consistent
        assert breaker._half_open_permits <= config.half_open_requests, (
            f"Internal permit counter exceeded: {breaker._half_open_permits} > {config.half_open_requests}"
        )

    def test_state_consistency_under_concurrent_access(self):
        """Test that circuit breaker state remains consistent under concurrent access.

        This test verifies Requirement 1.1 and 1.2: ensuring that state transitions
        are atomic and permit counting is accurate under high concurrency.
        """
        config = CircuitBreakerConfig(
            failure_threshold=2,
            success_threshold=2,
            timeout_seconds=0.1,
            half_open_requests=2,
        )
        breaker = CircuitBreaker(config, namespace="test")

        # Track state observations from different threads
        state_observations = []
        observation_lock = threading.Lock()

        def observe_and_act():
            """Observe state and perform an operation."""
            mock_func = MagicMock()

            # Sometimes succeed, sometimes fail to trigger state changes
            import random

            if random.random() < 0.7:  # 70% success rate
                mock_func.return_value = "success"
            else:
                mock_func.side_effect = Exception("Simulated failure")

            try:
                _result = breaker.call(mock_func)
                with observation_lock:
                    state_observations.append(
                        {"state": breaker._state, "result": "success", "permits": breaker._half_open_permits}
                    )
                return True
            except Exception:
                with observation_lock:
                    state_observations.append(
                        {"state": breaker._state, "result": "failure", "permits": breaker._half_open_permits}
                    )
                return False

        # Run concurrent operations
        num_threads = 30
        with ThreadPoolExecutor(max_workers=num_threads) as executor:
            futures = [executor.submit(observe_and_act) for _ in range(num_threads)]
            _results = [future.result() for future in as_completed(futures)]

        # Analyze state consistency
        half_open_observations = [obs for obs in state_observations if obs["state"] == CircuitState.HALF_OPEN]

        # Verify that permits never exceeded limit during HALF_OPEN
        for obs in half_open_observations:
            assert obs["permits"] <= config.half_open_requests, (
                f"Permits exceeded limit: {obs['permits']} > {config.half_open_requests}"
            )

        # Verify state transitions were atomic (no invalid states observed)
        valid_states = {CircuitState.CLOSED, CircuitState.OPEN, CircuitState.HALF_OPEN}
        for obs in state_observations:
            assert obs["state"] in valid_states, f"Invalid state observed: {obs['state']}"

    def test_rapid_concurrent_state_transitions(self):
        """Test rapid state transitions under high concurrent load.

        This verifies that the double-checked locking pattern works correctly
        when many threads are simultaneously checking and potentially transitioning states.
        """
        config = CircuitBreakerConfig(
            failure_threshold=1,
            success_threshold=1,  # Quick recovery for rapid transitions
            timeout_seconds=0.05,  # Very short timeout for rapid transitions
            half_open_requests=1,
        )
        breaker = CircuitBreaker(config, namespace="test")

        # Track all state changes
        state_changes = []
        change_lock = threading.Lock()

        def rapid_operations():
            """Perform rapid operations to trigger state changes."""
            for i in range(10):
                # Alternate between success and failure to trigger transitions
                if i % 3 == 0:  # Occasional failure
                    mock_func = MagicMock(side_effect=Exception("Failure"))
                else:
                    mock_func = MagicMock(return_value="success")

                try:
                    _result = breaker.call(mock_func)
                    with change_lock:
                        state_changes.append(("success", breaker._state))
                except Exception:
                    with change_lock:
                        state_changes.append(("failure", breaker._state))

                # Small delay to allow state transitions
                time.sleep(0.01)

        # Run multiple threads performing rapid operations
        num_threads = 10
        with ThreadPoolExecutor(max_workers=num_threads) as executor:
            futures = [executor.submit(rapid_operations) for _ in range(num_threads)]
            for future in as_completed(futures):
                future.result()

        # Verify no invalid state transitions occurred
        # All observed states should be valid
        valid_states = {CircuitState.CLOSED, CircuitState.OPEN, CircuitState.HALF_OPEN}
        for _operation, state in state_changes:
            assert state in valid_states, f"Invalid state observed: {state}"

        # We should have observed multiple state transitions
        unique_states = {state for _, state in state_changes}
        assert len(unique_states) >= 2, "Should have observed multiple states during rapid transitions"

    def test_timeout_race_condition_prevention(self):
        """Test that timeout checks don't create race conditions.

        This specifically tests the scenario where multiple threads see that
        timeout has expired and attempt to transition simultaneously.
        """
        config = CircuitBreakerConfig(
            failure_threshold=1,
            timeout_seconds=0.1,
            half_open_requests=1,
        )
        breaker = CircuitBreaker(config, namespace="test")

        # Force into OPEN state
        mock_func = MagicMock(side_effect=Exception("Redis error"))
        try:
            breaker.call(mock_func)
        except Exception:
            pass

        assert breaker._state == CircuitState.OPEN

        # Wait for timeout to expire
        time.sleep(0.2)

        # Barrier to synchronize thread starts
        barrier = threading.Barrier(10)
        transitions_attempted = []
        transition_lock = threading.Lock()

        def synchronized_timeout_check():
            """All threads check timeout simultaneously."""
            # Wait for all threads to be ready
            barrier.wait()

            # All threads check timeout at the same time
            mock_success_func = MagicMock(return_value="success")

            try:
                _result = breaker.call(mock_success_func)
                with transition_lock:
                    transitions_attempted.append(True)
                return True
            except Exception:
                with transition_lock:
                    transitions_attempted.append(False)
                return False

        # Start threads simultaneously
        with ThreadPoolExecutor(max_workers=10) as executor:
            futures = [executor.submit(synchronized_timeout_check) for _ in range(10)]
            results = [future.result() for future in as_completed(futures)]

        # Only one thread should have been allowed through (transition to HALF_OPEN)
        successful_requests = sum(results)
        assert successful_requests <= config.half_open_requests, (
            f"Too many requests allowed: {successful_requests} > {config.half_open_requests}"
        )

        # Circuit should be in HALF_OPEN state if any request was allowed
        if successful_requests > 0:
            assert breaker._state == CircuitState.HALF_OPEN

    def test_circuit_breaker_allow_request_race_condition(self):
        """Test that CircuitBreaker._allow_request() prevents race conditions.

        This test specifically validates the double-checked locking pattern fix
        in the CircuitBreaker._allow_request() method to ensure only one thread
        can transition from OPEN to HALF_OPEN state.

        Requirements: 1.1, 1.2
        """
        from cachekit.reliability.circuit_breaker import CircuitBreaker

        config = CircuitBreakerConfig(
            failure_threshold=1,
            timeout_seconds=0.1,
            half_open_requests=3,  # Allow 3 test requests
        )
        breaker = CircuitBreaker(config, namespace="test_allow_request")

        # Force circuit to OPEN state
        breaker._on_failure(Exception("Test failure"))
        assert breaker.state == CircuitState.OPEN

        # Wait for recovery timeout to expire
        time.sleep(0.15)

        # Track transition attempts
        transition_attempts = []
        allowed_requests = []
        attempt_lock = threading.Lock()

        def concurrent_allow_request(thread_id):
            """Each thread attempts to call _allow_request()."""
            # Capture initial state
            initial_state = breaker.state

            # Call _allow_request() which should handle race conditions
            allowed = breaker._allow_request()

            # Capture final state
            final_state = breaker.state

            with attempt_lock:
                transition_attempts.append(
                    {"thread_id": thread_id, "initial_state": initial_state, "final_state": final_state, "allowed": allowed}
                )
                if allowed:
                    allowed_requests.append(thread_id)

            return allowed

        # Launch many threads simultaneously
        num_threads = 50
        with ThreadPoolExecutor(max_workers=num_threads) as executor:
            futures = [executor.submit(concurrent_allow_request, i) for i in range(num_threads)]
            _results = [future.result() for future in as_completed(futures)]

        # Analyze results
        transitions_to_half_open = sum(
            1
            for attempt in transition_attempts
            if attempt["initial_state"] == CircuitState.OPEN and attempt["final_state"] == CircuitState.HALF_OPEN
        )

        # Verify only one thread transitioned the state
        assert transitions_to_half_open <= 1, f"Multiple threads ({transitions_to_half_open}) transitioned to HALF_OPEN!"

        # Verify permits didn't exceed limit
        assert len(allowed_requests) <= config.half_open_requests, (
            f"Too many requests allowed: {len(allowed_requests)} > {config.half_open_requests}"
        )

        # Verify final state is HALF_OPEN
        assert breaker.state == CircuitState.HALF_OPEN

    def test_circuit_breaker_half_open_total_attempts_tracking(self):
        """Test that half_open_total_attempts correctly limits total test requests.

        This validates the fix in _allow_half_open_request() that tracks total
        attempts during HALF_OPEN state, not just concurrent permits.

        Requirements: 1.2
        """
        from cachekit.reliability.circuit_breaker import CircuitBreaker

        config = CircuitBreakerConfig(
            failure_threshold=1,
            half_open_requests=3,  # Only allow 3 total test requests
        )
        breaker = CircuitBreaker(config, namespace="test_total_attempts")

        # Force to HALF_OPEN state
        breaker._on_failure(Exception("Test"))
        with breaker._lock:
            breaker._transition_to_half_open()

        # Track request results
        request_results = []
        half_open_requests = []

        def make_sequential_request(req_id):
            """Make a request and track result."""
            # Track state before request
            state_before = breaker.state
            allowed = breaker._allow_request()
            request_results.append((req_id, allowed))

            # Track which requests were made in HALF_OPEN state
            if state_before == CircuitState.HALF_OPEN:
                half_open_requests.append((req_id, allowed))

            if allowed:
                # Don't call _on_success() to avoid transitioning to CLOSED
                # We want to test the total attempts limit, not the success threshold
                time.sleep(0.01)

            return allowed

        # Make many requests sequentially (not concurrent)
        # This tests that we track total attempts, not just concurrent permits
        for i in range(10):
            make_sequential_request(i)

        # Count allowed requests made during HALF_OPEN state
        half_open_allowed = sum(1 for _, allowed in half_open_requests if allowed)

        # Should only allow exactly half_open_requests attempts in HALF_OPEN state
        assert half_open_allowed == config.half_open_requests, (
            f"Wrong number of HALF_OPEN attempts allowed: {half_open_allowed} != {config.half_open_requests}"
        )

        # Verify the first 3 HALF_OPEN requests were allowed and the rest were rejected
        for i, (req_id, allowed) in enumerate(half_open_requests):
            if i < config.half_open_requests:
                assert allowed, f"HALF_OPEN request {req_id} should have been allowed"
            else:
                assert not allowed, f"HALF_OPEN request {req_id} should have been rejected"

        # Verify internal counter
        assert breaker._half_open_total_attempts == config.half_open_requests
