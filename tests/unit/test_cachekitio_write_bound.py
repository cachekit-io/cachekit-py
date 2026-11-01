"""The writer's own L1 copy over CachekitIO (protocol spec/saas-api.md#remaining-freshness).

API-103: once the store accepts a PUT, the decorator must not serve its L1 copy of the value after
min(local_ttl, effective_ttl, fresh_for), where fresh_for is the PUT 2xx's X-CacheKit-Fresh-For read with
the GET grammar: absent = no bound (today's behaviour), malformed or 0 = no L1 copy at all. The server
sends a Fresh-For below the stated TTL when it clamped the TTL to the plan's maximum.

API-104: a PATCH /ttl response never creates, refreshes or extends an L1 bound.

Everything runs through the backend's real client on a fake pool (tests/utils/cachekitio_fakes.py), so the
header is parsed exactly as in production. L1 time is a fake clock: ``cachekit.l1_cache.time`` is swapped
for a shim whose ``time()`` the test advances.
"""

from __future__ import annotations

import asyncio
import threading
import time
import types
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import unquote

import pytest
from urllib3 import HTTPResponse

from cachekit import cache
from cachekit import l1_cache as l1_module
from cachekit.backends.cachekitio.backend import FRESH_FOR_HEADER, FRESHNESS_HEADER, CachekitIOBackend
from cachekit.cache_handler import StandardCacheHandler
from cachekit.decorators import write_behind as wb
from tests.utils.cachekitio_fakes import FakeRequest, fake_backend, response

pytestmark = pytest.mark.unit

_PREFIX = "/v1/cache/"
_GUARD_SECONDS = 10.0  # hang guard only: hitting it fails the test, it never decides an outcome


@dataclass
class _FakeSaaS:
    """Stores PUT bodies; answers each PUT 2xx with ``put_fresh_for`` (None omits the header)."""

    put_fresh_for: str | None = None
    fail_writes: bool = False
    get_fresh_for: str | None = None
    stale: bool = False  # label every hit stale, as in the entry's stale window
    store: dict[str, bytes] = field(default_factory=dict)
    ops: list[str] = field(default_factory=list)
    patched: threading.Event = field(default_factory=threading.Event)

    def respond(self, request: FakeRequest) -> HTTPResponse:
        encoded, _, suffix = request.path[len(_PREFIX) :].partition("/")
        key = unquote(encoded)
        op = f"{request.method} /{suffix}" if suffix else request.method
        self.ops.append(op)
        if op == "PUT" and self.fail_writes:
            return response(500)
        if op == "PUT":
            assert request.body is not None
            self.store[key] = request.body
            return response(200, headers=None if self.put_fresh_for is None else {FRESH_FOR_HEADER: self.put_fresh_for})
        if op == "GET":
            if key not in self.store:
                return response(404)
            if self.stale:
                return response(200, self.store[key], headers={FRESHNESS_HEADER: "stale", FRESH_FOR_HEADER: "0"})
            headers = None if self.get_fresh_for is None else {FRESH_FOR_HEADER: self.get_fresh_for}
            return response(200, self.store[key], headers=headers)
        if op == "GET /ttl":
            return response(200, json={"ttl": 1})  # under any refresh threshold: a refresh is due
        if op == "POST /lock":
            return response(200, json={"lock_id": "lock-1"})
        if op == "PATCH /ttl":
            self.patched.set()
            return response(200, json={"success": True})
        return response(200)

    def gets(self) -> int:
        return self.ops.count("GET")


class _Clock:
    """The L1 clock: ``now`` is what ``cachekit.l1_cache`` reads as ``time.time()``."""

    def __init__(self) -> None:
        self.now = 1_000_000.0

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> _Clock:
    clock = _Clock()
    shim = types.SimpleNamespace(time=lambda: clock.now, sleep=time.sleep, monotonic=time.monotonic)
    monkeypatch.setattr(l1_module, "time", shim)
    return clock


@pytest.fixture
def saas() -> _FakeSaaS:
    return _FakeSaaS()


@pytest.fixture
def backend(saas: _FakeSaaS) -> CachekitIOBackend:
    backend, _ = fake_backend(saas.respond)
    return backend


_namespaces = iter(range(1_000_000))


def _namespace() -> str:
    """A namespace no other test shares: L1 caches are per-namespace and process-wide."""
    return f"write-bound-{next(_namespaces)}-{time.monotonic_ns()}"


class TestBackendReportsPutFreshFor:
    """``_set_with_freshness`` reads the PUT 2xx's header with the GET grammar (API-32/33)."""

    @pytest.mark.parametrize(
        ("header", "expected"),
        [
            (None, None),  # absent: the stated TTL was applied
            ("5", 5),
            ("0", 0),
            ("2592000", 2592000),  # the header's own 30-day cap, below the 90-day TTL range
            ("2592001", 0),  # over the cap: malformed
            ("7776000", 0),  # the TTL range maximum is not a valid Fresh-For
            ("+5", 0),
            ("", 0),
            ("00000005", 0),  # eight digits: the length check runs first
        ],
    )
    def test_header_grammar(self, saas: _FakeSaaS, backend: CachekitIOBackend, header: str | None, expected: int | None) -> None:
        saas.put_fresh_for = header
        assert backend._set_with_freshness("k", b"v", ttl=60) == expected
        assert saas.ops == ["PUT"]

    def test_set_keeps_its_none_return(self, saas: _FakeSaaS, backend: CachekitIOBackend) -> None:
        saas.put_fresh_for = "5"
        assert backend.set("k", b"v", ttl=60) is None  # type: ignore[func-returns-value]


class TestHandlerSetWithFreshness:
    def test_cachekitio_write_reports_fresh_for(self, saas: _FakeSaaS, backend: CachekitIOBackend) -> None:
        saas.put_fresh_for = "5"
        handler = StandardCacheHandler(backend)
        assert handler.set_with_freshness("k", b"v", ttl=60) == (True, 5)
        assert handler.set("k", b"v", ttl=60) is True

    async def test_async_variant(self, saas: _FakeSaaS, backend: CachekitIOBackend) -> None:
        saas.put_fresh_for = "7"
        assert await StandardCacheHandler(backend).set_with_freshness_async("k", b"v", ttl=60) == (True, 7)

    def test_failed_write_reports_no_bound(self, saas: _FakeSaaS, backend: CachekitIOBackend) -> None:
        saas.fail_writes = True
        assert StandardCacheHandler(backend).set_with_freshness("k", b"v", ttl=60) == (False, None)

    def test_other_backends_report_none(self) -> None:
        written: list[tuple[Any, ...]] = []

        class _Plain:
            def set(self, key: str, value: bytes, ttl: int | None = None) -> None:
                written.append((key, value, ttl))

        assert StandardCacheHandler(_Plain()).set_with_freshness("k", b"v", ttl=60) == (True, None)  # type: ignore[arg-type]
        assert written == [("k", b"v", 60)]

    def test_set_override_still_runs(self, saas: _FakeSaaS) -> None:
        """A subclass or patch of ``set`` must keep running on every write: the fresh_for fast path then
        steps aside, which is the absent-header path (no bound beyond the stated TTL)."""
        seen: list[str] = []

        class _Recording(CachekitIOBackend):
            def set(self, key: str, value: bytes, ttl: int | None = None, stale_ttl: int | None = None) -> None:
                seen.append(key)
                super().set(key, value, ttl, stale_ttl)

        backend, _ = fake_backend(saas.respond)
        backend.__class__ = _Recording
        saas.put_fresh_for = "5"
        assert StandardCacheHandler(backend).set_with_freshness("k", b"v", ttl=60) == (True, None)
        assert seen == ["k"]

    def test_patched_class_set_still_runs(
        self, saas: _FakeSaaS, backend: CachekitIOBackend, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: list[str] = []
        original = CachekitIOBackend.set

        def recording_set(
            self: CachekitIOBackend, key: str, value: bytes, ttl: int | None = None, stale_ttl: int | None = None
        ) -> None:
            seen.append(key)
            original(self, key, value, ttl, stale_ttl)

        monkeypatch.setattr(CachekitIOBackend, "set", recording_set)
        saas.put_fresh_for = "5"
        assert StandardCacheHandler(backend).set_with_freshness("k", b"v", ttl=60) == (True, None)
        assert seen == ["k"]


def _decorated(
    backend: CachekitIOBackend, *, is_async: bool, ttl: int | None = 60, **kwargs: Any
) -> tuple[Callable[[], Any], list[int]]:
    """A decorated function counting its executions, run synchronously either way."""
    calls: list[int] = []
    namespace = _namespace()
    if is_async:

        @cache(backend=backend, ttl=ttl, namespace=namespace, **kwargs)
        async def compute_async() -> int:
            calls.append(1)
            return len(calls)

        return (lambda: asyncio.run(compute_async())), calls

    @cache(backend=backend, ttl=ttl, namespace=namespace, **kwargs)
    def compute() -> int:
        calls.append(1)
        return len(calls)

    return compute, calls


@pytest.mark.parametrize("is_async", [False, True], ids=["sync", "async"])
class TestWriterCopyBound:
    """API-103 on the miss path: the L1 copy of a stored value lives min(local_ttl, effective_ttl, fresh_for).

    Each test counts the entry GETs after the miss: the async miss also re-reads under its fill lock.
    """

    def test_header_below_ttl_bounds_the_copy(
        self, saas: _FakeSaaS, backend: CachekitIOBackend, clock: _Clock, is_async: bool
    ) -> None:
        saas.put_fresh_for = "5"  # the plan maximum clamped ttl=60 down to 5 s
        compute, calls = _decorated(backend, is_async=is_async)
        assert compute() == 1  # miss: PUT, then the bounded L1 copy
        gets = saas.gets()
        clock.advance(3.9)  # L1 drops an entry ttl_buffer_seconds (1 s) early: it lives until +4
        assert compute() == 1
        assert saas.gets() == gets  # served from L1
        clock.advance(1.2)  # +5.1: past fresh_for, the copy is gone
        assert compute() == 1  # from L2, not recomputed
        assert saas.gets() == gets + 1
        assert len(calls) == 1

    def test_absent_header_keeps_the_stated_ttl(
        self, saas: _FakeSaaS, backend: CachekitIOBackend, clock: _Clock, is_async: bool
    ) -> None:
        compute, calls = _decorated(backend, is_async=is_async)
        assert compute() == 1
        gets = saas.gets()
        clock.advance(58.5)  # still inside min(local_ttl, effective_ttl) = 60, less the 1 s buffer
        assert compute() == 1
        assert saas.gets() == gets
        clock.advance(1.0)  # +59.5: past it
        assert compute() == 1
        assert saas.gets() == gets + 1
        assert len(calls) == 1

    def test_header_above_ttl_never_extends(
        self, saas: _FakeSaaS, backend: CachekitIOBackend, clock: _Clock, is_async: bool
    ) -> None:
        saas.put_fresh_for = "2592000"
        compute, _ = _decorated(backend, is_async=is_async)
        assert compute() == 1
        gets = saas.gets()
        clock.advance(59.5)
        assert compute() == 1
        assert saas.gets() == gets + 1  # the 60 s copy expired: Fresh-For never lengthens the bound

    def test_no_ttl_is_bounded_by_the_header(
        self, saas: _FakeSaaS, backend: CachekitIOBackend, clock: _Clock, is_async: bool
    ) -> None:
        """No TTL: the server stores the plan maximum and says so; L1 keeps min(default 300 s, fresh_for)."""
        saas.put_fresh_for = "10"
        compute, _ = _decorated(backend, is_async=is_async, ttl=None)
        assert compute() == 1
        gets = saas.gets()
        clock.advance(8.5)
        assert compute() == 1
        assert saas.gets() == gets
        clock.advance(1.0)
        assert compute() == 1
        assert saas.gets() == gets + 1

    @pytest.mark.parametrize("header", ["0", "garbage", "+5", "3000000"])
    def test_malformed_or_zero_keeps_no_copy(
        self, saas: _FakeSaaS, backend: CachekitIOBackend, clock: _Clock, is_async: bool, header: str
    ) -> None:
        saas.put_fresh_for = header
        compute, calls = _decorated(backend, is_async=is_async)
        assert compute() == 1
        gets = saas.gets()
        assert compute() == 1  # no clock movement: an L1 copy would have served this
        assert saas.gets() == gets + 1
        assert len(calls) == 1


class TestWriteBehindCopyBound:
    """A write-behind miss puts its L1 copy before the PUT (API-36/37). Once the 2xx lands, a Fresh-For that
    would end the copy sooner drops it; a value the copy already honours keeps it."""

    @pytest.fixture(autouse=True)
    def _writers(self, monkeypatch: pytest.MonkeyPatch) -> Iterator[list[wb.WriteBehind]]:
        created: list[wb.WriteBehind] = []
        init = wb.WriteBehind.__init__

        def recording_init(self: wb.WriteBehind, *args: Any, **kwargs: Any) -> None:
            init(self, *args, **kwargs)
            created.append(self)

        monkeypatch.setattr(wb.WriteBehind, "__init__", recording_init)
        self.created = created
        yield created

    def _drain(self) -> None:
        for writer in self.created:
            assert writer.close(time.monotonic() + _GUARD_SECONDS) == [], "a deferred write never finished"

    @pytest.mark.parametrize("is_async", [False, True], ids=["sync", "async"])
    @pytest.mark.parametrize("lands_first", [False, True], ids=["put-first", "write-lands-first"])
    @pytest.mark.parametrize(
        ("header", "copy_kept"),
        [
            (None, True),
            ("59", True),  # floor(fresh_until - now) of an unclamped ttl=60: the 1 s buffer already covers it
            ("5", False),  # clamped by the plan maximum
            ("0", False),
            ("garbage", False),
        ],
    )
    def test_landed_write_bounds_the_copy(
        self,
        saas: _FakeSaaS,
        backend: CachekitIOBackend,
        clock: _Clock,
        monkeypatch: pytest.MonkeyPatch,
        header: str | None,
        copy_kept: bool,
        lands_first: bool,
        is_async: bool,
    ) -> None:
        """``lands_first`` holds the miss's L1 put until its write has landed: the writer then finds no copy to
        drop, and the put itself must see the landed bound."""
        if lands_first:
            original_put = l1_module.L1Cache.put

            def put_after_landing(self_: l1_module.L1Cache, key: str, value: bytes, **kwargs: Any) -> None:
                self._drain()
                original_put(self_, key, value, **kwargs)

            monkeypatch.setattr(l1_module.L1Cache, "put", put_after_landing)
        saas.put_fresh_for = header
        # The async locked miss path never defers (its write must land inside the lock).
        compute, _ = _decorated(backend, is_async=is_async, write_behind=True, distributed_lock=False)
        assert compute() == 1
        self._drain()
        assert saas.ops == ["GET", "PUT"]
        assert compute() == 1
        assert saas.gets() == (1 if copy_kept else 2)


class TestPatchTtlNeverExtendsL1:
    """API-104: refresh_ttl_on_get's background PATCH /ttl leaves every L1 bound as it was."""

    async def test_refresh_leaves_the_backfill_bound(self, saas: _FakeSaaS, backend: CachekitIOBackend, clock: _Clock) -> None:
        namespace = _namespace()

        @cache(backend=backend, ttl=60, namespace=namespace, refresh_ttl_on_get=True)
        async def compute() -> int:
            return 1

        assert await compute() == 1  # miss + PUT
        l1 = l1_module.get_l1_cache(namespace)
        l1.invalidate_many(list(l1._state.cache))  # drop L1 only: the next read is an L2 hit
        saas.get_fresh_for = "3"  # 3 s of freshness left: the backfill copy lives until +2 (1 s buffer)
        assert await compute() == 1  # L2 hit -> bounded backfill, and a refresh is due
        assert await asyncio.to_thread(saas.patched.wait, _GUARD_SECONDS), "no PATCH /ttl was sent"
        await asyncio.sleep(0)  # let the refresh task finish after its response
        clock.advance(2.5)  # past the backfill bound; a PATCH-derived 60 s bound would still serve L1
        gets = saas.gets()
        assert await compute() == 1
        assert saas.gets() == gets + 1


def _wait_for(predicate: Callable[[], bool]) -> bool:
    deadline = time.monotonic() + _GUARD_SECONDS
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


@pytest.mark.parametrize("is_async", [False, True], ids=["sync", "async"])
class TestRevalidationCopyBound:
    """API-103 on the SWR revalidation write: its L1 refresh is bounded by the revalidation PUT's 2xx."""

    @pytest.mark.parametrize(("header", "l1_ttl"), [(None, 60), ("5", 5), ("0", 0)])
    def test_revalidated_copy_is_bounded(
        self,
        saas: _FakeSaaS,
        backend: CachekitIOBackend,
        clock: _Clock,
        monkeypatch: pytest.MonkeyPatch,
        is_async: bool,
        header: str | None,
        l1_ttl: int,
    ) -> None:
        namespace = _namespace()
        calls: list[int] = []
        puts: list[Any] = []  # redis_ttl of each L1 put
        put_seen = threading.Event()
        original_put = l1_module.L1Cache.put

        def spy(self: l1_module.L1Cache, key: str, value: bytes, redis_ttl: Any = None, expires_at: Any = None) -> None:
            original_put(self, key, value, redis_ttl=redis_ttl, expires_at=expires_at)
            puts.append(redis_ttl)
            put_seen.set()

        if is_async:

            @cache(backend=backend, ttl=60, stale_ttl=60, namespace=namespace)
            async def compute_async() -> int:
                calls.append(1)
                return len(calls)

            def compute() -> Any:
                async def call_and_settle() -> int:
                    result = await compute_async()
                    if saas.stale:  # a revalidation runs on this loop: hold it open until its L1 put
                        await asyncio.to_thread(put_seen.wait, _GUARD_SECONDS)
                    return result

                return asyncio.run(call_and_settle())
        else:

            @cache(backend=backend, ttl=60, stale_ttl=60, namespace=namespace)
            def compute() -> int:
                calls.append(1)
                return len(calls)

        assert compute() == 1  # miss + PUT (no header: a 60 s copy)
        l1 = l1_module.get_l1_cache(namespace)
        l1.invalidate_many(list(l1._state.cache))  # the next read reaches L2, which labels it stale
        saas.stale = True
        saas.put_fresh_for = header
        monkeypatch.setattr(l1_module.L1Cache, "put", spy)
        assert compute() == 1  # stale served; the revalidation recomputes and writes in the background
        assert put_seen.wait(_GUARD_SECONDS), "the revalidation never refreshed L1"
        assert len(calls) == 2
        assert saas.ops.count("PUT") == 2
        assert puts == [l1_ttl]  # min(local_ttl, effective_ttl, fresh_for)
        assert len(l1._state.cache) == (1 if l1_ttl else 0)  # 0: no copy kept


class TestL1DeclinedPutDropsOlderEntry:
    """A put L1 declines because the value would already be expired (a 0 Fresh-For) drops the older value
    under the key, as an oversized put does: the newer write must not leave the previous one serving."""

    def test_zero_ttl_put_drops_the_older_entry(self, clock: _Clock) -> None:
        l1 = l1_module.L1Cache(namespace=_namespace())
        l1.put("k", b"old", redis_ttl=60)
        assert l1.get("k") == (True, b"old")
        l1.put("k", b"new", redis_ttl=0)
        assert l1.get("k") == (False, None)
