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

These tests pin the request path the backend's client hands its connection pool,
which urllib3 sends as the request target, to a bare 7-segment key — no
``%3Alock`` smuggled in via the encoded key portion. The Rust and
TypeScript SDKs already implement this contract; this regression test prevents
the Python SDK from drifting back out of conformance.
"""

from __future__ import annotations

from urllib.parse import unquote

import pytest
from urllib3 import HTTPResponse
from urllib3.util.url import _encode_target

from tests.utils.cachekitio_fakes import FakeRequest, fake_backend, response

# Canonical 7-segment cache key (matches saas/apps/cache/src/index.ts validator):
# ns:{namespace}:func:{module.function}:args:{64-hex-blake2b}:{flags}
_CANONICAL_KEY = "ns:articles-prod:func:insight_times.api.routes._list_articles_cached:args:" + ("a" * 64) + ":1s"


def _lock_api(request: FakeRequest) -> HTTPResponse:
    """The SaaS lock endpoint: POST grants, DELETE releases."""
    return response(200, json={"lock_id": "lock-1"} if request.method == "POST" else {})


@pytest.mark.unit
class TestBareCacheKeyContract:
    """The wrapper must pass the bare 7-segment cache key — no ``:lock`` suffix."""

    async def test_acquire_lock_url_preserves_seven_segments(self) -> None:
        """The POST path must be ``/v1/cache/{url-encoded bare 7-seg key}/lock`` — not 8 segments.

        Decoding the encoded-key portion must yield exactly 7 colon-separated parts.
        If the wrapper (or the backend) appends ``:lock`` to the key, the decoded key has
        8 segments and the SaaS validator returns 400.
        """
        backend, pool = fake_backend(_lock_api)

        async with backend.acquire_lock(_CANONICAL_KEY, timeout=30.0, blocking_timeout=None):
            pass

        assert [r.method for r in pool.requests] == ["POST", "DELETE"]
        post_path = pool.requests[0].path
        # The path the pool receives is the request target on the wire: urlopen re-encodes only characters
        # that are invalid in a path, and a percent-encoded key has none, so nothing below is undone in flight.
        assert _encode_target(post_path) == post_path

        # Path must be of the form "/v1/cache/<encoded-key>/lock" — exactly one "/lock" suffix.
        assert post_path.startswith("/v1/cache/"), f"POST path escaped /v1/cache/: {post_path!r}"
        assert post_path.endswith("/lock"), f"POST path must end with /lock, got {post_path!r}"
        encoded_key_portion = post_path[len("/v1/cache/") : -len("/lock")]

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
