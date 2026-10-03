"""One inline retry of a shed write (LAB-7686).

A PUT or DELETE answered 503 with ``Retry-After`` of at most 2 s is sent once more after
exactly that delay; every other failure is sent once. Requests go through a real httpx
client on a MockTransport, so status handling runs as in production; the sleeps are faked.
"""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import Callable, Iterator
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from cachekit.backends.cachekitio.backend import CachekitIOBackend
from cachekit.backends.errors import BackendError, BackendErrorType

_TEST_API_URL = "https://api.cachekit.io"
_TEST_API_KEY = "ck_test_abc123"  # pragma: allowlist secret — fake key, test fixture


class _Server:
    """Answers each request with the next queued response and records what it was sent."""

    def __init__(self, *responses: httpx.Response | Exception) -> None:
        self._responses = list(responses)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        answer = self._responses.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


def _shed(retry_after: str | None = "1") -> httpx.Response:
    headers = {} if retry_after is None else {"Retry-After": retry_after.encode()}  # bytes: httpx would ascii-encode a str
    return httpx.Response(503, headers=headers, json={"error": "request_deadline_exceeded"})


def _backend(server: _Server) -> CachekitIOBackend:
    transport = httpx.MockTransport(server)
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
        return CachekitIOBackend(api_url=_TEST_API_URL, api_key=_TEST_API_KEY)


@pytest.fixture
def sleeps() -> Iterator[MagicMock]:
    """Fake clock: records every retry wait, sync and async, and waits for none of them."""
    recorder = MagicMock()
    with (
        patch.object(time, "sleep", recorder),
        patch.object(asyncio, "sleep", AsyncMock(side_effect=lambda delay: recorder(delay))),
    ):
        yield recorder


def _write(backend: CachekitIOBackend, op: str, mode: str) -> None:
    """Run one set or delete, sync or async."""
    if mode == "sync":
        backend.set("k", b"v", ttl=60) if op == "set" else backend.delete("k")
    else:
        asyncio.run(backend.set_async("k", b"v", ttl=60) if op == "set" else backend.delete_async("k"))


_WRITES = [(op, mode) for op in ("set", "delete") for mode in ("sync", "async")]


class TestShedWriteIsRetriedOnce:
    @pytest.mark.parametrize(("op", "mode"), _WRITES)
    @pytest.mark.parametrize("retry_after", ["1", "2"])
    def test_retry_after_short_hint_lands_the_write(self, sleeps: MagicMock, op: str, mode: str, retry_after: str) -> None:
        server = _Server(_shed(retry_after), httpx.Response(200))
        _write(_backend(server), op, mode)

        assert len(server.requests) == 2
        sleeps.assert_called_once_with(int(retry_after))
        first, second = server.requests
        assert second.method == first.method == ("PUT" if op == "set" else "DELETE")
        assert second.url == first.url
        assert second.content == first.content

    @pytest.mark.parametrize(("op", "mode"), _WRITES)
    def test_second_503_is_not_retried_again(self, sleeps: MagicMock, op: str, mode: str) -> None:
        server = _Server(_shed("1"), _shed("1"))
        with pytest.raises(BackendError) as exc_info:
            _write(_backend(server), op, mode)

        assert exc_info.value.error_type == BackendErrorType.TRANSIENT
        assert len(server.requests) == 2
        sleeps.assert_called_once_with(1)


class TestNoRetry:
    @pytest.mark.parametrize(("op", "mode"), _WRITES)
    @pytest.mark.parametrize(
        "retry_after",
        [
            None,  # missing
            "",
            "soon",  # unparseable
            "1.5",  # fraction: not delta-seconds
            "-1",
            "Fri, 03 Oct 2026 01:59:32 GMT",  # HTTP-date form is not read
            "²",  # superscript two: str.isdigit() accepts it, int() does not
            "3",  # above the 2 s cap
            "10",  # the rate-limiter fault's hint
        ],
    )
    def test_503_without_a_short_hint(self, sleeps: MagicMock, op: str, mode: str, retry_after: str | None) -> None:
        server = _Server(_shed(retry_after))
        with pytest.raises(BackendError) as exc_info:
            _write(_backend(server), op, mode)

        assert exc_info.value.error_type == BackendErrorType.TRANSIENT
        assert len(server.requests) == 1
        sleeps.assert_not_called()

    @pytest.mark.parametrize(("op", "mode"), _WRITES)
    @pytest.mark.parametrize("status", [400, 401, 403, 404, 413, 429, 500, 502, 504])
    def test_other_status_even_with_retry_after(self, sleeps: MagicMock, op: str, mode: str, status: int) -> None:
        server = _Server(httpx.Response(status, headers={"Retry-After": "1"}))
        with pytest.raises(BackendError):
            _write(_backend(server), op, mode)

        assert len(server.requests) == 1
        sleeps.assert_not_called()

    @pytest.mark.parametrize(("op", "mode"), _WRITES)
    def test_timeout(self, sleeps: MagicMock, op: str, mode: str) -> None:
        server = _Server(httpx.ReadTimeout("timed out"))
        with pytest.raises(BackendError) as exc_info:
            _write(_backend(server), op, mode)

        assert exc_info.value.error_type == BackendErrorType.TIMEOUT
        assert len(server.requests) == 1
        sleeps.assert_not_called()

    @pytest.mark.parametrize(
        ("name", "call"),
        [
            ("get", lambda b: b.get("k")),
            ("exists", lambda b: b.exists("k")),
            ("get_async", lambda b: asyncio.run(b.get_async("k"))),
        ],
    )
    def test_reads(self, sleeps: MagicMock, name: str, call: Callable[[CachekitIOBackend], object]) -> None:
        server = _Server(_shed("1"))
        with pytest.raises(BackendError):
            call(_backend(server))

        assert len(server.requests) == 1
        sleeps.assert_not_called()

    def test_lock_acquire_post(self, sleeps: MagicMock) -> None:
        server = _Server(_shed("1"))
        with pytest.raises(BackendError):
            asyncio.run(_backend(server)._try_acquire_lock("k", 1.0))

        assert [r.method for r in server.requests] == ["POST"]
        sleeps.assert_not_called()

    def test_ttl_patch(self, sleeps: MagicMock) -> None:
        server = _Server(_shed("1"))
        assert asyncio.run(_backend(server).refresh_ttl("k", 60)) is False

        assert [r.method for r in server.requests] == ["PATCH"]
        sleeps.assert_not_called()
