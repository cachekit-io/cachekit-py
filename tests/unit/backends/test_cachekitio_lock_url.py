"""Regression test for bare-cache-key contract on LockableBackend.acquire_lock.

Production bug: SaaS cache worker returns HTTP 400 on lock POST because the
wrapper at ``decorators/wrapper.py:1064`` prepended ``:lock`` to the canonical
cache_key before passing it to ``acquire_lock``. The SaaS validator
(``apps/cache/src/index.ts``) requires exactly 7 colon-separated segments in
the canonical key format:

    ns:{namespace}:func:{module.function}:args:{64-hex-blake2b}:{flags}

The pollution turned a 7-segment key into 8 (`ns:...:1s:lock`), which fails
validation.

The architectural fix is to make the protocol contract explicit: every
LockableBackend method receives the **bare cache key** — identical to what
``get``/``set``/``delete`` see. Backends own any internal lock-namespace
derivation (Redis still uses ``key:lock`` on the wire; SaaS has no such notion
because the lock endpoint is ``/v1/cache/{key}/lock``).

These tests pin the URL path that lands at the SaaS edge to a bare 7-segment
key — no ``%3Alock`` smuggled in via the encoded key portion. The Rust and
TypeScript SDKs already implement this contract; this regression test prevents
the Python SDK from drifting back out of conformance.
"""

from __future__ import annotations

import json as _json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import unquote

import httpx
import pytest

from cachekit.backends.cachekitio.backend import CachekitIOBackend

_TEST_API_URL = "https://api.cachekit.io"
_TEST_API_KEY = "ck_test_abc123"  # pragma: allowlist secret — fake fixture, not a real key

# Canonical 7-segment cache key (matches saas/apps/cache/src/index.ts validator):
# ns:{namespace}:func:{module.function}:args:{64-hex-blake2b}:{flags}
_CANONICAL_KEY = "ns:articles-prod:func:insight_times.api.routes._list_articles_cached:args:" + ("a" * 64) + ":1s"


def _json_response(status_code: int, body: dict[str, Any]) -> httpx.Response:
    """Build a real httpx.Response with JSON body and request attached."""
    response = httpx.Response(status_code, content=_json.dumps(body).encode())
    response.request = httpx.Request("POST", f"{_TEST_API_URL}/v1/cache/key/lock")
    return response


@pytest.fixture
def backend() -> CachekitIOBackend:
    """Build a CachekitIOBackend with mocked HTTP clients."""
    with patch(
        "cachekit.backends.cachekitio.backend.get_sync_http_client",
        return_value=MagicMock(spec=httpx.Client),
    ):
        with patch(
            "cachekit.backends.cachekitio.backend.get_cached_async_http_client",
            return_value=MagicMock(spec=httpx.AsyncClient),
        ):
            return CachekitIOBackend(api_url=_TEST_API_URL, api_key=_TEST_API_KEY)


def _path_calls(mock: AsyncMock) -> list[str]:
    """Extract endpoint-path arg order from mock's await history."""
    return [call.args[1] for call in mock.await_args_list]


@pytest.mark.unit
class TestBareCacheKeyContract:
    """The wrapper must pass the bare 7-segment cache key — no ``:lock`` suffix."""

    async def test_acquire_lock_url_preserves_seven_segments(self, backend: CachekitIOBackend) -> None:
        """The POST endpoint must be ``{url-encoded bare 7-seg key}/lock`` — not 8 segments.

        Decoding the encoded-key portion must yield exactly 7 colon-separated parts.
        If the wrapper (or the backend) appends ``:lock`` to the key, the decoded key has
        8 segments and the SaaS validator returns 400.
        """
        request_mock = AsyncMock(return_value=_json_response(200, {"lock_id": "lock-1"}))
        backend._request_async = request_mock  # type: ignore[method-assign]

        async with backend.acquire_lock(_CANONICAL_KEY, timeout=30.0, blocking_timeout=None):
            pass

        post_path = _path_calls(request_mock)[0]

        # Endpoint must be of the form "<encoded-key>/lock" — exactly one "/lock" suffix.
        assert post_path.endswith("/lock"), f"POST endpoint must end with /lock, got {post_path!r}"
        encoded_key_portion = post_path[: -len("/lock")]

        # The encoded key portion must NOT itself end with the encoded form of `:lock`
        # (i.e. `%3Alock`). That is exactly the bug the canonical 7-segment validator
        # catches at the SaaS edge.
        assert not encoded_key_portion.endswith("%3Alock"), (
            f"encoded key smuggled :lock suffix; got {encoded_key_portion!r} — "
            f"the wrapper polluted the cache_key with ':lock' before encoding"
        )

        # Decode and assert canonical 7-segment shape — anchors the fix beyond the
        # negative `%3Alock` check above.
        decoded_key = unquote(encoded_key_portion)
        assert decoded_key == _CANONICAL_KEY, f"decoded key drift: {decoded_key!r} != {_CANONICAL_KEY!r}"
        assert decoded_key.count(":") == 6, (
            f"canonical SaaS key must have exactly 7 colon-segments (6 colons); got {decoded_key.count(':')} in {decoded_key!r}"
        )
