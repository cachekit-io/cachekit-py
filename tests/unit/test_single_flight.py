"""Concurrent misses on one key share one call within one process, in every mode.

Without this, every caller that missed a key ran the function: in L1-only mode and under
``@cache.local()`` on the cold path and past ``ttl``, and in backed mode, where each caller also
made its own L2 read and, when async, waited on the distributed lock, which dedups only across
processes.

Async and sync alike: ``@cache(backend=None)``, ``@cache.local()`` and backed mode. The sync backed
path takes no distributed lock, so across processes nothing dedups it; within one, its misses share
one trip as the async ones do.

Semantics pinned here: a joined caller gets the starter's object in L1-only mode and under
``@cache.local()``, and its own decoded copy in backed mode. It counts as an L1 hit in
``cache_info()`` when it gets the value, and not at all when the shared call raised.
"""

from __future__ import annotations

import asyncio
import contextvars
import os
import signal
import sys
import threading
import time
import traceback
import types
import uuid
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager
from datetime import timedelta
from typing import Any

import pytest
import time_machine

from cachekit import cache
from cachekit import object_cache as object_cache_module
from cachekit.backends.errors import BackendError
from cachekit.cache_handler import CacheSerializationHandler, VerifiedMsgpack
from cachekit.config.nested import L1CacheConfig
from cachekit.decorators import single_flight
from cachekit.decorators.single_flight import AsyncFlights, ThreadFlights
from cachekit.decorators.tenant_context import ContextVarExtractor
from cachekit.l1_cache import L1Cache, get_l1_cache_manager
from cachekit.reliability.circuit_breaker import CircuitBreaker, CircuitState

pytestmark = pytest.mark.unit

HERD = 20
ASYNC_MODES = ("l1_only", "local", "backed")
SYNC_MODES = ("l1_only", "local", "backed")

_scope: contextvars.ContextVar[str] = contextvars.ContextVar("_scope", default="")
MASTER_KEY = "61" * 32
TENANT_A = "550e8400-e29b-41d4-a716-446655440000"
TENANT_B = "6ba7b810-9dad-11d1-80b4-00c04fd430c8"


class _Backend:
    """In-memory lockable backend that records every request reaching it."""

    def __init__(self) -> None:
        self.store: dict[str, bytes] = {}
        self.requests: list[str] = []

    def _key(self, key: str) -> str:
        return key

    def get(self, key: str) -> bytes | None:
        self.requests.append("get")
        return self.store.get(self._key(key))

    def set(self, key: str, value: bytes, ttl: int | None = None, stale_ttl: int | None = None) -> None:
        self.requests.append("set")
        self.store[self._key(key)] = bytes(value)

    def delete(self, key: str) -> bool:
        self.requests.append("delete")
        return self.store.pop(self._key(key), None) is not None

    def exists(self, key: str) -> bool:
        self.requests.append("exists")
        return self._key(key) in self.store

    def health_check(self) -> tuple[bool, dict[str, Any]]:
        return True, {}

    @asynccontextmanager
    async def acquire_lock(self, key: str, timeout: float, blocking_timeout: float | None = None) -> AsyncIterator[bool]:
        self.requests.append("lock")
        yield True


class _ScopedBackend(_Backend):
    """Prefixes every key with the calling context's scope, as a tenant-scoped backend does."""

    @property
    def key_prefix(self) -> str:
        return _scope.get()

    def _key(self, key: str) -> str:
        return f"{self.key_prefix}{key}"


def _decorator(mode: str, backend: Any = None) -> Callable[[Any], Any]:
    if mode == "l1_only":
        return cache(backend=None, ttl=60)
    if mode == "l1_only_swr_off":
        return cache(backend=None, ttl=60, l1=L1CacheConfig(swr_enabled=False))
    if mode == "local":
        return cache.local(ttl=60)
    # A namespace per decoration: backed-mode L1 is shared by namespace across decorations.
    return cache(backend=backend if backend is not None else _Backend(), ttl=60, namespace=f"sf-{uuid.uuid4().hex}")


class _Body:
    """The cached function's body: counts its runs and holds each one at a gate until released."""

    def __init__(self, error: Exception | None = None) -> None:
        self.runs = 0
        self.error = error
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    def decorate(self, mode: str, backend: Any = None) -> Any:
        async def compute(x: int) -> dict[str, int]:
            self.runs += 1
            self.entered.set()
            await self.release.wait()
            if self.error is not None:
                raise self.error
            return {"x": x}

        return _decorator(mode, backend)(compute)

    async def herd(self, fn: Any, n: int = HERD) -> list[asyncio.Task[Any]]:
        """Start n concurrent calls on one key and return once the first run is held at the gate."""
        tasks = [asyncio.create_task(fn(1)) for _ in range(n)]
        await asyncio.wait_for(self.entered.wait(), 5)
        for _ in range(10):  # time for any caller that would run the function to reach it
            await asyncio.sleep(0)
        return tasks


@pytest.fixture(autouse=True)
def _clear_l1() -> Iterator[None]:
    yield
    get_l1_cache_manager().clear_all()


@pytest.fixture
def advance(monkeypatch: pytest.MonkeyPatch) -> Callable[[float], None]:
    """Move ObjectCache's clock forward, and nothing else's (the event loop keeps real time)."""
    offset = 0.0
    real = time.monotonic

    def monotonic() -> float:
        return real() + offset

    monkeypatch.setattr(object_cache_module, "time", types.SimpleNamespace(monotonic=monotonic))

    def shift(seconds: float) -> None:
        nonlocal offset
        offset += seconds

    return shift


# --------------------------------------------------------------------------------------------
# Async: one call per herd, every mode
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize("mode", ASYNC_MODES)
async def test_cold_herd_runs_the_function_once(mode: str) -> None:
    body = _Body()
    fn = body.decorate(mode)
    before = fn.cache_info()

    tasks = await body.herd(fn)
    body.release.set()
    results = await asyncio.gather(*tasks)
    after = fn.cache_info()

    assert body.runs == 1
    assert results == [{"x": 1}] * HERD
    # cache_info(): the call that ran the function is the miss; every joined caller is an L1 hit.
    assert after.misses - before.misses == 1
    assert (after.hits - before.hits, after.l1_hits - before.l1_hits, after.l2_hits - before.l2_hits) == (HERD - 1, HERD - 1, 0)


@pytest.mark.parametrize("mode", ["l1_only", "l1_only_swr_off", "local"])
async def test_expired_herd_runs_the_function_once(mode: str, advance: Callable[[float], None]) -> None:
    """Past ttl both L1-only reads (get_with_swr with SWR on, get with it off) report a miss."""
    body = _Body()
    body.release.set()
    fn = body.decorate(mode)
    assert await fn(1) == {"x": 1}

    advance(61)  # past ttl=60: a hard expiry, not the refresh-ahead band
    body.release.clear()
    body.entered.clear()
    tasks = await body.herd(fn)
    body.release.set()
    results = await asyncio.gather(*tasks)

    assert body.runs == 2
    assert results == [{"x": 1}] * HERD


@pytest.mark.parametrize("mode", ["l1_only", "local"])
async def test_joined_callers_get_the_starters_object_in_process_modes(mode: str, advance: Callable[[float], None]) -> None:
    """L1-only and @cache.local() hand every caller one object, cold and past ttl, as a later hit does."""
    body = _Body()
    fn = body.decorate(mode)

    tasks = await body.herd(fn)
    body.release.set()
    cold = await asyncio.gather(*tasks)
    assert all(r is cold[0] for r in cold)
    assert await fn(1) is cold[0]

    advance(61)
    body.release.clear()
    body.entered.clear()
    tasks = await body.herd(fn)
    body.release.set()
    expired = await asyncio.gather(*tasks)
    assert all(r is expired[0] for r in expired)
    assert expired[0] is not cold[0]


async def test_backed_herd_makes_one_trip() -> None:
    """The 19 joined callers make no backend request: the herd costs what one call costs."""
    single_backend = _Backend()
    single = _Body()
    single.release.set()
    assert await single.decorate("backed", single_backend)(1) == {"x": 1}

    backend = _Backend()
    body = _Body()
    tasks = await body.herd(body.decorate("backed", backend))
    body.release.set()
    results = await asyncio.gather(*tasks)

    assert body.runs == 1
    assert results == [{"x": 1}] * HERD
    assert backend.requests.count("lock") == 1
    assert backend.requests == single_backend.requests


async def test_backed_joined_callers_get_their_own_copy() -> None:
    """Backed mode decodes a copy per caller, as its L1 and L2 hits do."""
    body = _Body()
    tasks = await body.herd(body.decorate("backed"))
    body.release.set()
    results = await asyncio.gather(*tasks)

    assert results == [{"x": 1}] * HERD
    assert len({id(r) for r in results}) == HERD


@pytest.mark.parametrize("mode", ASYNC_MODES)
async def test_failure_raises_in_every_caller_and_pins_nothing(mode: str) -> None:
    error = RuntimeError("upstream down")
    body = _Body(error=error)
    fn = body.decorate(mode)
    before = fn.cache_info()

    tasks = await body.herd(fn)
    body.release.set()
    results = await asyncio.gather(*tasks, return_exceptions=True)
    after = fn.cache_info()

    assert body.runs == 1
    assert all(r is error for r in results)
    # cache_info(): one miss, and a joined caller of a call that raised counts as neither.
    assert (after.misses - before.misses, after.hits - before.hits) == (1, 0)

    body.error = None  # nothing was cached, and the key is free: the next call runs the function
    assert await fn(1) == {"x": 1}
    assert body.runs == 2


@pytest.mark.parametrize("mode", ASYNC_MODES)
async def test_cancelling_the_starter_leaves_joined_callers_the_value(mode: str) -> None:
    body = _Body()
    fn = body.decorate(mode)

    tasks = await body.herd(fn)
    starter, joined = tasks[0], tasks[1:]
    starter.cancel()
    for _ in range(5):
        await asyncio.sleep(0)
    body.release.set()

    assert await asyncio.gather(*joined) == [{"x": 1}] * (HERD - 1)
    with pytest.raises(asyncio.CancelledError):
        await starter
    assert body.runs == 1
    assert await fn(1) == {"x": 1}  # the call ran to completion and filled the cache
    assert body.runs == 1


@pytest.mark.parametrize("mode", ASYNC_MODES)
async def test_calls_on_different_keys_run_concurrently(mode: str) -> None:
    both_in = asyncio.Event()
    entered = 0

    async def compute(x: int) -> int:
        nonlocal entered
        entered += 1
        if entered == 2:
            both_in.set()
        await asyncio.wait_for(both_in.wait(), 2)  # times out if the keys were serialised
        return x

    fn = _decorator(mode)(compute)
    assert await asyncio.gather(fn(1), fn(2)) == [1, 2]


async def test_backed_flight_identity_carries_the_backend_key_prefix() -> None:
    """Two concurrent calls on one cache key under two key prefixes are two trips."""
    both_in = asyncio.Event()
    entered = 0

    async def compute(x: int) -> str:
        nonlocal entered
        entered += 1
        if entered == 2:
            both_in.set()
        await asyncio.wait_for(both_in.wait(), 2)  # times out if the scopes shared one call
        return _scope.get()

    fn = _decorator("backed", _ScopedBackend())(compute)

    async def under(scope: str) -> str:
        _scope.set(scope)
        return await fn(1)

    assert await asyncio.gather(under("t:a:"), under("t:b:")) == ["t:a:", "t:b:"]
    assert entered == 2


async def test_backed_flight_identity_carries_the_tenant_in_multi_tenant_encryption() -> None:
    """With a context tenant the cache key has no tenant: two tenants never share a call, its value or its error."""
    extractor = ContextVarExtractor()
    both_in = asyncio.Event()
    entered = 0

    async def compute(x: int) -> str:
        nonlocal entered
        entered += 1
        if entered == 2:
            both_in.set()
        await asyncio.wait_for(both_in.wait(), 2)  # times out if the tenants shared one call
        tenant = extractor.extract((), {})
        if x == 2:
            raise RuntimeError(f"failed for {tenant}")
        return tenant

    fn = cache.secure(
        master_key=MASTER_KEY, backend=_Backend(), ttl=60, namespace=f"sf-{uuid.uuid4().hex}", tenant_extractor=extractor
    )(compute)

    async def as_tenant(tenant: str, x: int) -> Any:
        ContextVarExtractor.set_tenant_id(tenant)
        try:
            return await fn(x)
        except RuntimeError as e:
            return str(e)

    assert await asyncio.gather(as_tenant(TENANT_A, 1), as_tenant(TENANT_B, 1)) == [TENANT_A, TENANT_B]
    entered = 0
    both_in.clear()
    assert await asyncio.gather(as_tenant(TENANT_A, 2), as_tenant(TENANT_B, 2)) == [
        f"failed for {TENANT_A}",
        f"failed for {TENANT_B}",
    ]


@pytest.mark.parametrize("mode", ASYNC_MODES)
async def test_a_read_after_invalidation_does_not_join_an_earlier_miss(mode: str) -> None:
    version, runs = 1, 0
    entered = asyncio.Event()
    release = asyncio.Event()

    async def compute(x: int) -> int:
        nonlocal runs
        runs += 1
        seen = version
        entered.set()
        if runs == 1:
            await release.wait()
        return seen

    fn = _decorator(mode)(compute)
    first = asyncio.create_task(fn(1))
    await asyncio.wait_for(entered.wait(), 5)

    version = 2
    await fn.ainvalidate_cache(1)
    assert await asyncio.wait_for(fn(1), 5) == 2  # its own call, not the one that read version 1
    release.set()
    assert await first == 1


class _RacingDeleteBackend(_Backend):
    """Holds its delete open, and every read issued once holding is on, so a trip can start mid-invalidation."""

    def __init__(self) -> None:
        super().__init__()
        self.deleting = threading.Event()
        self.finish_delete = threading.Event()
        self.hold_reads = False
        self.reads_held = 0
        self.release_reads = threading.Event()

    def get(self, key: str) -> bytes | None:
        value = super().get(key)  # read first, as a GET that left before the delete landed
        if self.hold_reads:
            self.reads_held += 1
            self.release_reads.wait(5)
        return value

    def delete(self, key: str) -> bool:
        self.deleting.set()
        self.finish_delete.wait(5)
        return super().delete(key)


async def test_a_read_after_invalidation_does_not_join_a_trip_that_started_during_it() -> None:
    """A trip that read the old L2 entry while the delete ran is forgotten once the delete is done."""
    version = 100

    async def compute(x: int) -> int:
        return version

    backend = _RacingDeleteBackend()
    fn = cache(backend=backend, ttl=60, l1_enabled=False, namespace=f"sf-{uuid.uuid4().hex}")(compute)
    assert await fn(1) == 100

    version = 200
    invalidation = asyncio.create_task(fn.ainvalidate_cache(1))
    assert await asyncio.to_thread(backend.deleting.wait, 5)
    backend.hold_reads = True
    during = asyncio.create_task(fn(1))  # reads the old entry, then is held in flight
    await _until_async(lambda: backend.reads_held == 1)
    backend.finish_delete.set()
    await invalidation

    after = asyncio.create_task(fn(1))
    for _ in range(10):
        await asyncio.sleep(0)
    backend.release_reads.set()
    assert await asyncio.wait_for(after, 5) == 200
    assert await during == 100  # it overlapped the invalidation


@pytest.mark.parametrize("invalidate_args", [(1,), ()], ids=["one-key", "whole-function"])
async def test_a_cancelled_invalidation_still_forgets_trips_that_started_during_its_delete(
    invalidate_args: tuple[int, ...], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The worker running the delete forgets the calls in flight after it, even when the caller
    awaiting it was cancelled: a read after the delete never joins a trip that read the old entry."""
    forgets = 0
    real_forget = AsyncFlights.forget

    def counting_forget(self: AsyncFlights, cache_keys: Any) -> None:
        nonlocal forgets
        real_forget(self, cache_keys)
        forgets += 1

    monkeypatch.setattr(AsyncFlights, "forget", counting_forget)
    version = 100

    async def compute(x: int) -> int:
        return version

    backend = _RacingDeleteBackend()
    fn = cache(backend=backend, ttl=60, l1_enabled=False, namespace=f"sf-{uuid.uuid4().hex}")(compute)
    assert await fn(1) == 100

    version = 200
    invalidation = asyncio.create_task(fn.ainvalidate_cache(*invalidate_args))
    assert await asyncio.to_thread(backend.deleting.wait, 5)
    backend.hold_reads = True
    during = asyncio.create_task(fn(1))  # reads the old entry, then is held in flight
    await _until_async(lambda: backend.reads_held == 1)
    invalidation.cancel()  # while its worker is still deleting
    with pytest.raises(asyncio.CancelledError):
        await invalidation
    backend.finish_delete.set()
    await _until_async(lambda: forgets == 1)  # the worker forgot after its delete

    after = asyncio.create_task(fn(1))
    for _ in range(10):
        await asyncio.sleep(0)
    backend.release_reads.set()
    assert await asyncio.wait_for(after, 5) == 200
    assert await during == 100  # it overlapped the invalidation


async def _until_async(predicate: Callable[[], bool], timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "timed out"
        await asyncio.sleep(0.005)


@pytest.mark.parametrize("mode", ASYNC_MODES)
async def test_joined_callers_run_again_when_the_shared_call_is_cancelled_under_them(mode: str) -> None:
    """The call raised CancelledError (something else cancelled what it awaited); no joined caller was cancelled."""
    body = _Body()
    error_on_first = [asyncio.CancelledError()]

    async def compute(x: int) -> dict[str, int]:
        body.runs += 1
        body.entered.set()
        await body.release.wait()
        if error_on_first:
            raise error_on_first.pop()
        return {"x": x}

    fn = _decorator(mode)(compute)
    tasks = await body.herd(fn)
    body.release.set()
    results = await asyncio.gather(*tasks, return_exceptions=True)

    assert isinstance(results[0], asyncio.CancelledError)  # the starter's own call raised it, as before
    assert results[1:] == [{"x": 1}] * (HERD - 1)
    # Each joined caller ran its own call, as it would have alone; a backed one may instead find
    # the value another already stored.
    assert 1 < body.runs <= HERD


@pytest.mark.parametrize("mode", ["l1_only", "local", "backed"])
async def test_a_cancelled_lone_caller_returns_after_its_call_unwound(mode: str) -> None:
    """With nobody left waiting the call is cancelled, as an unshared one would be, and its cleanup (a lock
    release, say) runs before the caller moves on; nothing is cached, so the next call runs again."""
    events: list[str] = []
    runs = 0

    async def compute(x: int) -> int:
        nonlocal runs
        runs += 1
        if runs == 1:
            try:
                await asyncio.sleep(60)
            finally:
                events.append("cleanup")
        return x

    fn = _decorator(mode)(compute)
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(fn(1), 0.05)
    assert events == ["cleanup"]
    assert await asyncio.wait_for(fn(1), 5) == 1  # not joined to the cancelled call: a fresh one
    assert runs == 2


async def test_joined_callers_leave_no_half_open_probe_slot_spent(live_breakers: list[CircuitBreaker]) -> None:
    """A joined caller hands its probe slot back: the herd leaves the slots one call leaves."""
    single = _Body()
    single.release.set()
    single_fn = single.decorate("backed")
    body = _Body()
    herd_fn = body.decorate("backed")
    single_breaker, herd_breaker = live_breakers

    with time_machine.travel(1_000_000.0, tick=False) as clock:
        for breaker in live_breakers:
            for _ in range(breaker.config.failure_threshold):
                breaker.record_failure()
            assert breaker.state == CircuitState.OPEN
        clock.shift(timedelta(seconds=single_breaker.config.timeout_seconds + 1))

        assert await single_fn(1) == {"x": 1}
        tasks = await body.herd(herd_fn)
        body.release.set()
        results = await asyncio.gather(*tasks)

        assert body.runs == 1  # no caller was rejected past the budget and run uncached
        assert results == [{"x": 1}] * HERD
        assert herd_breaker.state == single_breaker.state == CircuitState.HALF_OPEN
        assert _free_probe_slots(herd_breaker) == _free_probe_slots(single_breaker) > 0


def _free_probe_slots(breaker: CircuitBreaker) -> int:
    free = 0
    while breaker.admit() is not None and free <= breaker.config.half_open_requests:
        free += 1
    return free


async def test_same_key_reentry_from_inside_the_call_runs_unshared() -> None:
    """A call that re-enters its own key would wait on itself: it runs unshared, as before."""
    depth = 0

    @cache(backend=None, ttl=60)
    async def compute(x: int) -> int:
        nonlocal depth
        if depth == 0:
            depth = 1
            return await compute(x) + 1
        return 1

    assert await asyncio.wait_for(compute(1), 5) == 2


@pytest.mark.parametrize("mode", ASYNC_MODES)
async def test_same_key_reentry_through_another_cached_call_runs_unshared(mode: str) -> None:
    """compute(1) awaits compute(2), which awaits compute(1). compute(2)'s miss runs in a task of its
    own, but the inner compute(1) is still inside compute(1)'s call: it must not wait on that call."""
    entered: set[int] = set()

    @_decorator(mode)
    async def compute(x: int) -> int:
        if x in entered:  # the guard that ends the cycle
            return 0
        entered.add(x)
        return await compute(3 - x) + x

    assert await asyncio.wait_for(compute(1), 5) == 3


@pytest.mark.parametrize("mode", ASYNC_MODES)
async def test_same_key_reentry_from_a_task_the_call_starts_runs_unshared(mode: str) -> None:
    depth = 0

    @_decorator(mode)
    async def compute(x: int) -> int:
        nonlocal depth
        if depth == 0:
            depth = 1
            (inner,) = await asyncio.gather(compute(x))  # gather runs it in a task of its own
            return inner + 1
        return 1

    assert await asyncio.wait_for(compute(1), 5) == 2


@pytest.mark.parametrize("mode", ASYNC_MODES)
async def test_a_task_the_call_started_shares_its_misses_once_the_call_has_ended(mode: str) -> None:
    """A task started inside compute(1)'s call that outlives it is no longer inside it: its later
    miss on the same key joins the call in flight, like any other caller's."""
    runs = 0
    go = asyncio.Event()
    release = asyncio.Event()
    started: list[asyncio.Task[int]] = []

    @_decorator(mode)
    async def compute(x: int) -> int:
        nonlocal runs
        runs += 1
        if runs == 1:

            async def later() -> int:
                await go.wait()
                return await compute(x)

            started.append(asyncio.create_task(later()))  # outlives this call
            return 0
        await release.wait()
        return runs

    assert await compute(1) == 0
    await compute.ainvalidate_cache(1)
    herd = asyncio.create_task(compute(1))
    for _ in range(10):  # the herd's call is running
        await asyncio.sleep(0)
    go.set()
    for _ in range(10):  # time for the later task to reach the call, or to run its own
        await asyncio.sleep(0)
    release.set()
    assert await asyncio.wait_for(asyncio.gather(herd, started[0]), 5) == [2, 2]
    assert runs == 2


@pytest.mark.parametrize("mode", ASYNC_MODES)
async def test_a_self_rescheduling_chain_carries_no_ended_calls(mode: str) -> None:
    """Each call starts the next generation from a task that outlives it. A call inherits only the
    calls still running, so what it carries stays one deep however long the chain runs."""
    generations = 8
    depths: list[int] = []
    gates = [asyncio.Event() for _ in range(generations)]
    later: list[asyncio.Task[int]] = []

    @_decorator(mode)
    async def compute(x: int) -> int:
        depths.append(len(single_flight._inside.get()))  # pyright: ignore[reportPrivateUsage]
        if x + 1 < generations:

            async def next_generation() -> int:
                await gates[x].wait()
                return await compute(x + 1)

            later.append(asyncio.create_task(next_generation()))  # outlives this call
        return x

    assert await compute(0) == 0
    for x in range(generations - 1):
        gates[x].set()  # call x has returned, so it has ended
        assert await asyncio.wait_for(later[x], 5) == x + 1
    assert depths == [1] * generations


def test_a_caller_on_another_event_loop_runs_its_own_call() -> None:
    """A caller never awaits a task bound to another thread's loop."""
    started = threading.Event()
    release = threading.Event()
    runs: list[int] = []

    @cache(backend=None, ttl=60)
    async def compute(x: int) -> int:
        runs.append(threading.get_ident())
        if len(runs) == 1:
            started.set()
            await asyncio.to_thread(release.wait, 5)
        return x

    other = threading.Thread(target=lambda: asyncio.run(compute(1)))
    other.start()
    try:
        assert started.wait(5)
        assert asyncio.run(asyncio.wait_for(compute(1), 5)) == 1
    finally:
        release.set()
        other.join(5)
    assert len(runs) == 2


# --------------------------------------------------------------------------------------------
# Sync: threads
# --------------------------------------------------------------------------------------------


class _WaitCountingLock:
    """A flight's settled lock that counts the threads that wait on it."""

    def __init__(self, inner: Any, waiting: list[int], guard: threading.Lock) -> None:
        self._inner, self._waiting, self._guard = inner, waiting, guard

    def release(self) -> None:
        self._inner.release()

    def __enter__(self) -> _WaitCountingLock:
        with self._guard:
            self._waiting[0] += 1
        self._inner.acquire()
        return self

    def __exit__(self, *exc: object) -> None:
        self._inner.release()


@pytest.fixture
def waiting(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """How many threads have waited on a sync call in flight, so a body can hold until all joined."""
    count = [0]
    guard = threading.Lock()

    class _CountingFlight(single_flight._Flight):  # pyright: ignore[reportPrivateUsage]
        def __init__(self) -> None:
            super().__init__()
            self.settled = _WaitCountingLock(self.settled, count, guard)  # type: ignore[assignment]

    monkeypatch.setattr(single_flight, "_Flight", _CountingFlight)
    return count


def _until(predicate: Callable[[], bool], timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.005)


def _thread_herd(fn: Callable[[int], Any], caught: Callable[[], object] | None = None) -> list[Any]:
    """Call fn(1) from HERD threads at once; each slot holds the value or the exception raised.
    ``caught`` runs in a worker once it has caught an exception."""
    results: list[Any] = [None] * HERD
    barrier = threading.Barrier(HERD)

    def worker(i: int) -> None:
        barrier.wait(5)
        try:
            results[i] = fn(1)
        except Exception as e:
            results[i] = e
            if caught is not None:
                caught()

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(HERD)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    assert not any(t.is_alive() for t in threads)
    return results


@pytest.mark.parametrize("mode", SYNC_MODES)
def test_sync_cold_herd_runs_the_function_once(mode: str, waiting: list[int]) -> None:
    runs = 0

    @_decorator(mode)
    def compute(x: int) -> dict[str, int]:
        nonlocal runs
        runs += 1
        _until(lambda: waiting[0] == HERD - 1)  # every other thread joined this call
        return {"x": x}

    before = compute.cache_info()
    results = _thread_herd(compute)
    after = compute.cache_info()

    assert runs == 1
    assert results == [{"x": 1}] * HERD
    if mode == "backed":
        assert len({id(r) for r in results}) == HERD  # each caller decodes its own copy, as an L1 hit does
    else:
        assert all(r is results[0] for r in results)
    # cache_info(): the call that ran the function is the miss; every joined caller is an L1 hit.
    assert after.misses - before.misses == 1
    assert (after.hits - before.hits, after.l1_hits - before.l1_hits, after.l2_hits - before.l2_hits) == (HERD - 1, HERD - 1, 0)


@pytest.mark.parametrize("mode", SYNC_MODES)
def test_sync_failure_raises_in_every_caller_and_pins_nothing(mode: str, waiting: list[int]) -> None:
    error = RuntimeError("upstream down")
    runs = 0

    @_decorator(mode)
    def compute(x: int) -> dict[str, int]:
        nonlocal runs
        runs += 1
        if runs == 1:
            _until(lambda: waiting[0] == HERD - 1)
            raise error
        return {"x": x}

    before = compute.cache_info()
    results = _thread_herd(compute)
    after = compute.cache_info()

    assert runs == 1
    assert all(r is error for r in results)
    # cache_info(): one miss, and a joined caller of a call that raised counts as neither.
    assert (after.misses - before.misses, after.hits - before.hits) == (1, 0)
    assert compute(1) == {"x": 1}  # nothing was cached, and the key is free: the next call runs the function
    assert runs == 2


def test_sync_backed_herd_makes_one_trip(waiting: list[int]) -> None:
    """The 19 joined threads make no backend request: the herd costs what one call costs."""

    def compute(x: int) -> dict[str, int]:
        _until(lambda: waiting[0] == HERD - 1)  # every other thread joined this call
        return {"x": x}

    single_backend = _Backend()
    assert _decorator("backed", single_backend)(lambda x: {"x": x})(1) == {"x": 1}
    backend = _Backend()
    results = _thread_herd(_decorator("backed", backend)(compute))

    assert results == [{"x": 1}] * HERD
    assert backend.requests == single_backend.requests == ["get", "set"]  # no lock: the sync path takes none


def test_sync_backed_herd_shares_one_trip_while_the_backend_is_down(waiting: list[int]) -> None:
    """A read that fails reads as a miss and a write that fails still leaves the serialized value, so
    a herd on a backend that is down still runs the function once and makes one read and one write."""

    class _Down(_Backend):
        def get(self, key: str) -> bytes | None:
            self.requests.append("get")
            raise BackendError("down")

        def set(self, key: str, value: bytes, ttl: int | None = None, stale_ttl: int | None = None) -> None:
            self.requests.append("set")
            raise BackendError("down")

    runs = 0

    def compute(x: int) -> dict[str, int]:
        nonlocal runs
        runs += 1
        _until(lambda: waiting[0] == HERD - 1)
        return {"x": x}

    backend = _Down()
    results = _thread_herd(_decorator("backed", backend)(compute))

    assert runs == 1
    assert results == [{"x": 1}] * HERD
    assert len({id(r) for r in results}) == HERD
    assert backend.requests == ["get", "set"]


def _settle_another_trip_first(monkeypatch: pytest.MonkeyPatch, fn: Callable[[int], Any]) -> None:
    """Make fn's next flight lookup run another caller's whole trip on key 1 first, on this thread:
    a trip that settles between the caller's own L1 lookup and its flight lookup."""
    real_run = ThreadFlights.run
    lookups = 0

    def run_after_another_trip(self: ThreadFlights, *args: Any) -> Any:
        nonlocal lookups
        lookups += 1
        if lookups == 1:
            assert fn(1) == {"x": 1}  # another caller's trip, start to finish
        return real_run(self, *args)

    monkeypatch.setattr(ThreadFlights, "run", run_after_another_trip)


@pytest.mark.parametrize("l1_enabled", [True, False], ids=["l1-on", "l1-off"])
def test_sync_backed_recheck_after_a_trip_settled_since_the_callers_l1_lookup(
    l1_enabled: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With L1 on, the re-check finds the envelope that trip left in L1: the caller decodes it and
    makes no backend request. With L1 off there is nothing to re-check under the flight map's lock,
    so the caller reads L2 itself."""
    runs = 0

    def compute(x: int) -> dict[str, int]:
        nonlocal runs
        runs += 1
        return {"x": x}

    backend = _Backend()
    fn = cache(backend=backend, ttl=60, l1_enabled=l1_enabled, namespace=f"sf-{uuid.uuid4().hex}")(compute)
    _settle_another_trip_first(monkeypatch, fn)
    before = fn.cache_info()
    assert fn(1) == {"x": 1}
    after = fn.cache_info()

    assert runs == 1
    counts = (after.misses - before.misses, after.l1_hits - before.l1_hits, after.l2_hits - before.l2_hits)
    if l1_enabled:
        assert backend.requests == ["get", "set"]  # the other trip's: the re-check found its L1 entry
        assert counts == (1, 1, 0)
    else:
        assert backend.requests == ["get", "set", "get"]  # its own L2 read hit what the other trip stored
        assert counts == (1, 0, 1)


@pytest.fixture
def decoded(monkeypatch: pytest.MonkeyPatch) -> list[type]:
    """The type of every entry a backed-mode handler decodes: VerifiedMsgpack from L1, bytes for an envelope."""
    seen: list[type] = []
    real = CacheSerializationHandler.deserialize_data

    def recording(self: CacheSerializationHandler, data: Any, *args: Any, **kwargs: Any) -> Any:
        seen.append(type(data))
        return real(self, data, *args, **kwargs)

    monkeypatch.setattr(CacheSerializationHandler, "deserialize_data", recording)
    return seen


def test_sync_backed_recheck_decodes_a_verified_msgpack_l1_entry_without_a_trip(
    monkeypatch: pytest.MonkeyPatch, decoded: list[type]
) -> None:
    """Another caller's trip and then its L1 hit settle before this caller's flight lookup: the hit left the
    value's verified MessagePack in L1 (deserialize_l1). The re-check hands it to _decode_shared, which decodes
    it without retrieve() and without a trip of its own."""
    runs = 0

    def compute(x: int) -> dict[str, int]:
        nonlocal runs
        runs += 1
        return {"x": x}

    backend = _Backend()
    fn = _decorator("backed", backend)(compute)
    real_run = ThreadFlights.run
    lookups = 0

    def run_after_another_trip_and_hit(self: ThreadFlights, *args: Any) -> Any:
        nonlocal lookups
        lookups += 1
        if lookups == 1:
            assert fn(1) == fn(1) == {"x": 1}  # another caller's trip, then its L1 hit
        return real_run(self, *args)

    monkeypatch.setattr(ThreadFlights, "run", run_after_another_trip_and_hit)
    assert fn(1) == {"x": 1}

    assert (runs, backend.requests) == (1, ["get", "set"])  # one function call and one backend read in all
    assert decoded == [VerifiedMsgpack]


def test_sync_backed_joiner_decodes_the_trips_envelope(waiting: list[int], decoded: list[type]) -> None:
    """A joined caller decodes its own copy of the trip's envelope, as before: never the L1 entry."""

    def compute(x: int) -> dict[str, int]:
        _until(lambda: waiting[0] == HERD - 1)
        return {"x": x}

    backend = _Backend()
    results = _thread_herd(_decorator("backed", backend)(compute))

    assert results == [{"x": 1}] * HERD
    assert backend.requests == ["get", "set"]
    assert decoded == [bytes] * (HERD - 1)


def test_sync_backed_recheck_hit_hands_its_half_open_probe_slot_back(
    live_breakers: list[CircuitBreaker], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A caller the re-check answers never waited on a call, so on_join never ran for it: it hands its
    probe slot back afterwards. The pair of calls leaves the slots one call leaves."""
    single_fn = _decorator("backed")(lambda x: {"x": x})
    fn = _decorator("backed")(lambda x: {"x": x})
    single_breaker, breaker = live_breakers

    with time_machine.travel(1_000_000.0, tick=False) as clock:
        for each in live_breakers:
            for _ in range(each.config.failure_threshold):
                each.record_failure()
        clock.shift(timedelta(seconds=single_breaker.config.timeout_seconds + 1))

        assert single_fn(1) == {"x": 1}
        _settle_another_trip_first(monkeypatch, fn)
        assert fn(1) == {"x": 1}

        assert breaker.state == single_breaker.state == CircuitState.HALF_OPEN
        assert _free_probe_slots(breaker) == _free_probe_slots(single_breaker) > 0


def test_sync_backed_joiner_of_an_interrupted_trip_is_admitted_afresh(
    live_breakers: list[CircuitBreaker], monkeypatch: pytest.MonkeyPatch, waiting: list[int]
) -> None:
    """A starter ended by a non-Exception did not fail, so its joiner runs its own trip. The joiner
    handed its probe slot back when it joined, so that trip asks the breaker again."""

    class _Interrupt(BaseException):
        pass

    runs = 0

    def compute(x: int) -> dict[str, int]:
        nonlocal runs
        runs += 1
        if runs == 1:
            _until(lambda: waiting[0] == 1)  # the joiner waits on this call
            raise _Interrupt
        return {"x": x}

    fn = _decorator("backed")(compute)
    (breaker,) = live_breakers
    admits: list[int] = []
    real_admit = CircuitBreaker.admit

    def counting_admit(self: CircuitBreaker) -> int | None:
        if self is breaker:
            admits.append(1)
        return real_admit(self)

    monkeypatch.setattr(CircuitBreaker, "admit", counting_admit)
    results: dict[str, Any] = {}  # by caller: the joiner can finish before the starter records its interrupt

    def call(caller: str) -> None:
        try:
            results[caller] = fn(1)
        except _Interrupt as e:
            results[caller] = e

    starter = threading.Thread(target=call, args=("starter",))
    starter.start()
    _until(lambda: runs == 1)
    joiner = threading.Thread(target=call, args=("joiner",))
    joiner.start()
    starter.join(5)
    joiner.join(5)

    assert isinstance(results["starter"], _Interrupt)
    assert results["joiner"] == {"x": 1}
    assert runs == 2
    assert len(admits) == 3  # the starter, the joiner, and the joiner's own trip


def test_sync_backed_flight_identity_carries_the_backend_key_prefix() -> None:
    """Two concurrent calls on one cache key under two key prefixes are two trips."""
    both_in = threading.Barrier(2, timeout=2)  # breaks if the scopes shared one call

    def compute(x: int) -> str:
        both_in.wait()
        return _scope.get()

    fn = _decorator("backed", _ScopedBackend())(compute)
    results: dict[str, str] = {}

    def under(scope: str) -> None:
        _scope.set(scope)  # each thread runs in a context of its own
        results[scope] = fn(1)

    threads = [threading.Thread(target=under, args=(scope,)) for scope in ("t:a:", "t:b:")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(5)
    assert results == {"t:a:": "t:a:", "t:b:": "t:b:"}


def test_sync_backed_flight_identity_carries_the_tenant_in_multi_tenant_encryption() -> None:
    """With a context tenant the cache key has no tenant: two tenants never share a call, its value or its error."""
    extractor = ContextVarExtractor()
    both_in = threading.Barrier(2, timeout=2)  # breaks if the tenants shared one call

    def compute(x: int) -> str:
        both_in.wait()
        tenant = extractor.extract((), {})
        if x == 2:
            raise RuntimeError(f"failed for {tenant}")
        return tenant

    fn = cache.secure(
        master_key=MASTER_KEY, backend=_Backend(), ttl=60, namespace=f"sf-{uuid.uuid4().hex}", tenant_extractor=extractor
    )(compute)

    def herd_of_two(x: int) -> dict[str, Any]:
        results: dict[str, Any] = {}

        def as_tenant(tenant: str) -> None:
            ContextVarExtractor.set_tenant_id(tenant)
            try:
                results[tenant] = fn(x)
            except RuntimeError as e:
                results[tenant] = str(e)

        threads = [threading.Thread(target=as_tenant, args=(tenant,)) for tenant in (TENANT_A, TENANT_B)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(5)
        return results

    assert herd_of_two(1) == {TENANT_A: TENANT_A, TENANT_B: TENANT_B}
    assert herd_of_two(2) == {TENANT_A: f"failed for {TENANT_A}", TENANT_B: f"failed for {TENANT_B}"}


def test_sync_joined_callers_leave_no_half_open_probe_slot_spent(
    live_breakers: list[CircuitBreaker], waiting: list[int]
) -> None:
    """A joined thread hands its probe slot back before it waits: the herd leaves the slots one call
    leaves. The threads join one at a time, so no admission races another's hand-back."""
    release = threading.Event()
    runs = 0

    def compute(x: int) -> dict[str, int]:
        nonlocal runs
        runs += 1
        release.wait(5)
        return {"x": x}

    single_fn = _decorator("backed")(lambda x: {"x": x})
    herd_fn = _decorator("backed")(compute)
    single_breaker, herd_breaker = live_breakers
    results: list[Any] = [None] * HERD

    def worker(i: int) -> None:
        results[i] = herd_fn(1)

    with time_machine.travel(1_000_000.0, tick=False) as clock:
        for breaker in live_breakers:
            for _ in range(breaker.config.failure_threshold):
                breaker.record_failure()
            assert breaker.state == CircuitState.OPEN
        clock.shift(timedelta(seconds=single_breaker.config.timeout_seconds + 1))

        assert single_fn(1) == {"x": 1}
        threads = [threading.Thread(target=worker, args=(i,)) for i in range(HERD)]
        threads[0].start()
        _until(lambda: runs == 1)
        for i in range(1, HERD):
            threads[i].start()
            _until(lambda i=i: waiting[0] == i)  # joined, so its slot is back before the next admission
        release.set()
        for t in threads:
            t.join(5)

        assert runs == 1  # no caller was rejected past the budget and run uncached
        assert results == [{"x": 1}] * HERD
        assert herd_breaker.state == single_breaker.state == CircuitState.HALF_OPEN
        assert _free_probe_slots(herd_breaker) == _free_probe_slots(single_breaker) > 0


def test_sync_joined_callers_take_their_own_trip_after_an_mmap_hit(tmp_path: Any, waiting: list[int]) -> None:
    """A hit read through the sync mmap fast path leaves no envelope to share: each joined caller then
    takes its own trip and reads L2 itself, as it would have alone. Nothing runs the function again."""
    pd = pytest.importorskip("pandas")
    from cachekit.backends.file import FileBackend
    from cachekit.backends.file.config import FileBackendConfig

    class _HoldingFileBackend(FileBackend):
        """Counts the hits read through the mmap, and holds the first until every other caller joined it."""

        def __init__(self, config: FileBackendConfig) -> None:
            super().__init__(config)
            self.mmap_hits: list[int] = []
            self.holding = False
            self.all_joined = False

        def get_buffer(self, key: str) -> Any:
            handle = super().get_buffer(key)
            if handle is not None:
                self.mmap_hits.append(1)
                if self.holding and len(self.mmap_hits) == 1:
                    # Not _until: a raise in here would be read as a failed L2 read, not a test failure.
                    deadline = time.monotonic() + 5
                    while waiting[0] < HERD - 1 and time.monotonic() < deadline:
                        time.sleep(0.005)
                    self.all_joined = waiting[0] == HERD - 1
            return handle

    runs = 0
    frame = pd.DataFrame({"x": [1, 2, 3]})

    def compute(x: int) -> Any:
        nonlocal runs
        runs += 1
        return frame.copy()

    backend = _HoldingFileBackend(FileBackendConfig(cache_dir=tmp_path))
    fn = cache(backend=backend, serializer="arrow", ttl=60, namespace=f"sf-{uuid.uuid4().hex}")(compute)
    fn(1)  # stored: a plaintext Arrow value streams to the File backend, so L1 holds nothing
    assert (runs, backend.mmap_hits) == (1, [])
    backend.holding = True
    before = fn.cache_info()

    results = _thread_herd(fn)
    after = fn.cache_info()

    assert backend.all_joined  # the first read was the trip every other caller joined
    assert runs == 1
    for result in results:
        pd.testing.assert_frame_equal(result, frame)
    assert len(backend.mmap_hits) == HERD  # the shared trip's read, then one per joined caller
    assert (after.l2_hits - before.l2_hits, after.l1_hits - before.l1_hits) == (HERD, 0)


@pytest.mark.parametrize("mode", SYNC_MODES)
def test_sync_failure_traceback_stays_one_callers(mode: str, waiting: list[int], monkeypatch: pytest.MonkeyPatch) -> None:
    """Each waiting thread raises from the saved traceback, so frames never pile up across threads.

    Every caller raises the one shared exception. Two callers unwinding at the same moment can
    splice their frames into each other's chain (on a free-threaded build especially), so the
    callers here raise one at a time: a token passes from the starter, as it settles the call,
    to each waiter as it leaves its wait, and back once the caller's worker has caught the
    exception. A pile-up would still grow the chain by one caller's frames per waiter."""
    error = RuntimeError("upstream down")
    token = threading.Lock()
    flight_type = single_flight._Flight  # pyright: ignore[reportPrivateUsage] - the waiting fixture's

    class _OneRaiseAtATime(flight_type):  # type: ignore[misc, valid-type]
        def __init__(self) -> None:
            super().__init__()
            settled = self.settled

            class _Settled:
                def release(self) -> None:  # the starter settling: it raises on, holding the token
                    token.acquire()
                    settled.release()

                def __enter__(self) -> object:
                    return settled.__enter__()

                def __exit__(self, *exc: object) -> None:  # a waiter leaving its wait, to raise
                    token.acquire()
                    settled.__exit__(*exc)

            self.settled = _Settled()  # type: ignore[assignment]

    monkeypatch.setattr(single_flight, "_Flight", _OneRaiseAtATime)

    @_decorator(mode)
    def compute(x: int) -> int:
        _until(lambda: waiting[0] == HERD - 1)
        raise error

    results = _thread_herd(compute, caught=token.release)
    assert all(r is error for r in results)
    assert len(traceback.extract_tb(error.__traceback__)) < 15


@pytest.mark.parametrize("mode", SYNC_MODES)
def test_sync_read_after_invalidation_does_not_join_an_earlier_miss(mode: str) -> None:
    version, runs = 1, 0
    entered = threading.Event()
    release = threading.Event()

    @_decorator(mode)
    def compute(x: int) -> int:
        nonlocal runs
        runs += 1
        seen = version
        if runs == 1:
            entered.set()
            release.wait(5)
        return seen

    first: list[int] = []
    t = threading.Thread(target=lambda: first.append(compute(1)))
    t.start()
    try:
        assert entered.wait(5)
        version = 2
        compute.invalidate_cache(1)
        later: list[int] = []
        u = threading.Thread(target=lambda: later.append(compute(1)))
        u.start()
        u.join(2)
        assert later == [2]  # its own call; joining the first would block until release
    finally:
        release.set()
        t.join(5)
    assert first == [1]


def test_sync_read_after_invalidation_does_not_join_a_trip_that_started_during_it() -> None:
    """A sync trip that read the old L2 entry while the delete ran is forgotten once the delete is done."""
    version = 100

    def compute(x: int) -> int:
        return version

    backend = _RacingDeleteBackend()
    fn = cache(backend=backend, ttl=60, l1_enabled=False, namespace=f"sf-{uuid.uuid4().hex}")(compute)
    assert fn(1) == 100

    version = 200
    invalidation = threading.Thread(target=fn.invalidate_cache, args=(1,))
    invalidation.start()
    during: list[int] = []
    after: list[int] = []
    threads = [invalidation]
    try:
        assert backend.deleting.wait(5)
        backend.hold_reads = True
        threads.append(threading.Thread(target=lambda: during.append(fn(1))))  # reads the old entry, then is held in flight
        threads[-1].start()
        _until(lambda: backend.reads_held == 1)
        backend.finish_delete.set()
        invalidation.join(5)
        assert not invalidation.is_alive()

        threads.append(threading.Thread(target=lambda: after.append(fn(1))))
        threads[-1].start()
        _until(lambda: backend.reads_held == 2)  # its own read: it did not join the trip that read the old entry
    finally:
        backend.finish_delete.set()
        backend.release_reads.set()
        for t in threads:
            t.join(5)
    assert after == [200]
    assert during == [100]  # it overlapped the invalidation


@pytest.mark.parametrize("mode", SYNC_MODES)
def test_sync_calls_on_different_keys_run_concurrently(mode: str) -> None:
    barrier = threading.Barrier(2, timeout=2)  # breaks if the keys were serialised

    @_decorator(mode)
    def compute(x: int) -> int:
        barrier.wait()
        return x

    results: dict[int, int] = {}
    threads = [threading.Thread(target=lambda x=x: results.__setitem__(x, compute(x))) for x in (1, 2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(5)
    assert results == {1: 1, 2: 2}


@pytest.mark.parametrize("mode", SYNC_MODES)
def test_sync_same_key_reentry_from_inside_the_call_runs_unshared(mode: str) -> None:
    depth = 0

    @_decorator(mode)
    def compute(x: int) -> int:
        nonlocal depth
        if depth == 0:
            depth = 1
            return compute(x) + 1
        return 1

    result: list[int] = []
    t = threading.Thread(target=lambda: result.append(compute(1)))
    t.start()
    t.join(5)
    assert result == [2]  # not a deadlock on its own call


# --------------------------------------------------------------------------------------------
# The helpers: fork and interruption
# --------------------------------------------------------------------------------------------


async def test_async_flights_forked_child_starts_with_an_empty_map(monkeypatch: pytest.MonkeyPatch) -> None:
    flights = AsyncFlights()
    gate = asyncio.Event()

    async def parent_call() -> str:
        await gate.wait()
        return "parent"

    async def child_call() -> str:
        return "child"

    parent = asyncio.create_task(flights.run(("k",), parent_call))
    await asyncio.sleep(0)
    monkeypatch.setattr(flights, "_pid", -1)  # as a forked child sees it
    assert await flights.run(("k",), child_call) == ("child", False)
    gate.set()
    assert await parent == ("parent", False)


def test_thread_flights_forked_child_starts_with_an_empty_map(monkeypatch: pytest.MonkeyPatch) -> None:
    flights = ThreadFlights()
    held = threading.Event()
    gate = threading.Event()

    def parent_call() -> str:
        held.set()
        gate.wait(5)
        return "parent"

    parent = threading.Thread(target=lambda: flights.run(("k",), parent_call, lambda: (False, None)))
    parent.start()
    try:
        assert held.wait(5)
        monkeypatch.setattr(flights, "_pid", -1)
        assert flights.run(("k",), lambda: "child", lambda: (False, None)) == ("child", False)
    finally:
        gate.set()
        parent.join(5)


@pytest.mark.skipif(not hasattr(os, "fork"), reason="needs os.fork")
@pytest.mark.filterwarnings("ignore:This process .* is multi-threaded:DeprecationWarning")
@pytest.mark.parametrize("flights_type", [AsyncFlights, ThreadFlights])
def test_forked_child_never_waits_on_a_lock_a_parent_thread_held(flights_type: type[Any]) -> None:
    """Every entry point checks the owner PID before taking the lock: forget() and run() in a child
    forked while another thread held the map's lock start on a fresh one instead of hanging."""
    flights = flights_type()
    held = threading.Event()
    release = threading.Event()

    def hold() -> None:
        with flights._lock:  # pyright: ignore[reportPrivateUsage]
            held.set()
            release.wait(10)

    holder = threading.Thread(target=hold)
    holder.start()
    try:
        assert held.wait(5)
        pid = os.fork()
        if pid == 0:  # the child: a hang is killed by the alarm, which fails the exit-code check
            signal.alarm(5)
            try:
                flights.forget(["k"])
                flights.forget(None)
                if flights_type is ThreadFlights:
                    flights.run(("k",), lambda: "v", lambda: (False, None))
                else:
                    asyncio.run(flights.run(("k",), _const_coro))
            except BaseException:
                os._exit(1)
            os._exit(0)
        _, status = os.waitpid(pid, 0)
        assert os.waitstatus_to_exitcode(status) == 0
    finally:
        release.set()
        holder.join(5)


async def _const_coro() -> str:
    return "v"


@pytest.mark.skipif(sys.version_info < (3, 12), reason="asyncio.eager_task_factory is new in Python 3.12")
async def test_async_flights_keep_out_a_call_that_ran_past_a_forget_before_its_insert() -> None:
    """Under an eager task factory, create_task runs the call up to its first await (a trip's L2
    read) before run() puts it in the map. An invalidation that forgets the key in that gap must
    keep it out of the map: a read that starts afterwards never joins a call that read before it."""
    flights = AsyncFlights()
    loop = asyncio.get_running_loop()
    release = asyncio.Event()

    async def read_then_invalidated() -> str:
        flights.forget(["k"])  # the invalidation lands after this call's read, before the insert
        await release.wait()
        return "old"

    loop.set_task_factory(asyncio.eager_task_factory)
    try:
        first = asyncio.ensure_future(flights.run(("k",), read_then_invalidated))
        assert await asyncio.wait_for(flights.run(("k",), _const_coro), 5) == ("v", False)
    finally:
        loop.set_task_factory(None)
        release.set()
    assert await first == ("old", False)


def test_l1_peek_reads_an_entry_without_counting_it_or_moving_it() -> None:
    """The backed re-check's L1 read: the caller's own lookup already counted its miss. Like put, it
    runs the fork take-over first, since a wrapper's first call in a child can reach it before any put."""
    take_overs: list[int] = []
    l1 = L1Cache(namespace=f"sf-{uuid.uuid4().hex}", before_store=lambda: take_overs.append(1))
    l1.put("a", b"A", redis_ttl=60)
    l1.put("b", b"B", redis_ttl=60)
    stats = l1.get_stats()

    assert (l1.peek("a"), l1.peek("missing")) == (b"A", None)
    assert len(take_overs) == 4  # two puts, two peeks
    assert (l1.get_stats()["hits"], l1.get_stats()["misses"]) == (stats["hits"], stats["misses"])
    assert list(l1._state.cache) == ["a", "b"]  # pyright: ignore[reportPrivateUsage] - not moved in LRU order

    l1.put("old", b"O", expires_at=time.time() - 1)
    assert l1.peek("old") is None  # expired reads as absent


def test_thread_flights_on_join_runs_once_per_join_before_the_caller_waits(waiting: list[int]) -> None:
    """The starter never calls it; a joiner calls it before it blocks; a re-check hit shares without it."""
    flights = ThreadFlights()
    held = threading.Event()
    gate = threading.Event()
    joins: list[int] = []

    def call() -> str:
        held.set()
        gate.wait(5)
        return "v"

    starter = threading.Thread(target=lambda: flights.run(("k",), call, lambda: (False, None), lambda: joins.append(1)))
    starter.start()
    assert held.wait(5)
    result: list[tuple[str, bool]] = []
    joiner = threading.Thread(
        target=lambda: result.append(flights.run(("k",), call, lambda: (False, None), lambda: joins.append(2)))
    )
    joiner.start()
    _until(lambda: waiting[0] == 1)
    assert joins == [2]  # before it blocked on the call
    gate.set()
    starter.join(5)
    joiner.join(5)
    assert result == [("v", True)]
    assert flights.run(("k",), call, lambda: (True, "stored"), lambda: joins.append(3)) == ("stored", True)
    assert joins == [2]


def test_thread_flights_recheck_finds_a_value_stored_after_the_callers_lookup() -> None:
    """A call that settled between the caller's lookup and its map check stored its value: no rerun."""
    flights = ThreadFlights()

    def call() -> str:
        raise AssertionError("the function ran again")

    assert flights.run(("k",), call, lambda: (True, "stored")) == ("stored", True)


def test_thread_flights_waiters_retry_after_an_interrupted_call(waiting: list[int]) -> None:
    """A call ended by a non-Exception did not fail: its waiter starts its own instead of raising."""

    class _Interrupt(BaseException):
        pass

    flights = ThreadFlights()
    held = threading.Event()
    gate = threading.Event()
    interrupted: list[BaseException] = []

    def interrupted_call() -> str:
        held.set()
        gate.wait(5)
        raise _Interrupt

    def starter() -> None:
        try:
            flights.run(("k",), interrupted_call, lambda: (False, None))
        except _Interrupt as e:
            interrupted.append(e)

    first = threading.Thread(target=starter)
    first.start()
    assert held.wait(5)
    result: list[tuple[str, bool]] = []
    second = threading.Thread(target=lambda: result.append(flights.run(("k",), lambda: "retried", lambda: (False, None))))
    second.start()
    _until(lambda: waiting[0] == 1)  # the second thread waits on the first call
    gate.set()
    first.join(5)
    second.join(5)

    assert len(interrupted) == 1
    assert result == [("retried", False)]
