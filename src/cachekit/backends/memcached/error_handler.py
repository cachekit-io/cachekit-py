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

    # Timeout — socket.timeout or OSError with ETIMEDOUT.
    # Only the exception TYPE goes in the message: wrapped provider text has
    # unknown provenance and may echo the raw cache key, and the message reaches
    # log sinks via str(e) (CWE-532). Full details stay on original_exception.
    if isinstance(exc, (socket.timeout, TimeoutError)):
        return BackendError(
            message=f"Memcached timeout during {operation}: {type(exc).__name__}",
            error_type=BackendErrorType.TIMEOUT,
            original_exception=exc,
            operation=operation,
            key=key,
        )

    # Transient — connection closed, server errors (retriable). Type-only message:
    # pymemcache close/server errors can echo the raw key, and str(e) reaches log
    # sinks (CWE-532). Detail stays on original_exception.
    if isinstance(exc, (MemcacheUnexpectedCloseError, MemcacheServerError, ConnectionError, OSError)):
        return BackendError(
            message=f"Memcached transient error during {operation}: {type(exc).__name__}",
            error_type=BackendErrorType.TRANSIENT,
            original_exception=exc,
            operation=operation,
            key=key,
        )

    # Permanent — illegal input, client errors (don't retry).
    # Only the exception TYPE goes in the message: pymemcache embeds the raw
    # cache key in illegal-input error text ("Key is too long: %r"), and the
    # message reaches log sinks via str(e) (CWE-532). Full details stay on
    # original_exception for programmatic access.
    if isinstance(exc, (MemcacheIllegalInputError, MemcacheClientError)):
        return BackendError(
            message=f"Memcached permanent error during {operation}: {type(exc).__name__}",
            error_type=BackendErrorType.PERMANENT,
            original_exception=exc,
            operation=operation,
            key=key,
        )

    # Unknown — safe default. Arbitrary exception text has unknown provenance
    # and may embed the key, so only the type name goes in the message (CWE-532).
    return BackendError(
        message=f"Memcached unknown error during {operation}: {type(exc).__name__}",
        error_type=BackendErrorType.UNKNOWN,
        original_exception=exc,
        operation=operation,
        key=key,
    )
