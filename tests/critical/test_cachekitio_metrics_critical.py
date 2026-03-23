"""Critical path tests for CachekitIO metrics header injection.

Covers the _inject_metrics_headers() function and the _make_request/_request_async
header merging logic changed in the standalone L1-Status fix.

Performance target: < 1 second total.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx
import pytest

from cachekit.backends.cachekitio.backend import CachekitIOBackend, _inject_metrics_headers

_TEST_API_URL = "https://api.cachekit.io"
_TEST_API_KEY = "ck_test_critical_metrics"
_DUMMY_REQUEST = httpx.Request("GET", f"{_TEST_API_URL}/v1/cache/key")


def _make_response(status_code: int = 200, content: bytes = b"") -> httpx.Response:
    response = httpx.Response(status_code, content=content)
    response.request = _DUMMY_REQUEST
    return response


@pytest.fixture
def mock_sync_client():
    client = MagicMock(spec=httpx.Client)
    with (
        patch("cachekit.backends.cachekitio.backend.get_sync_http_client", return_value=client),
        patch(
            "cachekit.backends.cachekitio.backend.get_cached_async_http_client", return_value=MagicMock(spec=httpx.AsyncClient)
        ),
    ):
        yield client


@pytest.fixture
def mock_async_client():
    """Mock both clients but yield the async one for async tests."""
    async_client = MagicMock(spec=httpx.AsyncClient)
    with (
        patch("cachekit.backends.cachekitio.backend.get_sync_http_client", return_value=MagicMock(spec=httpx.Client)),
        patch("cachekit.backends.cachekitio.backend.get_cached_async_http_client", return_value=async_client),
    ):
        yield async_client


@pytest.fixture
def backend(mock_sync_client: MagicMock) -> CachekitIOBackend:
    return CachekitIOBackend(api_url=_TEST_API_URL, api_key=_TEST_API_KEY)


@pytest.fixture
def async_backend(mock_async_client: MagicMock) -> CachekitIOBackend:
    return CachekitIOBackend(api_url=_TEST_API_URL, api_key=_TEST_API_KEY)


@pytest.mark.critical
class TestInjectMetricsHeaders:
    """Test _inject_metrics_headers standalone function."""

    def test_none_stats_returns_default_l1_disabled(self) -> None:
        """stats=None returns L1-Status: disabled for standalone usage."""
        headers = _inject_metrics_headers(None)
        assert headers == {"X-CacheKit-L1-Status": "disabled"}

    def test_valid_stats_returns_full_headers(self) -> None:
        """Non-None stats returns all 7 metrics headers."""
        from cachekit.decorators.wrapper import _FunctionStats

        stats = _FunctionStats(function_identifier="test.fn")
        headers = _inject_metrics_headers(stats)
        assert "X-CacheKit-L1-Status" in headers
        assert "X-CacheKit-Session-ID" in headers
        assert len(headers) == 7


@pytest.mark.critical
class TestMakeRequestHeaderInjection:
    """Test that _make_request always injects metrics headers."""

    def test_headers_injected_without_stats_context(self, backend: CachekitIOBackend, mock_sync_client: MagicMock) -> None:
        """When no @cache context, L1-Status: disabled header is still sent."""
        mock_sync_client.request.return_value = _make_response(200, b"value")

        # Call outside any @cache context — get_current_function_stats() returns None
        backend.get("test-key")

        call_kwargs = mock_sync_client.request.call_args[1]
        headers = call_kwargs.get("headers", {})
        assert headers.get("X-CacheKit-L1-Status") == "disabled"

    def test_headers_merged_with_existing(self, backend: CachekitIOBackend, mock_sync_client: MagicMock) -> None:
        """Metrics headers merge with (not replace) existing headers like X-TTL."""
        mock_sync_client.request.return_value = _make_response(200)

        backend.set("test-key", b"data", ttl=60)

        call_kwargs = mock_sync_client.request.call_args[1]
        headers = call_kwargs.get("headers", {})
        # Both X-TTL (from set) and L1-Status (from metrics) present
        assert "X-TTL" in headers
        assert "X-CacheKit-L1-Status" in headers


@pytest.mark.critical
class TestAsyncRequestHeaderInjection:
    """Test that _request_async always injects metrics headers."""

    @pytest.mark.asyncio
    async def test_async_headers_injected_without_stats_context(
        self, async_backend: CachekitIOBackend, mock_async_client: MagicMock
    ) -> None:
        """Async path: L1-Status: disabled header sent when no @cache context."""
        mock_async_client.request.return_value = _make_response(200, b"value")

        await async_backend.get_async("test-key")

        call_kwargs = mock_async_client.request.call_args[1]
        headers = call_kwargs.get("headers", {})
        assert headers.get("X-CacheKit-L1-Status") == "disabled"

    @pytest.mark.asyncio
    async def test_async_headers_merged_with_existing(
        self, async_backend: CachekitIOBackend, mock_async_client: MagicMock
    ) -> None:
        """Async path: metrics headers merge with existing headers like X-TTL."""
        mock_async_client.request.return_value = _make_response(200)

        await async_backend.set_async("test-key", b"data", ttl=60)

        call_kwargs = mock_async_client.request.call_args[1]
        headers = call_kwargs.get("headers", {})
        assert "X-TTL" in headers
        assert "X-CacheKit-L1-Status" in headers
