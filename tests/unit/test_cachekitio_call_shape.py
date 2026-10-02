"""Call shape of each decorated path over CachekitIO: which HTTP requests block the caller (LAB-7054).

Each test runs one decorated call against a fake SaaS behind a real ``httpx`` client and pins two
things: the ordered list of requests the caller waits on, and the set of requests that run in the
background after it returns. A path that gains a blocking round trip, or a background request that
becomes blocking, changes the list and fails here. A round-trip fix edits the pinned list in its own
PR, and that diff is its proof.

Blocking is decided by state, never by a clock. Async: every request is parked on a gate. Once the
event loop has run everything it can, a parked request blocks if the caller is still pending, and is
background if the caller has returned. Sync: a request blocks if it runs on the caller's thread, where
the caller cannot return until it does; a request on any other thread is held until the caller has
returned, so it can only be background.

Only single-caller paths are pinned exactly. Herd paths (several callers on one key) vary in lock-poll
count and order from run to run, so they belong to invariant tests, not here.
"""

from __future__ import annotations

import asyncio
import contextvars
import json
import threading
from collections import Counter
from collections.abc import Awaitable, Callable, Iterator
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import MagicMock, patch
from urllib.parse import unquote

import httpx
import pytest

from cachekit import cache
from cachekit.backends.cachekitio.backend import FRESH_FOR_HEADER, FRESHNESS_HEADER, CachekitIOBackend

pytestmark = pytest.mark.unit

_API_URL = "https://api.cachekit.io"
_API_KEY = "ck_test_call_shape"  # pragma: allowlist secret — fake key, test fixture
_PREFIX = "/v1/cache/"
# Loop turns a parked request is given to show it is not awaited. The paths below need a few turns per
# task hop; the bound only has to exceed that, and it counts turns, not time. If it were ever too small,
# a background request would read as blocking: the pinned list fails, it does not pass by mistake.
_SETTLE_TURNS = 100
_GUARD_SECONDS = 10.0  # hang guard only: hitting it fails the test, it never classifies a request


@dataclass
class _FakeSaaS:
    """In-memory stand-in for the SaaS cache API, answering the way spec/saas-api.md describes."""

    store: dict[str, bytes] = field(default_factory=dict)
    stale: bool = False  # serve hits labelled stale (X-CacheKit-Freshness: stale)
    fail_reads: bool = False  # answer every entry GET with a 503
    fresh_for: int | None = 60  # X-CacheKit-Fresh-For on a fresh hit; None omits the header (pre-signal server)
    ttl_left: int = 1  # GET .../ttl answer; under the refresh threshold, so a refresh is due
    lock_held_for: int = 0  # answer this many lock POSTs "held elsewhere" ({"lock_id": null})
    filled_by_holder: bytes | None = None  # stored when the other holder's lock is first reported

    def respond(self, request: httpx.Request) -> httpx.Response:
        key, op = _parse(request)
        if op == "GET":
            if self.fail_reads:
                return httpx.Response(503)
            if key not in self.store:
                return httpx.Response(404)
            if self.stale:
                headers = {FRESHNESS_HEADER: "stale", FRESH_FOR_HEADER: "0"}
            else:
                headers = {} if self.fresh_for is None else {FRESH_FOR_HEADER: str(self.fresh_for)}
            return httpx.Response(200, content=self.store[key], headers=headers)
        if op == "PUT":
            self.store[key] = request.content
            return httpx.Response(200)
        if op == "DELETE":
            self.store.pop(key, None)
            return httpx.Response(200)
        if op == "POST /lock":
            if self.lock_held_for:
                self.lock_held_for -= 1
                if self.filled_by_holder is not None:
                    self.store[key] = self.filled_by_holder
                return httpx.Response(200, json={"lock_id": None})
            return httpx.Response(200, json={"lock_id": "lock-1"})
        if op == "GET /ttl":
            return httpx.Response(200, content=json.dumps({"ttl": self.ttl_left}).encode())
        return httpx.Response(200)  # DELETE /lock, PATCH /ttl, HEAD


def _parse(request: httpx.Request) -> tuple[str, str]:
    """Split a request into (cache key, op label), e.g. ``POST .../k/lock`` -> ``("k", "POST /lock")``.

    A path outside the cache API is labelled with the whole path instead of raising: inside a transport an
    exception becomes a BackendError the decorator swallows, while an odd label fails the pinned list.
    """
    path = request.url.raw_path.decode()
    if not path.startswith(_PREFIX):
        return "", f"{request.method} {path}"
    encoded_key, _, suffix = path[len(_PREFIX) :].partition("/")
    return unquote(encoded_key), f"{request.method} /{suffix}" if suffix else request.method


@dataclass
class _Shape:
    blocking: list[str] = field(default_factory=list)  # in the order the caller waited on them
    background: list[str] = field(default_factory=list)


class _Gate:
    """Transport handlers for both httpx clients, classifying each request as blocking or background."""

    def __init__(self, saas: _FakeSaaS) -> None:
        self.saas = saas
        self.shape = _Shape()
        self.errors: list[str] = []  # a handler's failure would surface as a swallowed BackendError
        self._respond_lock = threading.Lock()
        # Async run: requests from either client wait here until run_async releases them.
        self._loop: asyncio.AbstractEventLoop | None = None
        self._parked: list[tuple[str, Callable[[], None]]] = []
        self._arrived = asyncio.Event()
        # Sync run.
        self._caller: threading.Thread | None = None
        self._caller_returned = threading.Event()

    def _respond(self, request: httpx.Request) -> httpx.Response:
        with self._respond_lock:
            return self.saas.respond(request)

    def _park(self, op: str, release: Callable[[], None]) -> None:
        self._parked.append((op, release))
        self._arrived.set()

    def sync_handler(self, request: httpx.Request) -> httpx.Response:
        op = _parse(request)[1]
        if self._loop is not None:
            # An async caller reaching the sync client, e.g. through asyncio.to_thread: park it like any other.
            released = threading.Event()
            self._loop.call_soon_threadsafe(self._park, op, released.set)
            if not released.wait(_GUARD_SECONDS):
                self.errors.append(f"{op} was never released")
        elif threading.current_thread() is self._caller:
            self.shape.blocking.append(op)
        else:
            if not self._caller_returned.wait(_GUARD_SECONDS):
                self.errors.append(f"the caller waited on {op}, sent from another thread")
            self.shape.background.append(op)
        return self._respond(request)

    async def async_handler(self, request: httpx.Request) -> httpx.Response:
        released = asyncio.Event()
        self._park(_parse(request)[1], released.set)
        await released.wait()
        return self._respond(request)

    def run_sync(self, call: Callable[[], Any]) -> _Shape:
        self.shape = _Shape()
        before = set(threading.enumerate())
        self._caller = threading.current_thread()
        self._caller_returned.clear()
        try:
            call()
        finally:
            self._caller = None
            self._caller_returned.set()
        for thread in set(threading.enumerate()) - before:
            thread.join(_GUARD_SECONDS)
            assert not thread.is_alive(), f"background thread {thread.name} did not finish"
        assert not self.errors, self.errors
        return self.shape

    async def run_async(self, call: Awaitable[Any]) -> _Shape:
        self.shape = shape = _Shape()
        self._loop = asyncio.get_running_loop()
        try:
            caller = asyncio.ensure_future(call)
            while True:
                await _settle()
                if caller.done():
                    break
                if self._parked:
                    # Everything runnable has run and the caller still waits: it waits on this request.
                    op, release = self._parked.pop(0)
                    shape.blocking.append(op)
                    release()
                else:
                    # Nothing parked: the caller is computing, polling or in a worker thread.
                    await self._next_event({caller})
            caller.result()
            # The caller has returned: whatever it left running is background. Drain it to the end.
            while True:
                await _settle()
                if self._parked:
                    shape.background.extend(op for op, _ in self._parked)
                    for _, release in self._parked:
                        release()
                    self._parked.clear()
                    continue
                if not (pending := asyncio.all_tasks() - {asyncio.current_task()}):
                    break
                await self._next_event(pending)
        finally:
            self._loop = None
        assert not self.errors, self.errors
        return shape

    async def _next_event(self, tasks: set[asyncio.Task[Any]] | set[asyncio.Future[Any]]) -> None:
        """Wait until one of ``tasks`` finishes or a request is parked."""
        self._arrived.clear()
        if self._parked:
            return
        arrived = asyncio.ensure_future(self._arrived.wait())
        done, _ = await asyncio.wait({*tasks, arrived}, timeout=_GUARD_SECONDS, return_when=asyncio.FIRST_COMPLETED)
        arrived.cancel()
        await asyncio.gather(arrived, return_exceptions=True)
        assert done, "nothing finished and no request arrived"


async def _settle() -> None:
    for _ in range(_SETTLE_TURNS):
        await asyncio.sleep(0)


@pytest.fixture
def saas() -> _FakeSaaS:
    return _FakeSaaS()


@pytest.fixture
def gate(saas: _FakeSaaS) -> _Gate:
    return _Gate(saas)


@pytest.fixture
def backend(gate: _Gate) -> Iterator[CachekitIOBackend]:
    """A CachekitIOBackend whose real httpx clients reach the fake SaaS through ``gate``."""
    sync_client = httpx.Client(base_url=_API_URL, transport=httpx.MockTransport(gate.sync_handler))
    async_client = httpx.AsyncClient(base_url=_API_URL, transport=httpx.MockTransport(gate.async_handler))
    with (
        patch("cachekit.backends.cachekitio.backend.lease_sync_http_client", return_value=MagicMock(client=sync_client)),
        patch("cachekit.backends.cachekitio.backend.get_cached_async_http_client", return_value=async_client),
    ):
        yield CachekitIOBackend(api_url=_API_URL, api_key=_API_KEY)
    sync_client.close()


def _op_counts(shape: _Shape) -> dict[str, int]:
    """Requests sent, blocking or not. For several callers at once: a request one caller left in the
    background can park while a sibling still waits, so only the counts are an invariant there."""
    return dict(Counter(shape.blocking + shape.background))


def _only_key(saas: _FakeSaaS) -> str:
    (key,) = saas.store
    return key


# The hit tests turn L1 off so the second call reaches L2; with L1 on it would never leave the process.


class TestSyncCallShape:
    """Sync decorators: no distributed lock, so a miss is a read then a write."""

    def test_cold_miss(self, backend: CachekitIOBackend, gate: _Gate) -> None:
        @cache(backend=backend, ttl=60)
        def fn(x: int) -> int:
            return x * 2

        shape = gate.run_sync(lambda: fn(1))
        assert shape == _Shape(blocking=["GET", "PUT"])

    def test_l2_hit(self, backend: CachekitIOBackend, gate: _Gate) -> None:
        @cache(backend=backend, ttl=60, l1_enabled=False)
        def fn(x: int) -> int:
            return x * 2

        gate.run_sync(lambda: fn(1))
        shape = gate.run_sync(lambda: fn(1))
        assert shape == _Shape(blocking=["GET"])

    def test_stale_hit(self, backend: CachekitIOBackend, gate: _Gate, saas: _FakeSaaS) -> None:
        @cache(backend=backend, ttl=60, stale_ttl=60, l1_enabled=False)
        def fn(x: int) -> int:
            return x * 2

        gate.run_sync(lambda: fn(1))
        saas.stale = True
        shape = gate.run_sync(lambda: fn(1))
        assert shape == _Shape(blocking=["GET"], background=["PUT"])

    def test_refresh_ttl_on_get_hit(self, backend: CachekitIOBackend, gate: _Gate) -> None:
        @cache(backend=backend, ttl=60, refresh_ttl_on_get=True, l1_enabled=False)
        def fn(x: int) -> int:
            return x * 2

        gate.run_sync(lambda: fn(1))
        shape = gate.run_sync(lambda: fn(1))
        assert shape == _Shape(blocking=["GET"])

    def test_invalidate(self, backend: CachekitIOBackend, gate: _Gate) -> None:
        @cache(backend=backend, ttl=60)
        def fn(x: int) -> int:
            return x * 2

        gate.run_sync(lambda: fn(1))
        shape = gate.run_sync(lambda: fn.invalidate_cache(1))  # type: ignore[attr-defined]
        assert shape == _Shape(blocking=["DELETE"])

    def test_failed_read(self, backend: CachekitIOBackend, gate: _Gate, saas: _FakeSaaS) -> None:
        @cache(backend=backend, ttl=60)
        def fn(x: int) -> int:
            return x * 2

        saas.fail_reads = True  # a failed read is treated as a miss, so the write still goes out
        shape = gate.run_sync(lambda: fn(1))
        assert shape == _Shape(blocking=["GET", "PUT"])


class TestAsyncCallShape:
    """Async decorators: a miss takes the SaaS lock and re-reads before computing."""

    async def test_cold_miss(self, backend: CachekitIOBackend, gate: _Gate) -> None:
        @cache(backend=backend, ttl=60)
        async def fn(x: int) -> int:
            return x * 2

        shape = await gate.run_async(fn(1))
        assert shape == _Shape(blocking=["GET", "POST /lock", "GET", "PUT", "DELETE /lock"])

    async def test_l2_hit(self, backend: CachekitIOBackend, gate: _Gate) -> None:
        @cache(backend=backend, ttl=60, l1_enabled=False)
        async def fn(x: int) -> int:
            return x * 2

        await gate.run_async(fn(1))
        shape = await gate.run_async(fn(1))
        assert shape == _Shape(blocking=["GET"])

    async def test_stale_hit(self, backend: CachekitIOBackend, gate: _Gate, saas: _FakeSaaS) -> None:
        @cache(backend=backend, ttl=60, stale_ttl=60, l1_enabled=False)
        async def fn(x: int) -> int:
            return x * 2

        await gate.run_async(fn(1))
        saas.stale = True
        shape = await gate.run_async(fn(1))
        assert shape == _Shape(blocking=["GET"], background=["POST /lock", "PUT", "DELETE /lock"])

    async def test_locked_miss(self, backend: CachekitIOBackend, gate: _Gate, saas: _FakeSaaS) -> None:
        """The lock is held elsewhere; its holder stores the value, then the caller wins the lock and reads it."""

        @cache(backend=backend, ttl=60, l1_enabled=False)
        async def fn(x: int) -> int:
            return x * 2

        await gate.run_async(fn(1))
        saas.filled_by_holder = saas.store.pop(_only_key(saas))
        saas.lock_held_for = 1
        shape = await gate.run_async(fn(1))
        assert shape == _Shape(blocking=["GET", "POST /lock", "POST /lock", "GET", "DELETE /lock"])

    # refresh_ttl_on_get decides from the hit's Fresh-For and never blocks the caller (LAB-7074).

    async def test_refresh_ttl_on_get_hit(self, backend: CachekitIOBackend, gate: _Gate, saas: _FakeSaaS) -> None:
        """Fresh-For under ttl x threshold: the PATCH goes straight to the background, no GET /ttl."""

        @cache(backend=backend, ttl=60, refresh_ttl_on_get=True, l1_enabled=False)
        async def fn(x: int) -> int:
            return x * 2

        await gate.run_async(fn(1))
        saas.fresh_for = 10
        shape = await gate.run_async(fn(1))
        assert shape == _Shape(blocking=["GET"], background=["PATCH /ttl"])

    async def test_refresh_ttl_on_get_hit_not_due(self, backend: CachekitIOBackend, gate: _Gate) -> None:
        @cache(backend=backend, ttl=60, refresh_ttl_on_get=True, l1_enabled=False)
        async def fn(x: int) -> int:
            return x * 2

        await gate.run_async(fn(1))
        shape = await gate.run_async(fn(1))  # Fresh-For 60 is above 0.5 x 60
        assert shape == _Shape(blocking=["GET"])

    async def test_refresh_ttl_on_get_hit_in_swr_fresh_window(
        self, backend: CachekitIOBackend, gate: _Gate, saas: _FakeSaaS
    ) -> None:
        """With stale_ttl = ttl, GET /ttl counts to evict_at and reads 85 at age 35, so it never asked for a
        refresh while the entry was fresh. Fresh-For 25 does: the PATCH lands before fresh_until."""

        @cache(backend=backend, ttl=60, stale_ttl=60, refresh_ttl_on_get=True, l1_enabled=False)
        async def fn(x: int) -> int:
            return x * 2

        await gate.run_async(fn(1))
        saas.fresh_for, saas.ttl_left = 25, 85
        shape = await gate.run_async(fn(1))
        assert shape == _Shape(blocking=["GET"], background=["PATCH /ttl"])

    async def test_refresh_ttl_on_get_stale_hit(self, backend: CachekitIOBackend, gate: _Gate, saas: _FakeSaaS) -> None:
        """A stale entry can't be renewed (the server answers 409), so no PATCH is sent."""

        @cache(backend=backend, ttl=60, refresh_ttl_on_get=True, l1_enabled=False)
        async def fn(x: int) -> int:
            return x * 2

        await gate.run_async(fn(1))
        saas.stale = True
        shape = await gate.run_async(fn(1))
        assert shape == _Shape(blocking=["GET"])

    @pytest.mark.parametrize("fresh_for", [None, 0], ids=["no-header", "fresh-labelled-zero"])
    async def test_refresh_ttl_on_get_hit_without_fresh_for(
        self, backend: CachekitIOBackend, gate: _Gate, saas: _FakeSaaS, fresh_for: int | None
    ) -> None:
        """No usable Fresh-For: fall back to GET /ttl, in the background with the PATCH."""

        @cache(backend=backend, ttl=60, refresh_ttl_on_get=True, l1_enabled=False)
        async def fn(x: int) -> int:
            return x * 2

        await gate.run_async(fn(1))
        saas.fresh_for = fresh_for
        shape = await gate.run_async(fn(1))
        assert shape == _Shape(blocking=["GET"], background=["GET /ttl", "PATCH /ttl"])

    async def test_refresh_ttl_on_get_single_flight_is_per_key_prefix(
        self, backend: CachekitIOBackend, gate: _Gate, saas: _FakeSaaS, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A tenant-scoped backend (key_prefix per calling context) refreshes each tenant's entry,
        even when two tenants hit the same cache key at once."""
        prefix: contextvars.ContextVar[str] = contextvars.ContextVar("prefix", default="t:a:")
        monkeypatch.setattr(CachekitIOBackend, "key_prefix", property(lambda _self: prefix.get()), raising=False)

        @cache(backend=backend, ttl=60, refresh_ttl_on_get=True, l1_enabled=False)
        async def fn(x: int) -> int:
            return x * 2

        await gate.run_async(fn(1))
        saas.fresh_for = 10

        async def as_tenant(tenant: str) -> None:
            prefix.set(tenant)
            await fn(1)

        async def two_tenants() -> None:
            await asyncio.gather(as_tenant("t:a:"), as_tenant("t:b:"))

        shape = await gate.run_async(two_tenants())
        assert _op_counts(shape) == {"GET": 2, "PATCH /ttl": 2}

    async def test_invalidate(self, backend: CachekitIOBackend, gate: _Gate) -> None:
        """ainvalidate_cache sends its DELETE from a worker thread through the sync client, and awaits it."""

        @cache(backend=backend, ttl=60)
        async def fn(x: int) -> int:
            return x * 2

        await gate.run_async(fn(1))
        shape = await gate.run_async(fn.ainvalidate_cache(1))  # type: ignore[attr-defined]
        assert shape == _Shape(blocking=["DELETE"])

    async def test_failed_read(self, backend: CachekitIOBackend, gate: _Gate, saas: _FakeSaaS) -> None:
        @cache(backend=backend, ttl=60)
        async def fn(x: int) -> int:
            return x * 2

        saas.fail_reads = True  # a failed read is treated as a miss: the full locked-miss sequence follows
        shape = await gate.run_async(fn(1))
        assert shape == _Shape(blocking=["GET", "POST /lock", "GET", "PUT", "DELETE /lock"])
