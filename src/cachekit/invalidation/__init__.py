"""L1 cache invalidation system.

This package provides cross-pod invalidation for L1 caches via messaging
channels. The system is optional and degrades gracefully - L1 caches work
without invalidation, relying on TTL expiry.

Exports:
    InvalidationChannel: Protocol for invalidation broadcast
    InvalidationEvent: Immutable message for invalidation broadcast
    InvalidationLevel: Enum for invalidation scope
    InvalidationCallback: Type alias for event callback
    RedisInvalidationChannel: Redis Pub/Sub implementation of InvalidationChannel
"""

from cachekit.invalidation.channel import (
    InvalidationCallback,
    InvalidationChannel,
)
from cachekit.invalidation.event import (
    InvalidationEvent,
    InvalidationLevel,
)
from cachekit.invalidation.redis_channel import RedisInvalidationChannel

__all__ = [
    "InvalidationChannel",
    "InvalidationEvent",
    "InvalidationLevel",
    "InvalidationCallback",
    "RedisInvalidationChannel",
]
