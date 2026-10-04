"""Critical path tests for CachekitIO metrics header injection.

Covers the _inject_metrics_headers() function and the _request_sync/_request_async
header merging logic changed in the standalone L1-Status fix. Requests go through the
backend's real client on a fake pool (tests/utils/cachekitio_fakes.py), so the headers
asserted are the ones the pool is handed, after the client merges its own in.

Performance target: < 1 second total.
"""

from __future__ import annotations

import pytest

from cachekit.backends.cachekitio.backend import CachekitIOBackend, _inject_metrics_headers
from tests.utils.cachekitio_fakes import FakePool, fake_backend, response

_TEST_API_KEY = "ck_test_critical_metrics"


@pytest.fixture
def backend_and_pool() -> tuple[CachekitIOBackend, FakePool]:
    return fake_backend(lambda request: response(200, b"value"), api_key=_TEST_API_KEY)


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
class TestSyncRequestHeaderInjection:
    """Test that _request_sync always injects metrics headers."""

    def test_headers_injected_without_stats_context(self, backend_and_pool: tuple[CachekitIOBackend, FakePool]) -> None:
        """When no @cache context, L1-Status: disabled header is still sent."""
        backend, pool = backend_and_pool

        # Call outside any @cache context — get_current_function_stats() returns None
        backend.get("test-key")

        assert pool.requests[0].headers.get("X-CacheKit-L1-Status") == "disabled"

    def test_headers_merged_with_existing(self, backend_and_pool: tuple[CachekitIOBackend, FakePool]) -> None:
        """Metrics headers merge with (not replace) existing headers like X-TTL."""
        backend, pool = backend_and_pool

        backend.set("test-key", b"data", ttl=60)

        headers = pool.requests[0].headers
        # Both X-TTL (from set) and L1-Status (from metrics) present
        assert "X-TTL" in headers
        assert "X-CacheKit-L1-Status" in headers


@pytest.mark.critical
class TestAsyncRequestHeaderInjection:
    """Test that _request_async always injects metrics headers."""

    @pytest.mark.asyncio
    async def test_async_headers_injected_without_stats_context(
        self, backend_and_pool: tuple[CachekitIOBackend, FakePool]
    ) -> None:
        """Async path: L1-Status: disabled header sent when no @cache context."""
        backend, pool = backend_and_pool

        await backend.get_async("test-key")

        assert pool.requests[0].headers.get("X-CacheKit-L1-Status") == "disabled"

    @pytest.mark.asyncio
    async def test_async_headers_merged_with_existing(self, backend_and_pool: tuple[CachekitIOBackend, FakePool]) -> None:
        """Async path: metrics headers merge with existing headers like X-TTL."""
        backend, pool = backend_and_pool

        await backend.set_async("test-key", b"data", ttl=60)

        headers = pool.requests[0].headers
        assert "X-TTL" in headers
        assert "X-CacheKit-L1-Status" in headers
