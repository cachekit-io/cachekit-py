"""Redis exception classification for backend abstraction.

This module maps redis-py exceptions to BackendErrorType for circuit breaker
and retry logic. Handles version differences in redis-py library.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from cachekit.backends.errors import BackendError, BackendErrorType

if TYPE_CHECKING:
    pass


def classify_redis_error(
    exc: Exception,
    operation: str | None = None,
    key: str | None = None,
) -> BackendError:
    """Classify redis-py exception into BackendError with error_type.

    Maps redis library exceptions to BackendErrorType categories for
    circuit breaker and retry logic. Preserves original exception for
    debugging.

    Args:
        exc: Original redis-py exception
        operation: Operation that failed (get, set, delete, exists, health_check)
        key: Cache key involved (optional, for debugging)

    Returns:
        BackendError with appropriate error_type classification

    Examples:
        Connection errors are classified as TRANSIENT (retry with backoff):

        >>> from redis.exceptions import ConnectionError as RedisConnectionError
        >>> exc = RedisConnectionError("Connection refused")
        >>> error = classify_redis_error(exc, operation="get", key="user:123")
        >>> error.error_type.value
        'transient'
        >>> error.is_transient
        True

        Timeout errors get their own category for timeout-specific handling:

        >>> from redis.exceptions import TimeoutError as RedisTimeoutError
        >>> exc = RedisTimeoutError("Read timed out")
        >>> error = classify_redis_error(exc, operation="set", key="cache:data")
        >>> error.error_type.value
        'timeout'

        Authentication errors indicate credential issues:

        >>> from redis.exceptions import AuthenticationError
        >>> exc = AuthenticationError("NOAUTH Authentication required")
        >>> error = classify_redis_error(exc, operation="get")
        >>> error.error_type.value
        'authentication'
        >>> error.is_transient
        False

        Data/protocol errors are permanent (don't retry):

        >>> from redis.exceptions import ResponseError
        >>> exc = ResponseError("WRONGTYPE Operation against a key")
        >>> error = classify_redis_error(exc, operation="get", key="wrong:type")
        >>> error.error_type.value
        'permanent'

        Unknown exceptions are classified for investigation:

        >>> exc = RuntimeError("Unexpected error")
        >>> error = classify_redis_error(exc, operation="get")
        >>> error.error_type.value
        'unknown'

    Classification rules:
        - ConnectionError, BusyLoadingError: TRANSIENT (retry with backoff)
        - TimeoutError: TIMEOUT (configurable retry)
        - AuthenticationError, NoPermissionError: AUTHENTICATION (alert ops)
        - ResponseError, DataError: PERMANENT (don't retry)
        - ReadOnlyError, ClusterDownError: TRANSIENT (temporary cluster state)
        - All others: UNKNOWN (log and investigate)
    """
    # Import here to avoid circular dependency and handle missing redis
    try:
        from redis.exceptions import (
            AuthenticationError,
            BusyLoadingError,
            DataError,
            NoPermissionError,
            ReadOnlyError,
            ResponseError,
        )
        from redis.exceptions import (
            ConnectionError as RedisConnectionError,
        )
        from redis.exceptions import (
            TimeoutError as RedisTimeoutError,
        )
    except ImportError:
        # Redis not installed - treat as unknown error
        return BackendError(
            f"Redis error (redis-py not installed): {exc!s}",
            error_type=BackendErrorType.UNKNOWN,
            original_exception=exc,
            operation=operation,
            key=key,
        )

    # AUTHENTICATION: Credential/auth issues (check FIRST - subclass of ConnectionError)
    if isinstance(exc, (AuthenticationError, NoPermissionError)):
        return BackendError(
            f"Redis authentication error: {exc!s}",
            error_type=BackendErrorType.AUTHENTICATION,
            original_exception=exc,
            operation=operation,
            key=key,
        )

    # TIMEOUT: Operation exceeded time limit
    if isinstance(exc, RedisTimeoutError):
        return BackendError(
            f"Redis timeout: {exc!s}",
            error_type=BackendErrorType.TIMEOUT,
            original_exception=exc,
            operation=operation,
            key=key,
        )

    # TRANSIENT: Temporary failures, retry with exponential backoff
    if isinstance(exc, (RedisConnectionError, BusyLoadingError, ReadOnlyError)):
        return BackendError(
            f"Transient Redis error: {exc!s}",
            error_type=BackendErrorType.TRANSIENT,
            original_exception=exc,
            operation=operation,
            key=key,
        )

    # PERMANENT: Unfixable errors (data format, protocol errors)
    if isinstance(exc, (ResponseError, DataError)):
        return BackendError(
            f"Permanent Redis error: {exc!s}",
            error_type=BackendErrorType.PERMANENT,
            original_exception=exc,
            operation=operation,
            key=key,
        )

    # Handle ClusterDownError if available (redis-py 4.0+)
    try:
        from redis.exceptions import ClusterDownError

        if isinstance(exc, ClusterDownError):
            return BackendError(
                f"Redis cluster down: {exc!s}",
                error_type=BackendErrorType.TRANSIENT,
                original_exception=exc,
                operation=operation,
                key=key,
            )
    except ImportError:
        pass  # Older redis-py version, skip cluster-specific handling

    # UNKNOWN: Unclassified error - log for investigation
    return BackendError(
        f"Unknown Redis error: {exc!s}",
        error_type=BackendErrorType.UNKNOWN,
        original_exception=exc,
        operation=operation,
        key=key,
    )
