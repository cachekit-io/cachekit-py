"""Redis backend implementation for cachekit.

This module provides Redis storage backend using existing connection infrastructure.
"""

from __future__ import annotations

import time
from typing import Any, Optional

import redis

from cachekit.backends.base import BackendError
from cachekit.backends.provider import CacheClientProvider
from cachekit.backends.redis.config import RedisBackendConfig
from cachekit.di import DIContainer


class RedisBackend:
    """Redis storage backend implementing BaseBackend protocol.

    Reuses existing CacheClientProvider infrastructure for connection management.
    Implements the four required operations: get, set, delete, exists.

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
        redis_url: Optional[str] = None,
        client_provider: Optional[CacheClientProvider] = None,
    ):
        """Initialize RedisBackend with connection validation.

        Args:
            redis_url: Redis connection URL (optional, defaults to settings)
            client_provider: Optional CacheClientProvider instance (defaults to global container)

        Raises:
            BackendError: If REDIS_URL is not configured
        """
        # Get Redis URL from parameter or Redis backend config
        redis_config = RedisBackendConfig.from_env()
        self._redis_url = redis_url or redis_config.redis_url

        # Validate REDIS_URL is configured
        if not self._redis_url:
            raise BackendError(
                message="REDIS_URL configuration not set. Set CACHEKIT_REDIS_URL environment variable or pass redis_url parameter.",
                operation="init",
            )

        # Use explicit dependency or fallback to global container (backward compatibility)
        if client_provider is not None:
            self._client_provider = client_provider
        else:
            container = DIContainer()
            self._client_provider = container.get(CacheClientProvider)

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
            raise BackendError(
                message=f"Failed to create Redis client: {e}",
                operation="get_client",
            ) from e

    def get(self, key: str) -> Optional[bytes]:
        """Retrieve value from Redis storage.

        Args:
            key: Cache key to retrieve

        Returns:
            Bytes value if found, None if key doesn't exist

        Raises:
            BackendError: If Redis operation fails
        """
        try:
            client = self._get_client()
            value = client.get(key)
            # Redis client with decode_responses=True returns str, need bytes
            # But get() with binary data returns bytes if decode fails
            # For safety, encode if we got str
            if value is not None:
                if isinstance(value, str):
                    return value.encode("utf-8")
                if isinstance(value, bytes):
                    return value
            return None
        except Exception as e:
            raise BackendError(
                message=f"Redis GET failed: {e}",
                operation="get",
                key=key,
            ) from e

    def set(self, key: str, value: bytes, ttl: Optional[int] = None) -> None:
        """Store value in Redis storage.

        Args:
            key: Cache key to store
            value: Bytes value to store (encrypted or plaintext msgpack)
            ttl: Time-to-live in seconds (None = no expiry)

        Raises:
            BackendError: If Redis operation fails
        """
        try:
            client = self._get_client()
            if ttl is not None and ttl > 0:
                # Use SETEX for TTL (combines SET + EXPIRE atomically)
                client.setex(key, ttl, value)
            else:
                # Use SET without expiry
                client.set(key, value)
        except Exception as e:
            raise BackendError(
                message=f"Redis SET failed: {e}",
                operation="set",
                key=key,
            ) from e

    def delete(self, key: str) -> bool:
        """Delete key from Redis storage.

        Args:
            key: Cache key to delete

        Returns:
            True if key was deleted, False if key didn't exist

        Raises:
            BackendError: If Redis operation fails
        """
        try:
            client = self._get_client()
            result = client.delete(key)
            # Redis DELETE returns number of keys deleted (0 or 1 for single key)
            if not isinstance(result, int):
                raise BackendError(
                    message=f"Redis DELETE returned unexpected type: {type(result).__name__}",
                    operation="delete",
                    key=key,
                )
            return result > 0
        except Exception as e:
            raise BackendError(
                message=f"Redis DELETE failed: {e}",
                operation="delete",
                key=key,
            ) from e

    def exists(self, key: str) -> bool:
        """Check if key exists in Redis storage.

        Args:
            key: Cache key to check

        Returns:
            True if key exists, False otherwise

        Raises:
            BackendError: If Redis operation fails
        """
        try:
            client = self._get_client()
            result = client.exists(key)
            # Redis EXISTS returns number of keys that exist (0 or 1 for single key)
            if not isinstance(result, int):
                raise BackendError(
                    message=f"Redis EXISTS returned unexpected type: {type(result).__name__}",
                    operation="exists",
                    key=key,
                )
            return result > 0
        except Exception as e:
            raise BackendError(
                message=f"Redis EXISTS failed: {e}",
                operation="exists",
                key=key,
            ) from e

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
        try:
            client = self._get_client()

            # Measure ping latency
            start = time.time()
            client.ping()
            latency_ms = (time.time() - start) * 1000

            # Get Redis info
            info = client.info()
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
