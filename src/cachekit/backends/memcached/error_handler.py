"""Memcached exception classification for backend abstraction.

Maps pymemcache exceptions to BackendErrorType for circuit breaker and retry logic.
"""

from __future__ import annotations

import socket

from cachekit.backends.errors import BackendError, BackendErrorType


def classify_memcached_error(
    exc: Exception,
    operation: str | None = None,
    key: str | None = None,
) -> BackendError:
    """Classify pymemcache exception into BackendError with error_type.

    Args:
        exc: Original pymemcache exception.
        operation: Operation that failed (get, set, delete, exists, health_check).
        key: Cache key involved (optional, for debugging).

    Returns:
        BackendError with appropriate error_type classification.

    Examples:
        Connection errors are classified as TRANSIENT:

        >>> from pymemcache.exceptions import MemcacheUnexpectedCloseError
        >>> exc = MemcacheUnexpectedCloseError()
        >>> error = classify_memcached_error(exc, operation="get", key="user:123")
        >>> error.error_type.value
        'transient'

        Timeout errors get their own category:

        >>> exc = socket.timeout("timed out")
        >>> error = classify_memcached_error(exc, operation="set")
        >>> error.error_type.value
        'timeout'
    """
    from pymemcache.exceptions import (
        MemcacheClientError,
        MemcacheIllegalInputError,
        MemcacheServerError,
        MemcacheUnexpectedCloseError,
    )

    # Timeout — socket.timeout or OSError with ETIMEDOUT
    if isinstance(exc, (socket.timeout, TimeoutError)):
        return BackendError(
            message=f"Memcached timeout during {operation}: {exc}",
            error_type=BackendErrorType.TIMEOUT,
            original_exception=exc,
            operation=operation,
            key=key,
        )

    # Transient — connection closed, server errors (retriable)
    if isinstance(exc, (MemcacheUnexpectedCloseError, MemcacheServerError, ConnectionError, OSError)):
        return BackendError(
            message=f"Memcached transient error during {operation}: {exc}",
            error_type=BackendErrorType.TRANSIENT,
            original_exception=exc,
            operation=operation,
            key=key,
        )

    # Permanent — illegal input, client errors (don't retry)
    if isinstance(exc, (MemcacheIllegalInputError, MemcacheClientError)):
        return BackendError(
            message=f"Memcached permanent error during {operation}: {exc}",
            error_type=BackendErrorType.PERMANENT,
            original_exception=exc,
            operation=operation,
            key=key,
        )

    # Unknown — safe default
    return BackendError(
        message=f"Memcached unknown error during {operation}: {exc}",
        error_type=BackendErrorType.UNKNOWN,
        original_exception=exc,
        operation=operation,
        key=key,
    )
