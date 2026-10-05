"""Tests for CachekitIO HTTP error classification.

Every rule is pinned twice: directly through ``classify_http_error``, and end to end through a backend on a
fake pool (tests/utils/cachekitio_fakes.py), sync and async, so the classification the circuit breaker sees is
the one a real urllib3 response or exception produces.
"""

from __future__ import annotations

import asyncio
import pickle
import socket
import ssl
from collections.abc import Callable

import pytest
from urllib3 import BaseHTTPResponse
from urllib3 import exceptions as u3

from cachekit.backends.cachekitio.backend import _rate_limit_delay
from cachekit.backends.cachekitio.error_handler import HTTPStatusError, HTTPTransportError, classify_http_error
from cachekit.backends.errors import BackendError, BackendErrorType
from tests.utils.cachekitio_fakes import TEST_API_KEY, FakeRequest, fake_backend, response

pytestmark = pytest.mark.unit

# urllib3 exception text can embed the request URL, whose path carries the raw cache key. Every fixture
# exception below carries both, so a message that leaked either would fail the type-only assertions.
_SECRET_KEY = "user:secret-42"  # pragma: allowlist secret — fake credentials, test fixture
_URL = "https://api.cachekit.io/v1/cache/user%3Asecret-42"


def _status_error(status: int) -> tuple[HTTPStatusError, BaseHTTPResponse]:
    resp = response(status)
    return HTTPStatusError(status, resp), resp


def _send(mode: str, handler: Callable[[FakeRequest], BaseHTTPResponse]) -> BackendError:
    """Send one PUT (sync or async) to ``handler`` and return the BackendError it raises.

    PUT, not GET: a GET treats 404 as a miss, and the status rules must see every status.
    """
    backend, pool = fake_backend(handler)
    with pytest.raises(BackendError) as excinfo:
        if mode == "sync":
            backend.set(_SECRET_KEY, b"v")
        else:
            asyncio.run(backend.set_async(_SECRET_KEY, b"v"))
    assert len(pool.requests) == 1, "a classified failure is sent once"
    return excinfo.value


# (status, error type, message)
_STATUS_RULES = [
    (401, BackendErrorType.AUTHENTICATION, "Authentication failed: HTTP 401"),
    (403, BackendErrorType.AUTHENTICATION, "Authentication failed: HTTP 403"),
    (429, BackendErrorType.TRANSIENT, "Rate limit exceeded"),
    (500, BackendErrorType.TRANSIENT, "Server error: HTTP 500"),
    (502, BackendErrorType.TRANSIENT, "Server error: HTTP 502"),
    (503, BackendErrorType.TRANSIENT, "Server error: HTTP 503"),
    (504, BackendErrorType.TRANSIENT, "Server error: HTTP 504"),
    (400, BackendErrorType.PERMANENT, "Client error: HTTP 400"),
    (404, BackendErrorType.PERMANENT, "Client error: HTTP 404"),
    (409, BackendErrorType.PERMANENT, "Client error: HTTP 409"),
    (422, BackendErrorType.PERMANENT, "Client error: HTTP 422"),
    # No rule matches a 3xx (requests are sent with redirect=False): it is UNKNOWN, still a status with its response.
    (302, BackendErrorType.UNKNOWN, "Unknown HTTP error: HTTPStatusError"),
]


class TestHTTPStatusClassification:
    """Tests for HTTP status code → error type mapping."""

    @pytest.mark.parametrize(("status", "error_type", "message"), _STATUS_RULES)
    def test_status_rule(self, status: int, error_type: BackendErrorType, message: str) -> None:
        exc, resp = _status_error(status)
        result = classify_http_error(exc, response=resp)
        assert result.error_type == error_type
        assert result.message == message
        assert result.original_exception is exc

    def test_value_too_large_413_is_permanent(self) -> None:
        # Regression: a too-large value previously surfaced as a 500 → TRANSIENT and was
        # retried 3× before a silent graceful-degrade. 413 must be PERMANENT (retrying
        # never helps — the value must shrink) with a clear "too large" message.
        exc, resp = _status_error(413)
        result = classify_http_error(exc, response=resp, operation="put")
        assert result.error_type == BackendErrorType.PERMANENT
        assert "too large" in str(result).lower()

    def test_status_error_names_only_the_status(self) -> None:
        """HTTPStatusError is the cause logged with the BackendError: its text carries no URL."""
        exc, resp = _status_error(500)
        assert str(exc) == "HTTP 500"
        assert exc.status == 500
        assert exc.response is resp


class TestHTTPStatusEndToEnd:
    """A real response status, through the backend's request path, classifies as the rule says."""

    @pytest.mark.parametrize("mode", ["sync", "async"])
    @pytest.mark.parametrize(("status", "error_type", "message"), _STATUS_RULES)
    def test_status_rule(self, mode: str, status: int, error_type: BackendErrorType, message: str) -> None:
        err = _send(mode, lambda request: response(status))
        assert err.error_type == error_type
        assert err.message == message
        assert err.operation == "put"
        assert isinstance(err.original_exception, HTTPStatusError)
        assert err.original_exception.status == status

    @pytest.mark.parametrize("mode", ["sync", "async"])
    def test_value_too_large_413_is_permanent(self, mode: str) -> None:
        err = _send(mode, lambda request: response(413))
        assert err.error_type == BackendErrorType.PERMANENT
        assert "too large" in err.message.lower()
        assert isinstance(err.original_exception, HTTPStatusError)

    @pytest.mark.parametrize("mode", ["sync", "async"])
    def test_rate_limit_keeps_retry_after_on_the_cause(self, mode: str) -> None:
        """Paced invalidation reads Retry-After from the 429's cause (_rate_limit_delay): the response must ride along."""
        err = _send(mode, lambda request: response(429, headers={"Retry-After": "7"}))
        assert err.error_type == BackendErrorType.TRANSIENT
        assert isinstance(err.original_exception, HTTPStatusError)
        assert err.original_exception.response is not None
        assert err.original_exception.response.headers["Retry-After"] == "7"
        assert _rate_limit_delay(err) == 7


# (exception factory, error type, message). Each factory builds the exception as urllib3 raises it.
def _cert_error(verify_code: int, message: str) -> ssl.SSLCertVerificationError:
    """The verification error OpenSSL raises, with its X509_V_ERR code (20: unknown issuer, 62: hostname mismatch)."""
    err = ssl.SSLCertVerificationError(1, message)
    err.verify_code = verify_code
    return err


_TRANSPORT_RULES: list[tuple[Callable[[], Exception], BackendErrorType, str]] = [
    (
        lambda: u3.NewConnectionError(None, f"Failed to establish a new connection to {_URL}: [Errno 111] refused"),  # type: ignore[arg-type]
        BackendErrorType.TRANSIENT,
        "Connection failed: NewConnectionError",
    ),
    (
        lambda: u3.NameResolutionError("api.cachekit.io", None, socket.gaierror(-2, f"Name unknown for {_URL}")),  # type: ignore[arg-type]
        BackendErrorType.TRANSIENT,
        "Connection failed: NameResolutionError",
    ),
    (
        lambda: u3.ProtocolError(f"Connection aborted. {_URL}", ConnectionResetError()),
        BackendErrorType.TRANSIENT,
        "Connection failed: ProtocolError",
    ),
    (
        lambda: u3.SSLError(f"EOF occurred in violation of protocol for {_URL}"),
        BackendErrorType.TRANSIENT,
        "Connection failed: SSLError",
    ),
    (
        # A hostname mismatch is a certificate failure, but not the trust store's: it keeps the type-only message.
        lambda: u3.SSLError(_cert_error(62, f"Hostname mismatch, certificate is not valid for {_URL}")),
        BackendErrorType.TRANSIENT,
        "Connection failed: SSLError",
    ),
    (
        lambda: u3.ProxyError(f"Unable to connect to proxy for {_URL}", OSError("refused")),
        BackendErrorType.TRANSIENT,
        "Connection failed: ProxyError",
    ),
    (
        lambda: u3.ConnectTimeoutError(None, f"Connection to {_URL} timed out. (connect timeout=5)"),
        BackendErrorType.TIMEOUT,
        "Request timeout: ConnectTimeoutError",
    ),
    (
        lambda: u3.ReadTimeoutError(None, _URL, f"Read timed out for {_URL}. (read timeout=5)"),  # type: ignore[arg-type]
        BackendErrorType.TIMEOUT,
        "Request timeout: ReadTimeoutError",
    ),
    (
        # A host with no CA bundle: the message names the fix, still with no URL.
        lambda: u3.SSLError(_cert_error(20, f"unable to get local issuer certificate for {_URL}")),
        BackendErrorType.TRANSIENT,
        "Connection failed: certificate verification failed against the system trust store (see SSL_CERT_FILE)",
    ),
    (
        lambda: u3.ClosedPoolError(None, f"Pool for {_URL} is closed."),  # type: ignore[arg-type]
        BackendErrorType.UNKNOWN,
        "Unknown HTTP error: ClosedPoolError",
    ),
    (
        lambda: ValueError(f"unexpected {_URL}"),
        BackendErrorType.UNKNOWN,
        "Unknown HTTP error: ValueError",
    ),
]
_TRANSPORT_IDS = [message.split(": ")[1] for _, _, message in _TRANSPORT_RULES]


def _assert_type_only(err: BackendError) -> None:
    """CWE-532: the formatted error reaches log sinks, so it names the exception type and nothing of its text."""
    text = str(err)
    assert "secret-42" not in text
    assert "api.cachekit.io" not in text
    assert TEST_API_KEY not in text


def _assert_class_only_cause(err: BackendError, exc: Exception) -> None:
    """CWE-532: the error keeps urllib3's exception class, never the exception, whose traceback runs through urllib3's
    request frames and their Authorization header (tests/unit/backends/test_cachekitio_transport_error_frames.py)."""
    cause = err.original_exception
    assert isinstance(cause, HTTPTransportError)
    assert cause.exc_type is type(exc)
    assert str(cause) == type(exc).__name__
    assert cause.__traceback__ is None


class TestNetworkExceptionClassification:
    """Tests for network-level exception → error type mapping."""

    @pytest.mark.parametrize(("make_exc", "error_type", "message"), _TRANSPORT_RULES, ids=_TRANSPORT_IDS)
    def test_transport_rule(self, make_exc: Callable[[], Exception], error_type: BackendErrorType, message: str) -> None:
        exc = make_exc()
        result = classify_http_error(exc)
        assert result.error_type == error_type
        assert result.message == message
        _assert_class_only_cause(result, exc)
        _assert_type_only(result)

    @pytest.mark.parametrize("make_exc", [_TRANSPORT_RULES[0][0], _TRANSPORT_RULES[1][0]], ids=_TRANSPORT_IDS[:2])
    def test_connection_failure_is_not_a_timeout(self, make_exc: Callable[[], Exception]) -> None:
        """urllib3's NewConnectionError (and NameResolutionError under it) subclasses ConnectTimeoutError.

        A refused or unresolvable host is a connection failure, not a slow server: the connection rule must be
        checked before the timeout rule, or every refused connection reads as TIMEOUT.
        """
        exc = make_exc()
        assert isinstance(exc, u3.ConnectTimeoutError)
        assert classify_http_error(exc).error_type == BackendErrorType.TRANSIENT


class TestNetworkExceptionEndToEnd:
    """A urllib3 exception raised by the pool reaches the caller as the classified BackendError."""

    @pytest.mark.parametrize("mode", ["sync", "async"])
    @pytest.mark.parametrize(("make_exc", "error_type", "message"), _TRANSPORT_RULES, ids=_TRANSPORT_IDS)
    def test_transport_rule(
        self, mode: str, make_exc: Callable[[], Exception], error_type: BackendErrorType, message: str
    ) -> None:
        exc = make_exc()

        def handler(request: FakeRequest) -> BaseHTTPResponse:
            raise exc

        err = _send(mode, handler)
        assert err.error_type == error_type
        assert err.message == message
        assert err.operation == "put"
        _assert_class_only_cause(err, exc)
        # Raised outside the except block: urllib3's exception is not even the implicit __context__.
        assert err.__cause__ is err.original_exception
        assert err.__context__ is None
        _assert_type_only(err)


class TestErrorPickle:
    """A classified BackendError survives pickling, as every BackendError does (a ProcessPoolExecutor worker's error)."""

    @pytest.mark.parametrize(("make_exc", "error_type", "message"), _TRANSPORT_RULES, ids=_TRANSPORT_IDS)
    def test_transport_round_trip(self, make_exc: Callable[[], Exception], error_type: BackendErrorType, message: str) -> None:
        exc = make_exc()
        err = pickle.loads(pickle.dumps(classify_http_error(exc, operation="put", key="k")))  # noqa: S301 (own object)
        assert (err.error_type, err.message, err.operation, err.key) == (error_type, message, "put", "k")
        _assert_class_only_cause(err, exc)

    @pytest.mark.parametrize(("status", "error_type", "message"), _STATUS_RULES)
    def test_status_round_trip(self, status: int, error_type: BackendErrorType, message: str) -> None:
        """The copy keeps the status and drops the response: a live one holds its connection pool, which does not pickle."""
        exc, resp = _status_error(status)
        err = pickle.loads(pickle.dumps(classify_http_error(exc, response=resp, operation="put", key="k")))  # noqa: S301
        assert (err.error_type, err.message, err.operation, err.key) == (error_type, message, "put", "k")
        cause = err.original_exception
        assert isinstance(cause, HTTPStatusError)
        assert (cause.status, str(cause), cause.response) == (status, f"HTTP {status}", None)

    def test_copied_rate_limit_has_no_wait(self) -> None:
        """A copied 429 has no response to read Retry-After from, so paced invalidation does not wait on it."""
        resp = response(429, headers={"Retry-After": "7"})
        err = classify_http_error(HTTPStatusError(429, resp), response=resp)
        assert _rate_limit_delay(err) == 7
        assert _rate_limit_delay(pickle.loads(pickle.dumps(err))) is None  # noqa: S301 (own object)


class TestContextPropagation:
    """Operation and key context are preserved on the returned error."""

    def test_operation_and_key_attached(self) -> None:
        exc, resp = _status_error(500)
        result = classify_http_error(exc, response=resp, operation="get", key="user:99")
        assert result.operation == "get"
        assert result.key == "user:99"

    def test_none_context_when_not_provided(self) -> None:
        exc, resp = _status_error(404)
        result = classify_http_error(exc, response=resp)
        assert result.operation is None
        assert result.key is None

    def test_returns_backend_error_instance(self) -> None:
        result = classify_http_error(Exception("x"))
        assert isinstance(result, BackendError)
