"""Regression + protocol-conformance tests for CachekitIOBackend.acquire_lock.

Issue #129: @cache.io crashed in prod with
    TypeError: CachekitIOBackend.acquire_lock() got an unexpected keyword argument 'blocking_timeout'

The wrapper at decorators/wrapper.py:1072 calls the backend as an async context
manager with (key, timeout, blocking_timeout). The backend must conform to the
LockableBackend protocol in backends/base.py.

SaaS contract (saas/apps/cache/src/index.ts:732): POST {key}/lock always returns
HTTP 200 with body {"lock_id": <id_or_null>}; null indicates lock held by another
caller. DELETE {key}/lock releases, sending the lock id in the X-CacheKit-Lock-Id
header (CWE-532) rather than a ?lock_id= query param.
"""

from __future__ import annotations

import asyncio
import logging
import os
import urllib.parse
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from cachekit import cache
from cachekit.backends.cachekitio.backend import LOCK_ID_HEADER, CachekitIOBackend
from cachekit.backends.errors import BackendError, BackendErrorType
from cachekit.hash_utils import redact_cache_key, redact_error_for_log

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
        "cachekit.backends.cachekitio.backend.lease_sync_http_client",
        return_value=MagicMock(pid=os.getpid(), client=MagicMock(spec=httpx.Client)),
    ):
        with patch(
            "cachekit.backends.cachekitio.backend.lease_async_http_client",
            return_value=MagicMock(pid=os.getpid(), client=MagicMock(spec=httpx.AsyncClient)),
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

        # Behavior under test: the client RETRIED at least once after the first
        # held response. Don't pin a wall-clock count — slow runners flake on
        # tight timing assertions even when the retry logic is correct.
        methods = _method_calls(request_mock)
        assert methods.count("POST") >= 2, f"expected client to retry at least once, got {methods}"
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
        # DELETE path: "{encoded_key}/lock" — lock_id now rides the X-CacheKit-Lock-Id
        # header (CWE-532), so the path carries no query and the token never hits the URL.
        assert delete_path == "evil%3Flock_id%3DBOGUS%26x%3D/lock", f"bad DELETE: {delete_path!r}"
        assert "?" not in delete_path

    async def test_lock_id_sent_in_header_not_query(self, backend: CachekitIOBackend) -> None:
        """CWE-532: the lock capability token rides the X-CacheKit-Lock-Id request
        header, never the query string (which leaks via access/proxy logs + OTel spans)."""
        responses = [
            _json_response(200, {"lock_id": "lock-secret-123"}),  # acquire
            _json_response(200, {}),  # release
        ]
        request_mock = AsyncMock(side_effect=responses)
        backend._request_async = request_mock  # type: ignore[method-assign]

        async with backend.acquire_lock("k", timeout=30.0, blocking_timeout=None):
            pass

        delete_call = next(c for c in request_mock.await_args_list if c.args[0] == "DELETE")
        headers = delete_call.kwargs.get("headers") or {}
        endpoint = delete_call.args[1]
        # Token present in the header under the exact wire name...
        assert headers.get("X-CacheKit-Lock-Id") == "lock-secret-123"
        # ...and absent from the URL entirely (no query param, no leak).
        assert "lock_id" not in endpoint
        assert "lock-secret-123" not in endpoint

    async def test_lock_id_with_query_metachars_isolated_in_header(self, backend: CachekitIOBackend) -> None:
        """A server-issued lock_id containing query metacharacters can no longer smuggle a
        query param: it travels in the X-CacheKit-Lock-Id header, not the URL. The raw value
        is sent verbatim (headers are not URL-encoded; the server compares the raw token)."""
        request_mock = AsyncMock(
            side_effect=[
                _json_response(200, {"lock_id": "abc&injected=1"}),
                _json_response(200, {}),
            ]
        )
        backend._request_async = request_mock  # type: ignore[method-assign]

        async with backend.acquire_lock("k", timeout=30.0, blocking_timeout=None):
            pass

        delete_call = next(c for c in request_mock.await_args_list if c.args[0] == "DELETE")
        headers = delete_call.kwargs.get("headers") or {}
        endpoint = delete_call.args[1]
        # Sent verbatim in the header (server compares the raw token)...
        assert headers.get("X-CacheKit-Lock-Id") == "abc&injected=1"
        # ...and the metacharacters never touch the URL, so query smuggling is impossible.
        assert "injected" not in endpoint
        assert "?" not in endpoint

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

    @pytest.mark.parametrize(
        "error_type",
        [BackendErrorType.TRANSIENT, BackendErrorType.TIMEOUT, BackendErrorType.UNKNOWN],
    )
    async def test_retryable_error_ends_the_wait(self, backend: CachekitIOBackend, error_type: BackendErrorType) -> None:
        """A 429, 5xx, timeout or connect error is an error, not contention: one POST, then raise.

        Only ``200`` with a null ``lock_id`` is contested. Polling a failing endpoint used to
        stall every call for the whole ``blocking_timeout``.
        """
        request_mock = AsyncMock(side_effect=BackendError("server down", error_type=error_type))
        backend._request_async = request_mock  # type: ignore[method-assign]

        with pytest.raises(BackendError) as exc_info:
            async with backend.acquire_lock("k", timeout=30.0, blocking_timeout=5.0):
                pytest.fail("should never enter context body")
        assert exc_info.value.error_type == error_type
        assert request_mock.await_count == 1

    async def test_error_while_polling_ends_the_wait(self, backend: CachekitIOBackend) -> None:
        """An error on a retry after a contested answer ends the wait too."""
        request_mock = AsyncMock(
            side_effect=[_json_response(200, _HELD), BackendError("rate limited", error_type=BackendErrorType.TRANSIENT)]
        )
        backend._request_async = request_mock  # type: ignore[method-assign]

        with pytest.raises(BackendError):
            async with backend.acquire_lock("k", timeout=30.0, blocking_timeout=5.0):
                pytest.fail("should never enter context body")
        assert request_mock.await_count == 2


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

        entered = asyncio.Event()

        async def run() -> None:
            async with backend.acquire_lock("k", timeout=30.0, blocking_timeout=None):
                entered.set()  # explicit handshake — body has entered the context
                await asyncio.sleep(10)  # will be cancelled

        task = asyncio.create_task(run())
        await entered.wait()  # deterministic, no wall-clock dependence
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        # DELETE must have been issued even though the body was cancelled mid-flight.
        assert _method_calls(request_mock) == ["POST", "DELETE"]


class _GatedRequests:
    """Fake ``_request_async`` that can hold a POST / DELETE in flight and records completion.

    A held request waits on an ``asyncio.Event`` gate, so a test can cancel while it is in flight.
    Completion (``posts_done`` / ``deleted``) is recorded only after the gate opens: a cancel thrown
    into the request at its gate — what a bare await does to a real in-flight request — never
    completes it. ``AsyncMock`` would record the call on entry and could not tell the two apart.
    """

    def __init__(self, *posts: Any, held_posts: frozenset[int] = frozenset({0}), hold_delete: bool = False) -> None:
        self.posts = list(posts)  # one outcome per POST: a response to return or an exception to raise
        self.held_posts = held_posts
        self.hold_delete = hold_delete
        self.calls: list[str] = []
        self.post_entered, self.post_gate = asyncio.Event(), asyncio.Event()
        self.delete_entered, self.delete_gate = asyncio.Event(), asyncio.Event()
        self.posts_done = 0
        self.deleted: list[str] = []  # lock ids of DELETEs that ran to completion

    async def __call__(self, method: str, endpoint: str, **kwargs: Any) -> httpx.Response:
        index = self.calls.count(method)
        self.calls.append(method)
        if method == "POST":
            if index in self.held_posts:
                self.post_entered.set()
                await self.post_gate.wait()
            self.posts_done += 1
            outcome = self.posts[index]
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome
        assert method == "DELETE", method
        if self.hold_delete:
            self.delete_entered.set()
            await self.delete_gate.wait()
        self.deleted.append(kwargs["headers"][LOCK_ID_HEADER])
        return _json_response(200, {})


async def _hold_lock(
    backend: CachekitIOBackend, blocking_timeout: float | None = None, entered: asyncio.Event | None = None
) -> None:
    async with backend.acquire_lock("k", timeout=30.0, blocking_timeout=blocking_timeout):
        if entered is not None:
            entered.set()
        await asyncio.sleep(10)  # cancelled


async def _reached(event: asyncio.Event) -> None:
    """Wait for a handshake, bounded: a request that is never sent must fail the test, not hang it."""
    await asyncio.wait_for(event.wait(), timeout=2.0)


async def _cancel_twice(task: asyncio.Task[None]) -> None:
    for _ in range(2):
        task.cancel()
        await asyncio.sleep(0)


@pytest.mark.unit
class TestCancellationMidRequest:
    """A cancel landing while a lock POST / DELETE is in flight must not orphan a server-granted lock."""

    async def test_cancel_during_acquire_post_releases_the_lock_it_goes_on_to_win(self, backend: CachekitIOBackend) -> None:
        fake = _GatedRequests(_json_response(200, {"lock_id": "won"}))
        backend._request_async = fake  # type: ignore[method-assign]

        task = asyncio.create_task(_hold_lock(backend))
        await _reached(fake.post_entered)
        task.cancel()
        fake.post_gate.set()  # the server grants the lock after the cancel
        with pytest.raises(asyncio.CancelledError):
            await task

        # Checked with no further yield: a release left to a done-callback would not have run yet.
        assert fake.posts_done == 1
        assert fake.deleted == ["won"]

    async def test_second_cancel_during_acquire_post_still_releases_the_lock(self, backend: CachekitIOBackend) -> None:
        """A drain that absorbs one cancel per await lets the second one escape while the POST is in flight."""
        fake = _GatedRequests(_json_response(200, {"lock_id": "won"}))
        backend._request_async = fake  # type: ignore[method-assign]

        task = asyncio.create_task(_hold_lock(backend))
        await _reached(fake.post_entered)
        await _cancel_twice(task)
        fake.post_gate.set()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert fake.posts_done == 1
        assert fake.deleted == ["won"]

    async def test_two_cancels_during_release_after_a_drained_acquire(self, backend: CachekitIOBackend) -> None:
        fake = _GatedRequests(_json_response(200, {"lock_id": "won"}), hold_delete=True)
        backend._request_async = fake  # type: ignore[method-assign]

        task = asyncio.create_task(_hold_lock(backend))
        await _reached(fake.post_entered)
        task.cancel()
        fake.post_gate.set()
        await _reached(fake.delete_entered)
        await _cancel_twice(task)
        fake.delete_gate.set()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert fake.posts_done == 1
        assert fake.deleted == ["won"]

    async def test_two_cancels_during_release_from_the_context_exit(self, backend: CachekitIOBackend) -> None:
        fake = _GatedRequests(_json_response(200, {"lock_id": "held"}), held_posts=frozenset(), hold_delete=True)
        backend._request_async = fake  # type: ignore[method-assign]
        entered = asyncio.Event()

        task = asyncio.create_task(_hold_lock(backend, entered=entered))
        await _reached(entered)
        task.cancel()  # inside the async-with body: the finally starts the release
        await _reached(fake.delete_entered)
        await _cancel_twice(task)
        fake.delete_gate.set()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert fake.deleted == ["held"]

    async def test_cancel_during_a_retry_post_releases_the_lock_it_goes_on_to_win(self, backend: CachekitIOBackend) -> None:
        fake = _GatedRequests(_json_response(200, _HELD), _json_response(200, {"lock_id": "won"}), held_posts=frozenset({1}))
        backend._request_async = fake  # type: ignore[method-assign]

        task = asyncio.create_task(_hold_lock(backend, blocking_timeout=5.0))
        await _reached(fake.post_entered)
        task.cancel()
        fake.post_gate.set()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert fake.posts_done == 2
        assert fake.deleted == ["won"]

    @pytest.mark.parametrize("error_type", [BackendErrorType.AUTHENTICATION, BackendErrorType.PERMANENT])
    async def test_cancel_wins_over_a_drained_acquire_error(
        self, backend: CachekitIOBackend, caplog: pytest.LogCaptureFixture, error_type: BackendErrorType
    ) -> None:
        """Raising the drained error instead would let the cancelled task degrade to a no-lock call."""
        key = "ns:app:func:mod.fn:args:tenant-42-secret:0"
        err = BackendError("rejected", error_type=error_type)
        fake = _GatedRequests(err)
        backend._request_async = fake  # type: ignore[method-assign]

        async def acquire() -> None:
            async with backend.acquire_lock(key, timeout=30.0, blocking_timeout=None):
                pytest.fail("should never enter context body")

        caplog.set_level(logging.WARNING, logger="cachekit.backends.cachekitio.backend")
        task = asyncio.create_task(acquire())
        await _reached(fake.post_entered)
        task.cancel()
        fake.post_gate.set()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert fake.posts_done == 1
        assert "DELETE" not in fake.calls
        warnings = [
            r for r in caplog.records if r.name == "cachekit.backends.cachekitio.backend" and r.levelno == logging.WARNING
        ]
        assert len(warnings) == 1
        message = warnings[0].getMessage()
        assert redact_cache_key(key) in message
        assert redact_error_for_log(err) in message
        assert "tenant-42-secret" not in message

    async def test_cancel_during_a_retry_post_that_is_held_stops_polling(self, backend: CachekitIOBackend) -> None:
        fake = _GatedRequests(_json_response(200, _HELD), _json_response(200, _HELD), held_posts=frozenset({1}))
        backend._request_async = fake  # type: ignore[method-assign]

        task = asyncio.create_task(_hold_lock(backend, blocking_timeout=5.0))
        await _reached(fake.post_entered)
        task.cancel()
        fake.post_gate.set()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert fake.posts_done == 2
        assert fake.calls == ["POST", "POST"]

    async def test_cancel_through_the_decorator_wins_over_a_drained_lock_error(self, backend: CachekitIOBackend) -> None:
        """End to end: the cancelled caller sees CancelledError and never runs the function without the lock."""
        fake = _GatedRequests(BackendError("bad key", error_type=BackendErrorType.AUTHENTICATION))
        backend._request_async = fake  # type: ignore[method-assign]
        backend.get = MagicMock(return_value=None)  # type: ignore[method-assign]  # L2 miss: the lock path runs
        calls = 0

        @cache(backend=backend, ttl=300, l1_enabled=False)
        async def compute(x: int) -> int:
            nonlocal calls
            calls += 1
            return x * 2

        task = asyncio.create_task(compute(1))
        await _reached(fake.post_entered)
        task.cancel()
        fake.post_gate.set()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert fake.posts_done == 1
        assert fake.calls == ["POST"]
        assert calls == 0


def _lock_failing_backend(lock_status: int, lock_body: bytes) -> tuple[CachekitIOBackend, list[httpx.Request]]:
    """A backend over a real httpx client whose lock POST fails; every read misses, writes succeed."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.method == "POST" and request.url.raw_path.endswith(b"/lock"):
            return httpx.Response(lock_status, content=lock_body)
        if request.method == "GET":
            return httpx.Response(404)
        return httpx.Response(200, content=b"{}")

    transport = httpx.MockTransport(handler)
    with (
        patch(
            "cachekit.backends.cachekitio.backend.lease_sync_http_client",
            return_value=MagicMock(pid=os.getpid(), client=httpx.Client(base_url=_TEST_API_URL, transport=transport)),
        ),
        patch(
            "cachekit.backends.cachekitio.backend.lease_async_http_client",
            return_value=MagicMock(pid=os.getpid(), client=httpx.AsyncClient(base_url=_TEST_API_URL, transport=transport)),
        ),
    ):
        return CachekitIOBackend(api_url=_TEST_API_URL, api_key=_TEST_API_KEY), seen


@pytest.mark.unit
class TestDecoratorDegradesOnLockError:
    """End to end: a failed lock POST never reaches an async caller, and ends the lock wait.

    The function runs once, uncached, as a sync call would, and no ``cachekit.*`` log record
    names the cache key in either form: the raw ``HTTPStatusError`` carries the request URL, in
    which the key is percent-encoded.
    """

    @pytest.mark.parametrize(
        ("status", "body"),
        [
            pytest.param(401, b'{"error": "invalid api key"}', id="401"),
            pytest.param(403, b"<!DOCTYPE html><title>Just a moment...</title>", id="403-challenge"),
            pytest.param(403, b"error code: 1010", id="403-1010"),
            pytest.param(400, b'{"error": "invalid key"}', id="400"),
            pytest.param(429, b'{"error": "rate limited"}', id="429"),
        ],
    )
    async def test_runs_once_uncached_with_one_lock_post(
        self, status: int, body: bytes, caplog: pytest.LogCaptureFixture
    ) -> None:
        backend, seen = _lock_failing_backend(status, body)
        runs: list[int] = []

        @cache(backend=backend, ttl=300, l1_enabled=False, namespace="lab5346")
        async def compute(x: int) -> int:
            runs.append(x)
            return x * 2

        with caplog.at_level(logging.DEBUG, logger="cachekit"):
            assert await compute(1) == 2

        assert runs == [1]
        lock_posts = [r for r in seen if r.method == "POST" and r.url.raw_path.endswith(b"/lock")]
        assert len(lock_posts) == 1, "a lock error ends the wait: no polling"

        encoded_key = lock_posts[0].url.raw_path.decode().removeprefix("/v1/cache/").removesuffix("/lock")
        raw_key = urllib.parse.unquote(encoded_key)
        assert raw_key != encoded_key, "the key must carry a ':' for the encoded check to mean anything"
        formatter = logging.Formatter()
        for record in caplog.records:
            if record.name.startswith("cachekit"):
                text = formatter.format(record)
                assert raw_key not in text and encoded_key not in text, f"{record.name} logged the cache key: {text}"


async def _drain_background_releases() -> None:
    """Wait for the executor thread that sends the background DELETE, as ``asyncio.run`` teardown does."""
    await asyncio.wait_for(asyncio.get_running_loop().shutdown_default_executor(), timeout=5.0)


@pytest.mark.unit
class TestFillLock:
    """acquire_fill_lock (LAB-7064): reports a first-attempt grant and releases without blocking the caller."""

    @pytest.mark.parametrize(
        ("posts", "expected"),
        [
            ([{"lock_id": "l1"}], (True, True)),
            ([_HELD, {"lock_id": "l1"}], (True, False)),
            ([_HELD], (False, False)),
        ],
        ids=["first-attempt", "after-a-poll", "never"],
    )
    async def test_yields_acquired_and_uncontended(
        self, backend: CachekitIOBackend, posts: list[dict[str, Any]], expected: tuple[bool, bool]
    ) -> None:
        backend._request_async = AsyncMock(side_effect=[_json_response(200, body) for body in posts])  # type: ignore[method-assign]
        backend._request_sync = MagicMock(return_value=_json_response(200, {}))  # type: ignore[method-assign]
        blocking_timeout = 5.0 if len(posts) > 1 else None

        async with backend.acquire_fill_lock("k", timeout=30.0, blocking_timeout=blocking_timeout) as grant:
            assert grant == expected
        await _drain_background_releases()

        assert backend._request_sync.call_count == (1 if expected[0] else 0)

    async def test_release_is_sent_but_not_waited_on(self, backend: CachekitIOBackend) -> None:
        """The async with returns while the DELETE is still in flight; the DELETE then lands."""
        import threading

        in_flight, finish = threading.Event(), threading.Event()

        def delete(method: str, endpoint: str, **kwargs: Any) -> httpx.Response:
            in_flight.set()
            assert finish.wait(5.0)
            return _json_response(200, {})

        backend._request_async = AsyncMock(return_value=_json_response(200, {"lock_id": "lock-bg"}))  # type: ignore[method-assign]
        backend._request_sync = MagicMock(side_effect=delete)  # type: ignore[method-assign]

        async with backend.acquire_fill_lock("k", timeout=30.0, blocking_timeout=5.0):
            pass
        assert await asyncio.to_thread(in_flight.wait, 5.0)  # exited with the DELETE still unanswered
        finish.set()
        await _drain_background_releases()

        (call,) = backend._request_sync.call_args_list
        assert call.args == ("DELETE", "k/lock")
        assert call.kwargs["headers"] == {LOCK_ID_HEADER: "lock-bg"}

    async def test_release_survives_the_teardown_cancel_sweep(self, backend: CachekitIOBackend) -> None:
        """asyncio.run teardown cancels every Task, here before the drain Task has started. The DELETE was
        already submitted to the executor, so it still runs to the end (asyncio.run then waits for the
        executor; the cross-process test pins that part)."""
        import threading

        in_flight, finish, sent = threading.Event(), threading.Event(), threading.Event()
        done: list[str] = []

        def delete(method: str, endpoint: str, **kwargs: Any) -> httpx.Response:
            in_flight.set()
            assert finish.wait(5.0)
            done.append(endpoint)
            sent.set()
            return _json_response(200, {})

        backend._request_async = AsyncMock(return_value=_json_response(200, {"lock_id": "lock-sweep"}))  # type: ignore[method-assign]
        backend._request_sync = MagicMock(side_effect=delete)  # type: ignore[method-assign]

        async with backend.acquire_fill_lock("k", timeout=30.0, blocking_timeout=5.0):
            pass
        for task in asyncio.all_tasks() - {asyncio.current_task()}:
            task.cancel()  # the sweep
        assert await asyncio.to_thread(in_flight.wait, 5.0)
        finish.set()
        assert await asyncio.to_thread(sent.wait, 5.0)

        assert done == ["k/lock"]

    async def test_release_failure_is_logged_with_the_key_redacted(
        self, backend: CachekitIOBackend, caplog: pytest.LogCaptureFixture
    ) -> None:
        key = "ns:secret-tenant:func:m.f:args:ab:1s"
        backend._request_async = AsyncMock(return_value=_json_response(200, {"lock_id": "lock-f"}))  # type: ignore[method-assign]
        backend._request_sync = MagicMock(  # type: ignore[method-assign]
            side_effect=BackendError("release failed", error_type=BackendErrorType.TRANSIENT)
        )

        with caplog.at_level(logging.WARNING, logger="cachekit.backends.cachekitio.backend"):
            async with backend.acquire_fill_lock(key, timeout=30.0, blocking_timeout=None):
                pass
            await _drain_background_releases()

        (record,) = [r for r in caplog.records if "lock release" in r.getMessage()]
        assert redact_cache_key(key) in record.getMessage()
        assert "secret-tenant" not in record.getMessage()

    @pytest.mark.parametrize("sweep", [False, True], ids=["plain", "teardown-sweep"])
    async def test_unexpected_release_error_is_logged_redacted(
        self, backend: CachekitIOBackend, caplog: pytest.LogCaptureFixture, sweep: bool
    ) -> None:
        """An error that is not a BackendError, raised before the request (here by the lease lookup), is
        logged by the executor callable, redacted, with or without asyncio.run's cancel sweep; nothing is
        left on the future for asyncio to report unredacted."""
        key = "ns:secret-tenant:func:m.f:args:cd:1s"
        backend._request_async = AsyncMock(return_value=_json_response(200, {"lock_id": "lock-u"}))  # type: ignore[method-assign]
        backend._own_sync_lease = MagicMock(side_effect=RuntimeError("lease bug"))  # type: ignore[method-assign]
        loop = asyncio.get_running_loop()
        unhandled: list[dict[str, Any]] = []
        loop.set_exception_handler(lambda _loop, context: unhandled.append(context))

        with caplog.at_level(logging.WARNING, logger="cachekit.backends.cachekitio.backend"):
            async with backend.acquire_fill_lock(key, timeout=30.0, blocking_timeout=None):
                pass
            if sweep:
                for task in asyncio.all_tasks() - {asyncio.current_task()}:
                    task.cancel()
            await _drain_background_releases()

        (record,) = [r for r in caplog.records if "lock release" in r.getMessage()]
        assert redact_cache_key(key) in record.getMessage()
        assert "secret-tenant" not in record.getMessage()
        assert "RuntimeError" in record.getMessage()
        assert unhandled == []

    async def test_manual_loop_close_retains_nothing(self, backend: CachekitIOBackend) -> None:
        """run_until_complete then close(), without asyncio.run: the DELETE lands from its thread, and once
        it has, nothing still references the closed loop."""
        import gc
        import threading
        import weakref

        sent = threading.Event()

        def delete(method: str, endpoint: str, **kwargs: Any) -> httpx.Response:
            sent.set()
            return _json_response(200, {})

        backend._request_async = AsyncMock(return_value=_json_response(200, {"lock_id": "lock-m"}))  # type: ignore[method-assign]
        backend._request_sync = MagicMock(side_effect=delete)  # type: ignore[method-assign]

        async def miss() -> None:
            async with backend.acquire_fill_lock("k", timeout=30.0, blocking_timeout=None):
                pass

        def run_on_a_manual_loop() -> weakref.ref[asyncio.AbstractEventLoop]:
            loop = asyncio.new_event_loop()
            try:
                loop.run_until_complete(miss())
            finally:
                loop.close()  # no shutdown_default_executor: the executor thread is not joined
            return weakref.ref(loop)

        loop_ref = await asyncio.to_thread(run_on_a_manual_loop)
        assert await asyncio.to_thread(sent.wait, 5.0)
        for _ in range(50):  # the executor thread drops the work item just after the DELETE returns
            gc.collect()
            if loop_ref() is None:
                break
            await asyncio.sleep(0.01)
        assert loop_ref() is None, gc.get_referrers(loop_ref())
        backend._request_sync.assert_called_once()

    async def test_public_acquire_lock_still_releases_before_returning(self, backend: CachekitIOBackend) -> None:
        """The LockableBackend method is unchanged: its DELETE is awaited on the async client."""
        request_mock = AsyncMock(return_value=_json_response(200, {"lock_id": "lock-1"}))
        backend._request_async = request_mock  # type: ignore[method-assign]
        backend._request_sync = MagicMock()  # type: ignore[method-assign]

        async with backend.acquire_lock("k", timeout=30.0, blocking_timeout=5.0) as acquired:
            assert acquired is True

        assert _method_calls(request_mock) == ["POST", "DELETE"]
        backend._request_sync.assert_not_called()

    async def test_release_survives_the_sync_client_closing_first(self) -> None:
        """The release is still queued (one busy executor thread) when the caller closes the sync client,
        as close_sync_client() does to the client a backend's lease holds. The DELETE still lands."""
        import threading
        from concurrent.futures import ThreadPoolExecutor

        sent: list[tuple[str, str]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            sent.append((request.method, request.url.path))
            return httpx.Response(200)

        clients = [httpx.Client(base_url=_TEST_API_URL, transport=httpx.MockTransport(handler)) for _ in range(2)]
        leases = iter([MagicMock(pid=os.getpid(), client=client) for client in clients])
        loop = asyncio.get_running_loop()
        pool = ThreadPoolExecutor(max_workers=1)
        loop.set_default_executor(pool)
        with (
            patch("cachekit.backends.cachekitio.backend.lease_sync_http_client", side_effect=lambda _config: next(leases)),
            patch(
                "cachekit.backends.cachekitio.backend.lease_async_http_client",
                return_value=MagicMock(pid=os.getpid(), client=MagicMock(spec=httpx.AsyncClient)),
            ),
        ):
            backend = CachekitIOBackend(api_url=_TEST_API_URL, api_key=_TEST_API_KEY)
            backend._request_async = AsyncMock(return_value=_json_response(200, {"lock_id": "lock-c"}))  # type: ignore[method-assign]
            gate = threading.Event()
            busy = loop.run_in_executor(None, gate.wait, 5.0)

            async with backend.acquire_fill_lock("k", timeout=30.0, blocking_timeout=None):
                pass
            clients[0].close()
            gate.set()
            await busy
            await _drain_background_releases()

        clients[1].close()
        assert sent == [("DELETE", "/v1/cache/k/lock")]
