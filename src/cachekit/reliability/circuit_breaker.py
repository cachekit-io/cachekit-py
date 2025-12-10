"""Circuit breaker implementation for preventing cascading failures.

This module provides a production-ready circuit breaker implementation following
the classic Circuit Breaker pattern. The circuit breaker monitors error rates
and temporarily blocks requests when a service is struggling, giving it time
to recover.

The implementation follows established threading patterns from the codebase
using RLock and double-checked locking for thread safety.
"""

import logging
import threading
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Optional

# Import backend error types for failure detection
from cachekit.backends.errors import BackendError, BackendErrorType

# Import metrics from the metrics collection module
from cachekit.reliability.metrics_collection import (
    cache_operations,
    circuit_breaker_state,
)

logger = logging.getLogger(__name__)


class CircuitState(Enum):
    """Circuit breaker states following the classic pattern.

    State transitions:
    CLOSED -> OPEN: When failure_threshold is exceeded
    OPEN -> HALF_OPEN: After timeout_seconds have elapsed
    HALF_OPEN -> CLOSED: After success_threshold successful requests
    HALF_OPEN -> OPEN: On any failure during testing

    Examples:
        >>> CircuitState.CLOSED.value
        0
        >>> CircuitState.OPEN.value
        1
        >>> CircuitState.HALF_OPEN.value
        2
        >>> CircuitState.CLOSED.name
        'CLOSED'
    """

    CLOSED = 0  # Normal operation - all requests pass through
    OPEN = 1  # Failing fast - all requests immediately rejected
    HALF_OPEN = 2  # Testing recovery - limited requests allowed


@dataclass
class CircuitBreakerConfig:
    """Configuration for circuit breaker behavior.

    The circuit breaker prevents cascading failures by monitoring error rates
    and temporarily blocking requests when a service is struggling.

    Attributes:
        failure_threshold: Number of consecutive failures before opening circuit.
            Lower values make the circuit more sensitive to errors.
        success_threshold: Number of consecutive successes in HALF_OPEN before closing.
            Higher values ensure more stable recovery.
        timeout_seconds: How long to stay OPEN before testing recovery.
            Balance between giving service time to recover vs detecting recovery quickly.
        half_open_requests: Max concurrent requests allowed during HALF_OPEN testing.
            Usually 1 to minimize load during recovery testing.
        excluded_error_types: BackendErrorType values that don't count as failures.
            Example: BackendErrorType.PERMANENT for config errors

    Examples:
        Create with defaults:

        >>> config = CircuitBreakerConfig()
        >>> config.failure_threshold
        5
        >>> config.timeout_seconds
        30.0

        Create with custom values:

        >>> config = CircuitBreakerConfig(failure_threshold=10, timeout_seconds=60.0)
        >>> config.failure_threshold
        10

        Invalid values raise ValueError:

        >>> CircuitBreakerConfig(failure_threshold=0)  # doctest: +IGNORE_EXCEPTION_DETAIL
        Traceback (most recent call last):
            ...
        ValueError: failure_threshold must be positive, got 0
    """

    failure_threshold: int = 5  # Opens circuit after 5 consecutive failures
    success_threshold: int = 3  # Closes circuit after 3 consecutive successes
    timeout_seconds: float = 30.0  # Wait 30s before testing recovery
    half_open_requests: int = 1  # Allow 1 test request at a time
    excluded_error_types: tuple[BackendErrorType, ...] = ()  # No excluded error types by default

    def __post_init__(self):
        """Validate configuration."""
        # Validate thresholds
        if self.failure_threshold <= 0:
            raise ValueError(f"failure_threshold must be positive, got {self.failure_threshold}")
        if self.success_threshold <= 0:
            raise ValueError(f"success_threshold must be positive, got {self.success_threshold}")
        if self.timeout_seconds < 0:
            raise ValueError(f"timeout_seconds cannot be negative, got {self.timeout_seconds}")
        if self.half_open_requests <= 0:
            raise ValueError(f"half_open_requests must be positive, got {self.half_open_requests}")


@dataclass
class CacheOperationMetrics:
    """Local metrics tracking for cache operations.

    These metrics are instance-local and complement the global Prometheus metrics.
    Useful for debugging specific cache instances or namespaces.

    Examples:
        Track cache operations:

        >>> metrics = CacheOperationMetrics(
        ...     total_operations=100,
        ...     cache_hits=80,
        ...     cache_misses=15,
        ...     errors=5
        ... )
        >>> metrics.hit_rate
        0.8
        >>> metrics.error_rate
        0.05

        Empty metrics return 0.0 rates:

        >>> empty = CacheOperationMetrics()
        >>> empty.hit_rate
        0.0
        >>> empty.error_rate
        0.0
    """

    total_operations: int = 0  # All cache operations attempted
    cache_hits: int = 0  # Successful cache retrievals
    cache_misses: int = 0  # Key not found in cache
    errors: int = 0  # Redis errors (connection, timeout, etc.)
    fallbacks: int = 0  # Times fallback handler was used
    circuit_opens: int = 0  # Times circuit breaker has opened

    @property
    def hit_rate(self) -> float:
        """Calculate cache hit rate.

        Returns:
            Float between 0.0 and 1.0 representing hit percentage.
            Returns 0.0 if no operations have been performed.
        """
        if self.total_operations == 0:
            return 0.0
        return self.cache_hits / self.total_operations

    @property
    def error_rate(self) -> float:
        """Calculate error rate.

        Returns:
            Float between 0.0 and 1.0 representing error percentage.
            High error rates (>0.05) typically indicate infrastructure issues.
        """
        if self.total_operations == 0:
            return 0.0
        return self.errors / self.total_operations


class CircuitBreaker:
    """Production-ready circuit breaker with metrics.

    Implements the Circuit Breaker pattern to prevent cascading failures.
    When Redis is experiencing issues, the circuit breaker will "open" and
    start failing fast, giving the system time to recover.

    Thread-safe implementation using RLock to handle concurrent requests.
    Integrates with Prometheus metrics for monitoring circuit state.

    Example usage:
        config = CircuitBreakerConfig(failure_threshold=10, timeout_seconds=60)
        breaker = CircuitBreaker(config, namespace="user_api")

        try:
            result = breaker.call(redis_client.get, "key")
        except redis.ConnectionError:
            # Circuit is open, use fallback
            return cached_value
    """

    def __init__(self, config: CircuitBreakerConfig, namespace: str = "default"):
        self.config = config
        self.namespace = namespace
        self._state = CircuitState.CLOSED
        self._failure_count = 0  # Consecutive failures in CLOSED state
        self._success_count = 0  # Consecutive successes in HALF_OPEN state
        self._last_failure_time = 0.0  # Timestamp of last failure (for timeout)
        self._half_open_permits = 0  # Current test requests in HALF_OPEN
        self._half_open_total_attempts = 0  # Total requests attempted in HALF_OPEN cycle
        self._lock = threading.RLock()  # Reentrant lock for thread safety

        # Initialize Prometheus metric for this namespace
        circuit_breaker_state.labels(namespace=namespace).set(self._state.value)

    def call(self, func: Callable, *args, **kwargs) -> Any:
        """Execute function with circuit breaker protection.

        This is the main entry point for protected operations. The circuit breaker
        will check its state and either allow the request, reject it immediately,
        or allow it as a test request during recovery.

        Args:
            func: The function to execute (typically a backend operation)
            *args: Positional arguments to pass to func
            **kwargs: Keyword arguments to pass to func

        Returns:
            The result of func if successful

        Raises:
            BackendError: If circuit is OPEN (failing fast)
            Exception: Any exception raised by func (circuit may open as a result)
        """
        if not self._allow_request():
            # Circuit is open or half-open with no permits - fail fast
            cache_operations.labels(
                operation="circuit_breaker_open",
                status="rejected",
                serializer="",
                namespace=self.namespace,
            ).inc()
            raise BackendError("Circuit breaker is OPEN", error_type=BackendErrorType.TRANSIENT)

        try:
            # Execute the protected function
            result = func(*args, **kwargs)
            self._on_success()  # Record success for state management
            return result
        except Exception as e:
            # Check if it's a BackendError with an excluded error type
            if isinstance(e, BackendError) and e.error_type in self.config.excluded_error_types:
                # Don't count as failure, but still propagate
                raise
            # All other exceptions count as failures
            self._on_failure(e)
            raise  # Always re-raise to preserve error handling

    async def call_async(self, func: Callable, *args, **kwargs) -> Any:
        """Execute async function with circuit breaker protection."""
        if not self._allow_request():
            cache_operations.labels(
                operation="circuit_breaker_open",
                status="rejected",
                serializer="",
                namespace=self.namespace,
            ).inc()
            raise BackendError("Circuit breaker is OPEN", error_type=BackendErrorType.TRANSIENT)

        try:
            result = await func(*args, **kwargs)
            self._on_success()
            return result
        except Exception as e:
            # Check if it's a BackendError with an excluded error type
            if isinstance(e, BackendError) and e.error_type in self.config.excluded_error_types:
                # Don't count as failure, but still propagate
                raise
            # All other exceptions count as failures
            self._on_failure(e)
            raise

    def _allow_request(self) -> bool:
        """Check if request should be allowed based on circuit state.

        Decision flow:
        1. CLOSED: Always allow (normal operation)
        2. OPEN: Check if timeout expired
           - If yes: Transition to HALF_OPEN and check permits
           - If no: Reject request
        3. HALF_OPEN: Check if test permits available

        Thread-safe: Uses double-checked locking to ensure atomic state transitions.
        """
        with self._lock:
            current_time = time.time()

            if self._state == CircuitState.CLOSED:
                return True  # Normal operation - allow all requests

            if self._state == CircuitState.OPEN:
                # Check if we've waited long enough to test recovery
                if current_time - self._last_failure_time > self.config.timeout_seconds:
                    # Double-checked locking pattern to prevent race conditions:
                    #
                    # PROBLEM: Multiple threads could see the timeout has expired and all
                    # try to transition to HALF_OPEN state simultaneously. This would result
                    # in multiple "test" requests being sent to Redis when we only want one.
                    #
                    # SOLUTION: Even though we're already holding the lock, we check the state
                    # again after the timeout check. This ensures that if another thread already
                    # transitioned the state between our first check and now, we won't transition
                    # again. This is critical because the timeout check is a "read" operation
                    # that multiple threads could pass simultaneously before any transitions occur.
                    #
                    # TIMELINE EXAMPLE:
                    # Thread 1: Sees OPEN state, checks timeout (expired), about to transition
                    # Thread 2: Also sees OPEN state, checks timeout (expired), waiting for lock
                    # Thread 1: Transitions to HALF_OPEN, releases lock
                    # Thread 2: Acquires lock, but now the second state check prevents duplicate transition
                    if self._state == CircuitState.OPEN:
                        self._transition_to_half_open()
                        return self._allow_half_open_request()
                return False  # Still in timeout period - reject

            # HALF_OPEN state - limited testing
            return self._allow_half_open_request()

    def _allow_half_open_request(self) -> bool:
        """Check if half-open request should be allowed.

        Note: This method assumes it's already called within _lock context.
        The lock is acquired by the calling method (_allow_request).

        CRITICAL FIX: The increment MUST happen atomically with the check
        to prevent race conditions where multiple threads could all pass
        the check before any increment occurs.

        IMPORTANT: half_open_permits tracks CONCURRENT requests, not total requests.
        Once a request completes (success or failure), permits are decremented.
        But we should only allow new requests if we haven't reached the total
        number of test requests for this HALF_OPEN cycle.
        """
        # Already within _lock context from _allow_request
        # FIXED: Limit total test requests during HALF_OPEN, not just concurrent
        if self._half_open_total_attempts < self.config.half_open_requests:
            self._half_open_permits += 1
            self._half_open_total_attempts += 1
            return True
        return False

    def _on_success(self):
        """Handle successful operation."""
        with self._lock:
            if self._state == CircuitState.HALF_OPEN:
                # Decrement permits as request completes
                self._half_open_permits = max(0, self._half_open_permits - 1)
                self._success_count += 1
                if self._success_count >= self.config.success_threshold:
                    self._transition_to_closed()

    def _on_failure(self, error: Exception):
        """Handle failed operation."""
        with self._lock:
            # Decrement permits if in HALF_OPEN state
            if self._state == CircuitState.HALF_OPEN:
                self._half_open_permits = max(0, self._half_open_permits - 1)

            self._failure_count += 1
            self._last_failure_time = time.time()

            if self._state == CircuitState.CLOSED:
                if self._failure_count >= self.config.failure_threshold:
                    self._transition_to_open()
            elif self._state == CircuitState.HALF_OPEN:
                self._transition_to_open()

    def _transition_to_closed(self):
        """Transition to CLOSED state."""
        self._state = CircuitState.CLOSED
        self._failure_count = 0
        self._success_count = 0
        self._half_open_permits = 0  # Reset permit counter
        self._half_open_total_attempts = 0  # Reset attempt counter
        circuit_breaker_state.labels(namespace=self.namespace).set(CircuitState.CLOSED.value)
        logger.info(f"Circuit breaker {self.namespace} transitioned to CLOSED")

    def _transition_to_open(self):
        """Transition to OPEN state."""
        self._state = CircuitState.OPEN
        self._success_count = 0
        circuit_breaker_state.labels(namespace=self.namespace).set(CircuitState.OPEN.value)
        logger.warning(f"Circuit breaker {self.namespace} transitioned to OPEN")

    def _transition_to_half_open(self):
        """Transition to HALF_OPEN state."""
        self._state = CircuitState.HALF_OPEN
        self._success_count = 0
        self._half_open_permits = 0
        self._half_open_total_attempts = 0  # Reset attempt counter for new HALF_OPEN cycle
        circuit_breaker_state.labels(namespace=self.namespace).set(CircuitState.HALF_OPEN.value)
        logger.info(f"Circuit breaker {self.namespace} transitioned to HALF_OPEN")

    @property
    def state(self) -> CircuitState:
        """Get current circuit breaker state."""
        with self._lock:
            return self._state

    @property
    def failure_count(self) -> int:
        """Get current failure count."""
        with self._lock:
            return self._failure_count

    @property
    def success_count(self) -> int:
        """Get current success count (in HALF_OPEN state)."""
        with self._lock:
            return self._success_count

    def reset(self):
        """Reset circuit breaker to CLOSED state.

        This method allows manual recovery of the circuit breaker,
        useful for administrative operations or testing.
        """
        with self._lock:
            self._transition_to_closed()
            logger.info(f"Circuit breaker {self.namespace} manually reset to CLOSED")

    def record_failure(self, error: Optional[Exception] = None):
        """Record a failure for testing purposes.

        This method is primarily intended for unit testing the circuit breaker's
        state machine logic. In production code, failures are recorded automatically
        through the call() method.

        Args:
            error: Optional exception to record. If not provided, uses a generic Exception.
        """
        self._on_failure(error or Exception("Test failure"))

    def record_success(self):
        """Record a success for testing purposes.

        This method is primarily intended for unit testing the circuit breaker's
        state machine logic. In production code, successes are recorded automatically
        through the call() method.
        """
        self._on_success()

    def should_attempt_call(self) -> bool:
        """Check if a call should be attempted (for testing).

        This method is primarily intended for unit testing the circuit breaker's
        request-allowing logic. It returns whether the circuit breaker would allow
        a request in its current state.

        Returns:
            True if the circuit breaker would allow a request, False otherwise.
        """
        return self._allow_request()

    def get_state(self) -> CircuitState:
        """Get current circuit breaker state (alias for state property)."""
        return self.state

    def get_stats(self) -> dict:
        """Get current circuit breaker statistics.

        Returns:
            Dictionary with current state and counters
        """
        with self._lock:
            return {
                "state": self._state.name,
                "failure_count": self._failure_count,
                "success_count": self._success_count,
                "half_open_permits": self._half_open_permits,
                "last_failure_time": self._last_failure_time,
                "namespace": self.namespace,
                "config": {
                    "failure_threshold": self.config.failure_threshold,
                    "success_threshold": self.config.success_threshold,
                    "timeout_seconds": self.config.timeout_seconds,
                    "half_open_requests": self.config.half_open_requests,
                },
            }
