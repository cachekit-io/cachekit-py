"""Redis client factory functions with connection pooling and thread affinity.

This module centralizes ALL Redis connection concerns:
- Connection pool management (global singleton with configurable size)
- Thread-local client caching (eliminates ~1-2ms overhead per request)
- Client factory functions (sync/async)

Architecture:
- Connection pooling: Global singleton pools shared across all threads
- Thread affinity: Each thread caches its own Redis client instance
- Zero-copy optimization: Thread-local storage avoids locks on critical path
"""

import threading

import redis
import redis.asyncio as redis_async

from cachekit.backends.redis.config import RedisBackendConfig

# Global pool instances (shared across threads)
_pool_instance = None
_async_pool_instance = None
_pool_lock = threading.Lock()

# Thread-local client cache for performance optimization
_thread_local = threading.local()


def get_redis_client() -> redis.Redis:
    """Get a synchronous Redis client using connection pooling.

    Creates a global connection pool on first call, then reuses it.
    Pool configuration comes from RedisBackendConfig (env: CACHEKIT_REDIS_URL).

    Returns:
        redis.Redis: Synchronous Redis client with connection pooling

    Examples:
        Get a client and perform operations (requires Redis):

        >>> client = get_redis_client()  # doctest: +SKIP
        >>> client.ping()  # doctest: +SKIP
        True
    """
    global _pool_instance

    if _pool_instance is None:
        with _pool_lock:
            if _pool_instance is None:
                # Get Redis-specific configuration
                redis_config = RedisBackendConfig.from_env()

                # Use URL-based connection
                _pool_instance = redis.ConnectionPool.from_url(
                    redis_config.redis_url,
                    decode_responses=True,
                    max_connections=redis_config.connection_pool_size,
                )

    return redis.Redis(connection_pool=_pool_instance)


async def get_async_redis_client() -> redis_async.Redis:
    """Get an asynchronous Redis client with connection pooling.

    Creates a global async connection pool on first call, then reuses it.
    Pool configuration comes from RedisBackendConfig (env: CACHEKIT_REDIS_URL).

    Returns:
        redis_async.Redis: Asynchronous Redis client

    Examples:
        Async client usage (requires Redis and async context):

        >>> import asyncio
        >>> async def example():  # doctest: +SKIP
        ...     client = await get_async_redis_client()
        ...     return await client.ping()
        >>> asyncio.run(example())  # doctest: +SKIP
        True
    """
    global _async_pool_instance

    if _async_pool_instance is None:
        # Get Redis-specific configuration
        redis_config = RedisBackendConfig.from_env()

        # Use URL-based connection
        _async_pool_instance = redis_async.ConnectionPool.from_url(
            redis_config.redis_url,
            decode_responses=True,
            max_connections=redis_config.connection_pool_size,
        )

    return redis_async.Redis(connection_pool=_async_pool_instance)


def get_cached_redis_client() -> redis.Redis:
    """Get a thread-local cached synchronous Redis client.

    This provides the performance benefits of thread affinity by caching
    Redis client instances per-thread. Eliminates ~1-2ms overhead of
    repeated client creation in hot code paths.

    THREAD SAFETY: Each thread gets its own cached client instance via
    threading.local() storage, ensuring thread safety without locks.

    PERFORMANCE: Critical for decorator-based caching where the same
    thread makes multiple cache calls per request.

    Returns:
        redis.Redis: Thread-local cached Redis client with connection pooling

    Examples:
        Multiple calls return the same cached instance (per-thread):

        >>> client1 = get_cached_redis_client()  # doctest: +SKIP
        >>> client2 = get_cached_redis_client()  # doctest: +SKIP
        >>> client1 is client2  # doctest: +SKIP
        True
    """
    if not hasattr(_thread_local, "sync_client") or _thread_local.sync_client is None:
        _thread_local.sync_client = get_redis_client()
    return _thread_local.sync_client


async def get_cached_async_redis_client() -> redis_async.Redis:
    """Get an asynchronous Redis client (no caching needed).

    Async clients handle their own connection management efficiently,
    so thread-local caching provides no benefit and may interfere with
    asyncio's event loop model.

    Returns:
        redis_async.Redis: Asynchronous Redis client with connection pooling
    """
    return await get_async_redis_client()


def reset_global_pool():
    """Reset the global pool instances and thread-local cache.

    Useful for testing to ensure clean state between tests.
    Disconnects existing pools and clears thread-local client cache.

    Examples:
        Reset pools between tests:

        >>> reset_global_pool()  # Safe to call even if no pools exist
    """
    global _pool_instance, _async_pool_instance, _thread_local
    with _pool_lock:
        if _pool_instance:
            _pool_instance.disconnect()
        if _async_pool_instance:
            # Async pool cleanup would need await, skip for now
            pass
        _pool_instance = None
        _async_pool_instance = None

    # Reset thread-local cache
    if hasattr(_thread_local, "sync_client"):
        _thread_local.sync_client = None


__all__ = [
    "get_async_redis_client",
    "get_cached_async_redis_client",
    "get_cached_redis_client",
    "get_redis_client",
    "reset_global_pool",
]
