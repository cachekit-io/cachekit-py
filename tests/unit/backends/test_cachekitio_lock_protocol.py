"""Regression + protocol-conformance tests for CachekitIOBackend.acquire_lock.

Issue #129: @cache.io crashed in prod with
    TypeError: CachekitIOBackend.acquire_lock() got an unexpected keyword argument 'blocking_timeout'

The wrapper at decorators/wrapper.py:1072 calls the backend as an async context
manager with (key, timeout, blocking_timeout). The backend must conform to the
LockableBackend protocol in backends/base.py.

SaaS contract (saas/apps/cache/src/index.ts:732): POST {key}/lock always returns
HTTP 200 with body {"lock_id": <id_or_null>}; null indicates lock held by another
caller. DELETE {key}/lock?lock_id=... releases.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from cachekit.backends.cachekitio.backend import CachekitIOBackend
from cachekit.backends.errors import BackendError, BackendErrorType

_TEST_API_URL = "https://api.cachekit.io"
_TEST_API_KEY = "ck_test_abc123"  # pragma: allowlist secret — fake fixture, not a real key

_HELD = {"lock_id": None}


def _json_response(status_code: int, body: dict[str, Any]) -> httpx.Response:
    """Build a real httpx.Response with JSON body and a request attached."""
    import json as _json

    response = httpx.Response(status_code, content=_json.dumps(body).encode())
    response.request = httpx.Request("POST", f"{_TEST_API_URL}/v1/cache/key/lock")
    return response


def _raw_response(status_code: int, content: bytes) -> httpx.Response:
    """Build an httpx.Response with arbitrary bytes (for malformed-body tests)."""
    response = httpx.Response(status_code, content=content)
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


def _method_calls(mock: AsyncMock) -> list[str]:
    """Extract HTTP method order from mock's await history."""
    return [call.args[0] for call in mock.await_args_list]


def _path_calls(mock: AsyncMock) -> list[str]:
    """Extract URL-path argument order from mock's await history."""
    return [call.args[1] for call in mock.await_args_list]


@pytest.mark.unit
class TestLockProtocolRegression:
    """Issue #129 — wrapper-call-shape compatibility."""

    async def test_acquire_lock_accepts_blocking_timeout_kwarg(self, backend: CachekitIOBackend) -> None:
        """The wrapper passes blocking_timeout=... — backend must accept it without TypeError."""
        backend._request_async = AsyncMock(  # type: ignore[method-assign]
            return_value=_json_response(200, {"lock_id": "lock-abc"})
        )

        async with backend.acquire_lock("test:key", timeout=30.0, blocking_timeout=5.0) as acquired:
            assert acquired is True

    async def test_acquire_lock_is_async_context_manager(self, backend: CachekitIOBackend) -> None:
        """Backend must conform to LockableBackend protocol — async context manager yielding bool."""
        backend._request_async = AsyncMock(  # type: ignore[method-assign]
            return_value=_json_response(200, {"lock_id": "lock-xyz"})
        )

        ctx = backend.acquire_lock("test:key", timeout=30.0, blocking_timeout=None)
        assert hasattr(ctx, "__aenter__"), "acquire_lock must return an async context manager"
        assert hasattr(ctx, "__aexit__"), "acquire_lock must return an async context manager"
        async with ctx as acquired:
            assert isinstance(acquired, bool)


@pytest.mark.unit
class TestLockAcquisitionBehavior:
    """Lock semantics: immediate acquire, poll-and-retry, timeout."""

    async def test_acquired_on_first_attempt(self, backend: CachekitIOBackend) -> None:
        """When server returns lock_id immediately, yield True with one POST + one DELETE."""
        request_mock = AsyncMock(return_value=_json_response(200, {"lock_id": "lock-1"}))
        backend._request_async = request_mock  # type: ignore[method-assign]

        async with backend.acquire_lock("k", timeout=30.0, blocking_timeout=5.0) as acquired:
            assert acquired is True

        # Shape-only assertion (don't pin exact await_count — release helper is internal).
        methods = _method_calls(request_mock)
        assert methods.count("POST") == 1
        assert methods.count("DELETE") == 1

    async def test_blocking_timeout_exceeded_yields_false(self, backend: CachekitIOBackend) -> None:
        """When server keeps returning null lock_id and blocking_timeout elapses, yield False."""
        request_mock = AsyncMock(return_value=_json_response(200, _HELD))
        backend._request_async = request_mock  # type: ignore[method-assign]

        async with backend.acquire_lock("k", timeout=30.0, blocking_timeout=1.0) as acquired:
            assert acquired is False

        # Polling must have executed multiple attempts in 1s; jitter floor is 25ms,
        # ceiling 250ms, so at least 3 POSTs is comfortably reachable.
        methods = _method_calls(request_mock)
        assert methods.count("POST") >= 3, f"expected ≥3 POST attempts in 1s, got {methods}"
        assert "DELETE" not in methods

    async def test_eventually_acquires_after_poll(self, backend: CachekitIOBackend) -> None:
        """Held on first attempt, free on second — yield True after retry."""
        responses = [
            _json_response(200, _HELD),  # held
            _json_response(200, {"lock_id": "lock-late"}),  # free
            _json_response(200, {}),  # release
        ]
        request_mock = AsyncMock(side_effect=responses)
        backend._request_async = request_mock  # type: ignore[method-assign]

        async with backend.acquire_lock("k", timeout=30.0, blocking_timeout=5.0) as acquired:
            assert acquired is True

        methods = _method_calls(request_mock)
        assert methods.count("POST") >= 2
        assert "DELETE" in methods

    async def test_non_blocking_returns_immediately_on_held(self, backend: CachekitIOBackend) -> None:
        """blocking_timeout=None means non-blocking — yield False at once if held, no retry."""
        request_mock = AsyncMock(return_value=_json_response(200, _HELD))
        backend._request_async = request_mock  # type: ignore[method-assign]

        async with backend.acquire_lock("k", timeout=30.0, blocking_timeout=None) as acquired:
            assert acquired is False

        assert _method_calls(request_mock) == ["POST"]


@pytest.mark.unit
class TestLockRelease:
    """Lock must always be released on context exit, even on exception."""

    async def test_release_called_on_exception_inside_context(self, backend: CachekitIOBackend) -> None:
        """If user code raises inside the with block, the lock is still released."""
        responses = [
            _json_response(200, {"lock_id": "lock-cleanup"}),  # acquire
            _json_response(200, {}),  # release
        ]
        request_mock = AsyncMock(side_effect=responses)
        backend._request_async = request_mock  # type: ignore[method-assign]

        with pytest.raises(RuntimeError, match="user error"):
            async with backend.acquire_lock("k", timeout=30.0, blocking_timeout=5.0) as acquired:
                assert acquired is True
                raise RuntimeError("user error")

        assert _method_calls(request_mock) == ["POST", "DELETE"]

    async def test_no_release_when_never_acquired(self, backend: CachekitIOBackend) -> None:
        """Failed acquisition must not trigger a release call (no lock_id to release)."""
        request_mock = AsyncMock(return_value=_json_response(200, _HELD))
        backend._request_async = request_mock  # type: ignore[method-assign]

        async with backend.acquire_lock("k", timeout=30.0, blocking_timeout=None) as acquired:
            assert acquired is False

        assert "DELETE" not in _method_calls(request_mock)

    async def test_release_failure_does_not_mask_user_exception(self, backend: CachekitIOBackend) -> None:
        """If DELETE raises, the user's exception must still propagate (not be masked)."""

        async def side_effect(method: str, *_args: Any, **_kwargs: Any) -> httpx.Response:
            if method == "POST":
                return _json_response(200, {"lock_id": "lock-x"})
            raise BackendError("release failed", error_type=BackendErrorType.TRANSIENT)

        backend._request_async = AsyncMock(side_effect=side_effect)  # type: ignore[method-assign]

        with pytest.raises(RuntimeError, match="user error"):
            async with backend.acquire_lock("k", timeout=30.0, blocking_timeout=None):
                raise RuntimeError("user error")


@pytest.mark.unit
class TestSecurityHardening:
    """URL-encoding + malformed-input safety (review findings, issue #129)."""

    async def test_url_injection_via_lock_key_encoded(self, backend: CachekitIOBackend) -> None:
        """lock_key with `?`, `&`, `=` must be percent-encoded — positive assertion on canonical form
        so a broken encoder that merely strips dangerous chars cannot pass this test."""
        request_mock = AsyncMock(return_value=_json_response(200, {"lock_id": "lid"}))
        backend._request_async = request_mock  # type: ignore[method-assign]

        async with backend.acquire_lock("evil?lock_id=BOGUS&x=", timeout=30.0, blocking_timeout=None):
            pass

        post_path = _path_calls(request_mock)[0]
        # POST path is "{encoded_key}/lock"; the key must end with %3D (encoded `=`) before `/lock`.
        assert post_path == "evil%3Flock_id%3DBOGUS%26x%3D/lock", f"unexpected POST path: {post_path!r}"
        delete_path = _path_calls(request_mock)[1]
        # DELETE path: "{encoded_key}/lock?lock_id={encoded_id}" — exactly one `?` separates path/query.
        assert delete_path.startswith("evil%3Flock_id%3DBOGUS%26x%3D/lock?lock_id="), f"bad DELETE: {delete_path!r}"
        assert delete_path.count("?") == 1

    async def test_url_injection_via_lock_id_encoded(self, backend: CachekitIOBackend) -> None:
        """Server-issued lock_id with `&` must be URL-encoded in the DELETE query string."""
        request_mock = AsyncMock(
            side_effect=[
                _json_response(200, {"lock_id": "abc&injected=1"}),
                _json_response(200, {}),
            ]
        )
        backend._request_async = request_mock  # type: ignore[method-assign]

        async with backend.acquire_lock("k", timeout=30.0, blocking_timeout=None):
            pass

        delete_path = _path_calls(request_mock)[1]
        # The lock_id's `&` must be percent-encoded; raw `&` would smuggle a new param.
        assert "abc&injected" not in delete_path, f"unencoded `&` in lock_id: {delete_path!r}"
        assert "abc%26injected" in delete_path, f"expected percent-encoded lock_id in {delete_path!r}"

    async def test_malformed_json_body_treated_as_held(self, backend: CachekitIOBackend) -> None:
        """Empty/non-JSON 200 response must not crash the wrapper (root cause of #129 class)."""
        request_mock = AsyncMock(return_value=_raw_response(200, b""))
        backend._request_async = request_mock  # type: ignore[method-assign]

        async with backend.acquire_lock("k", timeout=30.0, blocking_timeout=None) as acquired:
            assert acquired is False  # malformed → treated as held, not crash

    async def test_non_string_lock_id_treated_as_held(self, backend: CachekitIOBackend) -> None:
        """SaaS contract violation (lock_id of unexpected type) must not be misinterpreted as acquired."""
        request_mock = AsyncMock(return_value=_json_response(200, {"lock_id": 42}))
        backend._request_async = request_mock  # type: ignore[method-assign]

        async with backend.acquire_lock("k", timeout=30.0, blocking_timeout=None) as acquired:
            assert acquired is False

    @pytest.mark.parametrize("bad_timeout", [float("nan"), float("inf"), float("-inf"), -5.0, 0.0])
    async def test_non_finite_or_non_positive_timeout_clamped(self, backend: CachekitIOBackend, bad_timeout: float) -> None:
        """NaN/inf/negative timeout must not escape as ValueError/OverflowError; clamped to ≥1ms.

        Inspects the POST body to pin the clamp — a future regression removing
        math.isfinite() would otherwise pass the no-crash assertion via AsyncMock.
        """
        import json as _json
        import math as _math

        request_mock = AsyncMock(return_value=_json_response(200, _HELD))
        backend._request_async = request_mock  # type: ignore[method-assign]

        async with backend.acquire_lock("k", timeout=bad_timeout, blocking_timeout=None) as acquired:
            assert acquired is False

        post_body = _json.loads(request_mock.await_args_list[0].kwargs["content"])
        sent = post_body["timeout_ms"]
        assert isinstance(sent, int) and sent >= 1, f"expected clamped finite int ≥1, got {sent!r}"
        assert _math.isfinite(sent)


@pytest.mark.unit
class TestErrorPropagation:
    """Non-retryable errors must escape — don't silently poll on bad API key."""

    async def test_authentication_error_propagates(self, backend: CachekitIOBackend) -> None:
        """AUTHENTICATION BackendError must NOT be swallowed — wrapper should degrade once, not spam."""
        request_mock = AsyncMock(side_effect=BackendError("bad api key", error_type=BackendErrorType.AUTHENTICATION))
        backend._request_async = request_mock  # type: ignore[method-assign]

        with pytest.raises(BackendError) as exc_info:
            async with backend.acquire_lock("k", timeout=30.0, blocking_timeout=5.0):
                pytest.fail("should never enter context body")
        assert exc_info.value.error_type == BackendErrorType.AUTHENTICATION
        # Single attempt only — no billable polling against an auth failure.
        assert request_mock.await_count == 1

    async def test_permanent_error_propagates(self, backend: CachekitIOBackend) -> None:
        """PERMANENT BackendError (e.g. malformed lock_key rejected by SaaS) must NOT be swallowed."""
        request_mock = AsyncMock(side_effect=BackendError("bad request", error_type=BackendErrorType.PERMANENT))
        backend._request_async = request_mock  # type: ignore[method-assign]

        with pytest.raises(BackendError):
            async with backend.acquire_lock("k", timeout=30.0, blocking_timeout=5.0):
                pytest.fail("should never enter context body")
        assert request_mock.await_count == 1

    async def test_transient_error_swallowed_for_retry(self, backend: CachekitIOBackend) -> None:
        """TRANSIENT errors (e.g. 503) must be swallowed so polling can retry the SaaS."""
        responses: list[Any] = [
            BackendError("server down", error_type=BackendErrorType.TRANSIENT),
            _json_response(200, {"lock_id": "lock-recovered"}),
            _json_response(200, {}),  # release
        ]
        request_mock = AsyncMock(side_effect=responses)
        backend._request_async = request_mock  # type: ignore[method-assign]

        async with backend.acquire_lock("k", timeout=30.0, blocking_timeout=5.0) as acquired:
            assert acquired is True

        assert request_mock.await_count == 3


@pytest.mark.unit
class TestCancellation:
    """Cooperative cancellation must still release the lock."""

    async def test_cancellation_inside_context_releases_lock(self, backend: CachekitIOBackend) -> None:
        """asyncio.CancelledError inside the with block must still trigger DELETE."""
        responses = [
            _json_response(200, {"lock_id": "lock-cancel"}),  # acquire
            _json_response(200, {}),  # release
        ]
        request_mock = AsyncMock(side_effect=responses)
        backend._request_async = request_mock  # type: ignore[method-assign]

        async def run() -> None:
            async with backend.acquire_lock("k", timeout=30.0, blocking_timeout=None):
                await asyncio.sleep(10)  # will be cancelled

        task = asyncio.create_task(run())
        await asyncio.sleep(0.01)  # let it enter the context
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        # DELETE must have been issued even though the body was cancelled mid-flight.
        assert _method_calls(request_mock) == ["POST", "DELETE"]
