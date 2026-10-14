"""An exact-key invalidation reports what it did in L2 (InvalidationOutcome).

invalidate_cache(*args) and ainvalidate_cache(*args) still never raise on a backend failure; the
return value is the caller's only per-call signal that the entry may still be served from L2.
Both real L2 backends run their own error handling: CachekitIO on the fake pool
(tests/utils/cachekitio_fakes.py), tenant-scoped Redis on fakeredis.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

import pytest
import redis
from fakeredis import FakeRedis
from urllib3 import HTTPResponse

from cachekit import InvalidationOutcome, cache
from cachekit.backends.redis.provider import PerRequestRedisBackend
from tests.utils.cachekitio_fakes import FakeRequest, fake_backend, response

pytestmark = pytest.mark.unit

_FAILED_LOG = "Failed to delete L2 key"


def _cachekitio(delete_status: int) -> Any:
    """A CachekitIO backend whose server stores every PUT and answers every DELETE with delete_status."""
    store: dict[str, bytes] = {}

    def handler(request: FakeRequest) -> HTTPResponse:
        if request.method == "PUT":
            store[request.path] = request.body or b""
            return response(200, json={"success": True})
        if request.method == "DELETE":
            # No Retry-After: a 503 is not sent a second time.
            return response(delete_status, json={"success": delete_status == 200})
        return response(200, body=store[request.path]) if request.path in store else response(404)

    backend, _ = fake_backend(handler)
    return backend


def _redis(delete_fails: bool) -> PerRequestRedisBackend:
    client = FakeRedis()
    if delete_fails:

        def refuse(*_keys: Any) -> int:
            raise redis.ConnectionError("connection refused")

        client.delete = refuse  # type: ignore[method-assign]
    return PerRequestRedisBackend(client, tenant_id="t1")


BACKENDS: list[Any] = [
    pytest.param(lambda: _cachekitio(200), InvalidationOutcome.SUCCEEDED, id="cachekitio-200"),
    pytest.param(lambda: _cachekitio(503), InvalidationOutcome.FAILED, id="cachekitio-503"),
    pytest.param(lambda: _redis(delete_fails=False), InvalidationOutcome.SUCCEEDED, id="redis-ok"),
    pytest.param(lambda: _redis(delete_fails=True), InvalidationOutcome.FAILED, id="redis-error"),
]


@pytest.mark.parametrize(("make_backend", "expected"), BACKENDS)
class TestL2Backends:
    def test_sync(
        self, make_backend: Callable[[], Any], expected: InvalidationOutcome, caplog: pytest.LogCaptureFixture
    ) -> None:
        @cache(backend=make_backend(), ttl=60, namespace="outcome-sync")
        def fn(x: int) -> int:
            return x

        fn(1)
        with caplog.at_level(logging.ERROR):
            outcome = fn.invalidate_cache(1)

        assert outcome is expected
        # The log line callers relied on before the return value existed is unchanged.
        assert (_FAILED_LOG in caplog.text) is (expected is InvalidationOutcome.FAILED)

    async def test_async(
        self, make_backend: Callable[[], Any], expected: InvalidationOutcome, caplog: pytest.LogCaptureFixture
    ) -> None:
        @cache(backend=make_backend(), ttl=60, namespace="outcome-async")
        async def fn(x: int) -> int:
            return x

        await fn(1)
        with caplog.at_level(logging.ERROR):
            outcome = await fn.ainvalidate_cache(1)

        assert outcome is expected
        assert (_FAILED_LOG in caplog.text) is (expected is InvalidationOutcome.FAILED)
        # An async function's invalidate_cache is ainvalidate_cache.
        assert await fn.invalidate_cache(1) is expected


class TestNoL2:
    def test_l1_only(self) -> None:
        @cache(backend=None)
        def fn(x: int) -> int:
            return x

        fn(1)
        assert fn.invalidate_cache(1) is InvalidationOutcome.NO_L2

    async def test_l1_only_async_function(self) -> None:
        @cache(backend=None)
        async def fn(x: int) -> int:
            return x

        await fn(1)
        assert await fn.ainvalidate_cache(1) is InvalidationOutcome.NO_L2

    async def test_local(self) -> None:
        @cache.local()
        def fn(x: int) -> object:
            return object()

        fn(1)
        assert fn.invalidate_cache(1) is InvalidationOutcome.NO_L2
        assert await fn.ainvalidate_cache(1) is InvalidationOutcome.NO_L2

    async def test_local_async_function(self) -> None:
        @cache.local()
        async def fn(x: int) -> object:
            return object()

        await fn(1)
        assert await fn.ainvalidate_cache(1) is InvalidationOutcome.NO_L2


class TestUnresolvedBackend:
    """A wrapper with no backend= whose backend cannot be resolved has an L2 it could not reach."""

    @pytest.fixture(autouse=True)
    def _no_backend(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def unresolvable() -> Any:
            raise RuntimeError("no backend configured")

        monkeypatch.setattr("cachekit.decorators.wrapper._resolve_lazy_backend", unresolvable)

    def test_sync(self) -> None:
        @cache(ttl=60, namespace="outcome-unresolved")
        def fn(x: int) -> int:
            return x

        assert fn.invalidate_cache(1) is InvalidationOutcome.FAILED

    async def test_async(self) -> None:
        @cache(ttl=60, namespace="outcome-unresolved-async")
        async def fn(x: int) -> int:
            return x

        assert await fn.ainvalidate_cache(1) is InvalidationOutcome.FAILED


class TestWholeFunction:
    """No arguments on a function that takes parameters deletes every entry and reports nothing."""

    def test_sync(self) -> None:
        @cache(backend=_redis(delete_fails=False), ttl=60, namespace="outcome-all")
        def fn(x: int) -> int:
            return x

        fn(1)
        assert fn.invalidate_cache() is None

    async def test_async(self) -> None:
        @cache(backend=_redis(delete_fails=False), ttl=60, namespace="outcome-all-async")
        async def fn(x: int) -> int:
            return x

        await fn(1)
        assert await fn.ainvalidate_cache() is None

    async def test_zero_parameter_function_reports(self) -> None:
        """A zero-parameter function has one entry, so its no-args invalidation is exact-key."""

        @cache(backend=_redis(delete_fails=False), ttl=60, namespace="outcome-zero")
        def fn() -> int:
            return 1

        @cache(backend=_redis(delete_fails=False), ttl=60, namespace="outcome-zero-async")
        async def afn() -> int:
            return 1

        fn()
        await afn()
        assert fn.invalidate_cache() is InvalidationOutcome.SUCCEEDED
        assert await afn.ainvalidate_cache() is InvalidationOutcome.SUCCEEDED
