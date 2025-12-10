"""Backend fixture helpers for advanced testing scenarios.

This module provides helper functions for creating backends with specific
capabilities or limitations for testing edge cases and graceful degradation.

Note: Examples in this file are illustrative only - not executed as doctests.
"""

from __future__ import annotations

from typing import Any

from tests.backends.mock_backend import MockBackendProvider, PerRequestMockBackend


def create_backend_with_capabilities(
    *,
    ttl_support: bool = True,
    locking_support: bool = True,
    timeout_support: bool = True,
    tenant_id: str = "test-tenant",
) -> Any:
    """Create a MockBackend with specific capability support.

    Args:
        ttl_support: Whether to include TTL inspection capability
        locking_support: Whether to include distributed locking capability
        timeout_support: Whether to include timeout configuration capability
        tenant_id: Tenant identifier for key scoping

    Returns:
        PerRequestMockBackend with specified capabilities

    Example:
        >>> # Backend with only TTL support (no locking or timeout)
        >>> backend = create_backend_with_capabilities(
        ...     ttl_support=True,
        ...     locking_support=False,
        ...     timeout_support=False
        ... )
    """
    store: dict[str, tuple[bytes, int | None]] = {}
    backend = PerRequestMockBackend(store, tenant_id=tenant_id)

    # Create a wrapper that selectively exposes methods
    class LimitedCapabilityBackend:
        """Wrapper that limits backend capabilities."""

        def __init__(self, wrapped_backend):
            self._backend = wrapped_backend

        def __getattr__(self, name):
            # Block TTL methods if not supported
            if not ttl_support and name in ("get_ttl", "refresh_ttl"):
                raise AttributeError(f"'{type(self).__name__}' object has no attribute '{name}'")

            # Block locking methods if not supported
            if not locking_support and name == "acquire_lock":
                raise AttributeError(f"'{type(self).__name__}' object has no attribute '{name}'")

            # Block timeout methods if not supported
            if not timeout_support and name == "with_timeout":
                raise AttributeError(f"'{type(self).__name__}' object has no attribute '{name}'")

            return getattr(self._backend, name)

    # Return full backend if all capabilities enabled
    if ttl_support and locking_support and timeout_support:
        return backend

    return LimitedCapabilityBackend(backend)


def create_backend_without_capabilities(*capabilities: str, tenant_id: str = "test-tenant") -> Any:
    """Create a MockBackend without specific capabilities.

    Args:
        *capabilities: Capability names to exclude ("ttl", "locking", "timeout")
        tenant_id: Tenant identifier for key scoping

    Returns:
        Backend with specified capabilities removed

    Example:
        >>> # Backend without locking and timeout
        >>> backend = create_backend_without_capabilities("locking", "timeout")
        >>> hasattr(backend, "acquire_lock")
        False
    """
    capability_map = {
        "ttl": {"get_ttl", "refresh_ttl"},
        "locking": {"acquire_lock"},
        "timeout": {"with_timeout"},
    }

    # Validate capabilities
    for capability in capabilities:
        if capability not in capability_map:
            raise ValueError(f"Unknown capability: {capability}. Valid: {list(capability_map.keys())}")

    # Build support flags
    return create_backend_with_capabilities(
        ttl_support="ttl" not in capabilities,
        locking_support="locking" not in capabilities,
        timeout_support="timeout" not in capabilities,
        tenant_id=tenant_id,
    )


def create_multi_tenant_provider() -> MockBackendProvider:
    """Create a MockBackendProvider for multi-tenant testing.

    Returns a provider with shared store that can create backends for
    multiple tenants with proper isolation.

    Returns:
        MockBackendProvider instance
    """
    return MockBackendProvider()


def create_failing_backend(
    *,
    fail_on_get: bool = False,
    fail_on_set: bool = False,
    fail_on_delete: bool = False,
    fail_on_exists: bool = False,
    error_message: str = "Backend operation failed",
    tenant_id: str = "test-tenant",
) -> PerRequestMockBackend:
    """Create a MockBackend that fails on specific operations.

    Useful for testing error handling and circuit breaker behavior.

    Args:
        fail_on_get: Whether get() should raise an error
        fail_on_set: Whether set() should raise an error
        fail_on_delete: Whether delete() should raise an error
        fail_on_exists: Whether exists() should raise an error
        error_message: Error message to use
        tenant_id: Tenant identifier for key scoping

    Returns:
        PerRequestMockBackend that fails on specified operations
    """
    from cachekit.backends.errors import BackendError, BackendErrorType

    store: dict[str, tuple[bytes, int | None]] = {}
    backend = PerRequestMockBackend(store, tenant_id=tenant_id)

    # Wrap methods to raise errors
    if fail_on_get:

        async def failing_get(key: str) -> bytes | None:
            raise BackendError(error_message, error_type=BackendErrorType.TRANSIENT)

        backend.get = failing_get  # type: ignore[method-assign]

    if fail_on_set:

        async def failing_set(key: str, value: bytes, ttl: int | None = None) -> None:
            raise BackendError(error_message, error_type=BackendErrorType.TRANSIENT)

        backend.set = failing_set  # type: ignore[method-assign]

    if fail_on_delete:

        async def failing_delete(key: str) -> bool:
            raise BackendError(error_message, error_type=BackendErrorType.TRANSIENT)

        backend.delete = failing_delete  # type: ignore[method-assign]

    if fail_on_exists:

        async def failing_exists(key: str) -> bool:
            raise BackendError(error_message, error_type=BackendErrorType.TRANSIENT)

        backend.exists = failing_exists  # type: ignore[method-assign]

    return backend


def verify_backend_capabilities(backend: Any) -> dict[str, bool]:
    """Verify which optional protocols a backend implements.

    Args:
        backend: Backend instance to check

    Returns:
        Dict mapping protocol names to support status

    Example:
        >>> backend = create_backend_without_capabilities("locking")
        >>> caps = verify_backend_capabilities(backend)
        >>> caps["ttl"]
        True
        >>> caps["locking"]
        False
    """
    return {
        "ttl": hasattr(backend, "get_ttl") and hasattr(backend, "refresh_ttl"),
        "locking": hasattr(backend, "acquire_lock"),
        "timeout": hasattr(backend, "with_timeout"),
    }
