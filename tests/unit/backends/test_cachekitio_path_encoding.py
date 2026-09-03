"""Path-injection regression: the cache key MUST be percent-encoded into the
CachekitIO request path (LAB-2846, CWE-22 / CWE-20).

Before the fix, the raw key was interpolated unquoted into
``/v1/cache/{key}``. httpx (pinned ``>=0.28.1``) normalises dot-segments and
splits ``?``/``#`` *client-side, before the request leaves the process* — so a
custom ``@cache(key=...)`` value could escape the ``/v1/cache/`` prefix and
address arbitrary ``api.cachekit.io`` endpoints with the application's bearer
token:

    k?x=1#f  ->  GET /v1/cache/k?x=1  (query/fragment injection)
    a/b      ->  GET /v1/cache/a/b    (extra path segment)
    ..       ->  GET /v1              (dot-segment collapse)
    ../ttl   ->  GET /v1/ttl          (collapse onto a *different* route)

The last case is the nastiest: ``quote(key, safe="")`` leaves RFC-3986 unreserved
``.`` raw, so a key of exactly ``.`` or ``..`` still collapses even after
encoding — ``_encode_key`` special-cases an all-dot segment to ``%2E`` so it
can't. See ``SECURITY.md`` for the full mechanism and the cross-SDK wire-parity
contract (cachekit-ts ``encodeURIComponent``, cachekit-rs ``urlencoding::encode``,
SaaS single decode + ``..`` reject).

These tests drive the real backend methods through a real ``httpx`` client backed
by a ``MockTransport`` and assert on ``request.url.raw_path`` — the actual bytes
that would go on the wire, *after* httpx's normalisation. Asserting the raw path
(not a mocked endpoint string) is what proves the traversal is neutralised at the
layer that used to defeat it.
"""

from __future__ import annotations

import json as _json
from collections.abc import Callable
from unittest.mock import patch
from urllib.parse import unquote

import httpx
import pytest

from cachekit.backends.cachekitio.backend import CachekitIOBackend

_TEST_API_URL = "https://api.cachekit.io"
_TEST_API_KEY = "ck_test_abc123"  # pragma: allowlist secret — fake fixture, not a real key

# Keys that weaponise httpx's client-side URL normalisation (the vuln vectors),
# plus the benign shapes that must still round-trip unchanged.
_TRAVERSAL_KEYS = [
    "default:../../admin",  # `/`-bearing traversal (every `/` → %2F, so no collapse)
    "..",  # bare dot-segment: collapses to /v1 (or /v1/ttl on suffix routes) unless encoded
    ".",  # single dot-segment: collapses to the collection endpoint unless encoded
    "a:..",  # trailing dots but NOT an all-dot segment → must stay raw dots, must not collapse
    "k?x=1#f",  # query + fragment injection
    "a b",  # space (must not become a raw space / '+' in the path)
    "ns:articles:func:mod.fn:args:" + ("a" * 64) + ":1s",  # canonical 7-seg key (`:` → %3A)
]


def _recording_transport() -> tuple[httpx.MockTransport, list[httpx.Request]]:
    """A MockTransport that records every request and answers plausibly.

    ``.../ttl`` gets a JSON body (get_ttl/refresh_ttl parse it); everything else
    gets an empty 200. The status is always 200 so no method takes its 404 branch.
    """
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.raw_path.endswith(b"/ttl"):
            return httpx.Response(200, content=_json.dumps({"ttl": 42}).encode())
        return httpx.Response(200, content=b"payload")

    return httpx.MockTransport(handler), seen


def _make_backend() -> tuple[CachekitIOBackend, list[httpx.Request]]:
    """Backend whose sync+async clients are real httpx clients over one recorder.

    Using a *real* httpx client (not a mocked ``_request_sync``) is deliberate:
    the bug lived in httpx's own path normalisation, so the test must exercise it.
    """
    transport, seen = _recording_transport()
    sync_client = httpx.Client(base_url=_TEST_API_URL, transport=transport)
    async_client = httpx.AsyncClient(base_url=_TEST_API_URL, transport=transport)
    with (
        patch("cachekit.backends.cachekitio.backend.get_sync_http_client", return_value=sync_client),
        patch("cachekit.backends.cachekitio.backend.get_cached_async_http_client", return_value=async_client),
    ):
        return CachekitIOBackend(api_url=_TEST_API_URL, api_key=_TEST_API_KEY), seen


def _assert_contained(request: httpx.Request, key: str, *, suffix: str = "") -> None:
    """The wire path must be exactly ``/v1/cache/<quote(key)>{suffix}`` — nothing escapes.

    Asserts on ``raw_path`` (the encoded bytes httpx actually sends) so a traversal
    that httpx would have collapsed shows up here as a failure, and proves the key
    survives a single decode intact (AC-3: ``%3A`` -> ``:`` once, matching the SaaS
    validator's single ``decodeURIComponent``).
    """
    raw_path = request.url.raw_path.decode()  # includes any query string
    assert raw_path.startswith("/v1/cache/"), f"path escaped /v1/cache/ prefix: {raw_path!r}"

    encoded_key = raw_path[len("/v1/cache/") :]
    if suffix:
        assert encoded_key.endswith(suffix), f"missing {suffix!r} suffix: {raw_path!r}"
        encoded_key = encoded_key[: -len(suffix)]

    # The encoded key segment carries no separator/delimiter that httpx (or the
    # SaaS router) could act on: every ``/`` is ``%2F``, so no *embedded* ``../``
    # can exist. A segment that is *entirely* dots (``.`` / ``..``) IS still a live
    # dot-segment even without an embedded ``/`` — httpx collapses it against the
    # ``/v1/cache/`` prefix (and the ``/ttl`` / ``/lock`` suffix supplies the trailing
    # boundary), so ``_encode_key`` must encode those dots too. The ``startswith``
    # check above is what catches a collapse: a bare ``..`` that leaked would show up
    # as ``/v1`` or ``/v1/ttl``, failing the prefix assertion. The SaaS validator
    # additionally rejects ``..`` in the decoded key as defence in depth.
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
    backend, seen = _make_backend()
    op(backend, key)
    assert len(seen) == 1, f"{op_name} made {len(seen)} requests"
    _assert_contained(seen[0], key)


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
    backend, seen = _make_backend()
    await op(backend, key)  # type: ignore[misc]
    assert len(seen) == 1, f"{op_name} made {len(seen)} requests"
    _assert_contained(seen[0], key)


# ---- ttl surface: GET .../ttl and PATCH .../ttl ---------------------------


@pytest.mark.unit
@pytest.mark.parametrize("key", _TRAVERSAL_KEYS)
async def test_get_ttl_key_is_encoded(key: str) -> None:
    backend, seen = _make_backend()
    await backend.get_ttl(key)
    assert len(seen) == 1
    _assert_contained(seen[0], key, suffix="/ttl")


@pytest.mark.unit
@pytest.mark.parametrize("key", _TRAVERSAL_KEYS)
async def test_refresh_ttl_key_is_encoded(key: str) -> None:
    backend, seen = _make_backend()
    await backend.refresh_ttl(key, ttl=99)
    assert len(seen) == 1
    _assert_contained(seen[0], key, suffix="/ttl")


# ---- health endpoint is a literal, not a key — must NOT be mangled ---------


@pytest.mark.unit
def test_health_endpoint_untouched() -> None:
    """``health`` is a fixed endpoint, not a user key; it must stay ``/v1/cache/health``."""
    backend, seen = _make_backend()
    backend.health_check()
    assert seen[0].url.raw_path == b"/v1/cache/health"
