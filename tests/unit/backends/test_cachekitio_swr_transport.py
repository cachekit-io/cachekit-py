"""SWR transport layer (LAB-381, protocol spec/saas-api.md#stale-while-revalidate).

Covers the freshness-aware read (X-CacheKit-Freshness mapping), the stale-grace
write headers (X-CacheKit-Stale-TTL + canonical/legacy TTL dual-send), and the
StandardCacheHandler plumbing incl. the non-SWR-backend fallbacks.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx
import pytest

from cachekit.backends.cachekitio.backend import (
    FRESHNESS_HEADER,
    LEGACY_TTL_HEADER,
    STALE_TTL_HEADER,
    TTL_HEADER,
    CachekitIOBackend,
)
from cachekit.backends.errors import BackendError, BackendErrorType
from cachekit.cache_handler import StandardCacheHandler, supports_swr

_TEST_API_URL = "https://api.cachekit.io"
_TEST_API_KEY = "ck_test_abc123"  # pragma: allowlist secret — fake key, test fixture

_DUMMY_REQUEST = httpx.Request("GET", "https://api.cachekit.io/v1/cache/key")


def _response(status: int, content: bytes = b"", headers: dict[str, str] | None = None) -> httpx.Response:
    response = httpx.Response(status, content=content, headers=headers)
    response.request = _DUMMY_REQUEST
    return response


@pytest.fixture
def backend() -> CachekitIOBackend:
    with (
        patch("cachekit.backends.cachekitio.backend.get_sync_http_client", return_value=MagicMock(spec=httpx.Client)),
        patch(
            "cachekit.backends.cachekitio.backend.get_cached_async_http_client",
            return_value=MagicMock(spec=httpx.AsyncClient),
        ),
    ):
        return CachekitIOBackend(api_url=_TEST_API_URL, api_key=_TEST_API_KEY)


class TestFreshnessRead:
    """X-CacheKit-Freshness mapping per spec: absent=fresh, unrecognized=stale."""

    @pytest.mark.parametrize(
        ("headers", "expected_stale"),
        [
            (None, False),  # pre-SWR server: no header → fresh
            ({FRESHNESS_HEADER: "fresh"}, False),
            ({FRESHNESS_HEADER: "stale"}, True),
            ({FRESHNESS_HEADER: "Fresh"}, True),  # case-sensitive tokens → unrecognized → stale
            ({FRESHNESS_HEADER: "expired"}, True),  # unknown token → conservative stale
        ],
    )
    def test_header_mapping(self, backend: CachekitIOBackend, headers: dict[str, str] | None, expected_stale: bool) -> None:
        with patch.object(backend, "_request_sync", return_value=_response(200, b"payload", headers)):
            result = backend.get_with_freshness("k")
        assert result == (b"payload", expected_stale)

    def test_miss_returns_none(self, backend: CachekitIOBackend) -> None:
        err = BackendError(
            "not found",
            error_type=BackendErrorType.PERMANENT,
            original_exception=httpx.HTTPStatusError("404", request=_DUMMY_REQUEST, response=_response(404)),
        )
        with patch.object(backend, "_request_sync", side_effect=err):
            assert backend.get_with_freshness("k") is None

    def test_non_404_error_propagates(self, backend: CachekitIOBackend) -> None:
        err = BackendError(
            "boom",
            error_type=BackendErrorType.TRANSIENT,
            original_exception=httpx.HTTPStatusError("500", request=_DUMMY_REQUEST, response=_response(500)),
        )
        with patch.object(backend, "_request_sync", side_effect=err):
            with pytest.raises(BackendError):
                backend.get_with_freshness("k")


class TestStaleGraceWrite:
    """PUT timing headers: canonical+legacy TTL dual-send, stale window rules."""

    def test_ttl_dual_send(self, backend: CachekitIOBackend) -> None:
        with patch.object(backend, "_request_sync") as req:
            backend.set("k", b"v", ttl=300)
        headers = req.call_args.kwargs["headers"]
        assert headers[TTL_HEADER] == "300"
        assert headers[LEGACY_TTL_HEADER] == "300"
        assert STALE_TTL_HEADER not in headers

    def test_stale_ttl_sent_with_ttl(self, backend: CachekitIOBackend) -> None:
        with patch.object(backend, "_request_sync") as req:
            backend.set("k", b"v", ttl=300, stale_ttl=600)
        headers = req.call_args.kwargs["headers"]
        assert headers[STALE_TTL_HEADER] == "600"
        assert headers[TTL_HEADER] == "300"

    @pytest.mark.parametrize(("ttl", "stale_ttl"), [(None, 600), (300, 0), (300, None), (None, None)])
    def test_stale_ttl_omitted(self, backend: CachekitIOBackend, ttl: int | None, stale_ttl: int | None) -> None:
        """Spec: the stale window requires an explicit TTL; 0 ≡ absent."""
        with patch.object(backend, "_request_sync") as req:
            backend.set("k", b"v", ttl=ttl, stale_ttl=stale_ttl)
        assert STALE_TTL_HEADER not in req.call_args.kwargs["headers"]


class _SWRBackend:
    """Minimal SWR-capable fake (matches SWRCapableBackend structurally)."""

    def __init__(self) -> None:
        self.set_calls: list[tuple] = []
        self.freshness: bool = False

    def get(self, key: str):
        return b"plain-get"

    def get_with_freshness(self, key: str):
        return (b"swr-get", self.freshness)

    def set(self, key: str, value: bytes, ttl=None, stale_ttl=None) -> None:
        self.set_calls.append((key, value, ttl, stale_ttl))

    def delete(self, key: str) -> bool:
        return True


class _PlainBackend:
    def __init__(self) -> None:
        self.store: dict[str, bytes] = {}

    def get(self, key: str):
        return self.store.get(key)

    def set(self, key: str, value: bytes, ttl=None) -> None:
        self.store[key] = value

    def delete(self, key: str) -> bool:
        return self.store.pop(key, None) is not None


class TestHandlerPlumbing:
    def test_supports_swr_guard(self) -> None:
        assert supports_swr(_SWRBackend())  # type: ignore[arg-type]
        assert not supports_swr(_PlainBackend())  # type: ignore[arg-type]

    def test_get_with_freshness_swr_backend(self) -> None:
        backend = _SWRBackend()
        backend.freshness = True
        handler = StandardCacheHandler(backend)  # type: ignore[arg-type]
        assert handler.get_with_freshness("k") == (b"swr-get", True)

    def test_get_with_freshness_fallback_reads_as_fresh(self) -> None:
        """Non-SWR backends degrade to plain get(), always fresh."""
        backend = _PlainBackend()
        backend.store["k"] = b"value"
        handler = StandardCacheHandler(backend)  # type: ignore[arg-type]
        assert handler.get_with_freshness("k") == (b"value", False)
        assert handler.get_with_freshness("missing") is None

    def test_set_threads_stale_ttl_to_swr_backend(self) -> None:
        backend = _SWRBackend()
        handler = StandardCacheHandler(backend)  # type: ignore[arg-type]
        assert handler.set("k", b"v", ttl=300, stale_ttl=600) is True
        assert backend.set_calls == [("k", b"v", 300, 600)]

    def test_set_drops_stale_ttl_for_plain_backend(self) -> None:
        """A plain backend's set(key, value, ttl) signature must never see stale_ttl."""
        backend = _PlainBackend()
        handler = StandardCacheHandler(backend)  # type: ignore[arg-type]
        assert handler.set("k", b"v", ttl=300, stale_ttl=600) is True
        assert backend.store["k"] == b"v"

    async def test_async_variants(self) -> None:
        backend = _SWRBackend()
        backend.freshness = True
        handler = StandardCacheHandler(backend)  # type: ignore[arg-type]
        assert await handler.get_with_freshness_async("k") == (b"swr-get", True)
        assert await handler.set_async("k", b"v", ttl=300, stale_ttl=600) is True
        assert backend.set_calls == [("k", b"v", 300, 600)]


class TestAsyncBackendVariants:
    """Async mirrors of the freshness read/write (codecov: the async bodies count)."""

    async def test_get_with_freshness_async_maps_header(self, backend: CachekitIOBackend) -> None:
        resp = _response(200, b"payload", {FRESHNESS_HEADER: "stale"})
        with patch.object(backend, "_request_async", return_value=resp):
            assert await backend.get_with_freshness_async("k") == (b"payload", True)

    async def test_get_with_freshness_async_miss_and_error(self, backend: CachekitIOBackend) -> None:
        miss = BackendError(
            "not found",
            error_type=BackendErrorType.PERMANENT,
            original_exception=httpx.HTTPStatusError("404", request=_DUMMY_REQUEST, response=_response(404)),
        )
        with patch.object(backend, "_request_async", side_effect=miss):
            assert await backend.get_with_freshness_async("k") is None

        boom = BackendError(
            "boom",
            error_type=BackendErrorType.TRANSIENT,
            original_exception=httpx.HTTPStatusError("500", request=_DUMMY_REQUEST, response=_response(500)),
        )
        with patch.object(backend, "_request_async", side_effect=boom):
            with pytest.raises(BackendError):
                await backend.get_with_freshness_async("k")


class _ExplodingBackend(_SWRBackend):
    """SWR backend whose reads raise (handler degradation paths)."""

    def __init__(self, exc: Exception) -> None:
        super().__init__()
        self.exc = exc

    def get_with_freshness(self, key: str):
        raise self.exc

    def get(self, key: str):
        raise self.exc


class TestHandlerDegradation:
    """Errors read as misses (caller recomputes) — sync and async, both error classes."""

    @pytest.mark.parametrize("exc", [BackendError("down", error_type=BackendErrorType.TRANSIENT), ValueError("weird")])
    def test_get_with_freshness_errors_read_as_miss(self, exc: Exception) -> None:
        handler = StandardCacheHandler(_ExplodingBackend(exc))  # type: ignore[arg-type]
        assert handler.get_with_freshness("k") is None

    @pytest.mark.parametrize("exc", [BackendError("down", error_type=BackendErrorType.TRANSIENT), ValueError("weird")])
    async def test_get_with_freshness_async_errors_read_as_miss(self, exc: Exception) -> None:
        handler = StandardCacheHandler(_ExplodingBackend(exc))  # type: ignore[arg-type]
        assert await handler.get_with_freshness_async("k") is None

    async def test_get_with_freshness_async_fallback_for_plain_backend(self) -> None:
        backend = _PlainBackend()
        backend.store["k"] = b"value"
        handler = StandardCacheHandler(backend)  # type: ignore[arg-type]
        assert await handler.get_with_freshness_async("k") == (b"value", False)
        assert await handler.get_with_freshness_async("missing") is None
