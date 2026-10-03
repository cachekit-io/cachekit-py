"""Circuit breaker implementation for preventing cascading failures.

This module provides a production-ready circuit breaker implementation following
the classic Circuit Breaker pattern. The circuit breaker monitors error rates
and temporarily blocks requests when a service is struggling, giving it time
to recover.

Every state read and transition happens under a single RLock, except the Prometheus
scrape, which reads each live breaker's ``_state`` as a single lock-free attribute read.
"""

import functools
import logging
import math
import os
import threading
import time
import weakref
from collections import deque
from dataclasses import dataclass
from enum import Enum
from time import monotonic
from typing import Optional

# Import backend error types for failure detection
from cachekit.backends.errors import BackendError, BackendErrorType
from cachekit.reliability.async_metrics import PROMETHEUS_AVAILABLE, circuit_breaker_gauge

# Legacy in-process 0/1/2 value (last writer per namespace wins). Not the Prometheus series.
from cachekit.reliability.metrics_collection import circuit_breaker_state as _legacy_state_value

logger = logging.getLogger(__name__)

# CLOSED opens on failure_threshold failures within this rolling window, the default
# in cachekit-ts and cachekit-rs. A plain count since the last close would let rare
# failures on a healthy backend open the breaker on any long-lived process. The window
# runs on the monotonic clock, so a wall-clock correction cannot keep old failures in it.
_FAILURE_WINDOW_SECONDS = 60.0


class CircuitState(Enum):
    """Circuit breaker states following the classic pattern.

    State transitions:
    CLOSED -> OPEN: When failure_threshold failures fall within a 60 s rolling window
    OPEN -> HALF_OPEN: After timeout_seconds have elapsed
    HALF_OPEN -> CLOSED: After success_threshold successful requests
    HALF_OPEN -> OPEN: On any failure during testing
    HALF_OPEN -> HALF_OPEN: A fresh probe cycle, when the probe budget is spent
        and no outcome has ended the cycle within timeout_seconds

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
        failure_threshold: Number of failures within a 60 s rolling window that opens
            the circuit. Successes do not reset the count; failures older than the
            window stop counting. Lower values make the circuit more sensitive to errors.
        success_threshold: Number of consecutive successes in HALF_OPEN before closing.
            Higher values ensure more stable recovery.
        timeout_seconds: How long to stay OPEN before testing recovery. It also bounds
            a HALF_OPEN cycle: once the cycle has admitted all its probes and began
            more than timeout_seconds ago without closing or reopening, a fresh cycle
            starts. Must be finite and > 0: NaN or inf never leaves OPEN, and that
            restart caps probing at half_open_requests per timeout_seconds. Balance
            between giving service time to recover vs detecting recovery quickly.
        half_open_requests: Probe requests admitted per HALF_OPEN cycle (a total,
            not a concurrency limit; a probe handed back with release_probe does not
            count against it). Keep it >= success_threshold, or a cycle can
            never collect enough successes from its probes to close. Unlike the
            @cache config, this class does not reject a smaller value.
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

    failure_threshold: int = 5  # Opens circuit after 5 failures within 60 s
    success_threshold: int = 3  # Closes circuit after 3 consecutive successes
    timeout_seconds: float = 30.0  # Wait 30s before testing recovery
    half_open_requests: int = 3  # Probes per HALF_OPEN cycle; must reach success_threshold to close
    excluded_error_types: tuple[BackendErrorType, ...] = ()  # No excluded error types by default

    def __post_init__(self):
        """Validate configuration."""
        # Validate thresholds
        if self.failure_threshold <= 0:
            raise ValueError(f"failure_threshold must be positive, got {self.failure_threshold}")
        if self.success_threshold <= 0:
            raise ValueError(f"success_threshold must be positive, got {self.success_threshold}")
        # NaN or inf would keep the breaker OPEN forever (NaN compares False). Zero
        # would let a spent HALF_OPEN cycle restart on every clock tick, removing
        # the half_open_requests-per-timeout cap on probes.
        if not math.isfinite(self.timeout_seconds) or self.timeout_seconds <= 0:
            raise ValueError(f"timeout_seconds must be a finite number > 0, got {self.timeout_seconds!r}")
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


# Live breakers per namespace. The gauge reads them at scrape time, so nothing calls into
# prometheus_client at GC time, where it could re-enter its non-reentrant locks on the same
# thread and hang. The only GC-time work is _on_collected's deque append.
_live_breakers: dict[str, "weakref.WeakSet[CircuitBreaker]"] = {}
# One lock per process, keyed by pid: a C-level fork skips at-fork hooks, so a child must
# never take a lock it inherited, possibly held by a thread that is gone.
_live_locks: dict[int, threading.Lock] = {}
# Namespaces that lost a breaker. Appended at GC time, which must take no lock;
# deque.append takes none. _track drains it to retire namespaces with no live breaker.
_lost_namespaces: deque[str] = deque()
_breaker_refs: set["weakref.ref[CircuitBreaker]"] = set()


def _on_collected(namespace: str, ref: "weakref.ref[CircuitBreaker]") -> None:
    _breaker_refs.discard(ref)
    _lost_namespaces.append(namespace)


def _live_lock() -> threading.Lock:
    pid = os.getpid()
    lock = _live_locks.get(pid)
    if lock is None:
        lock = _live_locks.setdefault(pid, threading.Lock())  # atomic, so racing threads share one
    return lock


# prometheus_client's own switch, made once when it is imported: either variable present,
# even empty. Its multiprocess collector would export a function gauge as 0, a false
# "healthy", so the gauge is left out there instead.
_MULTIPROCESS_MODE = "PROMETHEUS_MULTIPROC_DIR" in os.environ or "prometheus_multiproc_dir" in os.environ


def _count_in_state(namespace: str, state: "CircuitState") -> int:
    with _live_lock():
        # .get: a scrape can call a series that _track retired after the scrape copied it.
        breakers = list(_live_breakers.get(namespace, ()))
    return sum(1 for breaker in breakers if breaker._state is state)


def _track(breaker: "CircuitBreaker") -> None:
    """Count ``breaker`` in the ``circuit_breaker_state`` gauge for as long as it lives.

    Also retires namespaces whose breakers have all been collected, so transient namespaces
    do not leave series behind. That happens here, on an ordinary call, never at GC time; a
    dead namespace exports zeros until the next breaker is created. Gauge calls stay under
    _live_lock so a retirement cannot remove a namespace another thread is re-registering.
    """
    gauge = circuit_breaker_gauge() if PROMETHEUS_AVAILABLE and not _MULTIPROCESS_MODE else None
    with _live_lock():
        _breaker_refs.add(weakref.ref(breaker, functools.partial(_on_collected, breaker.namespace)))
        lost = set()
        while _lost_namespaces:
            lost.add(_lost_namespaces.popleft())
        lost.discard(breaker.namespace)
        dead = [ns for ns in lost if ns in _live_breakers and not _live_breakers[ns]]
        for ns in dead:
            del _live_breakers[ns]
        first = breaker.namespace not in _live_breakers
        _live_breakers.setdefault(breaker.namespace, weakref.WeakSet()).add(breaker)
        if gauge is None:
            return
        for state in CircuitState:
            for ns in dead:
                gauge.remove(ns, state.name)
            if first:
                gauge.labels(namespace=breaker.namespace, state=state.name).set_function(
                    functools.partial(_count_in_state, breaker.namespace, state)
                )


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

        # Guard the operation with the breaker's state, then record the outcome.
        # This is the split check/record pattern the SDK uses internally
        # (see FeatureOrchestrator.should_allow_request / record_success /
        # record_failure): the caller does its own work between the check and
        # the record, which a single wrapping call() cannot accommodate.
        if not breaker.should_attempt_call():
            return cached_value  # Fail fast — circuit is open
        try:
            result = redis_client.get("key")
            breaker.record_success()
            return result
        except redis.ConnectionError as err:
            breaker.record_failure(err)
            raise
    """

    def __init__(self, config: CircuitBreakerConfig, namespace: str = "default"):
        self.config = config
        self.namespace = namespace
        self._state = CircuitState.CLOSED
        self._failure_times: deque[float] = deque()  # monotonic() failure times, pruned to the window
        self._success_count = 0  # Consecutive successes in HALF_OPEN state
        self._last_failure_time = 0.0  # Timestamp of last failure (for timeout)
        self._half_open_permits = 0  # Current test requests in HALF_OPEN
        self._half_open_total_attempts = 0  # Total requests attempted in HALF_OPEN cycle
        self._half_open_since = 0.0  # When the current HALF_OPEN cycle started
        self._half_open_cycle = 0  # Number of the latest HALF_OPEN cycle (0 before the first); admit() hands it out
        self._lock = threading.RLock()  # Reentrant lock for thread safety

        _legacy_state_value.labels(namespace=namespace).set(self._state.value)
        _track(self)

    def _allow_request(self) -> bool:
        """Check if request should be allowed based on circuit state.

        Decision flow:
        1. CLOSED: Always allow (normal operation)
        2. OPEN: Check if timeout expired
           - If yes: Transition to HALF_OPEN and check permits
           - If no: Reject request
        3. HALF_OPEN: Check if test permits available
           - If the budget is spent and the cycle is older than timeout_seconds:
             start a fresh cycle and admit

        Thread-safe: the check and any transition run under one hold of ``_lock``.
        """
        with self._lock:
            current_time = time.time()

            if self._state == CircuitState.CLOSED:
                return True  # Normal operation - allow all requests

            if self._state == CircuitState.OPEN:
                # Check if we've waited long enough to test recovery
                if current_time - self._last_failure_time > self.config.timeout_seconds:
                    self._transition_to_half_open()
                    return self._allow_half_open_request()
                return False  # Still in timeout period - reject

            # HALF_OPEN state - limited testing.
            # A cycle ends only when a probe records an outcome. A probe that exits
            # without one (a cancelled async call, a fail-closed raise) would hold
            # its slot forever, so a spent cycle that outlives timeout_seconds
            # starts over instead of rejecting every call until restart.
            budget_spent = self._half_open_total_attempts >= self.config.half_open_requests
            if budget_spent and current_time - self._half_open_since > self.config.timeout_seconds:
                self._transition_to_half_open()
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
        """Handle failed operation.

        Failures whose ``error_type`` is listed in ``config.excluded_error_types``
        (e.g. PERMANENT config errors) do not count toward tripping the breaker.
        The exclusion is enforced here — the single point every path routes
        through (the orchestrator's live ``record_failure`` and the public
        ``record_failure`` helper) — so it holds everywhere, not just in a
        dedicated wrapper.
        """
        # Excluded error types never count as failures.
        if isinstance(error, BackendError) and error.error_type in self.config.excluded_error_types:
            return

        with self._lock:
            # Guard clause: already OPEN. A failure recorded now comes from a call
            # admitted before the breaker opened. Counting it would push
            # _last_failure_time forward and keep the breaker OPEN under steady
            # traffic.
            if self._state == CircuitState.OPEN:
                return

            # Decrement permits if in HALF_OPEN state
            if self._state == CircuitState.HALF_OPEN:
                self._half_open_permits = max(0, self._half_open_permits - 1)

            now = monotonic()
            self._failure_times.append(now)
            self._prune_failures(now)
            self._last_failure_time = time.time()

            if self._state == CircuitState.CLOSED:
                if len(self._failure_times) >= self.config.failure_threshold:
                    self._transition_to_open()
            elif self._state == CircuitState.HALF_OPEN:
                self._transition_to_open()

    def _prune_failures(self, now: float):
        """Drop failure timestamps older than the rolling window. Caller holds _lock."""
        while self._failure_times and self._failure_times[0] < now - _FAILURE_WINDOW_SECONDS:
            self._failure_times.popleft()

    def _transition_to_closed(self):
        """Transition to CLOSED state."""
        self._state = CircuitState.CLOSED
        self._failure_times.clear()
        self._success_count = 0
        self._half_open_permits = 0  # Reset permit counter
        self._half_open_total_attempts = 0  # Reset attempt counter
        _legacy_state_value.labels(namespace=self.namespace).set(CircuitState.CLOSED.value)
        logger.info(f"Circuit breaker {self.namespace} transitioned to CLOSED")

    def _transition_to_open(self):
        """Transition to OPEN state."""
        self._state = CircuitState.OPEN
        self._success_count = 0
        _legacy_state_value.labels(namespace=self.namespace).set(CircuitState.OPEN.value)
        logger.warning(f"Circuit breaker {self.namespace} transitioned to OPEN")

    def _transition_to_half_open(self):
        """Transition to HALF_OPEN state."""
        self._state = CircuitState.HALF_OPEN
        self._success_count = 0
        self._half_open_permits = 0
        self._half_open_total_attempts = 0  # Reset attempt counter for new HALF_OPEN cycle
        self._half_open_since = time.time()
        self._half_open_cycle += 1
        _legacy_state_value.labels(namespace=self.namespace).set(CircuitState.HALF_OPEN.value)
        logger.info(f"Circuit breaker {self.namespace} transitioned to HALF_OPEN")

    @property
    def state(self) -> CircuitState:
        """Get current circuit breaker state."""
        with self._lock:
            return self._state

    @property
    def failure_count(self) -> int:
        """Get the number of failures within the rolling window."""
        with self._lock:
            self._prune_failures(monotonic())
            return len(self._failure_times)

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
        """Record a failed operation.

        Public helper for tests and direct users of CircuitBreaker; delegates to
        the internal ``_on_failure``. The SDK's own reliability path records
        outcomes through that same internal method (driven by
        FeatureOrchestrator), so errors whose ``error_type`` is in
        ``config.excluded_error_types`` never count toward tripping the breaker.

        Args:
            error: Optional exception to record. If not provided, uses a generic Exception.
        """
        self._on_failure(error or Exception("Test failure"))

    def record_success(self):
        """Record a successful operation.

        Public helper for tests and direct users of CircuitBreaker; delegates to
        the internal ``_on_success``. The SDK's own reliability path records
        outcomes through that same internal method (driven by FeatureOrchestrator).
        """
        self._on_success()

    def release_probe(self, cycle: int):
        """Give back the probe slot of an admitted call that ends with no outcome.

        For an exit that says nothing about backend health, such as the decorated
        function raising. Recording nothing would leave its HALF_OPEN slot spent, and
        every call would be rejected until the cycle expires.

        Args:
            cycle: What ``admit`` returned for the call. Only a call admitted by the
                current HALF_OPEN cycle gets its slot back, for the next call. A call
                admitted while CLOSED, or by an earlier cycle, holds no slot of this
                one, and refunding it would let probes past ``half_open_requests``.
                A release after the call's own cycle ended changes nothing: every
                cycle starts from a fresh budget.
        """
        with self._lock:
            if cycle != self._half_open_cycle:
                return
            self._half_open_permits = max(0, self._half_open_permits - 1)
            self._half_open_total_attempts = max(0, self._half_open_total_attempts - 1)

    def admit(self) -> Optional[int]:
        """Admit or reject a call, and say which HALF_OPEN cycle admitted it.

        The same decision as ``should_attempt_call``, for a caller that may need
        ``release_probe``.

        Returns:
            None if the call must fail fast. Otherwise the number of the latest
            HALF_OPEN cycle, which admitted it if the breaker is HALF_OPEN: 0 before
            the first cycle, and falsy, so test the result with ``is None``.
        """
        with self._lock:
            return self._half_open_cycle if self._allow_request() else None

    def should_attempt_call(self) -> bool:
        """Admit or reject a call — the live admission check.

        ``FeatureOrchestrator.should_allow_request`` calls this for every
        decorated call. It is not a pure query: once ``timeout_seconds`` has
        passed since the breaker opened it moves OPEN to HALF_OPEN, and in
        HALF_OPEN each ``True`` consumes one of the cycle's
        ``half_open_requests`` probe slots. A spent cycle that no outcome has
        ended within ``timeout_seconds`` starts over with a fresh budget. Record
        the outcome of every admitted call with ``record_success`` /
        ``record_failure``; never record a rejection. A call that may end with no
        outcome is admitted with ``admit`` instead, so it can ``release_probe``.

        Returns:
            True if the call may proceed, False if it must fail fast.
        """
        return self.admit() is not None

    def get_state(self) -> CircuitState:
        """Get current circuit breaker state (alias for state property)."""
        return self.state

    def get_stats(self) -> dict:
        """Get current circuit breaker statistics.

        Returns:
            Dictionary with current state and counters
        """
        with self._lock:
            self._prune_failures(monotonic())
            return {
                "state": self._state.name,
                "failure_count": len(self._failure_times),
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
