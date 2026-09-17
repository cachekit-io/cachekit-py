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
        - HTTP 413: PERMANENT (value too large — retrying never helps)
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

        # PERMANENT: value too large. A 413 would already classify PERMANENT via the generic
        # 4xx branch below — this dedicated branch exists only to give an ACTIONABLE message
        # ("value too large") instead of "Client error: HTTP 413". Retrying never helps (the
        # value must shrink), so the decorator degrades: runs uncached, once.
        if status == 413:
            return BackendError(
                "Value too large for cachekit.io backend (HTTP 413): value exceeds the server's maximum cache value size",
                error_type=BackendErrorType.PERMANENT,
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

    # TIMEOUT: Request exceeded time limit.
    # Only the exception TYPE goes in the message: httpx exception text embeds the
    # request URL, which carries the raw cache key in its path, and the message reaches
    # log sinks via str(e) (CWE-532, LAB-304). Detail stays on original_exception.
    if isinstance(exc, httpx.TimeoutException):
        return BackendError(
            f"Request timeout: {type(exc).__name__}",
            error_type=BackendErrorType.TIMEOUT,
            original_exception=exc,
            operation=operation,
            key=key,
        )

    # TRANSIENT: Connection failures (retry). Type-only message — httpx text can echo
    # the request URL (raw key in path), and str(e) reaches log sinks (CWE-532).
    if isinstance(exc, (httpx.ConnectError, httpx.NetworkError)):
        return BackendError(
            f"Connection failed: {type(exc).__name__}",
            error_type=BackendErrorType.TRANSIENT,
            original_exception=exc,
            operation=operation,
            key=key,
        )

    # UNKNOWN: Unclassified error. Type-only message (CWE-532): arbitrary httpx text
    # can echo the request URL, which carries the raw key. Detail on original_exception.
    return BackendError(
        f"Unknown HTTP error: {type(exc).__name__}",
        error_type=BackendErrorType.UNKNOWN,
        original_exception=exc,
        operation=operation,
        key=key,
    )
