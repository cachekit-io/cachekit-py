"""Protocols for health monitoring components.

Defines the interfaces that health-checkable components must implement.
These enable health.py to check components without coupling to their concrete implementations.
"""

from typing import Any, Protocol


class PoolManagerProtocol(Protocol):
    """Protocol for pool managers that can be health checked."""

    def get_pool_statistics(self) -> dict[str, Any]:
        """Get pool statistics.

        Returns:
            Dictionary with pool statistics (utilization_ratio, etc.)
        """
        ...


class CircuitBreakerProtocol(Protocol):
    """Protocol for circuit breakers that can be health checked.

    ``cachekit.reliability.CircuitBreaker`` implements it.
    """

    def get_stats(self) -> dict[str, Any]:
        """Get a consistent snapshot of the breaker.

        Returns:
            Dictionary with at least ``state`` (the state name: ``"CLOSED"``, ``"OPEN"`` or
            ``"HALF_OPEN"``), ``failure_count``, ``success_count`` and ``last_failure_time``
            (epoch seconds of the last counted failure, ``0.0`` before the first).
        """
        ...


__all__ = [
    "CircuitBreakerProtocol",
    "PoolManagerProtocol",
]
