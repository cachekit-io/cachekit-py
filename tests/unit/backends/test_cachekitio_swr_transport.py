"""SWR transport layer (LAB-381, protocol spec/saas-api.md#stale-while-revalidate).

Covers the freshness-aware read (X-CacheKit-Freshness mapping), the stale-grace
write headers (X-CacheKit-Stale-TTL + the canonical TTL), and the
StandardCacheHandler plumbing incl. the non-SWR-backend fallbacks.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from unittest.mock import patch

import pytest

from cachekit.backends.cachekitio.backend import (
    FRESH_FOR_HEADER,
    FRESHNESS_HEADER,
    STALE_TTL_HEADER,
    TTL_HEADER,
    CachekitIOBackend,
    _parse_fresh_for,
)
from cachekit.backends.cachekitio.error_handler import HTTPStatusError
from cachekit.backends.errors import BackendError, BackendErrorType
from cachekit.cache_handler import StandardCacheHandler, supports_swr
from tests.utils.cachekitio_fakes import fake_backend
from tests.utils.cachekitio_fakes import response as _response


@pytest.fixture
def backend() -> Iterator[CachekitIOBackend]:
    """A backend whose tests patch ``_request_sync``, so its pool must never be reached."""
    backend, pool = fake_backend(lambda request: _response(500))
    yield backend
    assert pool.requests == [], f"a request bypassed the patched _request_sync: {pool.requests}"


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
        with patch.object(backend, "_request_sync", return_value=_response(200, b"payload", headers=headers)):
            result = backend.get_with_freshness("k")
        assert result == (b"payload", expected_stale, None)

    def test_miss_returns_none(self, backend: CachekitIOBackend) -> None:
        with patch.object(backend, "_request_sync", return_value=_response(404)) as request:
            assert backend.get_with_freshness("k") is None
        assert request.call_args.kwargs["miss_on_404"] is True

    def test_non_404_error_propagates(self, backend: CachekitIOBackend) -> None:
        err = BackendError(
            "boom",
            error_type=BackendErrorType.TRANSIENT,
            original_exception=HTTPStatusError(500, _response(500)),
        )
        with patch.object(backend, "_request_sync", side_effect=err):
            with pytest.raises(BackendError):
                backend.get_with_freshness("k")


class TestFreshForRead:
    """X-CacheKit-Fresh-For mapping (LAB-557, spec/saas-api.md#remaining-freshness):
    absent = None (pre-signal server, legacy); anything but 1-7 ASCII digits at most
    2,592,000 = 0 (never extend local service on drift — mirrors unrecognized-freshness → stale)."""

    @pytest.mark.parametrize(
        ("headers", "expected_fresh_for"),
        [
            (None, None),  # pre-signal server: no header → no bound
            ({FRESH_FOR_HEADER: "30"}, 30),
            ({FRESH_FOR_HEADER: "0"}, 0),  # freshness exhausted → do not backfill
            ({FRESH_FOR_HEADER: "garbage"}, 0),  # drift → conservative 0
            ({FRESH_FOR_HEADER: "-5"}, 0),  # negative → conservative 0
            ({FRESH_FOR_HEADER: "2.5"}, 0),  # non-integer → conservative 0
        ],
    )
    def test_fresh_for_mapping(
        self, backend: CachekitIOBackend, headers: dict[str, str] | None, expected_fresh_for: int | None
    ) -> None:
        with patch.object(backend, "_request_sync", return_value=_response(200, b"payload", headers=headers)):
            result = backend.get_with_freshness("k")
        assert result is not None
        assert result[2] == expected_fresh_for

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (None, None),  # absent header: no bound
            ("30", 30),
            ("0", 0),
            ("2592000", 2592000),  # exactly the 30-day cap
            ("", 0),
            ("+5", 0),  # int() accepts a sign
            ("1_0", 0),  # int() accepts underscores
            (" 5", 0),  # int() strips whitespace
            ("00000005", 0),  # eight digits: the length check runs first
            ("3000000", 0),  # seven digits, over the cap
            ("4297559296", 0),  # wraps to 2,592,000 under a 32-bit atoi
            ("٥", 0),  # Arabic-Indic five: int() and str.isdigit() accept it
            ("²", 0),  # superscript two: str.isdigit() accepts it
        ],
    )
    def test_parse_fresh_for_grammar(self, value: str | None, expected: int | None) -> None:
        """LAB-7838: 1-7 ASCII digits at most 2,592,000, else 0 — the header string alone, no response."""
        assert _parse_fresh_for(value) == expected

    def test_fresh_for_rides_alongside_staleness(self, backend: CachekitIOBackend) -> None:
        """A stale-window read carries 0 remaining freshness (server emits both headers)."""
        headers = {FRESHNESS_HEADER: "stale", FRESH_FOR_HEADER: "0"}
        with patch.object(backend, "_request_sync", return_value=_response(200, b"payload", headers=headers)):
            assert backend.get_with_freshness("k") == (b"payload", True, 0)


class TestStaleGraceWrite:
    """PUT timing headers: the canonical TTL alone, stale window rules."""

    def test_ttl_sent_canonical_only(self, backend: CachekitIOBackend) -> None:
        """API-44: SDKs send X-CacheKit-TTL only, never the legacy X-TTL."""
        with patch.object(backend, "_request_sync") as req:
            backend.set("k", b"v", ttl=300)
        headers = req.call_args.kwargs["headers"]
        assert headers == {TTL_HEADER: "300"}

    @pytest.mark.parametrize(
        ("ttl", "stale_ttl", "wire_ttl", "wire_stale"),
        [(0.5, 0.5, "1", "1"), (0.001, None, "1", None), (1.5, 2.25, "2", "3"), (60, 30, "60", "30")],
    )
    def test_ttls_ceiled_to_whole_seconds(
        self, backend: CachekitIOBackend, ttl: float, stale_ttl: float | None, wire_ttl: str, wire_stale: str | None
    ) -> None:
        """API-42 / API-54: a sub-second TTL or stale window goes out as 1, never 0 or a fraction."""
        with patch.object(backend, "_request_sync") as req:
            backend.set("k", b"v", ttl=ttl, stale_ttl=stale_ttl)  # type: ignore[arg-type]  # nothing enforces the int annotation
        headers = req.call_args.kwargs["headers"]
        assert headers[TTL_HEADER] == wire_ttl
        assert headers.get(STALE_TTL_HEADER) == wire_stale

    def test_stale_ttl_sent_with_ttl(self, backend: CachekitIOBackend) -> None:
        with patch.object(backend, "_request_sync") as req:
            backend.set("k", b"v", ttl=300, stale_ttl=600)
        headers = req.call_args.kwargs["headers"]
        assert headers[STALE_TTL_HEADER] == "600"
        assert headers[TTL_HEADER] == "300"

    @pytest.mark.parametrize(("ttl", "wire"), [(0.5, 1), (90, 90)])
    async def test_refresh_ttl_ceiled_to_whole_seconds(self, backend: CachekitIOBackend, ttl: float, wire: int) -> None:
        """PATCH /ttl takes the same validation as X-CacheKit-TTL, so a sub-second value goes out as 1."""
        with patch.object(backend, "_request_async") as req:
            assert await backend.refresh_ttl("k", ttl) is True  # type: ignore[arg-type]
        assert json.loads(req.call_args.kwargs["body"]) == {"ttl": wire}

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
        self.fresh_for: int | None = None

    def get(self, key: str) -> bytes | None:
        return b"plain-get"

    def get_with_freshness(self, key: str) -> tuple[bytes, bool, int | None] | None:
        return (b"swr-get", self.freshness, self.fresh_for)

    def set(self, key: str, value: bytes, ttl=None, stale_ttl=None) -> None:
        self.set_calls.append((key, value, ttl, stale_ttl))

    def delete(self, key: str) -> bool:
        return True


class _PlainBackend:
    def __init__(self) -> None:
        self.store: dict[str, bytes] = {}

    def get(self, key: str) -> bytes | None:
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
        assert handler.get_with_freshness("k") == (b"swr-get", True, None)

    def test_get_with_freshness_fallback_reads_as_fresh(self) -> None:
        """Non-SWR backends degrade to plain get(), always fresh."""
        backend = _PlainBackend()
        backend.store["k"] = b"value"
        handler = StandardCacheHandler(backend)  # type: ignore[arg-type]
        assert handler.get_with_freshness("k") == (b"value", False, None)
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
        assert await handler.get_with_freshness_async("k") == (b"swr-get", True, None)
        assert await handler.set_async("k", b"v", ttl=300, stale_ttl=600) is True
        assert backend.set_calls == [("k", b"v", 300, 600)]


class _ExplodingBackend(_SWRBackend):
    """SWR backend whose reads raise (handler degradation paths)."""

    def __init__(self, exc: Exception) -> None:
        super().__init__()
        self.exc = exc

    def get_with_freshness(self, key: str) -> tuple[bytes, bool, int | None] | None:
        raise self.exc

    def get(self, key: str) -> bytes | None:
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
        assert await handler.get_with_freshness_async("k") == (b"value", False, None)
        assert await handler.get_with_freshness_async("missing") is None


class _LegacyTupleBackend(_SWRBackend):
    """Third-party SWR backend on the released 0.5.x 2-tuple read protocol."""

    def get_with_freshness(self, key: str) -> tuple[bytes, bool] | None:  # type: ignore[override]
        return None if key == "missing" else (b"legacy", True)


class TestHandlerNormalisesLegacyTuple:
    """LAB-557: StandardCacheHandler owns the 3-tuple shape it promises. A legacy
    2-tuple backend passes supports_swr, so the handler pads it to
    (bytes, is_stale, None) — callers never see a 2-tuple."""

    def test_sync_pads_legacy_two_tuple(self) -> None:
        handler = StandardCacheHandler(_LegacyTupleBackend())  # type: ignore[arg-type]
        assert handler.get_with_freshness("k") == (b"legacy", True, None)
        assert handler.get_with_freshness("missing") is None

    async def test_async_pads_legacy_two_tuple(self) -> None:
        handler = StandardCacheHandler(_LegacyTupleBackend())  # type: ignore[arg-type]
        assert await handler.get_with_freshness_async("k") == (b"legacy", True, None)
        assert await handler.get_with_freshness_async("missing") is None
