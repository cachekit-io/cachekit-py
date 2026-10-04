"""HTTP exception classification for backend abstraction."""

from __future__ import annotations

from typing import TYPE_CHECKING

from urllib3 import exceptions as u3

from cachekit.backends.errors import BackendError, BackendErrorType

if TYPE_CHECKING:
    from urllib3 import BaseHTTPResponse


class HTTPStatusError(Exception):
    """A response with an error status, kept as the BackendError's ``original_exception``.

    The message names the status only: the response carries no request URL, so neither does this.
    """

    def __init__(self, response: BaseHTTPResponse) -> None:
        super().__init__(f"HTTP {response.status}")
        self.response = response


def classify_http_error(
    exc: Exception,
    response: BaseHTTPResponse | None = None,
    operation: str | None = None,
    key: str | None = None,
) -> BackendError:
    """Classify HTTP exception into BackendError with error_type.

    Maps HTTP status codes and network exceptions to BackendErrorType
    categories for the circuit breaker. A PUT or DELETE answered 503 with ``Retry-After``
    of at most 2 s is sent once more before it gets here; every other request is sent once.

    Args:
        exc: Original exception
        response: HTTP response if available
        operation: Operation that failed (get, set, delete, etc.)
        key: Cache key involved (optional, for debugging)

    Returns:
        BackendError with appropriate error_type classification

    Classification rules:
        - HTTP 401/403: AUTHENTICATION (alert ops)
        - HTTP 429: TRANSIENT (rate limit)
        - HTTP 413: PERMANENT (value too large — retrying never helps)
        - HTTP 5xx: TRANSIENT (server error)
        - HTTP 4xx: PERMANENT (client error)
        - urllib3 TimeoutError, EmptyPoolError: TIMEOUT (request, or the wait for a pooled
          connection, exceeded the time limit)
        - NewConnectionError, ProtocolError, SSLError, ProxyError: TRANSIENT (network issue)
        - All others: UNKNOWN (log and investigate)
    """
    # HTTP status code classification
    if response is not None:
        status = response.status

        # AUTHENTICATION: Credential/auth issues
        if status in (401, 403):
            return BackendError(
                f"Authentication failed: HTTP {status}",
                error_type=BackendErrorType.AUTHENTICATION,
                original_exception=exc,
                operation=operation,
                key=key,
            )

        # TRANSIENT: Rate limiting
        if status == 429:
            return BackendError(
                "Rate limit exceeded",
                error_type=BackendErrorType.TRANSIENT,
                original_exception=exc,
                operation=operation,
                key=key,
            )

        # TRANSIENT: Server errors
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

        # PERMANENT: Client errors
        if 400 <= status < 500:
            return BackendError(
                f"Client error: HTTP {status}",
                error_type=BackendErrorType.PERMANENT,
                original_exception=exc,
                operation=operation,
                key=key,
            )

    # TRANSIENT: Connection failures. Checked before TIMEOUT: urllib3's NewConnectionError subclasses
    # its ConnectTimeoutError. Only the exception TYPE goes in the message: urllib3 exception text
    # can embed the request URL, which carries the raw cache key in its path, and the message reaches
    # log sinks via str(e) (CWE-532, LAB-304). Detail stays on original_exception.
    if isinstance(exc, (u3.NewConnectionError, u3.ProtocolError, u3.SSLError, u3.ProxyError)):
        return BackendError(
            f"Connection failed: {type(exc).__name__}",
            error_type=BackendErrorType.TRANSIENT,
            original_exception=exc,
            operation=operation,
            key=key,
        )

    # TIMEOUT: Request, or the wait for a free pooled connection, exceeded the time limit.
    if isinstance(exc, (u3.TimeoutError, u3.EmptyPoolError)):
        return BackendError(
            f"Request timeout: {type(exc).__name__}",
            error_type=BackendErrorType.TIMEOUT,
            original_exception=exc,
            operation=operation,
            key=key,
        )

    # UNKNOWN: Unclassified error. Type-only message (CWE-532): arbitrary urllib3 text
    # can echo the request URL, which carries the raw key. Detail on original_exception.
    return BackendError(
        f"Unknown HTTP error: {type(exc).__name__}",
        error_type=BackendErrorType.UNKNOWN,
        original_exception=exc,
        operation=operation,
        key=key,
    )
