"""Mock backend for testing with per-request pattern.

Provides in-memory backend implementation matching production PerRequestRedisBackend
architecture. Implements singleton store + per-request wrapper with tenant isolation.

Fix #7: Matches production pattern exactly with URL-encoded tenant IDs and fail-fast validation.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from contextvars import ContextVar
from typing import Any
from urllib.parse import quote as url_encode

from cachekit.backends.errors import BackendError, BackendErrorType

# Module-level ContextVar for async-safe tenant isolation (matches production)
mock_tenant_context: ContextVar[str | None] = ContextVar("mock_tenant_context", default=None)


class PerRequestMockBackend:
    """Per-request mock backend wrapper with tenant isolation.

    Fix #7: Matches production PerRequestRedisBackend pattern:
    - Accepts shared store dict (not creating per operation)
    - URL-encodes tenant IDs (matching Redis pattern)
    - Fail-fast validation for missing tenant_id
    - Implements all optional protocols completely
    - Tenant scoping: t:{url_encoded_tenant_id}:{key} format

    Example:
        .. code-block:: python

            import asyncio
            store = {}
            backend = PerRequestMockBackend(store, tenant_id="org:123")
            asyncio.run(backend.set("key", b"value", ttl=60))
            # Stored as: t:org%3A123:key
    """

    def __init__(self, store: dict[str, tuple[bytes, int | None]], tenant_id: str | None):
        """Initialize per-request mock backend wrapper.

        Args:
            store: Shared storage dict (singleton from provider)
            tenant_id: Tenant identifier for key scoping (None = fail-fast)

        Raises:
            RuntimeError: If tenant_id is None (fail-fast validation - Fix #9)
        """
        # Fix #9: Fail-fast validation (matches production)
        if tenant_id is None:
            raise RuntimeError(
                "tenant_id cannot be None. Set tenant context via mock_tenant_context.set() "
                "or ensure tenant_extractor returns non-None value."
            )

        # Fix #7: Accept shared store (not creating per operation)
        self._store = store
        self._locks: dict[str, asyncio.Lock] = {}

        # Fix #7: URL-encode tenant ID to prevent ':' collision (matches production)
        self._tenant_id = url_encode(tenant_id, safe="")
        self._original_tenant_id = tenant_id

    def _scoped_key(self, key: str) -> str:
        """Generate tenant-scoped key with URL-encoded tenant ID.

        Format: t:{url_encoded_tenant_id}:{key} (matches production)

        Args:
            key: Original cache key

        Returns:
            Tenant-scoped key with URL-encoded tenant ID

        Example:
            >>> backend = PerRequestMockBackend({}, "org:123")
            >>> backend._scoped_key("user:456")
            't:org%3A123:user:456'
        """
        return f"t:{self._tenant_id}:{key}"

    # ====== BaseBackend Protocol Methods ======

    async def get(self, key: str) -> bytes | None:
        """Retrieve value from in-memory storage with tenant scoping.

        Args:
            key: Cache key to retrieve (will be tenant-scoped)

        Returns:
            Bytes value if found, None if key doesn't exist
        """
        scoped_key = self._scoped_key(key)
        if scoped_key not in self._store:
            return None

        value, ttl = self._store[scoped_key]

        # Simulate TTL expiration
        if ttl is not None and ttl <= 0:
            del self._store[scoped_key]
            return None

        return value

    async def set(self, key: str, value: bytes, ttl: int | None = None) -> None:
        """Store value in in-memory storage with tenant scoping.

        Args:
            key: Cache key to store (will be tenant-scoped)
            value: Bytes value to store
            ttl: Time-to-live in seconds (None = no expiry)
        """
        scoped_key = self._scoped_key(key)
        self._store[scoped_key] = (value, ttl)

    async def delete(self, key: str) -> bool:
        """Delete key from in-memory storage with tenant scoping.

        Args:
            key: Cache key to delete (will be tenant-scoped)

        Returns:
            True if key was deleted, False if key didn't exist
        """
        scoped_key = self._scoped_key(key)
        return self._store.pop(scoped_key, None) is not None

    async def exists(self, key: str) -> bool:
        """Check if key exists in in-memory storage with tenant scoping.

        Args:
            key: Cache key to check (will be tenant-scoped)

        Returns:
            True if key exists, False otherwise
        """
        scoped_key = self._scoped_key(key)
        return scoped_key in self._store

    async def health_check(self) -> tuple[bool, dict[str, Any]]:
        """Check backend health status.

        Fix #5: Does NOT leak tenant_id in health check response (matches production).

        Returns:
            Tuple of (is_healthy=True, details_dict)
        """
        return (True, {"backend_type": "mock", "latency_ms": 0.1})

    # ====== TTLInspectableBackend Protocol Methods ======

    async def get_ttl(self, key: str) -> int | None:
        """Get remaining TTL on key (in seconds) with tenant scoping.

        Args:
            key: Cache key to inspect (will be tenant-scoped)

        Returns:
            Remaining TTL in seconds, or None if key doesn't exist or has no TTL
        """
        scoped_key = self._scoped_key(key)
        if scoped_key not in self._store:
            return None

        _, ttl = self._store[scoped_key]
        return ttl

    async def refresh_ttl(self, key: str, ttl: int) -> bool:
        """Refresh TTL on existing key with tenant scoping.

        Args:
            key: Cache key to refresh (will be tenant-scoped)
            ttl: New TTL in seconds

        Returns:
            True if key existed and TTL was refreshed, False if key doesn't exist
        """
        scoped_key = self._scoped_key(key)
        if scoped_key not in self._store:
            return False

        value, _ = self._store[scoped_key]
        self._store[scoped_key] = (value, ttl)
        return True

    # ====== LockableBackend Protocol Methods ======

    @asynccontextmanager
    async def acquire_lock(
        self,
        key: str,
        timeout: float,
        blocking_timeout: float | None = None,
    ) -> AsyncIterator[bool]:
        """Acquire a distributed lock on key with tenant scoping.

        Args:
            key: Lock key (will be tenant-scoped)
            timeout: How long to hold the lock (seconds) - not used in mock
            blocking_timeout: Max time to wait for lock acquisition

        Yields:
            True if lock was acquired, False if timeout occurred
        """
        scoped_key = self._scoped_key(key)
        if scoped_key not in self._locks:
            self._locks[scoped_key] = asyncio.Lock()

        lock = self._locks[scoped_key]
        acquired = False

        try:
            if blocking_timeout is not None and blocking_timeout > 0:
                # Blocking acquisition with timeout
                try:
                    async with asyncio.timeout(blocking_timeout):
                        await lock.acquire()
                        acquired = True
                except TimeoutError:
                    acquired = False
            elif blocking_timeout == 0:
                # Non-blocking acquisition (blocking_timeout=0)
                try:
                    lock.acquire_nowait()
                    acquired = True
                except RuntimeError:
                    acquired = False
            else:
                # Block indefinitely (blocking_timeout=None)
                await lock.acquire()
                acquired = True
        except Exception:
            acquired = False

        try:
            yield acquired
        finally:
            if acquired:
                lock.release()

    # ====== TimeoutConfigurableBackend Protocol Methods ======

    @asynccontextmanager
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
        """
        try:
            async with asyncio.timeout(timeout_ms / 1000.0):
                yield
        except TimeoutError as e:
            raise BackendError(
                f"Operation '{operation}' exceeded timeout ({timeout_ms}ms)",
                error_type=BackendErrorType.TIMEOUT,
                operation=operation,
            ) from e


class MockBackendProvider:
    """Test provider with singleton store + per-request wrapper pattern.

    Fix #7: Matches RedisBackendProvider pattern exactly.
    - Singleton shared store (expensive to setup, shared across requests)
    - Per-request wrapper (cheap ~50ns, tenant-scoped)

    Example:
        .. code-block:: python

            import asyncio
            mock_tenant_context.set("org:123")
            provider = MockBackendProvider()
            backend = provider.get_backend()
            asyncio.run(backend.set("key", b"value"))
            # Stored as: t:org%3A123:key
    """

    def __init__(self):
        """Initialize provider with singleton shared store.

        Fix #7: Creates shared store ONCE (matches production pattern).
        """
        # Singleton shared store
        self._store: dict[str, tuple[bytes, int | None]] = {}

    def get_backend(self) -> PerRequestMockBackend:
        """Get per-request backend wrapper (cheap: ~50ns).

        Extracts tenant_id from ContextVar and creates tenant-scoped wrapper.

        Returns:
            PerRequestMockBackend with tenant isolation

        Raises:
            RuntimeError: If mock_tenant_context is not set (fail-fast - Fix #9)
        """
        # Extract tenant from ContextVar (matches production)
        tenant_id = mock_tenant_context.get()

        # Create per-request wrapper (cheap: ~50ns)
        # Fix #9: Fail-fast validation happens in PerRequestMockBackend.__init__
        return PerRequestMockBackend(self._store, tenant_id)

    def clear(self):
        """Clear backend storage (for test isolation)."""
        self._store.clear()
