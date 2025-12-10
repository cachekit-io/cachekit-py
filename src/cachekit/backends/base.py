"""Base backend protocol definitions.

This module defines the storage backend contract using PEP 544 protocol-based abstraction.
All L2 backends (Redis, HTTP, etc.) must implement BaseBackend protocol.

Optional capability protocols (TTLInspectableBackend, LockableBackend,
TimeoutConfigurableBackend) enable advanced features with graceful degradation.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any, Optional, Protocol, runtime_checkable

# Re-export BackendError for convenience (public API)
from cachekit.backends.errors import BackendError  # noqa: F401


@runtime_checkable
class BaseBackend(Protocol):
    """Protocol defining the L2 backend storage contract.

    All backend implementations must support these four operations on bytes.
    This protocol uses structural subtyping (PEP 544) - any class implementing
    these methods is considered a valid backend.

    Design principles:
    - Stateless operations (no connection management in protocol)
    - Bytes-only interface (language-agnostic, no Python-specific types)
    - No Redis-specific concepts (works for any backend: Redis, HTTP, DynamoDB, etc.)
    - Simple and focused (KISS principle)

    Example:
        >>> from cachekit.backends import BaseBackend, RedisBackend
        >>> backend = RedisBackend()
        >>> isinstance(backend, BaseBackend)  # Runtime checkable protocol
        True
        >>> backend.set("key", b"value", ttl=60)  # doctest: +SKIP
        >>> data = backend.get("key")  # doctest: +SKIP
    """

    def get(self, key: str) -> Optional[bytes]:
        """Retrieve value from backend storage.

        Args:
            key: Cache key to retrieve

        Returns:
            Bytes value if found, None if key doesn't exist

        Raises:
            BackendError: If backend operation fails (network, timeout, etc.)
        """
        ...

    def set(self, key: str, value: bytes, ttl: Optional[int] = None) -> None:
        """Store value in backend storage.

        Args:
            key: Cache key to store
            value: Bytes value to store (encrypted or plaintext msgpack)
            ttl: Time-to-live in seconds (None = no expiry)

        Raises:
            BackendError: If backend operation fails (network, timeout, etc.)
        """
        ...

    def delete(self, key: str) -> bool:
        """Delete key from backend storage.

        Args:
            key: Cache key to delete

        Returns:
            True if key was deleted, False if key didn't exist

        Raises:
            BackendError: If backend operation fails (network, timeout, etc.)
        """
        ...

    def exists(self, key: str) -> bool:
        """Check if key exists in backend storage.

        Args:
            key: Cache key to check

        Returns:
            True if key exists, False otherwise

        Raises:
            BackendError: If backend operation fails (network, timeout, etc.)
        """
        ...

    def health_check(self) -> tuple[bool, dict[str, Any]]:
        """Check backend health status.

        Returns:
            Tuple of (is_healthy, details_dict)
            Details must include 'latency_ms' and 'backend_type'

        Example:
            >>> backend = RedisBackend()  # doctest: +SKIP
            >>> is_healthy, details = backend.health_check()  # doctest: +SKIP
            >>> assert 'latency_ms' in details  # doctest: +SKIP
            >>> assert 'backend_type' in details  # doctest: +SKIP
        """
        ...


@runtime_checkable
class TTLInspectableBackend(Protocol):
    """Optional protocol for backends that support TTL inspection and refresh.

    Backends implementing this protocol can report remaining TTL on keys
    and refresh TTLs on existing keys. This enables features like automatic
    TTL refresh for frequently-accessed keys.

    Not all backends support this capability:
    - Supported: Redis, PostgreSQL, DynamoDB, SQLite, FileSystem
    - Not supported: HTTP (stateless), Memcached (limited), S3 (limited)

    Example:
        >>> # TTL inspection pattern (async context):
        >>> # if hasattr(backend, 'get_ttl'):
        >>> #     ttl = await backend.get_ttl("user:123")
        >>> #     if ttl and ttl < 60:
        >>> #         await backend.refresh_ttl("user:123", 3600)
    """

    async def get_ttl(self, key: str) -> Optional[int]:
        """Get remaining TTL on key (in seconds).

        Args:
            key: Cache key to inspect

        Returns:
            Remaining TTL in seconds, or None if:
            - Key doesn't exist
            - Key has no expiration (permanent)

        Raises:
            BackendError: If backend operation fails

        Example:
            >>> ttl = await backend.get_ttl("user:123")  # doctest: +SKIP
            >>> if ttl and ttl < 60:  # doctest: +SKIP
            ...     # Key expiring soon
            ...     pass  # doctest: +SKIP
        """
        ...

    async def refresh_ttl(self, key: str, ttl: int) -> bool:
        """Refresh (update) TTL on existing key.

        Args:
            key: Cache key to refresh
            ttl: New TTL in seconds

        Returns:
            True if key existed and TTL was refreshed
            False if key doesn't exist (no-op)

        Raises:
            BackendError: If backend operation fails

        Example:
            >>> refreshed = await backend.refresh_ttl("user:123", 3600)  # doctest: +SKIP
            >>> if refreshed:  # doctest: +SKIP
            ...     # TTL successfully updated
            ...     pass  # doctest: +SKIP
        """
        ...


@runtime_checkable
class LockableBackend(Protocol):
    """Optional protocol for backends supporting distributed locking.

    Backends implementing this protocol can provide distributed lock semantics
    for coordinating access across multiple processes/servers. This enables
    features like cache stampede prevention and critical sections.

    Not all backends support this capability:
    - Supported: Redis, PostgreSQL, DynamoDB
    - Local-only: SQLite, FileSystem (single-process locking)
    - Not supported: HTTP (stateless), Memcached, S3

    Example:
        >>> # Distributed locking pattern (async context):
        >>> # if hasattr(backend, 'acquire_lock'):
        >>> #     async with backend.acquire_lock("lock:compute", timeout=30) as acquired:
        >>> #         if acquired:
        >>> #             result = expensive_computation()
    """

    async def acquire_lock(
        self,
        key: str,
        timeout: float,
        blocking_timeout: Optional[float] = None,
    ) -> AsyncIterator[bool]:
        """Acquire a distributed lock on key.

        Args:
            key: Lock key (e.g., "lock:user:123")
            timeout: How long to hold the lock (seconds) before auto-release
            blocking_timeout: Max time to wait for lock acquisition (None = non-blocking)

        Yields:
            True if lock was acquired
            False if timeout occurred waiting for lock

        Raises:
            BackendError: If backend operation fails

        Example:
            >>> async with backend.acquire_lock("lock:key", timeout=30, blocking_timeout=5) as acquired:  # doctest: +SKIP
            ...     if acquired:  # doctest: +SKIP
            ...         # Lock held, safe to proceed
            ...         pass  # doctest: +SKIP
            ...     else:  # doctest: +SKIP
            ...         # Timeout waiting for lock
            ...         pass  # doctest: +SKIP

        Note:
            Lock is automatically released on context exit, even if exception occurs.
        """
        ...


@runtime_checkable
class TimeoutConfigurableBackend(Protocol):
    """Optional protocol for per-operation timeout configuration.

    Backends implementing this protocol allow fine-grained timeout control
    per operation. This enables features like adaptive timeouts that adjust
    based on operation latency.

    All backends support some timeout mechanism, but granularity varies:
    - Per-operation: HTTP, DynamoDB, PostgreSQL
    - Per-socket/transaction: Redis, Memcached, SQLite
    - Global: KV, S3

    Example:
        >>> # Per-operation timeout pattern (async context):
        >>> # if hasattr(backend, 'with_timeout'):
        >>> #     async with backend.with_timeout("get", 100):
        >>> #         value = await backend.get("key")
    """

    async def with_timeout(
        self,
        operation: str,
        timeout_ms: int,
    ) -> AsyncIterator[None]:
        """Set timeout for operations within context.

        Args:
            operation: Operation name (e.g., "get", "set", "delete")
            timeout_ms: Timeout in milliseconds

        Raises:
            BackendError: With error_type=TIMEOUT if timeout exceeded

        Example:
            >>> async with backend.with_timeout("get", 100):  # doctest: +SKIP
            ...     value = await backend.get("key")  # doctest: +SKIP

        Note:
            Backends without per-operation timeout may apply timeout at
            socket or global level (coarser-grained fallback).
        """
        ...
