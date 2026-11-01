"""Critical path tests for CachekitIO metrics header injection.

Covers the _inject_metrics_headers() function and the _request_sync/_request_async
header merging logic changed in the standalone L1-Status fix. Requests go through the
backend's real client on a fake pool (tests/utils/cachekitio_fakes.py), so the headers
asserted are the ones the pool is handed, after the client merges its own in.

Performance target: < 1 second total.
"""

from __future__ import annotations

import pytest
from urllib3 import HTTPResponse

from cachekit import cache
from cachekit.backends.cachekitio.backend import CachekitIOBackend, _inject_metrics_headers
from tests.utils.cachekitio_fakes import FakePool, FakeRequest, fake_backend, response

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
        """Metrics headers merge with (not replace) existing headers like X-CacheKit-TTL."""
        backend, pool = backend_and_pool

        backend.set("test-key", b"data", ttl=60)

        headers = pool.requests[0].headers
        # Both X-CacheKit-TTL (from set) and L1-Status (from metrics) present
        assert "X-CacheKit-TTL" in headers
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
        """Async path: metrics headers merge with existing headers like X-CacheKit-TTL."""
        backend, pool = backend_and_pool

        await backend.set_async("test-key", b"data", ttl=60)

        headers = pool.requests[0].headers
        assert "X-CacheKit-TTL" in headers
        assert "X-CacheKit-L1-Status" in headers


def _miss_backend() -> tuple[CachekitIOBackend, FakePool]:
    """Every read misses; every lock and write succeeds."""

    def respond(request: FakeRequest) -> HTTPResponse:
        if request.method == "GET":
            return response(404)
        if request.path.endswith("/lock"):
            return response(200, json={"lock_id": "lock-1"})
        return response(200, json={"success": True})

    return fake_backend(respond, api_key=_TEST_API_KEY)


def _reads(pool: FakePool) -> list[FakeRequest]:
    return [request for request in pool.requests if request.method == "GET"]


def _assert_reads_carry_function_stats(
    first_miss: list[FakeRequest], hit: list[FakeRequest], second_miss: list[FakeRequest]
) -> None:
    assert first_miss
    assert not hit  # the L1 hit sent nothing
    assert second_miss
    for request in first_miss:
        assert request.headers["X-CacheKit-L1-Status"] == "miss"  # "disabled" is the standalone default
        assert request.headers["X-CacheKit-L1-Hits"] == "0"
    for request in second_miss:
        assert request.headers["X-CacheKit-L1-Hits"] == "1"  # the hit in between, on this function's stats


@pytest.mark.critical
class TestDecoratedRequestCarriesFunctionStats:
    """A @cache call's L2 reads carry the decorated function's stats, not the standalone default.

    The wrapper sets the stats context only past the backed L1 hit, which makes no request. A miss, a hit and
    a second miss pin both sides: each miss's reads carry this function's stats, and the hit sends nothing.
    """

    def test_sync_miss_hit_miss(self) -> None:
        backend, pool = _miss_backend()

        @cache(backend=backend, ttl=60)
        def double(x: int) -> int:
            return x * 2

        calls = []
        for x in (1, 1, 2):
            before = len(_reads(pool))
            assert double(x) == x * 2
            calls.append(_reads(pool)[before:])
        _assert_reads_carry_function_stats(*calls)

    @pytest.mark.asyncio
    async def test_async_miss_hit_miss(self) -> None:
        backend, pool = _miss_backend()

        @cache(backend=backend, ttl=60)
        async def double(x: int) -> int:
            return x * 2

        calls = []
        for x in (1, 1, 2):
            before = len(_reads(pool))
            assert await double(x) == x * 2
            calls.append(_reads(pool)[before:])
        _assert_reads_carry_function_stats(*calls)
