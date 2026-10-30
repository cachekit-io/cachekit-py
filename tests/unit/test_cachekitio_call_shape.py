"""Call shape of each decorated path over CachekitIO: which HTTP requests block the caller.

Each test runs one decorated call against a fake SaaS behind the backend's real urllib3 client, on a
fake pool (tests/utils/cachekitio_fakes.py), and pins two things: the ordered list of requests the
caller waits on, and the set of requests that run in the background after it returns. A path that gains
a blocking round trip, or a background request that becomes blocking, changes the list and fails here.
A round-trip fix edits the pinned list in its own PR, and that diff is its proof.

Blocking is decided by state, never by a clock: a request blocks if the caller is still waiting on it.
The thread a request arrives on cannot tell that for an async caller. The async backend methods send on
the backend's one client through ``asyncio.to_thread``, so the caller's own requests and its background tasks'
all arrive on worker threads. The gate goes by what the caller is doing instead, and by whether anything
waits on the executor job that sent the request.

Async: every request is parked on the gate, its worker thread held until the event loop releases it.
A request from an executor job whose future nothing waits on (the miss path's lock release) is never released
while the caller is pending: as on the sync side, it is held until the caller has returned, so it is background
however long the caller keeps running, unless a timer fires meanwhile (see below).
Any other parked request is judged by the caller. Once the loop
has run everything it can, for a whole settle since the latest request parked, and the caller is still
pending, the caller waits on a parked request: the oldest is released as blocking. With none parked yet,
the gate waits for one to arrive or for the caller to finish, because a request still on its way through a
worker thread has not parked. Once the caller has returned, whatever it left running is background. A task
the caller spawns cannot send before the caller's current step ends, and on every pinned path the caller
returns within a few turns of that step: the miss runs in a single-flight task the caller awaits.
A caller that kept awaiting after spawning one would let the task's request read as blocking: the pinned
list fails, it does not pass by mistake. A wait on a held request that bypasses its job's future (on an
Event the job sets, say) is one the gate cannot see. Unbounded, it hangs until the guard fails the run.
Bounded, its timer fires while the request is held, and the gate books the request blocking. Any timer that
fires then counts, whichever task set it: a helper task can bound the wait for the caller and live on, so neither
the task nor its fate shows whose wait it was. The gate errs toward blocking instead. A caller that sleeps after
its background release, or background work whose timer fires before the caller returns, makes the pinned list
fail rather than pass by mistake. No pinned path has a timer after its background release. A wait bounded
without a loop timer (by counting turns, say) stays invisible.

Sync: a request blocks if it runs on the caller's thread, where the caller cannot return until it does;
a request on any other thread is held until the caller has returned, so it can only be background.

Only single-caller paths are pinned exactly. Herd paths (several callers on one key) vary in lock-poll
count and order from run to run, so they belong to invariant tests, not here.
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import json
import threading
from collections import Counter
from collections.abc import AsyncIterator, Awaitable, Callable
from concurrent.futures import Executor
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import unquote

import pytest
from urllib3 import HTTPResponse

from cachekit import cache
from cachekit.backends.cachekitio.backend import FRESH_FOR_HEADER, FRESHNESS_HEADER, CachekitIOBackend
from tests.utils.cachekitio_fakes import FakeRequest, fake_backend, response

pytestmark = pytest.mark.unit

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


@dataclass
class _Shape:
    blocking: list[str] = field(default_factory=list)  # in the order the caller waited on them
    background: list[str] = field(default_factory=list)


_running = threading.local()  # .job: the _Job a worker thread is running, for the requests it sends


class _Job(asyncio.Future[Any]):
    """An executor job's future as the gate's ``run_in_executor`` hands it back: a real future that settles as the
    loop's own future for the job does, and records whether anything waits on it. Every wait registers a done
    callback (``await``, ``asyncio.wait``, ``gather``, ``shield``), so registering one is what counts."""

    inner: asyncio.Future[Any]  # the loop's own future for the job
    awaited = False

    def __init__(self, loop: asyncio.AbstractEventLoop, on_awaited: Callable[[], None]) -> None:
        super().__init__(loop=loop)
        self._on_awaited = on_awaited

    def add_done_callback(
        self, fn: Callable[[asyncio.Future[Any]], object], /, *, context: contextvars.Context | None = None
    ) -> None:
        if not self.awaited:
            self.awaited = True
            self._on_awaited()
        super().add_done_callback(fn, context=context)

    def run(self, func: Callable[..., Any], *args: Any) -> Any:
        _running.job = self
        try:
            return func(*args)
        finally:
            _running.job = None

    def relay(self, inner: asyncio.Future[Any]) -> None:
        if self.done():
            return
        if inner.cancelled():
            self.cancel()
        elif (exc := inner.exception()) is not None:
            self.set_exception(exc)
        else:
            self.set_result(inner.result())


@dataclass(eq=False)
class _Parked:
    op: str
    job: _Job | None  # None: sent from outside any executor job
    release: Callable[[], None]

    @property
    def awaited(self) -> bool:
        """Whether something may wait on the request. Not when it comes from a job nothing awaits."""
        return self.job is None or self.job.awaited


class _Gate:
    """The fake pool's handler, classifying each request as blocking or background."""

    def __init__(self, saas: _FakeSaaS) -> None:
        self.saas = saas
        self.shape = _Shape()
        self.errors: list[str] = []  # a handler's failure would surface as a swallowed BackendError
        self._respond_lock = threading.Lock()
        # Async run: every request, each on a worker thread, waits here until run_async releases it.
        self._loop: asyncio.AbstractEventLoop | None = None
        self._waiting: asyncio.Future[Any] | None = None  # the caller, while run_async runs it
        self._jobs: list[_Job] = []  # every executor job of the run
        self._parked: list[_Parked] = []
        self._arrived = asyncio.Event()
        self.arrivals: list[str] = []  # every request parked this run, in order
        # Sync run.
        self._caller: threading.Thread | None = None
        self._caller_returned = threading.Event()

    def _respond(self, request: FakeRequest) -> HTTPResponse:
        with self._respond_lock:
            return self.saas.respond(request)

    def _park(self, parked: _Parked) -> None:
        self._parked.append(parked)
        self.arrivals.append(parked.op)
        self._arrived.set()

    async def arrival(self, op: str) -> None:
        """Return once ``op`` has parked this run."""
        while op not in self.arrivals:
            await asyncio.sleep(0)

    def _run_in_executor(self, executor: Executor | None, func: Callable[..., Any], *args: Any) -> _Job:
        """The loop's ``run_in_executor`` during an async run (``asyncio.to_thread`` calls it too)."""
        assert self._loop is not None
        job = _Job(self._loop, self._arrived.set)  # a new wait may be the one the caller is held on
        # The class's method: the loop's own attribute is this one while run_async runs.
        job.inner = type(self._loop).run_in_executor(self._loop, executor, job.run, func, *args)
        job.inner.add_done_callback(job.relay)
        self._jobs.append(job)
        return job

    def _call_at(
        self, when: float, callback: Callable[..., object], *args: Any, context: contextvars.Context | None = None
    ) -> asyncio.TimerHandle:
        """The loop's ``call_at`` during an async run (``call_later``, ``asyncio.sleep`` and ``wait_for`` call it too)."""
        assert self._loop is not None
        return type(self._loop).call_at(self._loop, when, self._timer_fired, callback, *args, context=context)

    def _timer_fired(self, callback: Callable[..., object], *args: Any) -> None:
        # A wait on a held request can bypass its job's future (on an Event the job sets): the gate cannot see that wait,
        # but a bounded one leaves a trace, its timer firing while the caller still waits. Nor can the gate tell whose
        # wait a timer bounds (a helper task can bound it for the caller and live on), so any timer counts: an unrelated
        # one fails the run instead of letting a real wait pass.
        if self._waiting is not None and not self._waiting.done():
            for held in [parked for parked in self._parked if not parked.awaited]:
                self._parked.remove(held)
                self.shape.blocking.append(held.op)
                held.release()
        callback(*args)

    def handler(self, request: FakeRequest) -> HTTPResponse:
        op = _parse(request)[1]
        if self._loop is not None:
            # An async run: the request comes from a worker thread (an executor job), never the loop's own.
            released = threading.Event()
            self._loop.call_soon_threadsafe(self._park, _Parked(op, getattr(_running, "job", None), released.set))
            if not released.wait(_GUARD_SECONDS):
                self.errors.append(f"{op} was never released")
        elif threading.current_thread() is self._caller:
            self.shape.blocking.append(op)
        else:
            if not self._caller_returned.wait(_GUARD_SECONDS):
                self.errors.append(f"the caller waited on {op}, sent from another thread")
            self.shape.background.append(op)
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
        self.arrivals, self._jobs = [], []
        self._loop = loop = asyncio.get_running_loop()
        loop.run_in_executor = self._run_in_executor  # type: ignore[method-assign]
        loop.call_at = self._call_at  # type: ignore[method-assign]
        try:
            self._waiting = caller = asyncio.ensure_future(call)
            while True:
                arrivals = len(self.arrivals)
                await _settle()
                if caller.done():
                    break
                if len(self.arrivals) > arrivals:
                    continue  # one arrived mid-settle: it gets a whole settle too before it is judged
                if waited := next((parked for parked in self._parked if parked.awaited), None):
                    # Everything runnable has run and the caller still waits: it waits on this request.
                    self._parked.remove(waited)
                    shape.blocking.append(waited.op)
                    waited.release()
                else:
                    # None the caller can wait on: it is computing, polling, or its request is still on the way
                    # through a worker thread. A request nothing awaits stays parked until the caller returns.
                    await self._next_event({caller})
            caller.result()
            # The caller has returned: whatever it left running is background. Drain it to the end.
            while True:
                await _settle()
                if self._parked:
                    shape.background.extend(parked.op for parked in self._parked)
                    for parked in self._parked:
                        parked.release()
                    self._parked.clear()
                    continue
                # Background work is a Task, or an executor job (the lock release runs in a thread).
                tasks = asyncio.all_tasks() - {asyncio.current_task()}
                if not (pending := tasks | {job.inner for job in self._jobs if not job.inner.done()}):
                    break
                await self._next_event(pending)
        finally:
            for parked in self._parked:  # left by a caller that raised: never strand a worker thread
                parked.release()
            self._parked.clear()
            del loop.run_in_executor, loop.call_at
            self._loop = self._waiting = None
        assert not self.errors, self.errors
        return shape

    async def _next_event(self, tasks: set[Any]) -> None:
        """Wait until one of ``tasks`` finishes, a request is parked, or a job is first waited on."""
        self._arrived.clear()
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
def backend(gate: _Gate) -> CachekitIOBackend:
    """A CachekitIOBackend whose real client reaches the fake SaaS through ``gate``, sync and async alike."""
    backend, _ = fake_backend(gate.handler, api_key=_API_KEY)
    return backend


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

    def test_cold_miss_write_behind(self, backend: CachekitIOBackend, gate: _Gate) -> None:
        @cache(backend=backend, ttl=60, write_behind=True)
        def fn(x: int) -> int:
            return x * 2

        shape = gate.run_sync(lambda: fn(1))
        assert shape == _Shape(blocking=["GET"], background=["PUT"])


class TestAsyncCallShape:
    """Async decorators: a miss takes the SaaS lock, re-reads only if it had to wait or its read failed, and releases in the background."""

    async def test_cold_miss(self, backend: CachekitIOBackend, gate: _Gate) -> None:
        @cache(backend=backend, ttl=60)
        async def fn(x: int) -> int:
            return x * 2

        shape = await gate.run_async(fn(1))
        # The first lock POST won after a clean miss: no re-read, and the release is not waited on.
        assert shape == _Shape(blocking=["GET", "POST /lock", "PUT"], background=["DELETE /lock"])

    async def test_cold_miss_write_behind_keeps_the_put_inside_the_lock(self, backend: CachekitIOBackend, gate: _Gate) -> None:
        @cache(backend=backend, ttl=60, write_behind=True)
        async def fn(x: int) -> int:
            return x * 2

        shape = await gate.run_async(fn(1))
        # Deferred, the PUT would land after the release, and a waiter's re-read would miss and recompute.
        assert shape == _Shape(blocking=["GET", "POST /lock", "PUT"], background=["DELETE /lock"])

    async def test_cold_miss_without_distributed_lock(self, backend: CachekitIOBackend, gate: _Gate) -> None:
        @cache(backend=backend, ttl=60, distributed_lock=False)
        async def fn(x: int) -> int:
            return x * 2

        shape = await gate.run_async(fn(1))
        assert shape == _Shape(blocking=["GET", "PUT"])

    async def test_cold_miss_write_behind_without_distributed_lock(self, backend: CachekitIOBackend, gate: _Gate) -> None:
        @cache(backend=backend, ttl=60, write_behind=True, distributed_lock=False)
        async def fn(x: int) -> int:
            return x * 2

        shape = await gate.run_async(fn(1))
        # No lock for a waiter to re-read under, so the PUT leaves the caller's path as on the sync side.
        assert shape == _Shape(blocking=["GET"], background=["PUT"])

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
        assert shape == _Shape(blocking=["GET"])

    @pytest.mark.parametrize("distributed_lock", [True, False])
    async def test_stale_hit(self, backend: CachekitIOBackend, gate: _Gate, saas: _FakeSaaS, distributed_lock: bool) -> None:
        """The revalidation lease runs off the caller's path, so the miss-path option leaves it alone."""

        @cache(backend=backend, ttl=60, stale_ttl=60, l1_enabled=False, distributed_lock=distributed_lock)
        async def fn(x: int) -> int:
            return x * 2

        await gate.run_async(fn(1))
        saas.stale = True
        shape = await gate.run_async(fn(1))
        assert shape == _Shape(blocking=["GET"], background=["POST /lock", "PUT", "DELETE /lock"])

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
            for _ in range(_SETTLE_TURNS - 10):  # most of a settle: the gate's own wake-up costs a few turns
                await asyncio.sleep(0)
            return value

        shape = await gate.run_async(late_caller())
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
        # A waited grant keeps the double-check read: the holder may have filled the key meanwhile.
        assert shape == _Shape(blocking=["GET", "POST /lock", "POST /lock", "GET"], background=["DELETE /lock"])

    # refresh_ttl_on_get decides from the hit's Fresh-For and never blocks the caller.

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
        """ainvalidate_cache sends its DELETE from a worker thread, and awaits it."""

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

        saas.fail_reads = True  # a failed read is treated as a miss, and is read again once the lock is won
        shape = await gate.run_async(fn(1))
        assert shape == _Shape(blocking=["GET", "POST /lock", "GET", "PUT"], background=["DELETE /lock"])

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
        assert shape == _Shape(blocking=["GET", "POST /lock", "GET"], background=["DELETE /lock"])
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
                await _settle()  # the gate judges the parked release while this caller still runs
            return value

        shape = await gate.run_async(late_caller())
        assert shape == _Shape(blocking=["GET", "POST /lock", "GET"], background=["DELETE /lock"])

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
        assert shape == _Shape(blocking=["GET", "POST /lock", "GET", "DELETE /lock"])

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
        assert shape == _Shape(blocking=["GET", "POST /lock", "GET", "DELETE /lock"])
