"""Backend storage abstraction for cachekit.

This module provides protocol-based abstraction for L2 backend storage with
dependency injection pattern. Backends can be Redis, HTTP, DynamoDB, or any
key-value store.

Public API:
    - BaseBackend: Core protocol (5 methods: get, set, delete, exists, health_check)
    - TTLInspectableBackend: Optional protocol for TTL inspection/refresh
    - LockableBackend: Optional protocol for distributed locking
    - TimeoutConfigurableBackend: Optional protocol for per-operation timeouts
    - BackendProvider: Dependency injection protocol
    - BackendError: Exception raised by backend operations
    - BackendErrorType: Error classification enum
    - CapabilityNotAvailableError: Exception for missing optional capabilities
    - RedisBackend: Redis implementation (default)

Usage:
    >>> from cachekit.backends import BaseBackend, RedisBackend, BackendError
    >>> # RedisBackend usage requires Redis connection
    >>> # See RedisBackend documentation for connection details

Dependency injection pattern:
    >>> from cachekit.backends import BackendProvider
    >>> # Implement BackendProvider protocol in your application
    >>> # See BackendProvider documentation for implementation examples
"""

from __future__ import annotations

from typing import Protocol

from cachekit.backends.base import (
    BaseBackend,
    LockableBackend,
    TimeoutConfigurableBackend,
    TTLInspectableBackend,
)
from cachekit.backends.errors import (
    BackendError,
    BackendErrorType,
    CapabilityNotAvailableError,
)
from cachekit.backends.redis import RedisBackend

__all__ = [
    "BaseBackend",
    "TTLInspectableBackend",
    "LockableBackend",
    "TimeoutConfigurableBackend",
    "BackendProvider",
    "BackendError",
    "BackendErrorType",
    "CapabilityNotAvailableError",
    "RedisBackend",
]


class BackendProvider(Protocol):
    """Protocol for dependency injection of backend instances.

    Enables testability and pluggable backends without hardcoding concrete
    implementations. The provider manages backend lifecycle (singleton,
    pooling, per-request creation, etc.).

    Example:
        >>> from cachekit.backends import BackendProvider, BaseBackend
        >>> # Implement BackendProvider protocol:
        >>> # class MyProvider:
        >>> #     def get_backend(self) -> BaseBackend:
        >>> #         return RedisBackend()
        >>> # provider = MyProvider()
        >>> # backend = provider.get_backend()

    Note:
        This is a structural protocol (PEP 544) - any class with a get_backend()
        method satisfies this protocol without explicit inheritance.
    """

    def get_backend(self) -> BaseBackend:
        """Return a BaseBackend instance.

        Implementation can manage singleton, pooling, or per-request creation
        depending on backend requirements.

        Returns:
            BaseBackend instance ready for cache operations

        Example:
            >>> provider = RedisBackendProvider()  # doctest: +SKIP
            >>> backend = provider.get_backend()  # doctest: +SKIP
            >>> backend.set("key", b"value", ttl=60)  # doctest: +SKIP
        """
        ...
