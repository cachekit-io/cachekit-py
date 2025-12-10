"""InvalidationChannel protocol for cross-pod cache invalidation.

This module defines the protocol for broadcasting invalidation events across
multiple L1 cache instances. Implementations may use Redis Pub/Sub, HTTP SSE,
WebSockets, or other messaging systems.

Protocol uses structural subtyping (PEP 544) - any class implementing these
methods is considered a valid InvalidationChannel.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Callable, Protocol

if TYPE_CHECKING:
    pass

# Type aliases for invalidation system
InvalidationCallback = Callable[[Any], None]
"""Callback invoked when invalidation event is received.

Args:
    event: The InvalidationEvent containing invalidation details

Note:
    Callback must not raise exceptions. Exceptions are logged and suppressed
    to prevent breaking the invalidation listener.
"""


class InvalidationChannel(Protocol):
    """Protocol for broadcasting cache invalidation events.

    This protocol defines the contract for invalidation channels that enable
    cross-pod L1 cache invalidation. All methods must be thread-safe.

    Design principles:
    - Fire-and-forget publishing (never raises, never blocks)
    - Multiple subscribers supported (fan-out)
    - Graceful degradation (cache works even if channel unavailable)
    - Idempotent lifecycle (start/stop can be called multiple times)

    Example:
        >>> from cachekit.invalidation import RedisInvalidationChannel
        >>> channel = RedisInvalidationChannel(redis_client)  # doctest: +SKIP
        >>> channel.subscribe(lambda event: cache.handle_invalidation(event))  # doctest: +SKIP
        >>> channel.start()  # doctest: +SKIP
        >>> channel.stop()  # doctest: +SKIP
    """

    def publish(self, event: Any) -> None:
        """Publish invalidation event to all subscribers.

        This is a fire-and-forget operation that never raises exceptions
        and never blocks. Failures are logged but do not propagate.

        Args:
            event: The InvalidationEvent to broadcast

        Note:
            If channel is unavailable, event is dropped and logged. This is
            acceptable because L1 cache entries eventually expire via TTL.
            Cross-pod invalidation is a best-effort optimization.
        """
        ...

    def subscribe(self, callback: InvalidationCallback) -> None:
        """Register callback to receive invalidation events.

        Multiple callbacks may be registered. All callbacks are invoked
        when an event is received. Callbacks are invoked synchronously
        in the listener thread.

        Args:
            callback: Function to invoke when event is received

        Note:
            Callback exceptions are caught, logged, and suppressed to prevent
            breaking the listener. Callback should complete quickly (<10ms)
            to avoid blocking other subscribers.
        """
        ...

    def start(self) -> None:
        """Start the invalidation channel listener.

        This is idempotent - calling multiple times has no effect if already
        started. The listener runs in a background thread and automatically
        reconnects if connection is lost.

        Raises:
            RuntimeError: If channel fails to start (e.g., connection error)

        Note:
            Implementations should start a daemon thread so process can exit
            cleanly even if listener is running.
        """
        ...

    def stop(self) -> None:
        """Stop the invalidation channel listener.

        Blocks until listener thread terminates (max 5 seconds). This is
        idempotent - calling multiple times has no effect if already stopped.

        Note:
            After stop(), channel can be restarted via start(). Pending
            callbacks may still execute during shutdown.
        """
        ...

    def is_available(self) -> bool:
        """Check if channel is operational.

        Returns:
            True if channel is connected and ready to publish/receive events
            False if channel is disconnected or unavailable

        Note:
            This is a snapshot check. Channel may become unavailable
            immediately after returning True. Use for health checks and
            metrics, not for critical logic.
        """
        ...
