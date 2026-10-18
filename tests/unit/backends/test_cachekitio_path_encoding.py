"""Path-injection regression: the cache key MUST be percent-encoded into the
CachekitIO request path (LAB-2846, CWE-22 / CWE-20).

Before the fix, the raw key was interpolated unquoted into
``/v1/cache/{key}``. The HTTP client then (httpx, at the time) normalised
dot-segments and split ``?``/``#`` *client-side, before the request left the
process*, and urllib3 still splits off ``?``/``#`` — so a custom
``@cache(key=...)`` value could escape the ``/v1/cache/`` prefix and address
arbitrary ``api.cachekit.io`` endpoints with the application's bearer token:

    k?x=1#f  ->  GET /v1/cache/k?x=1  (query/fragment injection)
    a/b      ->  GET /v1/cache/a/b    (extra path segment)
    ..       ->  GET /v1              (dot-segment collapse)
    ../ttl   ->  GET /v1/ttl          (collapse onto a *different* route)

A bare ``.`` / ``..`` key has no safe wire form (percent-encoded dots are collapsed
server-side), and neither do the route tokens ``health`` / ``ttl`` / ``lock`` or the
empty key: those six are rejected before any request (protocol rule 2, LAB-2880,
LAB-6550). See ``SECURITY.md``.

These tests drive the real backend methods through the backend's real client on a
fake pool (tests/utils/cachekitio_fakes.py) and assert on the path the client hands
the pool. urllib3's ``HTTPConnectionPool.urlopen`` sends that path as the request
target after ``_encode_target``, which splits off a raw ``?`` / ``#``, uppercases
percent escapes and percent-encodes characters invalid in a path, but removes no
dot-segments. Each test checks that ``_encode_target`` leaves the path unchanged, so
the asserted path is the one on the wire (not a mocked endpoint string).
"""

from __future__ import annotations

import json as _json
from collections.abc import Callable
from pathlib import Path
from urllib.parse import unquote

import pytest
from urllib3 import HTTPResponse
from urllib3.util.url import _encode_target

from cachekit.backends.cachekitio.backend import CachekitIOBackend
from cachekit.backends.errors import BackendError, BackendErrorType
from tests.utils.cachekitio_fakes import FakePool, FakeRequest, fake_backend, response

# Every transmittable row of the pinned protocol fixture, asserted on the request path the
# pool receives (API-15: the rows pin the request, not just the encoder). Rule 2 reserved segments are the fixture's ``reject`` rows: no wire
# form reaches the SaaS key validator, so the client must raise before building the URL
# (dot segments collapse client- or server-side; the words are route tokens under
# /v1/cache/; an empty key addresses no stored entry). A new row of either kind reaches
# every operation below by re-vendoring alone.
_PATH_ENCODING_FIXTURE = Path(__file__).parents[1] / "protocol" / "fixtures" / "path-encoding.json"
_VECTORS = _json.loads(_PATH_ENCODING_FIXTURE.read_text(encoding="utf-8"))["vectors"]
_FIXTURE_ENCODED = {v["key"]: v["encoded"] for v in _VECTORS if not v.get("reject")}
_RESERVED_KEYS = [v["key"] for v in _VECTORS if v.get("reject")]
_TRAVERSAL_KEYS = list(_FIXTURE_ENCODED)


def _handler(request: FakeRequest) -> HTTPResponse:
    """Answer plausibly: ``.../ttl`` gets a JSON body (get_ttl/refresh_ttl parse it); everything else
    gets a 200 with a body. The status is always 200 so no method takes its 404 branch.
    """
    if request.path.endswith("/ttl"):
        return response(200, json={"ttl": 42})
    return response(200, b"payload")


def _make_backend() -> tuple[CachekitIOBackend, FakePool]:
    """Backend whose real client (sync and, through to_thread, async) sends to one recording pool.

    Using the *real* client (not a mocked ``_request_sync``) is deliberate: the path the
    pool receives is built by the backend and prefixed by the client, as in production.
    """
    return fake_backend(_handler)


def _assert_contained(request: FakeRequest, key: str, *, suffix: str = "") -> None:
    """The wire path must be exactly ``/v1/cache/<quote(key)>{suffix}`` — nothing escapes.

    Asserts on the path the pool receives, after checking that urllib3 sends it as-is, so a
    traversal that a client or the SaaS router would act on shows up here as a failure, and
    proves the key survives a single decode intact (AC-3: ``%3A`` -> ``:`` once, matching the
    SaaS validator's single ``decodeURIComponent``).
    """
    raw_path = request.path  # includes any query string
    # urlopen sends _encode_target(path): equal means it neither split off a query/fragment nor re-encoded.
    assert _encode_target(raw_path) == raw_path, f"urllib3 would rewrite the request target: {raw_path!r}"
    assert raw_path.startswith("/v1/cache/"), f"path escaped /v1/cache/ prefix: {raw_path!r}"

    encoded_key = raw_path[len("/v1/cache/") :]
    if suffix:
        assert encoded_key.endswith(suffix), f"missing {suffix!r} suffix: {raw_path!r}"
        encoded_key = encoded_key[: -len(suffix)]

    # The encoded key segment carries no separator/delimiter that a client (or the
    # SaaS router) could act on: every ``/`` is ``%2F``, so no *embedded* ``../``
    # can exist. A segment that is *entirely* dots never gets here — it is rejected
    # (see the reserved-key tests below). The ``startswith`` check above is what
    # catches a collapse: a leaked ``..`` would show up as ``/v1`` or ``/v1/ttl``.
    for bad in ("/", "?", "#"):
        assert bad not in encoded_key, f"unencoded {bad!r} survived in key segment: {encoded_key!r}"

    # Round-trip: decode-once recovers the original key byte-for-byte. This is
    # AC-3 — the client encodes with exactly the inverse of the SaaS validator's
    # single ``decodeURIComponent`` (cache-key-validator.ts), so no double-encoding
    # and no crafted key resolves to a *different* server-side key.
    assert unquote(encoded_key) == key, f"key not recoverable by single decode: {encoded_key!r} != {key!r}"

    # A ``:`` (canonical ``ns:…:args:…`` shape) must be encoded, not passed raw —
    # pins that ``%3A`` decodes back to ``:`` exactly once for the canonical key.
    if ":" in key:
        assert "%3A" in encoded_key, f"colon not percent-encoded in key segment: {encoded_key!r}"

    # The wire form is pinned byte-for-byte: Python hits the fixture's ``encoded`` exactly,
    # not an alternate, which keeps it byte-identical to cachekit-rs.
    assert encoded_key == _FIXTURE_ENCODED[key], f"wire segment {encoded_key!r} != fixture {_FIXTURE_ENCODED[key]!r}"


# ---- sync surface: GET / GET(stale) / PUT / DELETE / HEAD ------------------

_SYNC_OPS: list[tuple[str, Callable[[CachekitIOBackend, str], object]]] = [
    ("get", lambda b, k: b.get(k)),
    ("get_with_freshness", lambda b, k: b.get_with_freshness(k)),
    ("set", lambda b, k: b.set(k, b"v", ttl=30)),
    ("delete", lambda b, k: b.delete(k)),
    ("exists", lambda b, k: b.exists(k)),
]


@pytest.mark.unit
@pytest.mark.parametrize("key", _TRAVERSAL_KEYS)
@pytest.mark.parametrize(("op_name", "op"), _SYNC_OPS, ids=[o[0] for o in _SYNC_OPS])
def test_sync_key_is_encoded(key: str, op_name: str, op: Callable[[CachekitIOBackend, str], object]) -> None:
    backend, pool = _make_backend()
    op(backend, key)
    assert len(pool.requests) == 1, f"{op_name} made {len(pool.requests)} requests"
    _assert_contained(pool.requests[0], key)


# ---- async surface: GET / PUT / DELETE / HEAD -----------------------------

_ASYNC_OPS: list[tuple[str, Callable[[CachekitIOBackend, str], object]]] = [
    ("get_async", lambda b, k: b.get_async(k)),
    ("set_async", lambda b, k: b.set_async(k, b"v", ttl=30)),
    ("delete_async", lambda b, k: b.delete_async(k)),
    ("exists_async", lambda b, k: b.exists_async(k)),
]


@pytest.mark.unit
@pytest.mark.parametrize("key", _TRAVERSAL_KEYS)
@pytest.mark.parametrize(("op_name", "op"), _ASYNC_OPS, ids=[o[0] for o in _ASYNC_OPS])
async def test_async_key_is_encoded(key: str, op_name: str, op: Callable[[CachekitIOBackend, str], object]) -> None:
    backend, pool = _make_backend()
    await op(backend, key)  # type: ignore[misc]
    assert len(pool.requests) == 1, f"{op_name} made {len(pool.requests)} requests"
    _assert_contained(pool.requests[0], key)


# ---- ttl surface: GET .../ttl and PATCH .../ttl ---------------------------


@pytest.mark.unit
@pytest.mark.parametrize("key", _TRAVERSAL_KEYS)
async def test_get_ttl_key_is_encoded(key: str) -> None:
    backend, pool = _make_backend()
    await backend.get_ttl(key)
    assert len(pool.requests) == 1
    _assert_contained(pool.requests[0], key, suffix="/ttl")


@pytest.mark.unit
@pytest.mark.parametrize("key", _TRAVERSAL_KEYS)
async def test_refresh_ttl_key_is_encoded(key: str) -> None:
    backend, pool = _make_backend()
    await backend.refresh_ttl(key, ttl=99)
    assert len(pool.requests) == 1
    _assert_contained(pool.requests[0], key, suffix="/ttl")


# ---- health endpoint is a literal, not a key — must NOT be mangled ---------


@pytest.mark.unit
def test_health_endpoint_untouched() -> None:
    """``health`` is a fixed endpoint, not a user key; it must stay ``/v1/cache/health``."""
    backend, pool = _make_backend()
    backend.health_check()
    assert pool.requests[0].path == "/v1/cache/health"


# ---- reserved segments: rejected before any request is made (LAB-2880) -----


def _assert_rejected(exc: BackendError, pool: FakePool) -> None:
    assert exc.error_type is BackendErrorType.PERMANENT, "reserved key must fail fast, not be retried"
    assert pool.requests == [], f"a request left the process for a reserved key: {[r.path for r in pool.requests]}"


@pytest.mark.unit
@pytest.mark.parametrize("key", _RESERVED_KEYS)
@pytest.mark.parametrize(("op_name", "op"), _SYNC_OPS, ids=[o[0] for o in _SYNC_OPS])
def test_sync_reserved_key_rejected(key: str, op_name: str, op: Callable[[CachekitIOBackend, str], object]) -> None:
    backend, pool = _make_backend()
    with pytest.raises(BackendError) as excinfo:
        op(backend, key)
    _assert_rejected(excinfo.value, pool)


@pytest.mark.unit
@pytest.mark.parametrize("key", _RESERVED_KEYS)
@pytest.mark.parametrize(("op_name", "op"), _ASYNC_OPS, ids=[o[0] for o in _ASYNC_OPS])
async def test_async_reserved_key_rejected(key: str, op_name: str, op: Callable[[CachekitIOBackend, str], object]) -> None:
    backend, pool = _make_backend()
    with pytest.raises(BackendError) as excinfo:
        await op(backend, key)  # type: ignore[misc]
    _assert_rejected(excinfo.value, pool)


@pytest.mark.unit
@pytest.mark.parametrize("key", _RESERVED_KEYS)
async def test_get_ttl_reserved_key_rejected(key: str) -> None:
    """Raises rather than returning None: a reserved key is not a missing key."""
    backend, pool = _make_backend()
    with pytest.raises(BackendError) as excinfo:
        await backend.get_ttl(key)
    _assert_rejected(excinfo.value, pool)


@pytest.mark.unit
@pytest.mark.parametrize("key", _RESERVED_KEYS)
async def test_refresh_ttl_reserved_key_rejected(key: str) -> None:
    """Raises rather than returning False: nothing was attempted."""
    backend, pool = _make_backend()
    with pytest.raises(BackendError) as excinfo:
        await backend.refresh_ttl(key, ttl=99)
    _assert_rejected(excinfo.value, pool)


@pytest.mark.unit
@pytest.mark.parametrize("key", _RESERVED_KEYS)
async def test_acquire_lock_reserved_key_rejected(key: str) -> None:
    """PERMANENT propagates out of acquire_lock, so the wrapper degrades to no-lock once."""
    backend, pool = _make_backend()
    with pytest.raises(BackendError) as excinfo:
        async with backend.acquire_lock(key, timeout=5.0, blocking_timeout=1.0):
            pytest.fail("lock body must not run for a reserved key")
    _assert_rejected(excinfo.value, pool)
