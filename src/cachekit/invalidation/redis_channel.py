"""Redis Pub/Sub implementation of InvalidationChannel protocol.

This module provides production-ready cross-pod L1 cache invalidation using
Redis Pub/Sub messaging. Supports multiple subscribers with automatic
reconnection and exponential backoff.

IMPORTANT: Redis Pub/Sub is at-most-once delivery (fire-and-forget). Messages
published while a subscriber is disconnected are lost. This is acceptable for
cache invalidation because L1 entries eventually expire via TTL. Cross-pod
invalidation is a best-effort optimization for reducing stale cache window.

Architecture:
- Dedicated PubSub connection (separate from main Redis client)
- Daemon listener thread (name="cachekit-invalidation-listener")
- threading.RLock for state protection
- Exponential backoff reconnection (1.0s -> 2.0s -> 4.0s -> ... -> 30.0s max)
- Optional metrics hooks via self._metrics attribute
"""

from __future__ import annotations

import logging
import threading
import time
from typing import TYPE_CHECKING, Any, Optional

import redis

from cachekit.invalidation.event import InvalidationEvent

if TYPE_CHECKING:
    from cachekit.invalidation.channel import InvalidationCallback

logger = logging.getLogger(__name__)

# Redis Pub/Sub channel name for invalidation events
INVALIDATION_CHANNEL = "cachekit:invalidation"

# Reconnection backoff parameters
INITIAL_BACKOFF = 1.0  # Start with 1 second
MAX_BACKOFF = 30.0  # Cap at 30 seconds
BACKOFF_MULTIPLIER = 2.0  # Exponential backoff


class RedisInvalidationChannel:
    """Redis Pub/Sub implementation of InvalidationChannel protocol.

    Provides cross-pod cache invalidation using Redis Pub/Sub messaging.
    Implements the InvalidationChannel protocol with automatic reconnection
    and exponential backoff.

    IMPORTANT: This implementation provides at-most-once delivery semantics.
    Messages may be lost during network partitions or subscriber restarts.
    This is acceptable for cache invalidation - entries expire via TTL anyway.

    Thread Safety:
        All public methods are thread-safe via threading.RLock.

    Examples:
        >>> from cachekit.invalidation import RedisInvalidationChannel, InvalidationEvent, InvalidationLevel
        >>> import redis
        >>> client = redis.Redis.from_url("redis://localhost:6379")  # doctest: +SKIP
        >>> channel = RedisInvalidationChannel(client)  # doctest: +SKIP
        >>> channel.subscribe(lambda event: print(f"Received: {event}"))  # doctest: +SKIP
        >>> channel.start()  # doctest: +SKIP
        >>> event = InvalidationEvent(level=InvalidationLevel.GLOBAL, namespace=None, params_hash=None)  # doctest: +SKIP
        >>> channel.publish(event)  # doctest: +SKIP
        >>> channel.stop()  # doctest: +SKIP
    """

    def __init__(
        self,
        redis_client: redis.Redis,
        channel_name: str = INVALIDATION_CHANNEL,
        metrics: Optional[Any] = None,
    ):
        """Initialize RedisInvalidationChannel.

        Args:
            redis_client: Redis client instance (used to create dedicated PubSub connection)
            channel_name: Redis channel name for invalidation messages (default: "cachekit:invalidation")
            metrics: Optional metrics collector (must have inc() method)

        Note:
            Creates a dedicated PubSub connection separate from the main Redis client
            to avoid blocking cache operations.
        """
        self._redis_client = redis_client
        self._channel_name = channel_name
        self._metrics = metrics

        # State protected by RLock
        self._lock = threading.RLock()
        self._callbacks: list[InvalidationCallback] = []
        self._listener_thread: Optional[threading.Thread] = None
        self._running = False
        self._pubsub: Any = None  # redis.client.PubSub type not exported
        self._backoff = INITIAL_BACKOFF

    def publish(self, event: Any) -> None:
        """Publish invalidation event to all subscribers.

        This is a fire-and-forget operation that never raises exceptions
        and never blocks. Failures are logged but do not propagate.

        Args:
            event: InvalidationEvent to broadcast

        Note:
            If Redis is unavailable, event is dropped and logged at CRITICAL level.
            This is acceptable because L1 cache entries eventually expire via TTL.

        Examples:
            >>> from cachekit.invalidation import InvalidationEvent, InvalidationLevel
            >>> channel = RedisInvalidationChannel(redis_client)  # doctest: +SKIP
            >>> event = InvalidationEvent(level=InvalidationLevel.GLOBAL, namespace=None, params_hash=None)  # doctest: +SKIP
            >>> channel.publish(event)  # Never raises, never blocks  # doctest: +SKIP
        """
        try:
            # Serialize event to bytes
            data = event.to_bytes()

            # Publish to Redis (returns subscriber_count)
            subscriber_count = self._redis_client.publish(self._channel_name, data)

            # Warn if no subscribers (message was lost)
            if subscriber_count == 0:
                logger.warning(
                    "Published invalidation but no subscribers listening (message lost)",
                    extra={"level": event.level.value, "namespace": event.namespace},
                )
                if self._metrics:
                    self._metrics.inc("invalidation_no_subscribers_total")

            # Success metric
            if self._metrics:
                self._metrics.inc("invalidation_published_total")

        except Exception as e:
            # CRITICAL: Other pods will serve stale data until TTL expires
            logger.error(
                "CRITICAL: Failed to publish invalidation: %s (other pods will serve stale until TTL)",
                e,
                extra={"level": event.level.value, "namespace": event.namespace},
            )
            if self._metrics:
                self._metrics.inc("invalidation_publish_failed_total")

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

        Examples:
            >>> def handle_invalidation(event):  # doctest: +SKIP
            ...     print(f"Cache invalidated: {event}")
            >>> channel.subscribe(handle_invalidation)  # doctest: +SKIP
        """
        with self._lock:
            self._callbacks.append(callback)

    def start(self) -> None:
        """Start the invalidation channel listener.

        This is idempotent - calling multiple times has no effect if already
        started. The listener runs in a background thread and automatically
        reconnects if connection is lost.

        Raises:
            RuntimeError: If channel fails to start (e.g., connection error)

        Note:
            Starts a daemon thread so process can exit cleanly even if
            listener is running.

        Examples:
            >>> channel = RedisInvalidationChannel(redis_client)  # doctest: +SKIP
            >>> channel.start()  # Idempotent  # doctest: +SKIP
            >>> channel.start()  # No-op if already started  # doctest: +SKIP
        """
        with self._lock:
            # Idempotent - no-op if already started
            if self._running:
                return

            try:
                # Create dedicated PubSub connection
                self._pubsub = self._redis_client.pubsub(ignore_subscribe_messages=True)
                self._pubsub.subscribe(self._channel_name)

                # Start daemon listener thread
                self._running = True
                self._listener_thread = threading.Thread(
                    target=self._listen_loop,
                    name="cachekit-invalidation-listener",
                    daemon=True,
                )
                self._listener_thread.start()

                logger.info("Started invalidation channel listener on %s", self._channel_name)

            except Exception as e:
                # Cleanup on failure
                self._running = False
                if self._pubsub:
                    try:
                        self._pubsub.close()
                    except Exception as close_err:
                        logger.debug("Error closing PubSub during start failure cleanup: %s", close_err)
                    self._pubsub = None

                raise RuntimeError(f"Failed to start invalidation channel: {e}") from e

    def stop(self) -> None:
        """Stop the invalidation channel listener.

        Blocks until listener thread terminates (max 5 seconds). This is
        idempotent - calling multiple times has no effect if already stopped.

        Note:
            After stop(), channel can be restarted via start(). Pending
            callbacks may still execute during shutdown.

        Examples:
            >>> channel.stop()  # Blocks until listener exits  # doctest: +SKIP
            >>> channel.stop()  # No-op if already stopped  # doctest: +SKIP
        """
        with self._lock:
            # Idempotent - no-op if already stopped
            if not self._running:
                return

            # Signal thread to stop
            self._running = False

        # Wait for thread to exit (max 5 seconds, without holding lock)
        if self._listener_thread and self._listener_thread.is_alive():
            self._listener_thread.join(timeout=5.0)

        # Cleanup PubSub connection
        with self._lock:
            if self._pubsub:
                try:
                    self._pubsub.unsubscribe(self._channel_name)
                    self._pubsub.close()
                except Exception as e:
                    logger.debug("Error closing PubSub connection during shutdown: %s", e)
                finally:
                    self._pubsub = None

            self._listener_thread = None
            self._backoff = INITIAL_BACKOFF  # Reset backoff for next start

        logger.info("Stopped invalidation channel listener")

    def is_available(self) -> bool:
        """Check if channel is operational.

        Returns:
            True if channel is connected and ready to publish/receive events
            False if channel is disconnected or unavailable

        Note:
            This is a snapshot check. Channel may become unavailable
            immediately after returning True. Use for health checks and
            metrics, not for critical logic.

        Examples:
            >>> channel.start()  # doctest: +SKIP
            >>> channel.is_available()  # doctest: +SKIP
            True
            >>> channel.stop()  # doctest: +SKIP
            >>> channel.is_available()  # doctest: +SKIP
            False
        """
        with self._lock:
            return self._running and self._pubsub is not None

    def _listen_loop(self) -> None:
        """Background listener thread that receives and dispatches events.

        Runs until self._running is set to False. Implements exponential
        backoff reconnection on failure (1s -> 2s -> 4s -> ... -> 30s max).

        Note:
            This method runs in a daemon thread and should never raise exceptions.
            All errors are caught, logged, and trigger reconnection.
        """
        while self._running:
            try:
                # Get message from PubSub (blocking call)
                with self._lock:
                    pubsub = self._pubsub

                if pubsub is None:
                    # Shutting down, exit gracefully
                    break

                # Listen for messages (timeout=1.0 allows checking self._running periodically)
                message = pubsub.get_message(timeout=1.0)

                if message is None:
                    # Timeout, continue loop
                    continue

                # Only process data messages (ignore subscribe/unsubscribe confirmations)
                if message["type"] != "message":
                    continue

                # Deserialize and dispatch event
                try:
                    data = message["data"]

                    # Handle both str and bytes (Redis may decode based on decode_responses)
                    if isinstance(data, str):
                        data = data.encode("utf-8")

                    event = InvalidationEvent.from_bytes(data)

                    # Dispatch to all callbacks (suppress exceptions)
                    with self._lock:
                        callbacks = self._callbacks.copy()

                    for callback in callbacks:
                        try:
                            callback(event)
                        except Exception as e:
                            logger.error("Callback raised exception during invalidation: %s", e, exc_info=True)
                            if self._metrics:
                                self._metrics.inc("invalidation_callback_error_total")

                    # Success - reset backoff
                    with self._lock:
                        self._backoff = INITIAL_BACKOFF

                    if self._metrics:
                        self._metrics.inc("invalidation_received_total")

                except ValueError as e:
                    # Malformed message - log and skip
                    logger.warning("Received malformed invalidation message: %s", e)
                    if self._metrics:
                        self._metrics.inc("invalidation_malformed_total")

            except Exception as e:
                # Connection error or other failure - reconnect with backoff
                logger.error("Invalidation listener error: %s (will reconnect after %s seconds)", e, self._backoff)

                if self._metrics:
                    self._metrics.inc("invalidation_listener_error_total")

                # Sleep before reconnection attempt (without holding lock)
                time.sleep(self._backoff)

                # Exponential backoff
                with self._lock:
                    self._backoff = min(self._backoff * BACKOFF_MULTIPLIER, MAX_BACKOFF)

                # Attempt reconnection
                self._reconnect()

    def _reconnect(self) -> None:
        """Reconnect PubSub connection after failure.

        Closes existing connection and creates a new one. Used by listener
        thread during reconnection backoff loop.

        Note:
            This method is called from the listener thread and should never
            raise exceptions. All errors are logged and suppressed.
        """
        with self._lock:
            # Only reconnect if still running
            if not self._running:
                return

            # Close existing connection
            if self._pubsub:
                try:
                    self._pubsub.close()
                except Exception as e:
                    logger.debug("Error closing PubSub during reconnection: %s", e)
                finally:
                    self._pubsub = None

            # Create new connection
            try:
                self._pubsub = self._redis_client.pubsub(ignore_subscribe_messages=True)
                self._pubsub.subscribe(self._channel_name)
                logger.info("Reconnected to invalidation channel")
            except Exception as e:
                logger.error("Failed to reconnect to invalidation channel: %s", e)
                # Leave _pubsub as None, will retry on next loop iteration
