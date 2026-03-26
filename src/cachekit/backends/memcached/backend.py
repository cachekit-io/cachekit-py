"""Memcached backend implementation for cachekit.

Thread-safe Memcached backend using pymemcache HashClient with consistent hashing
for multi-server support. Implements BaseBackend protocol.
"""

from __future__ import annotations

import time
from typing import Any, Optional

from cachekit.backends.memcached.config import MAX_MEMCACHED_TTL, MemcachedBackendConfig
from cachekit.backends.memcached.error_handler import classify_memcached_error


def _parse_server(server: str) -> tuple[str, int]:
    """Parse 'host:port' string into (host, port) tuple.

    Args:
        server: Server address in 'host:port' format.

    Returns:
        Tuple of (host, port).
    """
    host, port_str = server.rsplit(":", 1)
    return (host, int(port_str))


class MemcachedBackend:
    """Memcached storage backend implementing BaseBackend protocol.

    Uses pymemcache HashClient for consistent-hashing across multiple servers.
    Thread-safe via HashClient's internal connection pooling.

    Examples:
        Create backend with defaults (requires running Memcached):

        >>> backend = MemcachedBackend()  # doctest: +SKIP
        >>> backend.set("key", b"value", ttl=60)  # doctest: +SKIP
        >>> backend.get("key")  # doctest: +SKIP
        b'value'
        >>> backend.delete("key")  # doctest: +SKIP
        True

        Create with explicit config:

        >>> from cachekit.backends.memcached.config import MemcachedBackendConfig
        >>> config = MemcachedBackendConfig(servers=["mc1:11211", "mc2:11211"])
        >>> backend = MemcachedBackend(config)  # doctest: +SKIP
    """

    def __init__(self, config: MemcachedBackendConfig | None = None) -> None:
        """Initialize MemcachedBackend.

        Args:
            config: Optional configuration. Defaults to loading from environment.
        """
        from pymemcache.client.hash import HashClient

        self._config = config or MemcachedBackendConfig.from_env()
        servers = [_parse_server(s) for s in self._config.servers]

        self._client: HashClient = HashClient(
            servers=servers,
            connect_timeout=self._config.connect_timeout,
            timeout=self._config.timeout,
            max_pool_size=self._config.max_pool_size,
            retry_attempts=self._config.retry_attempts,
        )
        self._key_prefix = self._config.key_prefix

    def _prefixed_key(self, key: str) -> str:
        """Apply key prefix if configured."""
        if self._key_prefix:
            return f"{self._key_prefix}{key}"
        return key

    def get(self, key: str) -> Optional[bytes]:
        """Retrieve value from Memcached.

        Args:
            key: Cache key to retrieve.

        Returns:
            Bytes value if found, None if key doesn't exist.

        Raises:
            BackendError: If Memcached operation fails.
        """
        try:
            result = self._client.get(self._prefixed_key(key))
            if result is None:
                return None
            # pymemcache returns bytes by default
            return bytes(result) if not isinstance(result, bytes) else result
        except Exception as exc:
            raise classify_memcached_error(exc, operation="get", key=key) from exc

    def set(self, key: str, value: bytes, ttl: Optional[int] = None) -> None:
        """Store value in Memcached.

        Args:
            key: Cache key to store.
            value: Bytes value to store.
            ttl: Time-to-live in seconds. None or 0 means no expiry.
                 Clamped to 30-day Memcached maximum.

        Raises:
            BackendError: If Memcached operation fails.
        """
        expire = 0
        if ttl is not None and ttl > 0:
            expire = min(ttl, MAX_MEMCACHED_TTL)

        try:
            self._client.set(self._prefixed_key(key), value, expire=expire)
        except Exception as exc:
            raise classify_memcached_error(exc, operation="set", key=key) from exc

    def delete(self, key: str) -> bool:
        """Delete key from Memcached.

        Args:
            key: Cache key to delete.

        Returns:
            True if key existed and was deleted, False otherwise.

        Raises:
            BackendError: If Memcached operation fails.
        """
        try:
            return bool(self._client.delete(self._prefixed_key(key), noreply=False))
        except Exception as exc:
            raise classify_memcached_error(exc, operation="delete", key=key) from exc

    def exists(self, key: str) -> bool:
        """Check if key exists in Memcached.

        Memcached has no native EXISTS command; uses GET and checks for None.

        Args:
            key: Cache key to check.

        Returns:
            True if key exists, False otherwise.

        Raises:
            BackendError: If Memcached operation fails.
        """
        try:
            return self._client.get(self._prefixed_key(key)) is not None
        except Exception as exc:
            raise classify_memcached_error(exc, operation="exists", key=key) from exc

    def health_check(self) -> tuple[bool, dict[str, Any]]:
        """Check Memcached health by calling stats on all servers.

        Returns:
            Tuple of (is_healthy, details_dict) with latency_ms and backend_type.
        """
        start = time.perf_counter()
        try:
            stats = self._client.stats()
            elapsed_ms = (time.perf_counter() - start) * 1000
            # stats() returns dict of {server: stats_dict}
            # Healthy if at least one server responded
            is_healthy = len(stats) > 0
            return (
                is_healthy,
                {
                    "backend_type": "memcached",
                    "latency_ms": round(elapsed_ms, 2),
                    "servers": len(stats),
                    "configured_servers": len(self._config.servers),
                },
            )
        except Exception as exc:
            elapsed_ms = (time.perf_counter() - start) * 1000
            return (
                False,
                {
                    "backend_type": "memcached",
                    "latency_ms": round(elapsed_ms, 2),
                    "error": str(exc),
                    "servers": 0,
                    "configured_servers": len(self._config.servers),
                },
            )
