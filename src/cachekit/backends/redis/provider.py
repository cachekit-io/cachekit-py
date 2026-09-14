"""Per-request Redis backend with tenant isolation.

This module implements the hybrid singleton pool + per-request wrapper pattern
for multi-tenant caching. All Code-Craftsman fixes (#1-#10) are applied.

Architecture:
- Singleton: Connection pool (expensive, created once in __init__)
- Per-request: Backend wrapper (cheap ~50ns, tenant-scoped)
- Tenant isolation: Via URL-encoded tenant_id in key prefix (t:{tenant}:{key})
"""

from __future__ import annotations

import asyncio
import functools
import logging
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from contextvars import ContextVar
from typing import Any, Optional, TypeVar
from urllib.parse import quote as url_encode

import redis
from redis.exceptions import LockNotOwnedError

from cachekit.backends.base import BaseBackend
from cachekit.backends.errors import BackendError
from cachekit.backends.redis.error_handler import classify_redis_error

logger = logging.getLogger(__name__)

# Module-level ContextVar for async-safe tenant isolation
tenant_context: ContextVar[Optional[str]] = ContextVar("tenant_context", default=None)

T = TypeVar("T")


async def _await_uninterrupted(fut: asyncio.Future[T]) -> T:
    """Await ``fut`` to completion even if the current task is cancelled meanwhile.

    ``asyncio.to_thread`` work is uninterruptible once an executor thread picks it up, and a
    still-queued work item is dropped if its future is cancelled first — so a cancelled awaiter
    either loses the outcome of a round-trip that still completes, or loses the round-trip
    itself. ``asyncio.wait`` never cancels its inputs and never unwraps their result, so keep
    waiting on ``fut`` until it is really done, absorbing every cancellation, then re-raise the
    last one: callers read ``fut`` for the real outcome before letting it propagate.

    Pass a plain future (``loop.run_in_executor``), never a Task: ``all_tasks()`` sweeps such as
    ``asyncio.run`` teardown cancel Tasks out from under the drain, and the outcome is lost again.
    """
    cancelled: Optional[asyncio.CancelledError] = None
    while not fut.done():
        try:
            await asyncio.wait({fut})
        except asyncio.CancelledError as exc:
            cancelled = exc
    if cancelled is not None:
        raise cancelled
    return fut.result()


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

    @property
    def key_prefix(self) -> str:
        """Wire-level key prefix (contract for interop mode's fail-closed guard).

        Every key this wrapper touches is rewritten to ``t:{tenant}:{key}`` —
        invisible to other SDKs reading the same Redis, so interop mode MUST
        reject this backend (see cachekit.interop.ensure_interop_backend_compatible).
        Backends that rewrite keys on the wire MUST expose the prefix here.
        """
        return f"t:{self._tenant_id}:"

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
            key: Bare cache key (will be tenant-scoped and ``:lock``-suffixed internally).
                The LockableBackend protocol passes the same key as ``get``/``set``/``delete``;
                the ``:lock`` namespace is a Redis-backend implementation detail kept
                on-wire for zero-migration compatibility with existing deployments.
            timeout: How long to hold lock (seconds) before auto-release
            blocking_timeout: Max time to wait for lock (None = non-blocking)

        Yields:
            True if lock acquired, False if timeout waiting

        Raises:
            BackendError: If Redis operation fails

        Note:
            Each acquisition attempt is one non-blocking ``SET NX`` round-trip run via
            ``loop.run_in_executor()`` and drained through ``_await_uninterrupted`` (so a
            cancellation cannot drop a round-trip that still completes); the wait between
            attempts is an ``asyncio.sleep`` on the event loop, never a sleep inside an
            executor thread. A blocking ``Lock.acquire`` run in the executor would pin one
            executor thread per waiter for up to ``blocking_timeout``. The default executor
            has only ``min(32, cpu_count + 4)`` threads (8 when ``cpu_count`` is 4), so once
            concurrent misses on one key reach that size the holder's own
            ``get``/``set``/``release`` — also executor calls — queue behind the waiters,
            every waiter times out, and all of them recompute.
            Sets thread_local=False because attempts and release may run on different
            executor threads.
            Cancellation is drained, not raced: an in-flight attempt or release round-trip
            always runs to completion, a lock the attempt wins is released, and only then is
            the ``CancelledError`` re-raised.
        """
        # Derive the on-wire Redis lock name from the bare cache key: ``<scoped_key>:lock``.
        # Keeping this suffix on the wire preserves compatibility with existing Redis
        # deployments — the lock identity didn't change, only the protocol boundary
        # (the wrapper no longer pollutes the cache_key passed in).
        scoped_key = f"{self._scoped_key(key)}:lock"
        from cachekit.cache_handler import redact_cache_key  # local: cache_handler imports the backends package

        try:
            from redis.lock import Lock

            lock = Lock(
                self._client,
                name=scoped_key,
                timeout=timeout,
                thread_local=False,  # attempts and release may land on different executor threads
            )

            loop = asyncio.get_running_loop()
            deadline = None if blocking_timeout is None else loop.time() + blocking_timeout
            token = uuid.uuid4().hex  # one token for the whole acquisition, however many attempts

            def _release_sync() -> None:
                # Catch inside the executor callable, not around _release(): once a cancellation has
                # landed, _await_uninterrupted re-raises it and an error left on the future would only
                # surface as asyncio's "exception was never retrieved" at GC.
                try:
                    lock.release()
                except LockNotOwnedError as e:
                    logger.debug("Redis lock already expired or taken over before release: %s", e)  # nothing to orphan
                except redis.RedisError as e:
                    logger.warning(
                        "Redis lock release for %s failed; the key lives until its TTL", redact_cache_key(key), exc_info=e
                    )

            async def _release() -> None:
                # Drained: a cancel landing while this still queues for a thread must not drop the release.
                await _await_uninterrupted(loop.run_in_executor(None, _release_sync))

            while True:
                # Drained: a cancel cannot stop the thread's SET NX from winning, only hide that it did.
                attempt = loop.run_in_executor(None, functools.partial(lock.acquire, blocking=False, token=token))
                try:
                    acquired = await _await_uninterrupted(attempt)
                except asyncio.CancelledError:
                    # The attempt has finished. One that failed (e.g. a Redis ConnectionError) cannot
                    # have won; log it rather than let it mask the cancellation.
                    if (err := attempt.exception()) is not None:
                        logger.warning(
                            "Redis lock attempt for %s failed while acquire_lock was being cancelled",
                            redact_cache_key(key),
                            exc_info=err,
                        )
                    elif attempt.result():
                        await _release()
                    raise
                # Same give-up rule as redis-py's Lock.acquire: stop once the next attempt
                # would land past the deadline. blocking_timeout=None means a single attempt.
                if acquired or deadline is None or loop.time() + lock.sleep > deadline:
                    break
                await asyncio.sleep(lock.sleep)
            try:
                yield acquired
            finally:
                # Release lock if acquired (also run in thread pool)
                if acquired:
                    await _release()
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
            # Fix #1: Create connection pool ONCE. Shared builder wires the
            # finite socket timeouts, so the ping below fails fast on an
            # unreachable Redis instead of blocking on the OS TCP timeout.
            from cachekit.backends.redis.client import create_connection_pool

            self._pool = create_connection_pool(redis_url, max_connections=pool_size)

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
