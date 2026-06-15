"""Tests for CachekitIO HTTP error classification."""

import httpx
import pytest

from cachekit.backends.cachekitio.error_handler import classify_http_error
from cachekit.backends.errors import BackendError, BackendErrorType

pytestmark = pytest.mark.unit


def _response(status_code: int) -> httpx.Response:
    return httpx.Response(status_code=status_code, request=httpx.Request("GET", "http://test"))


class TestHTTPStatusClassification:
    """Tests for HTTP status code → error type mapping."""

    @pytest.mark.parametrize("status", [401, 403])
    def test_auth_errors(self, status: int) -> None:
        exc = Exception("auth failure")
        result = classify_http_error(exc, response=_response(status))
        assert result.error_type == BackendErrorType.AUTHENTICATION
        assert result.original_exception is exc

    def test_rate_limit_is_transient(self) -> None:
        exc = Exception("rate limited")
        result = classify_http_error(exc, response=_response(429))
        assert result.error_type == BackendErrorType.TRANSIENT

    @pytest.mark.parametrize("status", [500, 502, 503])
    def test_server_errors_are_transient(self, status: int) -> None:
        exc = Exception("server error")
        result = classify_http_error(exc, response=_response(status))
        assert result.error_type == BackendErrorType.TRANSIENT

    @pytest.mark.parametrize("status", [400, 404, 409])
    def test_client_errors_are_permanent(self, status: int) -> None:
        exc = Exception("client error")
        result = classify_http_error(exc, response=_response(status))
        assert result.error_type == BackendErrorType.PERMANENT

    def test_value_too_large_413_is_permanent(self) -> None:
        # Regression: a too-large value previously surfaced as a 500 → TRANSIENT and was
        # retried 3× before a silent graceful-degrade. 413 must be PERMANENT (retrying
        # never helps — the value must shrink) with a clear "too large" message.
        exc = Exception("payload too large")
        result = classify_http_error(exc, response=_response(413), operation="put")
        assert result.error_type == BackendErrorType.PERMANENT
        assert "too large" in str(result).lower()


class TestNetworkExceptionClassification:
    """Tests for network-level exception → error type mapping."""

    def test_timeout_exception(self) -> None:
        exc = httpx.ReadTimeout("timed out", request=httpx.Request("GET", "http://test"))
        result = classify_http_error(exc)
        assert result.error_type == BackendErrorType.TIMEOUT
        assert result.original_exception is exc

    def test_connect_error_is_transient(self) -> None:
        exc = httpx.ConnectError("connection refused")
        result = classify_http_error(exc)
        assert result.error_type == BackendErrorType.TRANSIENT

    def test_unknown_exception_is_unknown(self) -> None:
        exc = ValueError("unexpected")
        result = classify_http_error(exc)
        assert result.error_type == BackendErrorType.UNKNOWN


class TestContextPropagation:
    """Operation and key context are preserved on the returned error."""

    def test_operation_and_key_attached(self) -> None:
        exc = Exception("err")
        result = classify_http_error(exc, response=_response(500), operation="get", key="user:99")
        assert result.operation == "get"
        assert result.key == "user:99"

    def test_none_context_when_not_provided(self) -> None:
        exc = Exception("err")
        result = classify_http_error(exc, response=_response(404))
        assert result.operation is None
        assert result.key is None

    def test_returns_backend_error_instance(self) -> None:
        result = classify_http_error(Exception("x"))
        assert isinstance(result, BackendError)
