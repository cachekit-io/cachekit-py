"""Google Cloud Storage exception classification for backend abstraction.

Maps google-cloud-storage, google-auth and transport exceptions to BackendErrorType for the
circuit breaker.
"""

from __future__ import annotations

from cachekit.backends.errors import BackendError, BackendErrorType


def _classify(exc: Exception) -> tuple[BackendErrorType, str]:
    """The error type for exc, and the word that names it in the message."""
    import requests.exceptions
    from google.api_core.exceptions import GoogleAPICallError
    from google.auth.exceptions import DefaultCredentialsError, GoogleAuthError, RefreshError

    # Before the OSError branch: requests' exceptions are OSErrors, and its ConnectTimeout is also
    # a ConnectionError.
    if isinstance(exc, (requests.exceptions.Timeout, TimeoutError)):
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
    if isinstance(exc, (DefaultCredentialsError, RefreshError)):
        return BackendErrorType.AUTHENTICATION, "authentication error"
    # Any other auth failure is the token endpoint or metadata server being unreachable.
    if isinstance(exc, (GoogleAuthError, OSError)):
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
