"""Per-request Redis backend with tenant isolation.

This module implements the hybrid singleton pool + per-request wrapper pattern
for multi-tenant caching. All Code-Craftsman fixes (#1-#10) are applied.

Architecture:
- Singleton: Connection pool (expensive, created once in __init__)
- Per-request: Backend wrapper (cheap ~50ns, tenant-scoped)
- Tenant isolation: Via URL-encoded tenant_id in key prefix (t:{tenant}:{key})
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from contextvars import ContextVar
from typing import Any, Optional
from urllib.parse import quote as url_encode

import redis

from cachekit.backends.base import BaseBackend
from cachekit.backends.errors import BackendError
from cachekit.backends.redis.error_handler import classify_redis_error

logger = logging.getLogger(__name__)

# Module-level ContextVar for async-safe tenant isolation
tenant_context: ContextVar[Optional[str]] = ContextVar("tenant_context", default=None)


class PerRequestRedisBackend:
    """Per-request Redis backend wrapper with tenant isolation.

    Implements all Code-Craftsman fixes:
    - Fix #1: Accepts shared Redis client (not creating per operation)
    - Fix #2: URL-encodes tenant IDs to prevent ':' collision
    - Fix #3: Uses centralized classify_redis_error()
    - Fix #4: No request_id parameter (YAGNI)
    - Fix #5: health_check() doesn't leak tenant_id
    - Fix #6: Implements ALL optional protocols completely
    - Fix #9: Fail-fast validation - raises RuntimeError if tenant_id is None

    Tenant scoping format: t:{url_encoded_tenant_id}:{key}

    Examples:
        Key scoping with URL-encoded tenant ID:

        >>> from unittest.mock import Mock
        >>> mock_client = Mock()
        >>> backend = PerRequestRedisBackend(mock_client, tenant_id="org:123")
        >>> backend._scoped_key("user:456")
        't:org%3A123:user:456'

        Special characters in tenant_id are URL-encoded:

        >>> backend2 = PerRequestRedisBackend(Mock(), tenant_id="tenant/with:special@chars")
        >>> backend2._scoped_key("key")
        't:tenant%2Fwith%3Aspecial%40chars:key'

        None tenant_id raises RuntimeError (fail-fast):

        >>> PerRequestRedisBackend(Mock(), tenant_id=None)  # doctest: +IGNORE_EXCEPTION_DETAIL
        Traceback (most recent call last):
            ...
        RuntimeError: tenant_id cannot be None...
    """

    def __init__(self, client: redis.Redis, tenant_id: str | None):
        """Initialize per-request backend wrapper.

        Args:
            client: Shared Redis client (singleton from provider)
            tenant_id: Tenant identifier for key scoping (None = fail-fast)

        Raises:
            RuntimeError: If tenant_id is None (fail-fast validation - Fix #9)
        """
        # Fix #9: Fail-fast validation
        if tenant_id is None:
            raise RuntimeError(
                "tenant_id cannot be None. Set tenant context via tenant_context.set() "
                "or ensure tenant_extractor returns non-None value."
            )

        # Fix #1: Accept shared client (not creating per operation)
        self._client = client

        # Fix #2: URL-encode tenant ID to prevent ':' collision
        self._tenant_id = url_encode(tenant_id, safe="")
        self._original_tenant_id = tenant_id

    def _scoped_key(self, key: str) -> str:
        """Generate tenant-scoped key with URL-encoded tenant ID.

        Format: t:{url_encoded_tenant_id}:{key}

        Args:
            key: Original cache key

        Returns:
            Tenant-scoped key with URL-encoded tenant ID

        Examples:
            Standard key scoping:

            >>> from unittest.mock import Mock
            >>> backend = PerRequestRedisBackend(Mock(), "org:123")
            >>> backend._scoped_key("user:456")
            't:org%3A123:user:456'

            Keys with colons are preserved (only tenant_id is encoded):

            >>> backend._scoped_key("cache:user:profile:settings")
            't:org%3A123:cache:user:profile:settings'
        """
        return f"t:{self._tenant_id}:{key}"

    def get(self, key: str) -> Optional[bytes]:
        """Retrieve value from Redis storage with tenant scoping.

        Args:
            key: Cache key to retrieve (will be tenant-scoped)

        Returns:
            Bytes value if found, None if key doesn't exist

        Raises:
            BackendError: If Redis operation fails (classified via Fix #3)
        """
        scoped_key = self._scoped_key(key)
        try:
            value = self._client.get(scoped_key)
            if value is not None:
                # Handle both bytes and str responses
                if isinstance(value, str):
                    return value.encode("utf-8")
                if isinstance(value, bytes):
                    return value
            return None
        except Exception as exc:
            # Fix #3: Use centralized error classification
            raise classify_redis_error(exc, operation="get", key=key) from exc

    def set(self, key: str, value: bytes, ttl: Optional[int] = None) -> None:
        """Store value in Redis storage with tenant scoping.

        Args:
            key: Cache key to store (will be tenant-scoped)
            value: Bytes value to store
            ttl: Time-to-live in seconds (None = no expiry)

        Raises:
            BackendError: If Redis operation fails (classified via Fix #3)
        """
        scoped_key = self._scoped_key(key)
        try:
            if ttl is not None and ttl > 0:
                self._client.setex(scoped_key, ttl, value)
            else:
                self._client.set(scoped_key, value)
        except Exception as exc:
            # Fix #3: Use centralized error classification
            raise classify_redis_error(exc, operation="set", key=key) from exc

    def delete(self, key: str) -> bool:
        """Delete key from Redis storage with tenant scoping.

        Args:
            key: Cache key to delete (will be tenant-scoped)

        Returns:
            True if key was deleted, False if key didn't exist

        Raises:
            BackendError: If Redis operation fails (classified via Fix #3)
        """
        scoped_key = self._scoped_key(key)
        try:
            result = self._client.delete(scoped_key)
            if not isinstance(result, int):
                raise BackendError(
                    message=f"Redis DELETE returned unexpected type: {type(result).__name__}",
                    operation="delete",
                    key=key,
                )
            return result > 0
        except Exception as exc:
            # Fix #3: Use centralized error classification
            raise classify_redis_error(exc, operation="delete", key=key) from exc

    def exists(self, key: str) -> bool:
        """Check if key exists in Redis storage with tenant scoping.

        Args:
            key: Cache key to check (will be tenant-scoped)

        Returns:
            True if key exists, False otherwise

        Raises:
            BackendError: If Redis operation fails (classified via Fix #3)
        """
        scoped_key = self._scoped_key(key)
        try:
            result = self._client.exists(scoped_key)
            if not isinstance(result, int):
                raise BackendError(
                    message=f"Redis EXISTS returned unexpected type: {type(result).__name__}",
                    operation="exists",
                    key=key,
                )
            return result > 0
        except Exception as exc:
            # Fix #3: Use centralized error classification
            raise classify_redis_error(exc, operation="exists", key=key) from exc

    def health_check(self) -> tuple[bool, dict[str, Any]]:
        """Check Redis backend health status.

        Fix #5: Does NOT leak tenant_id in health check response.
        Returns generic Redis health without tenant-specific info.

        Returns:
            Tuple of (is_healthy, details_dict)
        """
        try:
            import time

            start = time.time()
            self._client.ping()
            latency_ms = (time.time() - start) * 1000

            info = self._client.info()
            if not isinstance(info, dict):
                raise BackendError(
                    message=f"Redis INFO returned unexpected type: {type(info).__name__}",
                    operation="health_check",
                    key="N/A",
                )

            return (
                True,
                {
                    "backend_type": "redis",
                    "latency_ms": round(latency_ms, 2),
                    "version": info.get("redis_version", "unknown"),
                    "used_memory_human": info.get("used_memory_human", "unknown"),
                    "connected_clients": info.get("connected_clients", 0),
                },
            )
        except Exception as exc:
            # Fix #3: Use centralized error classification
            error = classify_redis_error(exc, operation="health_check")
            return (
                False,
                {
                    "backend_type": "redis",
                    "latency_ms": -1,
                    "error": error.message,
                    "error_type": error.error_type.value,
                },
            )

    # Fix #6: Implement ALL optional protocols completely

    async def get_ttl(self, key: str) -> Optional[int]:
        """Get remaining TTL on key (TTLInspectableBackend protocol).

        Args:
            key: Cache key to inspect (will be tenant-scoped)

        Returns:
            Remaining TTL in seconds, or None if key doesn't exist or has no expiry

        Raises:
            BackendError: If Redis operation fails
        """
        scoped_key = self._scoped_key(key)
        try:
            ttl = self._client.ttl(scoped_key)
            if not isinstance(ttl, int):
                raise BackendError(
                    message=f"Redis TTL returned unexpected type: {type(ttl).__name__}",
                    operation="get_ttl",
                    key=key,
                )
            # Redis TTL returns:
            # -2 if key doesn't exist
            # -1 if key exists but has no expiry
            # >0 for remaining TTL in seconds
            if ttl == -2 or ttl == -1:
                return None
            return ttl if ttl > 0 else None
        except Exception as exc:
            raise classify_redis_error(exc, operation="get_ttl", key=key) from exc

    async def refresh_ttl(self, key: str, ttl: int) -> bool:
        """Refresh TTL on existing key (TTLInspectableBackend protocol).

        Args:
            key: Cache key to refresh (will be tenant-scoped)
            ttl: New TTL in seconds

        Returns:
            True if key existed and TTL was refreshed, False if key doesn't exist

        Raises:
            BackendError: If Redis operation fails
        """
        scoped_key = self._scoped_key(key)
        try:
            result = self._client.expire(scoped_key, ttl)
            # Redis EXPIRE returns 1 if TTL was set, 0 if key doesn't exist
            return bool(result)
        except Exception as exc:
            raise classify_redis_error(exc, operation="refresh_ttl", key=key) from exc

    @asynccontextmanager
    async def acquire_lock(
        self,
        key: str,
        timeout: float,
        blocking_timeout: Optional[float] = None,
    ) -> AsyncIterator[bool]:
        """Acquire distributed lock (LockableBackend protocol).

        Args:
            key: Lock key (will be tenant-scoped)
            timeout: How long to hold lock (seconds) before auto-release
            blocking_timeout: Max time to wait for lock (None = non-blocking)

        Yields:
            True if lock acquired, False if timeout waiting

        Raises:
            BackendError: If Redis operation fails

        Note:
            Uses asyncio.to_thread() to run sync Redis lock operations without blocking event loop.
            Sets thread_local=False to avoid thread-local token issues with thread pool.
        """
        import asyncio

        scoped_key = self._scoped_key(key)
        lock = None
        try:
            from redis.lock import Lock

            # Create Redis lock with tenant-scoped key
            # CRITICAL: thread_local=False allows lock to work across thread pool
            lock = Lock(
                self._client,
                name=scoped_key,
                timeout=timeout,
                blocking_timeout=blocking_timeout if blocking_timeout is not None else 0,
                thread_local=False,  # Disable thread-local storage for async/thread pool compatibility
            )

            # Run sync lock.acquire() in thread pool to avoid blocking event loop
            acquired = await asyncio.to_thread(lock.acquire, blocking=blocking_timeout is not None)
            try:
                yield acquired
            finally:
                # Release lock if acquired (also run in thread pool)
                if acquired:
                    try:
                        await asyncio.to_thread(lock.release)
                    except Exception as e:
                        # Lock may have expired - log but don't fail
                        logger.debug("Error releasing Redis lock (may have expired): %s", e)
        except Exception as exc:
            raise classify_redis_error(exc, operation="acquire_lock", key=key) from exc

    @asynccontextmanager
    async def with_timeout(
        self,
        operation: str,
        timeout_ms: int,
    ) -> AsyncIterator[None]:
        """Set timeout for operations (TimeoutConfigurableBackend protocol).

        Redis supports per-socket timeout, applied here as best-effort.
        Note: This is coarser-grained than per-operation timeout.

        Args:
            operation: Operation name (get, set, delete, etc.)
            timeout_ms: Timeout in milliseconds

        Raises:
            BackendError: With error_type=TIMEOUT if timeout exceeded
        """
        # Redis socket timeout is set globally on client
        # This is a best-effort implementation (coarser-grained)
        original_timeout = self._client.connection_pool.connection_kwargs.get("socket_timeout")
        timeout_sec = timeout_ms / 1000.0

        try:
            # Set socket timeout
            self._client.connection_pool.connection_kwargs["socket_timeout"] = timeout_sec
            yield
        except Exception as exc:
            raise classify_redis_error(exc, operation=operation) from exc
        finally:
            # Restore original timeout
            if original_timeout is not None:
                self._client.connection_pool.connection_kwargs["socket_timeout"] = original_timeout
            else:
                self._client.connection_pool.connection_kwargs.pop("socket_timeout", None)


class RedisBackendProvider:
    """Provider for Redis backend with singleton pool + per-request wrapper.

    Fix #1: Creates connection pool ONCE in __init__ (expensive).
    Creates singleton Redis client from pool.
    get_backend() returns new PerRequestRedisBackend per call (cheap: ~50ns).

    Implements BackendProvider protocol for dependency injection.

    Example:
        >>> _ = tenant_context.set("org:123")  # doctest: +ELLIPSIS
        >>> # Usage pattern (requires Redis connection):
        >>> # provider = RedisBackendProvider(redis_url="redis://localhost")
        >>> # backend = provider.get_backend()
        >>> # backend.set("key", b"value")
        >>> # Stored as: t:org%3A123:key
    """

    def __init__(self, redis_url: str, pool_size: int = 50):
        """Initialize provider with singleton connection pool.

        Fix #1: Creates pool ONCE (expensive operation).

        Args:
            redis_url: Redis connection URL
            pool_size: Connection pool size (default: 50)

        Raises:
            BackendError: If Redis connection fails
        """
        try:
            # Fix #1: Create connection pool ONCE
            self._pool = redis.ConnectionPool.from_url(
                redis_url,
                max_connections=pool_size,
                decode_responses=False,  # We handle bytes explicitly
            )

            # Create singleton Redis client from pool
            self._client = redis.Redis(connection_pool=self._pool)

            # Validate connection works
            self._client.ping()
        except Exception as exc:
            raise classify_redis_error(exc, operation="init") from exc

    def get_backend(self) -> BaseBackend:
        """Get per-request backend wrapper (cheap: ~50ns).

        Extracts tenant_id from ContextVar and creates tenant-scoped wrapper.

        Returns:
            PerRequestRedisBackend with tenant isolation

        Raises:
            RuntimeError: If tenant_context is not set (fail-fast - Fix #9)
        """
        # Extract tenant from ContextVar
        tenant_id = tenant_context.get()

        # Create per-request wrapper (cheap: ~50ns)
        # Fix #9: Fail-fast validation happens in PerRequestRedisBackend.__init__
        return PerRequestRedisBackend(self._client, tenant_id)

    def close(self) -> None:
        """Close connection pool and cleanup resources."""
        try:
            self._pool.disconnect()
        except Exception as e:
            # Best effort cleanup - log but don't raise
            logger.debug("Error closing Redis connection pool: %s", e)
