"""HTTP exception classification for backend abstraction."""

from __future__ import annotations

from typing import TYPE_CHECKING

import httpx

from cachekit.backends.errors import BackendError, BackendErrorType

if TYPE_CHECKING:
    pass


def classify_http_error(
    exc: Exception,
    response: httpx.Response | None = None,
    operation: str | None = None,
    key: str | None = None,
) -> BackendError:
    """Classify HTTP exception into BackendError with error_type.

    Maps HTTP status codes and network exceptions to BackendErrorType
    categories for circuit breaker and retry logic.

    Args:
        exc: Original exception
        response: HTTP response if available
        operation: Operation that failed (get, set, delete, etc.)
        key: Cache key involved (optional, for debugging)

    Returns:
        BackendError with appropriate error_type classification

    Classification rules:
        - HTTP 401/403: AUTHENTICATION (alert ops, don't retry)
        - HTTP 429: TRANSIENT (rate limit, exponential backoff)
        - HTTP 5xx: TRANSIENT (server error, retry)
        - HTTP 4xx: PERMANENT (client error, don't retry)
        - TimeoutException: TIMEOUT (configurable retry)
        - ConnectError: TRANSIENT (network issue, retry)
        - All others: UNKNOWN (log and investigate)
    """
    # HTTP status code classification
    if response is not None:
        status = response.status_code

        # AUTHENTICATION: Credential/auth issues
        if status in (401, 403):
            return BackendError(
                f"Authentication failed: HTTP {status}",
                error_type=BackendErrorType.AUTHENTICATION,
                original_exception=exc,
                operation=operation,
                key=key,
            )

        # TRANSIENT: Rate limiting (exponential backoff)
        if status == 429:
            return BackendError(
                "Rate limit exceeded",
                error_type=BackendErrorType.TRANSIENT,
                original_exception=exc,
                operation=operation,
                key=key,
            )

        # TRANSIENT: Server errors (retry with backoff)
        if 500 <= status < 600:
            return BackendError(
                f"Server error: HTTP {status}",
                error_type=BackendErrorType.TRANSIENT,
                original_exception=exc,
                operation=operation,
                key=key,
            )

        # PERMANENT: Client errors (don't retry)
        if 400 <= status < 500:
            return BackendError(
                f"Client error: HTTP {status}",
                error_type=BackendErrorType.PERMANENT,
                original_exception=exc,
                operation=operation,
                key=key,
            )

    # TIMEOUT: Request exceeded time limit
    if isinstance(exc, httpx.TimeoutException):
        return BackendError(
            f"Request timeout: {exc}",
            error_type=BackendErrorType.TIMEOUT,
            original_exception=exc,
            operation=operation,
            key=key,
        )

    # TRANSIENT: Connection failures (retry)
    if isinstance(exc, (httpx.ConnectError, httpx.NetworkError)):
        return BackendError(
            f"Connection failed: {exc}",
            error_type=BackendErrorType.TRANSIENT,
            original_exception=exc,
            operation=operation,
            key=key,
        )

    # UNKNOWN: Unclassified error
    return BackendError(
        f"Unknown HTTP error: {exc}",
        error_type=BackendErrorType.UNKNOWN,
        original_exception=exc,
        operation=operation,
        key=key,
    )
