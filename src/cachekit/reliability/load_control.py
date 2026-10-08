"""Load control and backpressure management for cache operations.

This module provides backpressure control to prevent overloading backend instances
and protect against cascading failures caused by request queuing.

Key Features:
- Semaphore-based concurrency limiting
- Queue depth tracking and rejection counting
- Context manager interface for clean resource management
- Prometheus metrics integration for monitoring

The BackpressureController implements a two-level protection mechanism:
1. Queue depth limiting: Prevents unbounded memory growth
2. Concurrency limiting: Prevents overwhelming backend connections

Example:
    controller = BackpressureController(max_concurrent=50)

    try:
        with controller.acquire():
            backend.get("key")  # Protected operation
    except BackendError:
        # Request rejected due to overload
        return cached_fallback
"""

import threading
from contextlib import ContextDecorator
from typing import Optional

from prometheus_client import Counter

from cachekit.backends.errors import BackendError, BackendErrorType


def _get_or_create_metric(metric_class, name: str, description: str, label_names: Optional[list] = None):
    """Get existing metric or create new one.

    This function handles the case where metrics might already be registered
    in the Prometheus registry, which can happen during testing or when
    multiple modules import the same metrics.
    """
    from prometheus_client import REGISTRY

    try:
        return metric_class(name, description, labelnames=label_names or [])
    except ValueError:
        # Metric already registered, return existing instance
        return REGISTRY._names_to_collectors[name]


# Prometheus metrics for backpressure monitoring
cache_operations = _get_or_create_metric(
    Counter,
    "redis_cache_operations_total",
    "Total cache operations by type and status. Use this to calculate hit rates, "
    "error rates, and operation distribution. Labels allow filtering by namespace "
    "for multi-tenant scenarios.",
    ["operation", "status", "serializer", "namespace"],
)


class BackpressureController:
    """Control request rate to prevent overload.

    Implements a two-level protection mechanism:
    1. Queue depth limiting: Prevents unbounded memory growth
    2. Concurrency limiting: Prevents overwhelming backend connections

    This pattern is crucial for preventing cascading failures where
    slow backend responses lead to request queuing, memory exhaustion,
    and eventual system crash.

    Example scenario:
        - Backend slows from 10ms to 1000ms response time
        - Without backpressure: 1000s of requests queue up
        - With backpressure: After 100 concurrent + 1000 queued, new requests rejected
        - System remains stable and can recover when backend speeds up

    Usage:
        controller = BackpressureController(max_concurrent=50)

        try:
            with controller.acquire():
                backend.get("key")  # Protected operation
        except BackendError:
            # Request rejected due to overload
            return cached_fallback

    Examples:
        Create controller with defaults:

        >>> controller = BackpressureController()
        >>> controller.max_concurrent
        100
        >>> controller.queue_size
        1000

        Check queue depth and rejected count:

        >>> controller.queue_depth
        0
        >>> controller.rejected_count
        0

        Get stats:

        >>> stats = controller.get_stats()
        >>> stats["healthy"]
        True
        >>> stats["max_concurrent"]
        100

        Reset stats:

        >>> controller.reset_stats()
        >>> controller.rejected_count
        0
    """

    def __init__(self, max_concurrent: int = 100, queue_size: int = 1000, timeout: float = 0.1):
        self.max_concurrent = max_concurrent  # Max backend operations in flight
        self.queue_size = queue_size  # Max requests waiting for permit
        self.timeout = timeout  # How long to wait for permit
        self._semaphore = threading.Semaphore(max_concurrent)  # Concurrency control
        self._queue_depth = 0  # Current requests waiting
        self._rejected_count = 0  # Monitoring metric
        self._lock = threading.Lock()  # Protect queue depth counter

    def acquire(self) -> "_Permit":
        """Acquire permit with backpressure protection.

        Returns a context manager that implements a two-phase check on entry:
        1. Queue admission control: Reject if too many requests waiting
        2. Concurrency control: Wait for available permit (with timeout)

        The permit is released on every exit from the ``with`` block,
        even if the protected operation fails.

        Raises (on entering the ``with`` block):
            BackendError: If queue full or timeout acquiring permit
        """
        return _Permit(self)

    @property
    def queue_depth(self) -> int:
        """Current number of requests waiting in queue."""
        with self._lock:
            return self._queue_depth

    @property
    def rejected_count(self) -> int:
        """Total number of requests rejected due to backpressure."""
        with self._lock:
            return self._rejected_count

    def reset_stats(self) -> None:
        """Reset monitoring statistics."""
        with self._lock:
            self._rejected_count = 0

    def get_stats(self) -> dict:
        """Get backpressure statistics."""
        with self._lock:
            return {
                "queue_depth": self._queue_depth,
                "rejected_count": self._rejected_count,
                "max_concurrent": self.max_concurrent,
                "queue_size": self.queue_size,
                "healthy": self._queue_depth < self.queue_size * 0.8,
            }


class _Permit(ContextDecorator):
    """One ``BackpressureController.acquire()`` call.

    A class, not a ``@contextmanager`` generator: this runs on every backend
    operation, and a generator context manager costs several times the
    semaphore it guards. It holds no permit state - ``__exit__`` runs only after
    ``__enter__`` returned, which means a permit is held - so reuse as a
    decorator (``ContextDecorator``) is safe across concurrent calls.
    """

    def __init__(self, controller: BackpressureController):
        self._controller = controller

    def __enter__(self) -> None:
        c = self._controller
        # Phase 1: Check if we can join the queue
        with c._lock:
            if c._queue_depth >= c.queue_size:
                c._rejected_count += 1
                cache_operations.labels(  # type: ignore[attr-defined]
                    operation="backpressure",
                    status="rejected",
                    serializer="",
                    namespace="",
                ).inc()
                raise BackendError("Request queue full", error_type=BackendErrorType.TRANSIENT)
            c._queue_depth += 1  # We're now in the queue

        # Phase 2: Try to acquire execution permit. We leave the queue however the
        # wait ends: permit, timeout, or an exception (KeyboardInterrupt included).
        try:
            acquired = c._semaphore.acquire(timeout=c.timeout)
        finally:
            with c._lock:
                c._queue_depth -= 1
        if not acquired:
            with c._lock:
                c._rejected_count += 1
            raise BackendError("Failed to acquire permit", error_type=BackendErrorType.TIMEOUT)

    def __exit__(self, *exc_info: object) -> None:
        self._controller._semaphore.release()
