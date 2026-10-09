"""Redis backend implementation for cachekit.

This module provides Redis storage backend using existing connection infrastructure.
"""

from __future__ import annotations

import time
from typing import Any, Optional, Union

import redis
from pydantic import SecretStr

from cachekit.backends._uninterrupted import _ClearedOnInterrupt
from cachekit.backends.base import BackendError
from cachekit.backends.provider import CacheClientProvider, PooledClientProvider
from cachekit.backends.redis.config import RedisBackendConfig
from cachekit.backends.redis.error_handler import kept_cause
from cachekit.config.validation import hide_secret, reveal_secret
from cachekit.di import DIContainer


def _command_error(command: str, operation: str, exc: Exception, key: Optional[str] = None) -> BackendError:
    """The BackendError for a failed Redis command: a type-only message, and the cause ``kept_cause`` keeps."""
    return BackendError(
        message=f"Redis {command} failed: {type(exc).__name__}",
        original_exception=kept_cause(exc),
        operation=operation,
        key=key,
    )


class RedisBackend:
    """Redis storage backend implementing BaseBackend protocol.

    Reuses existing CacheClientProvider infrastructure for connection management.
    Implements the four required operations: get, set, delete, exists.

    A failed operation raises its BackendError outside the ``except`` block, from a
    cause that keeps no redis-py frame (see ``kept_cause``), and from a frame that
    holds no client in a local: a redis-py client's repr lists the password, and an
    error tracker sends the locals of every frame on a raised error's traceback (CWE-532).
    The frame deletes its ``error`` local as the error leaves. An interrupt raised while
    redis-py is mid-call (KeyboardInterrupt, a worker timeout's SystemExit, gevent.Timeout)
    propagates as itself, with the locals of the finished frames it ran through, redis-py's
    included, cleared (see ``_ClearedOnInterrupt``).

    Examples:
        Create backend with explicit redis_url (requires running Redis):

        >>> backend = RedisBackend(redis_url="redis://localhost:6379")  # doctest: +SKIP
        >>> backend.set("key", b"value", ttl=60)  # doctest: +SKIP
        >>> backend.get("key")  # doctest: +SKIP
        b'value'
        >>> backend.delete("key")  # doctest: +SKIP
        True
        >>> backend.exists("key")  # doctest: +SKIP
        False

        Health check returns status and latency (requires running Redis):

        >>> is_healthy, details = backend.health_check()  # doctest: +SKIP
        >>> is_healthy  # doctest: +SKIP
        True
        >>> "latency_ms" in details  # doctest: +SKIP
        True
    """

    def __init__(
        self,
        redis_url: Optional[Union[str, SecretStr, RedisBackendConfig]] = None,
        client_provider: Optional[CacheClientProvider] = None,
    ) -> None:
        """Initialize RedisBackend.

        Provider resolution precedence (#222):
            1. Explicit ``client_provider`` — used as-is (caller owns the pool).
            2. Explicit ``redis_url`` (or a ``RedisBackendConfig``) — a
               per-instance pool bound to that URL. The URL you pass is the
               URL that gets used; it is never silently overridden by
               CACHEKIT_REDIS_URL/REDIS_URL or a DI-registered provider.
            3. Zero-config — a DI-registered ``CacheClientProvider`` when one
               exists (app-level override / test isolation), otherwise a
               per-instance pool built from env config. Works out of the box.

        Args:
            redis_url: Redis connection URL, or a full RedisBackendConfig
                (its redis_url plus pool/timeout knobs are honoured)
            client_provider: Optional CacheClientProvider instance

        Raises:
            BackendError: If no Redis URL can be resolved
        """
        # The URL may carry a password: no local here holds it raw (CWE-532), it is unwrapped only inline.
        redis_url = hide_secret(redis_url)
        if isinstance(redis_url, RedisBackendConfig):
            redis_config = redis_url
            explicit_url: Optional[SecretStr] = hide_secret(redis_config.redis_url)
        else:
            redis_config = RedisBackendConfig.from_env()
            explicit_url = redis_url

        self._redis_url = reveal_secret(explicit_url) or redis_config.redis_url

        # Validate a Redis URL is configured
        if not self._redis_url:
            raise BackendError(
                message="REDIS_URL configuration not set. Set CACHEKIT_REDIS_URL environment variable or pass redis_url parameter.",
                operation="init",
            )

        if client_provider is not None:
            self._client_provider = client_provider
        elif explicit_url:
            # An explicitly passed URL must be honoured: build a per-instance
            # pool bound to it. Falling through to the env-configured global
            # pool could silently read/write a *different* Redis (#222).
            self._client_provider = PooledClientProvider(explicit_url, redis_config)
        else:
            # Zero-config: honour a DI-registered provider when present,
            # otherwise build a per-instance pool from env config so
            # RedisBackend() works without any registration step (#222).
            try:
                self._client_provider = DIContainer().get(CacheClientProvider)
            except ValueError:
                self._client_provider = PooledClientProvider(self._redis_url, redis_config)

    def _get_client(self) -> redis.Redis:
        """Get Redis client from provider.

        Returns:
            Redis client instance

        Raises:
            BackendError: If client creation fails
        """
        try:
            return self._client_provider.get_sync_client()
        except Exception as e:
            error = BackendError(
                message=f"Failed to create Redis client: {type(e).__name__}",
                original_exception=kept_cause(e),
                operation="get_client",
            )
        try:
            raise error from error.original_exception
        finally:
            del error

    def get(self, key: str) -> Optional[bytes]:
        """Retrieve value from Redis storage.

        Args:
            key: Cache key to retrieve

        Returns:
            Bytes value if found, None if key doesn't exist

        Raises:
            BackendError: If Redis operation fails
        """
        with _ClearedOnInterrupt():
            try:
                value = self._get_client().get(key)
                # The pool is configured with decode_responses=False (see redis/client.py),
                # so Redis returns raw bytes, or None for a missing key. Cached payloads are
                # binary (LZ4/Arrow/AES ciphertext) and must never be UTF-8 decoded. The
                # isinstance check enforces the bytes|None contract without any str coercion.
                return value if isinstance(value, bytes) else None
            except Exception as e:
                error = _command_error("GET", "get", e, key)
        try:
            raise error from error.original_exception
        finally:
            del error

    def set(self, key: str, value: bytes, ttl: Optional[int] = None) -> None:
        """Store value in Redis storage.

        Args:
            key: Cache key to store
            value: Bytes value to store (encrypted or plaintext msgpack)
            ttl: Time-to-live in seconds (None = no expiry)

        Raises:
            BackendError: If Redis operation fails
        """
        with _ClearedOnInterrupt():
            try:
                if ttl is not None and ttl > 0:
                    # Use SETEX for TTL (combines SET + EXPIRE atomically)
                    self._get_client().setex(key, ttl, value)
                else:
                    # Use SET without expiry
                    self._get_client().set(key, value)
                return
            except Exception as e:
                error = _command_error("SET", "set", e, key)
        try:
            raise error from error.original_exception
        finally:
            del error

    def delete(self, key: str) -> bool:
        """Delete key from Redis storage.

        Args:
            key: Cache key to delete

        Returns:
            True if key was deleted, False if key didn't exist

        Raises:
            BackendError: If Redis operation fails
        """
        with _ClearedOnInterrupt():
            try:
                result = self._get_client().delete(key)
                # Redis DELETE returns number of keys deleted (0 or 1 for single key)
                if not isinstance(result, int):
                    raise BackendError(
                        message=f"Redis DELETE returned unexpected type: {type(result).__name__}",
                        operation="delete",
                        key=key,
                    )
                return result > 0
            except Exception as e:
                error = _command_error("DELETE", "delete", e, key)
        try:
            raise error from error.original_exception
        finally:
            del error

    def _delete_many(self, keys: list[str]) -> set[str]:
        """Delete many keys in one ``UNLINK`` (internal: whole-function invalidation).

        A reply means Redis applied the command to every key, so no key is ever reported
        failed; an absent key counts as deleted. A failed command raises, and the caller
        then deletes the keys one by one.

        Raises:
            BackendError: If the ``UNLINK`` fails (connection, proxy that cannot route it, ...)
        """
        if not keys:
            return set()
        with _ClearedOnInterrupt():
            try:
                self._get_client().unlink(*keys)
                return set()
            except Exception as e:
                error = _command_error("UNLINK", "delete", e)
        try:
            raise error from error.original_exception
        finally:
            del error

    def exists(self, key: str) -> bool:
        """Check if key exists in Redis storage.

        Args:
            key: Cache key to check

        Returns:
            True if key exists, False otherwise

        Raises:
            BackendError: If Redis operation fails
        """
        with _ClearedOnInterrupt():
            try:
                result = self._get_client().exists(key)
                # Redis EXISTS returns number of keys that exist (0 or 1 for single key)
                if not isinstance(result, int):
                    raise BackendError(
                        message=f"Redis EXISTS returned unexpected type: {type(result).__name__}",
                        operation="exists",
                        key=key,
                    )
                return result > 0
            except Exception as e:
                error = _command_error("EXISTS", "exists", e, key)
        try:
            raise error from error.original_exception
        finally:
            del error

    def health_check(self) -> tuple[bool, dict[str, Any]]:
        """Check Redis backend health status.

        Pings Redis to verify connectivity and measures latency.
        Returns backend information including version and connected clients.

        Returns:
            Tuple of (is_healthy, details_dict)
            is_healthy: True if Redis is responsive
            details_dict: Contains latency_ms, backend_type, version, etc.

        Example:
            >>> backend = RedisBackend()  # doctest: +SKIP
            >>> is_healthy, details = backend.health_check()  # doctest: +SKIP
            >>> print(f"Latency: {details['latency_ms']}ms")  # doctest: +SKIP
        """
        with _ClearedOnInterrupt():
            try:
                # Measure ping latency
                start = time.time()
                self._get_client().ping()
                latency_ms = (time.time() - start) * 1000

                # Get Redis info
                info = self._get_client().info()
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
            except Exception as e:
                return (
                    False,
                    {
                        "backend_type": "redis",
                        "latency_ms": -1,
                        "error": str(e),
                        "error_type": type(e).__name__,
                    },
                )
