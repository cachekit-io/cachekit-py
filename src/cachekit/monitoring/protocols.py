"""Protocols for health monitoring components.

Defines the interfaces that health-checkable components must implement.
These enable health.py to check components without coupling to their concrete implementations.
"""

from datetime import datetime
from typing import Any, Optional, Protocol


class PoolManagerProtocol(Protocol):
    """Protocol for pool managers that can be health checked."""

    def get_pool_statistics(self) -> dict[str, Any]:
        """Get pool statistics.

        Returns:
            Dictionary with pool statistics (utilization_ratio, etc.)
        """
        ...


class CircuitBreakerProtocol(Protocol):
    """Protocol for circuit breakers that can be health checked."""

    @property
    def state(self) -> str:
        """Get circuit breaker state (OPEN, HALF_OPEN, CLOSED)."""
        ...

    @property
    def failure_count(self) -> int:
        """Get failure count."""
        ...

    @property
    def success_count(self) -> int:
        """Get success count."""
        ...

    @property
    def last_failure_time(self) -> Optional[datetime]:
        """Get last failure time."""
        ...


__all__ = [
    "CircuitBreakerProtocol",
    "PoolManagerProtocol",
]
