"""Redis exception classification for backend abstraction.

This module maps redis-py exceptions to BackendErrorType for the circuit breaker.
Handles version differences in redis-py library.
"""

from __future__ import annotations

from cachekit.backends.errors import BackendError, BackendErrorType


class RedisClientError(Exception):
    """A failed redis-py call, kept as the BackendError's ``original_exception``: the exception's class, nothing more.

    The exception itself is not kept (CWE-532). Its traceback runs through redis-py's frames, whose locals hold the
    client, its connection pool or the AUTH arguments, and the client's and the pool's reprs list every connection
    argument, the password included: an error tracker that captures frame locals (Sentry does by default) would send
    it. Its text can also name the cache key.
    """

    def __init__(self, exc_type: type[Exception]) -> None:
        super().__init__(exc_type.__name__)
        self.exc_type = exc_type

    def __reduce__(self) -> tuple[type[RedisClientError], tuple[type[Exception]]]:
        # Pickled by the class, not by args (its name), so a BackendError carrying one still pickles and copies.
        return (type(self), (self.exc_type,))


def kept_cause(exc: Exception) -> Exception:
    """What a BackendError raised for ``exc`` keeps as its cause.

    ``exc`` itself when it is a BackendError: cachekit's own, raised from a cause like this one, so it reaches no
    redis-py frame. Anything else came out of redis-py and keeps only its class (see RedisClientError). The
    BackendError is raised outside the ``except`` block that caught ``exc``, from this cause: raised inside it,
    Python would chain ``exc`` as its ``__context__``, which ``raise ... from`` does not clear.
    """
    return exc if isinstance(exc, BackendError) else RedisClientError(type(exc))


def classify_redis_error(
    exc: Exception,
    operation: str | None = None,
    key: str | None = None,
    *,
    keep_exception: bool = False,
) -> BackendError:
    """Classify redis-py exception into BackendError with error_type.

    Maps redis library exceptions to BackendErrorType categories for
    the circuit breaker. Raise the result outside the ``except`` block that
    caught ``exc``, from its ``original_exception`` (see kept_cause).

    Args:
        exc: Original redis-py exception
        operation: Operation that failed (get, set, delete, exists, health_check)
        key: Cache key involved (optional, for debugging)
        keep_exception: ``exc`` was raised by the caller's own code inside a lock
            or timeout block, not by redis-py: keep it whole, so the caller can
            re-raise it

    Returns:
        BackendError with appropriate error_type classification. Its
        ``original_exception`` is ``kept_cause(exc)``, or ``exc`` itself with
        ``keep_exception``.

    Examples:
        Connection errors are classified as TRANSIENT:

        >>> from redis.exceptions import ConnectionError as RedisConnectionError
        >>> exc = RedisConnectionError("Connection refused")
        >>> error = classify_redis_error(exc, operation="get", key="user:123")
        >>> error.error_type.value
        'transient'
        >>> error.is_transient
        True

        Only the exception's class is kept, never the exception:

        >>> error.original_exception
        RedisClientError('ConnectionError')
        >>> error.original_exception.exc_type is RedisConnectionError
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

        Data/protocol errors are permanent:

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
        - ConnectionError, BusyLoadingError: TRANSIENT (connection lost or server loading)
        - TimeoutError: TIMEOUT (operation exceeded time limit)
        - AuthenticationError, NoPermissionError: AUTHENTICATION (alert ops)
        - ResponseError, DataError, InvalidResponse, LockError: PERMANENT (data or protocol error)
        - ReadOnlyError, ClusterDownError, TryAgainError: TRANSIENT (temporary cluster state)
        - All others: UNKNOWN (log and investigate)
    """
    # Every branch below puts only type(exc).__name__ in the message, never the raw
    # exception text: redis-py surfaces the offending key in ResponseError/NoPermission
    # text ("NOPERM ... keys used as arguments", "WRONGTYPE ... key ..."), and the
    # message reaches log sinks via str(e) (CWE-532). The text is not kept on
    # original_exception either (see kept_cause); the key is on the .key attribute
    # (redacted by _format_message).
    cause = exc if keep_exception else kept_cause(exc)
    # Import here to avoid circular dependency and handle missing redis
    try:
        from redis.exceptions import (
            AuthenticationError,
            BusyLoadingError,
            DataError,
            InvalidResponse,
            LockError,
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
            f"Redis error (redis-py not installed): {type(exc).__name__}",
            error_type=BackendErrorType.UNKNOWN,
            original_exception=cause,
            operation=operation,
            key=key,
        )

    # AUTHENTICATION: Credential/auth issues (check FIRST - subclass of ConnectionError)
    if isinstance(exc, (AuthenticationError, NoPermissionError)):
        return BackendError(
            f"Redis authentication error: {type(exc).__name__}",
            error_type=BackendErrorType.AUTHENTICATION,
            original_exception=cause,
            operation=operation,
            key=key,
        )

    # TIMEOUT: Operation exceeded time limit
    if isinstance(exc, RedisTimeoutError):
        return BackendError(
            f"Redis timeout: {type(exc).__name__}",
            error_type=BackendErrorType.TIMEOUT,
            original_exception=cause,
            operation=operation,
            key=key,
        )

    # TRANSIENT: Temporary failures
    if isinstance(exc, (RedisConnectionError, BusyLoadingError, ReadOnlyError)):
        return BackendError(
            f"Transient Redis error: {type(exc).__name__}",
            error_type=BackendErrorType.TRANSIENT,
            original_exception=cause,
            operation=operation,
            key=key,
        )

    # TRANSIENT: Cluster failover / resharding. Checked before PERMANENT because redis-py
    # declares ClusterDownError(ClusterError, ResponseError) and TryAgainError(ResponseError);
    # both are absent from older redis-py releases.
    try:
        from redis.exceptions import ClusterDownError, TryAgainError

        if isinstance(exc, (ClusterDownError, TryAgainError)):
            return BackendError(
                f"Transient Redis cluster error: {type(exc).__name__}",
                error_type=BackendErrorType.TRANSIENT,
                original_exception=cause,
                operation=operation,
                key=key,
            )
    except ImportError:
        pass  # Older redis-py version, skip cluster-specific handling

    # PERMANENT: Unfixable errors (data format, protocol errors)
    if isinstance(exc, (ResponseError, DataError, InvalidResponse, LockError)):
        return BackendError(
            f"Permanent Redis error: {type(exc).__name__}",
            error_type=BackendErrorType.PERMANENT,
            original_exception=cause,
            operation=operation,
            key=key,
        )

    # UNKNOWN: Unclassified error - log for investigation
    return BackendError(
        f"Unknown Redis error: {type(exc).__name__}",
        error_type=BackendErrorType.UNKNOWN,
        original_exception=cause,
        operation=operation,
        key=key,
    )
