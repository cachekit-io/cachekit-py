"""One inline retry of a shed write (LAB-7686).

A PUT or DELETE answered 503 with ``Retry-After`` of at most 2 s is sent once more after
exactly that delay; every other failure is sent once. Requests go through the backend's real
client on a fake pool (tests/utils/cachekitio_fakes.py), so status handling runs as in
production; the sleeps are faked.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Iterator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from urllib3 import HTTPResponse
from urllib3.exceptions import ReadTimeoutError

from cachekit.backends.cachekitio.backend import CachekitIOBackend
from cachekit.backends.errors import BackendError, BackendErrorType
from tests.utils.cachekitio_fakes import FakePool, FakeRequest, fake_backend, response


class _Server:
    """Answers each request with the next queued response, or raises the next queued exception."""

    def __init__(self, *responses: HTTPResponse | Exception) -> None:
        self._responses = list(responses)

    def __call__(self, request: FakeRequest) -> HTTPResponse:
        answer = self._responses.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


def _shed(retry_after: str | None = "1") -> HTTPResponse:
    headers = {} if retry_after is None else {"Retry-After": retry_after}
    return response(503, headers=headers, json={"error": "request_deadline_exceeded"})


def _backend(*responses: HTTPResponse | Exception) -> tuple[CachekitIOBackend, FakePool]:
    return fake_backend(_Server(*responses))


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
    @pytest.mark.parametrize(
        ("retry_after", "delay"),
        [
            ("1", 1),
            ("2", 2),
            ("01", 1),  # zero padding is valid delta-seconds
            pytest.param("0" * 5000 + "1", 1, id="padded-past-int-digit-limit"),
        ],
    )
    def test_retry_after_short_hint_lands_the_write(
        self, sleeps: MagicMock, op: str, mode: str, retry_after: str, delay: int
    ) -> None:
        backend, pool = _backend(_shed(retry_after), response(200))
        _write(backend, op, mode)

        assert len(pool.requests) == 2
        sleeps.assert_called_once_with(delay)
        first, second = pool.requests
        assert second.method == first.method == ("PUT" if op == "set" else "DELETE")
        assert second.path == first.path
        assert second.body == first.body

    @pytest.mark.parametrize(("op", "mode"), _WRITES)
    def test_second_503_is_not_retried_again(self, sleeps: MagicMock, op: str, mode: str) -> None:
        backend, pool = _backend(_shed("1"), _shed("1"))
        with pytest.raises(BackendError) as exc_info:
            _write(backend, op, mode)

        assert exc_info.value.error_type == BackendErrorType.TRANSIENT
        assert len(pool.requests) == 2
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
            pytest.param("9" * 5000, id="past-int-digit-limit"),  # no retry, and still a TRANSIENT 503
        ],
    )
    def test_503_without_a_short_hint(self, sleeps: MagicMock, op: str, mode: str, retry_after: str | None) -> None:
        backend, pool = _backend(_shed(retry_after))
        with pytest.raises(BackendError) as exc_info:
            _write(backend, op, mode)

        assert exc_info.value.error_type == BackendErrorType.TRANSIENT
        assert len(pool.requests) == 1
        sleeps.assert_not_called()

    @pytest.mark.parametrize(("op", "mode"), _WRITES)
    @pytest.mark.parametrize("status", [400, 401, 403, 404, 413, 429, 500, 502, 504])
    def test_other_status_even_with_retry_after(self, sleeps: MagicMock, op: str, mode: str, status: int) -> None:
        backend, pool = _backend(response(status, headers={"Retry-After": "1"}))
        with pytest.raises(BackendError):
            _write(backend, op, mode)

        assert len(pool.requests) == 1
        sleeps.assert_not_called()

    @pytest.mark.parametrize(("op", "mode"), _WRITES)
    def test_timeout(self, sleeps: MagicMock, op: str, mode: str) -> None:
        backend, pool = _backend(ReadTimeoutError(None, "/v1/cache/k", "timed out"))  # type: ignore[arg-type]
        with pytest.raises(BackendError) as exc_info:
            _write(backend, op, mode)

        assert exc_info.value.error_type == BackendErrorType.TIMEOUT
        assert len(pool.requests) == 1
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
        backend, pool = _backend(_shed("1"))
        with pytest.raises(BackendError):
            call(backend)

        assert len(pool.requests) == 1
        sleeps.assert_not_called()

    def test_lock_acquire_post(self, sleeps: MagicMock) -> None:
        backend, pool = _backend(_shed("1"))
        with pytest.raises(BackendError):
            asyncio.run(backend._try_acquire_lock("k", 1.0))

        assert [r.method for r in pool.requests] == ["POST"]
        sleeps.assert_not_called()

    def test_ttl_patch(self, sleeps: MagicMock) -> None:
        backend, pool = _backend(_shed("1"))
        assert asyncio.run(backend.refresh_ttl("k", 60)) is False

        assert [r.method for r in pool.requests] == ["PATCH"]
        sleeps.assert_not_called()
