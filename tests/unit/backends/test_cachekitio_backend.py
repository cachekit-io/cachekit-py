"""Unit tests for CachekitIOBackend sync methods.

Tests backend.py method logic using mocked httpx clients.
Async methods mirror the same logic and are not duplicated here.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import httpx
import pytest

from cachekit.backends.cachekitio.backend import CachekitIOBackend
from cachekit.backends.errors import BackendError, BackendErrorType

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_TEST_API_URL = "https://api.cachekit.io"
_TEST_API_KEY = "ck_test_abc123"


_DUMMY_REQUEST = httpx.Request("GET", "https://api.cachekit.io/v1/cache/key")


def _make_response(status_code: int, content: bytes = b"", json_body: dict[str, Any] | None = None) -> httpx.Response:
    """Build a real httpx.Response with a request attached (required for raise_for_status)."""
    if json_body is not None:
        import json

        content = json.dumps(json_body).encode()
    response = httpx.Response(status_code, content=content)
    response.request = _DUMMY_REQUEST
    return response


def _make_status_error(status_code: int) -> httpx.HTTPStatusError:
    """Build an httpx.HTTPStatusError wrapping a response of the given status."""
    response = _make_response(status_code)
    request = httpx.Request("GET", "https://api.cachekit.io/v1/cache/key")
    return httpx.HTTPStatusError(f"HTTP {status_code}", request=request, response=response)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_sync_client() -> Any:
    """Yield a mock httpx.Client and patch both client factories during backend init."""
    client = MagicMock(spec=httpx.Client)
    with patch(
        "cachekit.backends.cachekitio.backend.get_sync_http_client",
        return_value=client,
    ):
        with patch(
            "cachekit.backends.cachekitio.backend.get_cached_async_http_client",
            return_value=MagicMock(spec=httpx.AsyncClient),
        ):
            yield client


@pytest.fixture
def backend(mock_sync_client: MagicMock) -> CachekitIOBackend:
    """Create CachekitIOBackend with mocked HTTP clients."""
    return CachekitIOBackend(
        api_url=_TEST_API_URL,
        api_key=_TEST_API_KEY,
    )


# ---------------------------------------------------------------------------
# TestInit
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestInit:
    """Tests for CachekitIOBackend.__init__."""

    def test_manual_config_accepted(self, mock_sync_client: MagicMock) -> None:
        """Both api_url and api_key provided: backend initialises cleanly."""
        b = CachekitIOBackend(api_url=_TEST_API_URL, api_key=_TEST_API_KEY)
        assert b._config.api_url == _TEST_API_URL
        assert b._config.api_key.get_secret_value() == _TEST_API_KEY

    def test_timeout_override_stored(self, mock_sync_client: MagicMock) -> None:
        """Explicit timeout is stored in config."""
        b = CachekitIOBackend(api_url=_TEST_API_URL, api_key=_TEST_API_KEY, timeout=30.0)
        assert b._config.timeout == 30.0

    def test_timeout_defaults_to_five(self, mock_sync_client: MagicMock) -> None:
        """Omitting timeout defaults to 5.0 seconds."""
        b = CachekitIOBackend(api_url=_TEST_API_URL, api_key=_TEST_API_KEY)
        assert b._config.timeout == 5.0

    def test_partial_config_raises_value_error_no_key(self, mock_sync_client: MagicMock) -> None:
        """api_url without api_key raises ValueError."""
        with pytest.raises(ValueError, match="Both api_url and api_key required"):
            CachekitIOBackend(api_url=_TEST_API_URL)

    def test_partial_config_raises_value_error_no_url(self, mock_sync_client: MagicMock) -> None:
        """api_key without api_url raises ValueError."""
        with pytest.raises(ValueError, match="Both api_url and api_key required"):
            CachekitIOBackend(api_key=_TEST_API_KEY)

    def test_env_based_config(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """All-None args triggers env-based config load."""
        monkeypatch.setenv("CACHEKIT_API_KEY", _TEST_API_KEY)
        with patch("cachekit.backends.cachekitio.backend.get_sync_http_client", return_value=MagicMock(spec=httpx.Client)):
            with patch(
                "cachekit.backends.cachekitio.backend.get_cached_async_http_client",
                return_value=MagicMock(spec=httpx.AsyncClient),
            ):
                b = CachekitIOBackend()
                assert b._config.api_key.get_secret_value() == _TEST_API_KEY


# ---------------------------------------------------------------------------
# TestGet
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestGet:
    """Tests for CachekitIOBackend.get()."""

    def test_cache_hit_returns_bytes(self, backend: CachekitIOBackend, mock_sync_client: MagicMock) -> None:
        """200 response returns response.content bytes."""
        payload = b"cached-value"
        mock_sync_client.request.return_value = _make_response(200, content=payload)

        result = backend.get("my-key")

        assert result == payload
        mock_sync_client.request.assert_called_once()
        call_args = mock_sync_client.request.call_args
        assert call_args[0][0] == "GET"
        assert "my-key" in call_args[0][1]

    def test_cache_miss_returns_none(self, backend: CachekitIOBackend, mock_sync_client: MagicMock) -> None:
        """404 HTTPStatusError is caught and returns None (cache miss)."""
        exc_404 = _make_status_error(404)
        # _request_sync raises a BackendError wrapping exc_404
        backend_error = BackendError(
            "Client error: HTTP 404",
            error_type=BackendErrorType.PERMANENT,
            original_exception=exc_404,
        )
        mock_sync_client.request.side_effect = exc_404

        # Patch classify_http_error to produce the BackendError so raise_for_status fires
        with patch("cachekit.backends.cachekitio.backend.classify_http_error", return_value=backend_error):
            # Trigger the raise path: raise_for_status will throw, classify is called
            mock_sync_client.request.return_value = _make_response(404)
            mock_sync_client.request.side_effect = None
            # Simulate the full path: request succeeds but raise_for_status raises
            real_response = _make_response(404)

            def side_effect_raise(*args: Any, **kwargs: Any) -> httpx.Response:
                return real_response

            mock_sync_client.request.side_effect = side_effect_raise

            with patch.object(real_response, "raise_for_status", side_effect=exc_404):
                result = backend.get("missing-key")

        assert result is None

    def test_non_404_error_reraises(self, backend: CachekitIOBackend, mock_sync_client: MagicMock) -> None:
        """500 BackendError propagates from get()."""
        exc_500 = _make_status_error(500)
        backend_error = BackendError(
            "Server error: HTTP 500",
            error_type=BackendErrorType.TRANSIENT,
            original_exception=exc_500,
        )
        real_response = _make_response(500)

        def side_effect_raise(*args: Any, **kwargs: Any) -> httpx.Response:
            return real_response

        mock_sync_client.request.side_effect = side_effect_raise

        with patch("cachekit.backends.cachekitio.backend.classify_http_error", return_value=backend_error):
            with patch.object(real_response, "raise_for_status", side_effect=exc_500):
                with pytest.raises(BackendError) as exc_info:
                    backend.get("bad-key")

        assert exc_info.value.error_type == BackendErrorType.TRANSIENT


# ---------------------------------------------------------------------------
# TestSet
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestSet:
    """Tests for CachekitIOBackend.set()."""

    def test_set_sends_put(self, backend: CachekitIOBackend, mock_sync_client: MagicMock) -> None:
        """set() issues a PUT request."""
        mock_sync_client.request.return_value = _make_response(200)

        backend.set("cache-key", b"data")

        call_args = mock_sync_client.request.call_args
        assert call_args[0][0] == "PUT"
        assert "cache-key" in call_args[0][1]

    def test_set_with_ttl_includes_x_ttl_header(self, backend: CachekitIOBackend, mock_sync_client: MagicMock) -> None:
        """When ttl is provided, X-TTL header is included in the request."""
        mock_sync_client.request.return_value = _make_response(200)

        backend.set("cache-key", b"data", ttl=300)

        call_kwargs = mock_sync_client.request.call_args[1]
        headers = call_kwargs.get("headers", {})
        assert "X-TTL" in headers
        assert headers["X-TTL"] == "300"

    def test_set_without_ttl_omits_x_ttl_header(self, backend: CachekitIOBackend, mock_sync_client: MagicMock) -> None:
        """When ttl is None, X-TTL header is NOT present."""
        mock_sync_client.request.return_value = _make_response(200)

        backend.set("cache-key", b"data", ttl=None)

        call_kwargs = mock_sync_client.request.call_args[1]
        headers = call_kwargs.get("headers", {})
        assert "X-TTL" not in headers

    def test_set_passes_content_bytes(self, backend: CachekitIOBackend, mock_sync_client: MagicMock) -> None:
        """set() forwards the value bytes as content."""
        mock_sync_client.request.return_value = _make_response(200)
        payload = b"\x00\x01\x02binary-data"

        backend.set("cache-key", payload)

        call_kwargs = mock_sync_client.request.call_args[1]
        assert call_kwargs.get("content") == payload


# ---------------------------------------------------------------------------
# TestDelete
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestDelete:
    """Tests for CachekitIOBackend.delete()."""

    def test_delete_success_returns_true(self, backend: CachekitIOBackend, mock_sync_client: MagicMock) -> None:
        """Successful DELETE returns True."""
        mock_sync_client.request.return_value = _make_response(200)

        result = backend.delete("del-key")

        assert result is True
        call_args = mock_sync_client.request.call_args
        assert call_args[0][0] == "DELETE"

    def test_delete_404_returns_false(self, backend: CachekitIOBackend, mock_sync_client: MagicMock) -> None:
        """404 on DELETE returns False (key didn't exist)."""
        exc_404 = _make_status_error(404)
        backend_error = BackendError(
            "Client error: HTTP 404",
            error_type=BackendErrorType.PERMANENT,
            original_exception=exc_404,
        )
        real_response = _make_response(404)

        def side_effect_raise(*args: Any, **kwargs: Any) -> httpx.Response:
            return real_response

        mock_sync_client.request.side_effect = side_effect_raise

        with patch("cachekit.backends.cachekitio.backend.classify_http_error", return_value=backend_error):
            with patch.object(real_response, "raise_for_status", side_effect=exc_404):
                result = backend.delete("missing-key")

        assert result is False

    def test_delete_server_error_reraises(self, backend: CachekitIOBackend, mock_sync_client: MagicMock) -> None:
        """Non-404 BackendError from DELETE propagates."""
        exc_503 = _make_status_error(503)
        backend_error = BackendError(
            "Server error: HTTP 503",
            error_type=BackendErrorType.TRANSIENT,
            original_exception=exc_503,
        )
        real_response = _make_response(503)

        def side_effect_raise(*args: Any, **kwargs: Any) -> httpx.Response:
            return real_response

        mock_sync_client.request.side_effect = side_effect_raise

        with patch("cachekit.backends.cachekitio.backend.classify_http_error", return_value=backend_error):
            with patch.object(real_response, "raise_for_status", side_effect=exc_503):
                with pytest.raises(BackendError) as exc_info:
                    backend.delete("del-key")

        assert exc_info.value.error_type == BackendErrorType.TRANSIENT


# ---------------------------------------------------------------------------
# TestExists
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestExists:
    """Tests for CachekitIOBackend.exists()."""

    def test_exists_uses_head_method(self, backend: CachekitIOBackend, mock_sync_client: MagicMock) -> None:
        """exists() issues a HEAD request, not GET."""
        mock_sync_client.request.return_value = _make_response(200)

        result = backend.exists("some-key")

        assert result is True
        call_args = mock_sync_client.request.call_args
        assert call_args[0][0] == "HEAD"

    def test_exists_true_on_200(self, backend: CachekitIOBackend, mock_sync_client: MagicMock) -> None:
        """200 response means key exists."""
        mock_sync_client.request.return_value = _make_response(200)
        assert backend.exists("some-key") is True

    def test_exists_false_on_404(self, backend: CachekitIOBackend, mock_sync_client: MagicMock) -> None:
        """404 BackendError caught, returns False."""
        exc_404 = _make_status_error(404)
        backend_error = BackendError(
            "Client error: HTTP 404",
            error_type=BackendErrorType.PERMANENT,
            original_exception=exc_404,
        )
        real_response = _make_response(404)

        def side_effect_raise(*args: Any, **kwargs: Any) -> httpx.Response:
            return real_response

        mock_sync_client.request.side_effect = side_effect_raise

        with patch("cachekit.backends.cachekitio.backend.classify_http_error", return_value=backend_error):
            with patch.object(real_response, "raise_for_status", side_effect=exc_404):
                result = backend.exists("missing-key")

        assert result is False

    def test_exists_reraises_non_404(self, backend: CachekitIOBackend, mock_sync_client: MagicMock) -> None:
        """Non-404 BackendError from exists() propagates."""
        exc_401 = _make_status_error(401)
        backend_error = BackendError(
            "Authentication failed: HTTP 401",
            error_type=BackendErrorType.AUTHENTICATION,
            original_exception=exc_401,
        )
        real_response = _make_response(401)

        def side_effect_raise(*args: Any, **kwargs: Any) -> httpx.Response:
            return real_response

        mock_sync_client.request.side_effect = side_effect_raise

        with patch("cachekit.backends.cachekitio.backend.classify_http_error", return_value=backend_error):
            with patch.object(real_response, "raise_for_status", side_effect=exc_401):
                with pytest.raises(BackendError) as exc_info:
                    backend.exists("auth-key")

        assert exc_info.value.error_type == BackendErrorType.AUTHENTICATION


# ---------------------------------------------------------------------------
# TestHealthCheck
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestHealthCheck:
    """Tests for CachekitIOBackend.health_check()."""

    def test_healthy_returns_true_with_details(self, backend: CachekitIOBackend, mock_sync_client: MagicMock) -> None:
        """Successful health check returns (True, details) with expected keys."""
        mock_sync_client.request.return_value = _make_response(200, json_body={"version": "1.2.3"})

        healthy, details = backend.health_check()

        assert healthy is True
        assert details["backend_type"] == "saas"
        assert "latency_ms" in details
        assert details["latency_ms"] >= 0
        assert details["version"] == "1.2.3"
        assert "api_url" in details

    def test_healthy_latency_is_numeric(self, backend: CachekitIOBackend, mock_sync_client: MagicMock) -> None:
        """latency_ms in healthy response is a non-negative number."""
        mock_sync_client.request.return_value = _make_response(200, json_body={})

        _, details = backend.health_check()

        assert isinstance(details["latency_ms"], (int, float))
        assert details["latency_ms"] >= 0

    def test_unhealthy_returns_false_with_error(self, backend: CachekitIOBackend, mock_sync_client: MagicMock) -> None:
        """Backend error during health check returns (False, details) with error info."""
        mock_sync_client.request.side_effect = RuntimeError("connection refused")

        healthy, details = backend.health_check()

        assert healthy is False
        assert details["backend_type"] == "saas"
        assert details["latency_ms"] == -1
        assert "error" in details
        assert "error_type" in details

    def test_unhealthy_error_type_is_exception_class_name(self, backend: CachekitIOBackend, mock_sync_client: MagicMock) -> None:
        """error_type in failure details is the exception class name of what health_check catches.

        _request_sync wraps all exceptions in BackendError, so health_check() sees BackendError.
        """
        mock_sync_client.request.side_effect = RuntimeError("timeout")

        _, details = backend.health_check()

        # _request_sync converts RuntimeError -> BackendError before health_check sees it
        assert details["error_type"] == "BackendError"

    def test_health_check_version_defaults_to_unknown(self, backend: CachekitIOBackend, mock_sync_client: MagicMock) -> None:
        """Missing version in response body defaults to 'unknown'."""
        mock_sync_client.request.return_value = _make_response(200, json_body={})

        _, details = backend.health_check()

        assert details["version"] == "unknown"


# ---------------------------------------------------------------------------
# TestWithTimeout
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestWithTimeout:
    """Tests for CachekitIOBackend.with_timeout()."""

    def test_returns_new_instance(self, backend: CachekitIOBackend, mock_sync_client: MagicMock) -> None:
        """with_timeout() returns a different CachekitIOBackend object."""
        new_backend = backend.with_timeout(10.0)
        assert new_backend is not backend

    def test_new_instance_has_updated_timeout(self, backend: CachekitIOBackend, mock_sync_client: MagicMock) -> None:
        """New backend instance has the requested timeout."""
        new_backend = backend.with_timeout(42.0)
        assert new_backend._config.timeout == 42.0

    def test_original_instance_unchanged(self, backend: CachekitIOBackend, mock_sync_client: MagicMock) -> None:
        """Original backend timeout is unaffected by with_timeout()."""
        original_timeout = backend._config.timeout
        backend.with_timeout(99.0)
        assert backend._config.timeout == original_timeout

    def test_preserves_api_url_and_key(self, backend: CachekitIOBackend, mock_sync_client: MagicMock) -> None:
        """New instance preserves the same api_url and api_key."""
        new_backend = backend.with_timeout(7.0)
        assert new_backend._config.api_url == backend._config.api_url
        assert new_backend._config.api_key.get_secret_value() == backend._config.api_key.get_secret_value()


# ---------------------------------------------------------------------------
# TestRequestSyncErrorClassification
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestRequestSyncErrorClassification:
    """Tests for _request_sync() error classification via classify_http_error."""

    def test_http_status_error_is_classified(self, backend: CachekitIOBackend, mock_sync_client: MagicMock) -> None:
        """HTTPStatusError triggers classify_http_error and raises BackendError."""
        exc_500 = _make_status_error(500)
        real_response = _make_response(500)
        mock_sync_client.request.return_value = real_response

        with patch.object(real_response, "raise_for_status", side_effect=exc_500):
            with pytest.raises(BackendError):
                backend._request_sync("GET", "some-key")

    def test_non_http_exception_is_classified(self, backend: CachekitIOBackend, mock_sync_client: MagicMock) -> None:
        """Non-HTTP exceptions are also wrapped by classify_http_error."""
        mock_sync_client.request.side_effect = RuntimeError("unexpected")

        with pytest.raises(BackendError):
            backend._request_sync("GET", "some-key")

    def test_timeout_exception_raises_backend_error(self, backend: CachekitIOBackend, mock_sync_client: MagicMock) -> None:
        """httpx.TimeoutException is classified as TIMEOUT BackendError."""
        mock_sync_client.request.side_effect = httpx.TimeoutException("timed out")

        with pytest.raises(BackendError) as exc_info:
            backend._request_sync("GET", "some-key")

        assert exc_info.value.error_type == BackendErrorType.TIMEOUT
