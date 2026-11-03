"""A fill that read its source before an invalidation stores nothing once that invalidation has returned.

Each test holds a fill between its source read and its store, runs an invalidation to completion in
this process, releases the fill, and checks that no cache tier keeps the value read before it: the
next call computes again. The interleavings are fixed by events, not timing, so they hold on the
free-threaded build too.
"""

from __future__ import annotations

import asyncio
import threading
import uuid
from collections.abc import Callable
from contextlib import asynccontextmanager
from typing import Any

import pytest

from cachekit import cache
from cachekit.decorators import wrapper
from cachekit.decorators.single_flight import ThreadFlights

pytestmark = pytest.mark.unit

_WAIT = 10.0  # seconds: a bound on each held step, never reached when the code is right


class _Source:
    """The data a cached function reads, plus a gate that holds the next read's caller after it read."""

    def __init__(self) -> None:
        self.value = "old"
        self.reads = 0
        self._hold = False
        self.entered = threading.Event()
        self.release = threading.Event()

    def hold_next(self) -> None:
        self._hold = True
        self.entered.clear()
        self.release.clear()

    def read(self) -> str:
        value = self.value
        self.reads += 1
        if self._hold:
            self._hold = False
            self.entered.set()
            assert self.release.wait(_WAIT)
        return value

    async def aread(self) -> str:
        value = self.value
        self.reads += 1
        if self._hold:
            self._hold = False
            self.entered.set()
            assert await asyncio.to_thread(self.release.wait, _WAIT)
        return value


def _race_sync(call: Callable[[], Any], source: _Source, invalidate: Callable[[], Any], gate: _Source | None = None) -> Any:
    """Hold call() where gate holds it (by default, after its source read), change the source, run
    invalidate() to completion, then release it."""
    gate = gate or source
    if gate is source:
        source.hold_next()
    out: list[Any] = []
    t = threading.Thread(target=lambda: out.append(call()))
    t.start()
    assert gate.entered.wait(_WAIT)
    source.value = "new"
    invalidate()
    gate.release.set()
    t.join(_WAIT)
    assert not t.is_alive()
    return out[0]


async def _race_async(
    call: Callable[[], Any], source: _Source, invalidate: Callable[[], Any], gate: _Source | None = None
) -> Any:
    """_race_sync for a coroutine function; invalidate() may return an awaitable."""
    gate = gate or source
    if gate is source:
        source.hold_next()
    task = asyncio.ensure_future(call())
    assert await asyncio.to_thread(gate.entered.wait, _WAIT)
    source.value = "new"
    done = invalidate()
    if asyncio.iscoroutine(done):
        await done
    gate.release.set()
    return await asyncio.wait_for(task, _WAIT)


# ── L1-only (@cache(backend=None)) and @cache.local() ──────────────────────────────────────────

_SYNC_INVALIDATIONS = {
    "cache_clear": lambda fn: fn.cache_clear(),
    "invalidate_all": lambda fn: fn.invalidate_cache(),
    "invalidate_key": lambda fn: fn.invalidate_cache(1),
}
_ASYNC_INVALIDATIONS = {
    "cache_clear": lambda fn: fn.cache_clear(),
    "ainvalidate_all": lambda fn: fn.ainvalidate_cache(),
    "ainvalidate_key": lambda fn: fn.ainvalidate_cache(1),
}
_LOCAL_INVALIDATIONS = ("cache_clear", "invalidate_key")


@pytest.mark.parametrize("how", sorted(_SYNC_INVALIDATIONS))
def test_l1_only_sync_fill_after_invalidation_is_not_stored(how: str) -> None:
    source = _Source()

    @cache(backend=None)  # no ttl: a stored value would stay a year
    def get(x: int) -> str:
        return source.read()

    assert _race_sync(lambda: get(1), source, lambda: _SYNC_INVALIDATIONS[how](get)) == "old"
    assert get(1) == "new"
    assert get(1) == "new"  # and that one is stored
    assert source.reads == 2


@pytest.mark.parametrize("how", sorted(_ASYNC_INVALIDATIONS))
async def test_l1_only_async_fill_after_invalidation_is_not_stored(how: str) -> None:
    source = _Source()

    @cache(backend=None)
    async def get(x: int) -> str:
        return await source.aread()

    assert await _race_async(lambda: get(1), source, lambda: _ASYNC_INVALIDATIONS[how](get)) == "old"
    assert await get(1) == "new"
    assert await get(1) == "new"
    assert source.reads == 2


@pytest.mark.parametrize("how", _LOCAL_INVALIDATIONS)
def test_local_sync_fill_after_invalidation_is_not_stored(how: str) -> None:
    source = _Source()

    @cache.local()
    def get(x: int) -> str:
        return source.read()

    assert _race_sync(lambda: get(1), source, lambda: _SYNC_INVALIDATIONS[how](get)) == "old"
    assert get(1) == "new"
    assert get(1) == "new"
    assert source.reads == 2


@pytest.mark.parametrize("how", _LOCAL_INVALIDATIONS)
async def test_local_async_fill_after_invalidation_is_not_stored(how: str) -> None:
    source = _Source()

    @cache.local()
    async def get(x: int) -> str:
        return await source.aread()

    assert await _race_async(lambda: get(1), source, lambda: _SYNC_INVALIDATIONS[how](get)) == "old"
    assert await get(1) == "new"
    assert await get(1) == "new"
    assert source.reads == 2


def test_l1_only_fill_of_another_key_after_a_single_key_invalidation_is_not_stored() -> None:
    """The ObjectCache epoch is one per cache: invalidating key 2 drops the overlapping fill of key 1."""
    source = _Source()

    @cache(backend=None)
    def get(x: int) -> str:
        return source.read()

    assert _race_sync(lambda: get(1), source, lambda: get.invalidate_cache(2)) == "old"
    assert get(1) == "new"


# ── Backed ──────────────────────────────────────────────────────────────────────────────────────


class DictBackend:
    """A sync backend whose get can hold its caller after it read the entry."""

    def __init__(self) -> None:
        self.store: dict[str, bytes] = {}
        self.sets: list[str] = []
        self.stale = False
        self.hold_get: _Source | None = None  # its gate holds the next get after it read

    def _read(self, key: str) -> bytes | None:
        value = self.store.get(key)
        gate, self.hold_get = self.hold_get, None
        if gate is not None:
            gate.entered.set()
            assert gate.release.wait(_WAIT)
        return value

    def get(self, key: str) -> bytes | None:
        return self._read(key)

    def set(self, key: str, value: bytes, ttl: int | None = None, stale_ttl: int | None = None) -> None:
        self.store[key] = value
        self.sets.append(key)

    def delete(self, key: str) -> bool:
        return self.store.pop(key, None) is not None

    def exists(self, key: str) -> bool:
        return key in self.store

    def health_check(self) -> tuple[bool, dict[str, Any]]:
        return True, {}


class LockingBackend(DictBackend):
    """DictBackend with the async lock, so an async miss takes the locked path."""

    @asynccontextmanager
    async def acquire_lock(self, key: str, timeout: float, blocking_timeout: float | None = None):
        yield True


class SWRBackend(DictBackend):
    """DictBackend with the freshness read, so a stale-labelled hit revalidates in the background."""

    def get_with_freshness(self, key: str) -> tuple[bytes, bool, int | None] | None:
        value = self._read(key)
        return None if value is None else (value, self.stale, None)


def _ns() -> str:
    """A namespace of its own per test: backed functions share an L1 per namespace, and these share a qualname."""
    return f"race-{uuid.uuid4().hex}"


@pytest.mark.parametrize("how", ["invalidate_key", "invalidate_all"])
def test_backed_sync_fill_after_invalidation_writes_neither_tier(how: str) -> None:
    source, backend = _Source(), DictBackend()

    @cache(backend=backend, ttl=300, namespace=_ns())
    def get(x: int) -> str:
        return source.read()

    assert _race_sync(lambda: get(1), source, lambda: _SYNC_INVALIDATIONS[how](get)) == "old"
    assert backend.store == {}  # the L2 write was skipped
    assert get(1) == "new"  # and the L1 put: an L1 copy of "old" would be served here
    assert source.reads == 2


@pytest.mark.parametrize("backend_cls", [DictBackend, LockingBackend], ids=["unlocked", "locked"])
@pytest.mark.parametrize("how", ["ainvalidate_key", "ainvalidate_all"])
async def test_backed_async_fill_after_invalidation_writes_neither_tier(backend_cls: type[DictBackend], how: str) -> None:
    source, backend = _Source(), backend_cls()

    @cache(backend=backend, ttl=300, namespace=_ns())
    async def get(x: int) -> str:
        return await source.aread()

    assert await _race_async(lambda: get(1), source, lambda: _ASYNC_INVALIDATIONS[how](get)) == "old"
    assert backend.store == {}
    assert await get(1) == "new"
    assert source.reads == 2


def test_backed_sync_backfill_after_invalidation_is_skipped() -> None:
    """A read that fetched the old L2 entry before an invalidation does not copy it into L1 after it."""
    source, backend = _Source(), DictBackend()

    @cache(backend=backend, ttl=300, namespace=_ns())
    def get(x: int) -> str:
        return source.read()

    assert get(1) == "old"
    get._cachekit_evict(None)  # L1 only, as another process's announced invalidation would
    backend.hold_get = gate = _Source()  # holds the next L2 read after it read the entry
    assert _race_sync(lambda: get(1), source, lambda: get.invalidate_cache(1), gate) == "old"  # an L2 hit
    assert get(1) == "new"  # a backfilled L1 copy of "old" would be served here
    assert source.reads == 2


async def test_backed_async_backfill_after_invalidation_is_skipped() -> None:
    source, backend = _Source(), DictBackend()

    @cache(backend=backend, ttl=300, namespace=_ns())
    async def get(x: int) -> str:
        return await source.aread()

    assert await get(1) == "old"
    get._cachekit_evict(None)
    backend.hold_get = gate = _Source()
    assert await _race_async(lambda: get(1), source, lambda: get.ainvalidate_cache(1), gate) == "old"
    assert await get(1) == "new"
    assert source.reads == 2


def _await_threads(name: str) -> None:
    for t in threading.enumerate():
        if t.name == name:
            t.join(_WAIT)
            assert not t.is_alive()


def test_backed_sync_swr_recompute_after_invalidation_writes_neither_tier() -> None:
    source, backend = _Source(), SWRBackend()

    @cache(backend=backend, ttl=60, stale_ttl=120, namespace=_ns())
    def get(x: int) -> str:
        return source.read()

    assert get(1) == "old"
    get._cachekit_evict(None)
    backend.stale = True
    source.hold_next()
    assert get(1) == "old"  # the stale hit; its revalidation now holds in source.read()
    assert source.entered.wait(_WAIT)
    source.value = "new"
    get.invalidate_cache(1)
    backend.stale = False
    source.release.set()
    _await_threads("cachekit-swr-revalidate")
    assert backend.store == {}
    assert len(backend.sets) == 1  # the miss's write only
    assert get(1) == "new"


async def test_backed_async_swr_recompute_after_invalidation_writes_neither_tier() -> None:
    source, backend = _Source(), SWRBackend()

    @cache(backend=backend, ttl=60, stale_ttl=120, namespace=_ns())
    async def get(x: int) -> str:
        return await source.aread()

    assert await get(1) == "old"
    get._cachekit_evict(None)
    backend.stale = True
    source.hold_next()
    assert await get(1) == "old"
    assert await asyncio.to_thread(source.entered.wait, _WAIT)
    source.value = "new"
    await get.ainvalidate_cache(1)
    backend.stale = False
    source.release.set()
    pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
    await asyncio.wait_for(asyncio.gather(*pending, return_exceptions=True), _WAIT)
    assert backend.store == {}
    assert len(backend.sets) == 1
    assert await get(1) == "new"


@pytest.mark.parametrize("stripe", ["same", "other"])
def test_backed_single_key_invalidation_drops_another_key_only_in_its_stripe(stripe: str) -> None:
    source, backend = _Source(), DictBackend()

    @cache(backend=backend, ttl=300, namespace=_ns(), key=lambda x: f"k{x}")
    def get(x: int) -> str:
        return source.read()

    # Learn the stripe of each argument's key from the keys the backend stores.
    for x in range(4 * wrapper._EPOCH_STRIPES):
        get(x)
    stripes = {int(stored.rsplit(":k", 1)[1]): hash(stored) & wrapper._EPOCH_STRIPE_MASK for stored in backend.store}
    get.cache_clear()
    backend.store.clear()
    k = 0
    j = next(x for x in stripes if x != k and (stripes[x] == stripes[k]) is (stripe == "same"))

    assert _race_sync(lambda: get(j), source, lambda: get.invalidate_cache(k)) == "old"
    landed = any(stored.endswith(f":k{j}") for stored in backend.store)
    assert landed is (stripe == "other")


class HoldDeleteBackend(DictBackend):
    """DictBackend whose delete can hold the invalidation before it deletes."""

    def __init__(self) -> None:
        super().__init__()
        self.hold_delete: _Source | None = None

    def delete(self, key: str) -> bool:
        gate, self.hold_delete = self.hold_delete, None
        if gate is not None:
            gate.entered.set()
            assert gate.release.wait(_WAIT)
        return super().delete(key)


@pytest.mark.parametrize("how", ["invalidate_key", "invalidate_all"])
def test_backed_read_during_the_invalidation_does_not_backfill_the_old_entry(how: str) -> None:
    """A read that starts inside the invalidation, before its L2 delete, can fetch the old entry. Once the
    invalidation has returned, L1 must not hold it."""
    source, backend = _Source(), HoldDeleteBackend()

    @cache(backend=backend, ttl=300, namespace=_ns())
    def get(x: int) -> str:
        return source.read()

    assert get(1) == "old"
    get._cachekit_evict(None)  # L1 empty, L2 holds "old"
    source.value = "new"
    backend.hold_delete = deleting = _Source()
    invalidator = threading.Thread(target=lambda: _SYNC_INVALIDATIONS[how](get))
    invalidator.start()
    assert deleting.entered.wait(_WAIT)  # inside the invalidation, before its L2 delete
    backend.hold_get = reading = _Source()
    out: list[str] = []
    reader = threading.Thread(target=lambda: out.append(get(1)))
    reader.start()
    assert reading.entered.wait(_WAIT)  # the reader has fetched "old" from L2
    deleting.release.set()
    invalidator.join(_WAIT)
    assert not invalidator.is_alive()
    reading.release.set()
    reader.join(_WAIT)
    assert out == ["old"]  # its call began before the invalidation returned
    assert get(1) == "new"


def test_backed_callers_that_joined_a_skipped_fill_share_its_value(monkeypatch: pytest.MonkeyPatch) -> None:
    """A skipped fill still hands its value to the callers that joined it before the invalidation: they
    do not each run the function again."""
    joined = threading.Event()
    run = ThreadFlights.run

    def run_signalling_joins(self: ThreadFlights, key: Any, call: Any, recheck: Any, on_join: Any = None) -> Any:
        def hook() -> None:
            if on_join is not None:
                on_join()
            joined.set()

        return run(self, key, call, recheck, hook)

    monkeypatch.setattr(ThreadFlights, "run", run_signalling_joins)
    source, backend = _Source(), DictBackend()

    @cache(backend=backend, ttl=300, namespace=_ns())
    def get(x: int) -> str:
        return source.read()

    source.hold_next()
    out: list[str] = []
    first = threading.Thread(target=lambda: out.append(get(1)))
    first.start()
    assert source.entered.wait(_WAIT)
    second = threading.Thread(target=lambda: out.append(get(1)))
    second.start()
    assert joined.wait(_WAIT)
    source.value = "new"
    get.invalidate_cache(1)
    source.release.set()
    for t in (first, second):
        t.join(_WAIT)
        assert not t.is_alive()
    assert out == ["old", "old"]
    assert source.reads == 1
    assert backend.store == {}
    assert get(1) == "new"
