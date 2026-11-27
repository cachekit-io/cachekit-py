"""Google Cloud Storage exception classification for backend abstraction.

Maps google-cloud-storage, google-auth and transport exceptions to BackendErrorType for the
circuit breaker.
"""

from __future__ import annotations

from cachekit.backends.errors import BackendError, BackendErrorType


def _classify(exc: Exception) -> tuple[BackendErrorType, str]:
    """The error type for exc, and the word that names it in the message."""
    import requests.exceptions
    import urllib3.exceptions
    from google.api_core.exceptions import GoogleAPICallError
    from google.auth.exceptions import GoogleAuthError, TransportError
    from google.cloud.storage.exceptions import DataCorruption

    cause = exc.args[0] if exc.args else None
    # Before the OSError branch: requests' exceptions are OSErrors, and its ConnectTimeout is also
    # a ConnectionError. A read that times out part-way through a response body reaches here as a
    # plain requests ConnectionError around urllib3's timeout.
    if isinstance(exc, (requests.exceptions.Timeout, TimeoutError)) or isinstance(cause, urllib3.exceptions.TimeoutError):
        return BackendErrorType.TIMEOUT, "timeout"
    if isinstance(exc, GoogleAPICallError):
        status = exc.code
        if status in (401, 403):
            return BackendErrorType.AUTHENTICATION, "authentication error"
        if status == 408:
            return BackendErrorType.TIMEOUT, "timeout"
        if status == 429 or (status is not None and status >= 500):
            return BackendErrorType.TRANSIENT, "transient error"
        return BackendErrorType.PERMANENT, "permanent error"
    if isinstance(exc, GoogleAuthError):
        # The token endpoint or metadata server being unreachable is not a refused credential. It
        # arrives as a TransportError, as a RefreshError wrapping one, or flagged retryable.
        unreachable = isinstance(exc, TransportError) or isinstance(cause, TransportError) or getattr(exc, "retryable", False)
        return (
            (BackendErrorType.TRANSIENT, "transient error")
            if unreachable
            else (BackendErrorType.AUTHENTICATION, "authentication error")
        )
    # DataCorruption: the bytes received do not match the checksum the server sent with them.
    if isinstance(exc, (OSError, DataCorruption)):
        return BackendErrorType.TRANSIENT, "transient error"
    return BackendErrorType.UNKNOWN, "unknown error"


def classify_gcs_error(
    exc: Exception,
    operation: str | None = None,
    key: str | None = None,
) -> BackendError:
    """Classify a Google Cloud Storage exception into BackendError with error_type.

    Args:
        exc: Original exception from google-cloud-storage, google-auth or the HTTP transport.
        operation: Operation that failed (get, set, delete, exists, health_check).
        key: Cache key involved (optional, for debugging).

    Returns:
        BackendError with appropriate error_type classification.

    Examples:
        A 403 is an authentication error:

        >>> from google.api_core.exceptions import Forbidden, ServiceUnavailable
        >>> classify_gcs_error(Forbidden("denied"), operation="get").error_type.value
        'authentication'

        A 503 is transient, and a transport timeout gets its own category:

        >>> classify_gcs_error(ServiceUnavailable("busy"), operation="set").error_type.value
        'transient'
        >>> classify_gcs_error(TimeoutError(), operation="get").error_type.value
        'timeout'
    """
    error_type, label = _classify(exc)
    # Only the exception TYPE and the HTTP status go in the message: Google's error text carries the
    # request URL, which names the bucket and object, and the message reaches log sinks via str(e)
    # (CWE-532). Full details stay on original_exception.
    status = getattr(exc, "code", None)
    detail = f"{type(exc).__name__} (HTTP {status})" if isinstance(status, int) else type(exc).__name__
    return BackendError(
        message=f"GCS {label} during {operation}: {detail}",
        error_type=error_type,
        original_exception=exc,
        operation=operation,
        key=key,
    )
