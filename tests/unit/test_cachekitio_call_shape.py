"""Call shape of each decorated path over CachekitIO: which HTTP requests block the caller.

Each test runs one decorated call against a fake SaaS behind the backend's real urllib3 client, on a
fake pool (tests/utils/cachekitio_fakes.py), and pins two things: the ordered list of requests the
caller waits on, and the set of requests that run in the background after it returns. A path that gains
a blocking round trip, or a background request that becomes blocking, changes the list and fails here.
A round-trip fix edits the pinned list in its own PR, and that diff is its proof.

The gate that classifies each request is tests/utils/call_shape_gate.py; its docstring says how.
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import json
import threading
from collections import Counter
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from urllib.parse import unquote

import pytest
from urllib3 import HTTPResponse

from cachekit import cache
from cachekit.backends.cachekitio.backend import FRESH_FOR_HEADER, FRESHNESS_HEADER, CachekitIOBackend
from tests.utils.cachekitio_fakes import FakeRequest, fake_backend, response
from tests.utils.call_shape_gate import SETTLE_TURNS, CallShapeGate, Shape, settle

pytestmark = pytest.mark.unit

_API_KEY = "ck_test_call_shape"  # pragma: allowlist secret — fake key, test fixture
_PREFIX = "/v1/cache/"


@dataclass
class _FakeSaaS:
    """In-memory stand-in for the SaaS cache API, answering the way spec/saas-api.md describes."""

    store: dict[str, bytes] = field(default_factory=dict)
    stale: bool = False  # serve hits labelled stale (X-CacheKit-Freshness: stale)
    fail_reads: bool = False  # answer every entry GET with a 503
    fail_next_reads: int = 0  # answer this many entry GETs with a 503, then serve normally
    fresh_for: int | None = 60  # X-CacheKit-Fresh-For on a fresh hit; None omits the header (pre-signal server)
    ttl_left: int = 1  # GET .../ttl answer; under the refresh threshold, so a refresh is due
    lock_held_for: int = 0  # answer this many lock POSTs "held elsewhere" ({"lock_id": null})
    filled_by_holder: bytes | None = None  # stored when the other holder's lock is first reported

    def respond(self, request: FakeRequest) -> HTTPResponse:
        key, op = _parse(request)
        if op == "GET":
            if self.fail_reads:
                return response(503)
            if self.fail_next_reads:
                self.fail_next_reads -= 1
                return response(503)
            if key not in self.store:
                return response(404)
            if self.stale:
                headers = {FRESHNESS_HEADER: "stale", FRESH_FOR_HEADER: "0"}
            else:
                headers = {} if self.fresh_for is None else {FRESH_FOR_HEADER: str(self.fresh_for)}
            return response(200, self.store[key], headers=headers)
        if op == "PUT":
            assert request.body is not None, "a PUT carries the value"
            self.store[key] = request.body
            return response(200)
        if op == "DELETE":
            self.store.pop(key, None)
            return response(200)
        if op == "POST /lock":
            if self.lock_held_for:
                self.lock_held_for -= 1
                if self.filled_by_holder is not None:
                    self.store[key] = self.filled_by_holder
                return response(200, json={"lock_id": None})
            return response(200, json={"lock_id": "lock-1"})
        if op == "GET /ttl":
            return response(200, json.dumps({"ttl": self.ttl_left}).encode())
        return response(200)  # DELETE /lock, PATCH /ttl, HEAD


def _parse(request: FakeRequest) -> tuple[str, str]:
    """Split a request into (cache key, op label), e.g. ``POST .../k/lock`` -> ``("k", "POST /lock")``.

    A path outside the cache API is labelled with the whole path instead of raising: inside the pool an
    exception becomes a BackendError the decorator swallows, while an odd label fails the pinned list.
    """
    path = request.path
    if not path.startswith(_PREFIX):
        return "", f"{request.method} {path}"
    encoded_key, _, suffix = path[len(_PREFIX) :].partition("/")
    return unquote(encoded_key), f"{request.method} /{suffix}" if suffix else request.method


class _Gate(CallShapeGate):
    """The fake pool's handler: each request is classified, then answered by the fake SaaS."""

    def __init__(self, saas: _FakeSaaS) -> None:
        super().__init__()
        self.saas = saas
        self._respond_lock = threading.Lock()

    def handler(self, request: FakeRequest) -> HTTPResponse:
        self.observe(_parse(request)[1])
        with self._respond_lock:
            return self.saas.respond(request)


@pytest.fixture
def saas() -> _FakeSaaS:
    return _FakeSaaS()


@pytest.fixture
def gate(saas: _FakeSaaS) -> _Gate:
    return _Gate(saas)


@pytest.fixture
def backend(gate: _Gate) -> CachekitIOBackend:
    """A CachekitIOBackend whose real client reaches the fake SaaS through ``gate``, sync and async alike."""
    backend, _ = fake_backend(gate.handler, api_key=_API_KEY)
    return backend


def _op_counts(shape: Shape) -> dict[str, int]:
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
        assert shape == Shape(blocking=["GET", "PUT"])

    def test_l2_hit(self, backend: CachekitIOBackend, gate: _Gate) -> None:
        @cache(backend=backend, ttl=60, l1_enabled=False)
        def fn(x: int) -> int:
            return x * 2

        gate.run_sync(lambda: fn(1))
        shape = gate.run_sync(lambda: fn(1))
        assert shape == Shape(blocking=["GET"])

    def test_stale_hit(self, backend: CachekitIOBackend, gate: _Gate, saas: _FakeSaaS) -> None:
        @cache(backend=backend, ttl=60, stale_ttl=60, l1_enabled=False)
        def fn(x: int) -> int:
            return x * 2

        gate.run_sync(lambda: fn(1))
        saas.stale = True
        shape = gate.run_sync(lambda: fn(1))
        assert shape == Shape(blocking=["GET"], background=["PUT"])

    def test_refresh_ttl_on_get_hit(self, backend: CachekitIOBackend, gate: _Gate) -> None:
        @cache(backend=backend, ttl=60, refresh_ttl_on_get=True, l1_enabled=False)
        def fn(x: int) -> int:
            return x * 2

        gate.run_sync(lambda: fn(1))
        shape = gate.run_sync(lambda: fn(1))
        assert shape == Shape(blocking=["GET"])

    def test_invalidate(self, backend: CachekitIOBackend, gate: _Gate) -> None:
        @cache(backend=backend, ttl=60)
        def fn(x: int) -> int:
            return x * 2

        gate.run_sync(lambda: fn(1))
        shape = gate.run_sync(lambda: fn.invalidate_cache(1))  # type: ignore[attr-defined]
        assert shape == Shape(blocking=["DELETE"])

    def test_failed_read(self, backend: CachekitIOBackend, gate: _Gate, saas: _FakeSaaS) -> None:
        @cache(backend=backend, ttl=60)
        def fn(x: int) -> int:
            return x * 2

        saas.fail_reads = True  # a failed read is treated as a miss, so the write still goes out
        shape = gate.run_sync(lambda: fn(1))
        assert shape == Shape(blocking=["GET", "PUT"])

    def test_cold_miss_write_behind(self, backend: CachekitIOBackend, gate: _Gate) -> None:
        @cache(backend=backend, ttl=60, write_behind=True)
        def fn(x: int) -> int:
            return x * 2

        shape = gate.run_sync(lambda: fn(1))
        assert shape == Shape(blocking=["GET"], background=["PUT"])


class TestAsyncCallShape:
    """Async decorators: a miss takes the SaaS lock, re-reads only if it had to wait or its read failed, and releases in the background."""

    async def test_cold_miss(self, backend: CachekitIOBackend, gate: _Gate) -> None:
        @cache(backend=backend, ttl=60)
        async def fn(x: int) -> int:
            return x * 2

        shape = await gate.run_async(fn(1))
        # The first lock POST won after a clean miss: no re-read, and the release is not waited on.
        assert shape == Shape(blocking=["GET", "POST /lock", "PUT"], background=["DELETE /lock"])

    async def test_cold_miss_write_behind_keeps_the_put_inside_the_lock(self, backend: CachekitIOBackend, gate: _Gate) -> None:
        @cache(backend=backend, ttl=60, write_behind=True)
        async def fn(x: int) -> int:
            return x * 2

        shape = await gate.run_async(fn(1))
        # Deferred, the PUT would land after the release, and a waiter's re-read would miss and recompute.
        assert shape == Shape(blocking=["GET", "POST /lock", "PUT"], background=["DELETE /lock"])

    async def test_cold_miss_without_distributed_lock(self, backend: CachekitIOBackend, gate: _Gate) -> None:
        @cache(backend=backend, ttl=60, distributed_lock=False)
        async def fn(x: int) -> int:
            return x * 2

        shape = await gate.run_async(fn(1))
        assert shape == Shape(blocking=["GET", "PUT"])

    async def test_cold_miss_write_behind_without_distributed_lock(self, backend: CachekitIOBackend, gate: _Gate) -> None:
        @cache(backend=backend, ttl=60, write_behind=True, distributed_lock=False)
        async def fn(x: int) -> int:
            return x * 2

        shape = await gate.run_async(fn(1))
        # No lock for a waiter to re-read under, so the PUT leaves the caller's path as on the sync side.
        assert shape == Shape(blocking=["GET"], background=["PUT"])

    async def test_herd_without_distributed_lock_runs_once(self, backend: CachekitIOBackend, gate: _Gate) -> None:
        """An invariant, not a pinned list: 10 concurrent callers on one cold key share one in-process trip."""
        calls: list[int] = []

        @cache(backend=backend, ttl=60, distributed_lock=False)
        async def fn(x: int) -> int:
            calls.append(x)
            return x * 2

        async def herd() -> list[int]:
            return await asyncio.gather(*(fn(1) for _ in range(10)))

        shape = await gate.run_async(herd())
        assert calls == [1]
        assert _op_counts(shape) == {"GET": 1, "PUT": 1}

    async def test_l2_hit(self, backend: CachekitIOBackend, gate: _Gate) -> None:
        @cache(backend=backend, ttl=60, l1_enabled=False)
        async def fn(x: int) -> int:
            return x * 2

        await gate.run_async(fn(1))
        shape = await gate.run_async(fn(1))
        assert shape == Shape(blocking=["GET"])

    @pytest.mark.parametrize("distributed_lock", [True, False])
    async def test_stale_hit(self, backend: CachekitIOBackend, gate: _Gate, saas: _FakeSaaS, distributed_lock: bool) -> None:
        """The revalidation lease runs off the caller's path, so the miss-path option leaves it alone."""

        @cache(backend=backend, ttl=60, stale_ttl=60, l1_enabled=False, distributed_lock=distributed_lock)
        async def fn(x: int) -> int:
            return x * 2

        await gate.run_async(fn(1))
        saas.stale = True
        shape = await gate.run_async(fn(1))
        assert shape == Shape(blocking=["GET"], background=["POST /lock", "PUT", "DELETE /lock"])

    async def test_stale_hit_outlived_by_its_caller(self, backend: CachekitIOBackend, gate: _Gate, saas: _FakeSaaS) -> None:
        """The caller is still running when its revalidation's first request arrives, for most of a settle after:
        it has returned before a whole settle since that arrival has passed, so the request is background."""

        @cache(backend=backend, ttl=60, stale_ttl=60, l1_enabled=False)
        async def fn(x: int) -> int:
            return x * 2

        await gate.run_async(fn(1))
        saas.stale = True

        async def late_caller() -> int:
            value = await fn(1)
            await gate.arrival("POST /lock")
            for _ in range(SETTLE_TURNS - 10):  # most of a settle: the gate's own wake-up costs a few turns
                await asyncio.sleep(0)
            return value

        shape = await gate.run_async(late_caller())
        assert shape == Shape(blocking=["GET"], background=["POST /lock", "PUT", "DELETE /lock"])

    async def test_locked_miss(self, backend: CachekitIOBackend, gate: _Gate, saas: _FakeSaaS) -> None:
        """The lock is held elsewhere; its holder stores the value, then the caller wins the lock and reads it."""

        @cache(backend=backend, ttl=60, l1_enabled=False)
        async def fn(x: int) -> int:
            return x * 2

        await gate.run_async(fn(1))
        saas.filled_by_holder = saas.store.pop(_only_key(saas))
        saas.lock_held_for = 1
        shape = await gate.run_async(fn(1))
        # A waited grant keeps the double-check read: the holder may have filled the key meanwhile.
        assert shape == Shape(blocking=["GET", "POST /lock", "POST /lock", "GET"], background=["DELETE /lock"])

    # refresh_ttl_on_get decides from the hit's Fresh-For and never blocks the caller.

    async def test_refresh_ttl_on_get_hit(self, backend: CachekitIOBackend, gate: _Gate, saas: _FakeSaaS) -> None:
        """Fresh-For under ttl x threshold: the PATCH goes straight to the background, no GET /ttl."""

        @cache(backend=backend, ttl=60, refresh_ttl_on_get=True, l1_enabled=False)
        async def fn(x: int) -> int:
            return x * 2

        await gate.run_async(fn(1))
        saas.fresh_for = 10
        shape = await gate.run_async(fn(1))
        assert shape == Shape(blocking=["GET"], background=["PATCH /ttl"])

    async def test_refresh_ttl_on_get_hit_not_due(self, backend: CachekitIOBackend, gate: _Gate) -> None:
        @cache(backend=backend, ttl=60, refresh_ttl_on_get=True, l1_enabled=False)
        async def fn(x: int) -> int:
            return x * 2

        await gate.run_async(fn(1))
        shape = await gate.run_async(fn(1))  # Fresh-For 60 is above 0.5 x 60
        assert shape == Shape(blocking=["GET"])

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
        assert shape == Shape(blocking=["GET"], background=["PATCH /ttl"])

    async def test_refresh_ttl_on_get_stale_hit(self, backend: CachekitIOBackend, gate: _Gate, saas: _FakeSaaS) -> None:
        """A stale entry can't be renewed (the server answers 409), so no PATCH is sent."""

        @cache(backend=backend, ttl=60, refresh_ttl_on_get=True, l1_enabled=False)
        async def fn(x: int) -> int:
            return x * 2

        await gate.run_async(fn(1))
        saas.stale = True
        shape = await gate.run_async(fn(1))
        assert shape == Shape(blocking=["GET"])

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
        assert shape == Shape(blocking=["GET"], background=["GET /ttl", "PATCH /ttl"])

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
        """ainvalidate_cache sends its DELETE from a worker thread, and awaits it."""

        @cache(backend=backend, ttl=60)
        async def fn(x: int) -> int:
            return x * 2

        await gate.run_async(fn(1))
        shape = await gate.run_async(fn.ainvalidate_cache(1))  # type: ignore[attr-defined]
        assert shape == Shape(blocking=["DELETE"])

    async def test_failed_read(self, backend: CachekitIOBackend, gate: _Gate, saas: _FakeSaaS) -> None:
        @cache(backend=backend, ttl=60)
        async def fn(x: int) -> int:
            return x * 2

        saas.fail_reads = True  # a failed read is treated as a miss, and is read again once the lock is won
        shape = await gate.run_async(fn(1))
        assert shape == Shape(blocking=["GET", "POST /lock", "GET", "PUT"], background=["DELETE /lock"])

    async def test_failed_read_of_a_live_entry(self, backend: CachekitIOBackend, gate: _Gate, saas: _FakeSaaS) -> None:
        """The primary read fails on an entry that is still live, and the first lock POST wins. The post-lock
        read is the only retry, so it still runs and serves the entry: no recompute, no PUT."""
        calls: list[int] = []

        @cache(backend=backend, ttl=60, l1_enabled=False)
        async def fn(x: int) -> int:
            calls.append(x)
            return x * 2

        await gate.run_async(fn(1))
        saas.fail_next_reads = 1
        shape = await gate.run_async(fn(1))
        assert shape == Shape(blocking=["GET", "POST /lock", "GET"], background=["DELETE /lock"])
        assert calls == [1]

    async def test_failed_read_of_a_live_entry_outlived_by_its_caller(
        self, backend: CachekitIOBackend, gate: _Gate, saas: _FakeSaaS
    ) -> None:
        """The caller is still running when the lock release arrives, and for several settles after: a loaded
        runner once made it so. The release is background all the same, because nothing awaits it."""

        @cache(backend=backend, ttl=60, l1_enabled=False)
        async def fn(x: int) -> int:
            return x * 2

        await gate.run_async(fn(1))
        saas.fail_next_reads = 1

        async def late_caller() -> int:
            value = await fn(1)
            await gate.arrival("DELETE /lock")
            for _ in range(3):
                await settle()  # the gate judges the parked release while this caller still runs
            return value

        shape = await gate.run_async(late_caller())
        assert shape == Shape(blocking=["GET", "POST /lock", "GET"], background=["DELETE /lock"])

    @pytest.mark.parametrize("in_helper", [False, True], ids=["in-the-call", "in-a-helper-that-lives-on"])
    async def test_a_bounded_wait_on_the_release_reads_as_blocking(
        self,
        backend: CachekitIOBackend,
        gate: _Gate,
        saas: _FakeSaaS,
        monkeypatch: pytest.MonkeyPatch,
        in_helper: bool,
    ) -> None:
        """A check on the gate itself, through a release that regressed: it still runs in the background, but the
        caller then waits up to a bound for an Event the release sets. That wait bypasses the job's future, and
        the gate must book the DELETE blocking all the same, also when a helper task runs the bound, wakes the
        caller once it expires, and lives on until the release lands."""
        released = asyncio.Event()
        loop = asyncio.get_running_loop()
        delete_lock_sync = CachekitIOBackend._delete_lock_sync
        acquire_fill_lock = CachekitIOBackend.acquire_fill_lock
        helpers: list[asyncio.Future[None]] = []

        def delete_and_signal(self: CachekitIOBackend, lock_key: str, lock_id: str) -> None:
            delete_lock_sync(self, lock_key, lock_id)
            loop.call_soon_threadsafe(released.set)

        async def bounded_wait() -> None:
            # asyncio.wait sets its bound with call_later, a loop timer on every Python. wait_for (3.12+) uses call_soon
            # instead if the deadline has passed by the time it is set: under load that is a wait of no length at all.
            waiting = asyncio.ensure_future(released.wait())
            await asyncio.wait({waiting}, timeout=0.001)
            waiting.cancel()

        async def bound_then_drain(expired: asyncio.Event) -> None:
            await bounded_wait()
            expired.set()
            await released.wait()

        @contextlib.asynccontextmanager
        async def wait_for_the_release(
            self: CachekitIOBackend, key: str, timeout: float, blocking_timeout: float | None = None
        ) -> AsyncIterator[tuple[bool, bool]]:
            async with acquire_fill_lock(self, key, timeout, blocking_timeout) as grant:
                yield grant
            await gate.arrival("DELETE /lock")  # parked, so the bound's timer fires while it is held
            if not in_helper:
                await bounded_wait()
                return
            expired = asyncio.Event()
            helpers.append(asyncio.ensure_future(bound_then_drain(expired)))
            await expired.wait()

        @cache(backend=backend, ttl=60, l1_enabled=False)
        async def fn(x: int) -> int:
            return x * 2

        await gate.run_async(fn(1))
        monkeypatch.setattr(CachekitIOBackend, "_delete_lock_sync", delete_and_signal)
        monkeypatch.setattr(CachekitIOBackend, "acquire_fill_lock", wait_for_the_release)
        saas.fail_next_reads = 1
        shape = await gate.run_async(fn(1))
        assert shape == Shape(blocking=["GET", "POST /lock", "GET", "DELETE /lock"])

    async def test_any_timer_while_the_release_is_held_reads_as_blocking(
        self, backend: CachekitIOBackend, gate: _Gate, saas: _FakeSaaS
    ) -> None:
        """A check on the gate itself, pinning the side it errs on: a timer of unrelated background work that fires
        while the release is held and the caller still runs books the release blocking. The gate cannot tell it
        from a bounded wait in a helper task, so the run fails rather than passes by mistake."""

        @cache(backend=backend, ttl=60, l1_enabled=False)
        async def fn(x: int) -> int:
            return x * 2

        await gate.run_async(fn(1))
        saas.fail_next_reads = 1
        fired = asyncio.Event()

        async def background() -> None:
            await asyncio.sleep(0.001)
            fired.set()

        async def caller() -> int:
            value = await fn(1)
            await gate.arrival("DELETE /lock")
            task = asyncio.ensure_future(background())
            await fired.wait()  # the background timer has fired while the release was held
            assert task.done()  # and that work is over: nothing of the caller's waited on the release
            return value

        shape = await gate.run_async(caller())
        assert shape == Shape(blocking=["GET", "POST /lock", "GET", "DELETE /lock"])
