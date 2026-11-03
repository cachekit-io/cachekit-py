"""A fill that read its source before an invalidation stores nothing once that invalidation has returned.

Each test holds a fill between its source read and its store, runs an invalidation to completion in
this process, releases the fill, and checks that no cache tier keeps the value read before it: the
next call computes again. The interleavings are fixed by events, not timing, so they hold on the
free-threaded build too.
"""

from __future__ import annotations

import asyncio
import itertools
import sys
import threading
import uuid
from collections.abc import Callable
from contextlib import asynccontextmanager
from typing import Any

import pytest

from cachekit import cache
from cachekit.decorators.single_flight import ThreadFlights
from cachekit.object_cache import EPOCH_STRIPES, ObjectCache, epoch_stripe

pytestmark = pytest.mark.unit

_WAIT = 60.0  # seconds: a bound on each held step, never reached when the code is right, even on a slow runner


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


def _stripe_pairs(n: int = 4 * EPOCH_STRIPES) -> tuple[tuple[str, str], tuple[str, str]]:
    """Two key pairs among n keys: one sharing an epoch stripe, one not. str hashes vary per process, so the
    keys are grouped by stripe: 256 keys in 64 stripes put at least two in one stripe, and use two stripes."""
    by_stripe: dict[int, list[str]] = {}
    for x in range(n):
        by_stripe.setdefault(epoch_stripe(f"k{x}"), []).append(f"k{x}")
    same = next(tuple(keys[:2]) for keys in by_stripe.values() if len(keys) >= 2)
    first, second = list(by_stripe.values())[:2]
    return (same[0], same[1]), (first[0], second[0])


@pytest.mark.parametrize("stripe", ["same", "other"])
def test_object_cache_delete_drops_another_keys_fill_only_in_its_stripe(stripe: str) -> None:
    same, other = _stripe_pairs()
    invalidated, filled = same if stripe == "same" else other
    oc = ObjectCache(max_entries=8)
    epoch = oc.invalidation_epoch  # the fill of `filled` starts
    oc.delete(invalidated)
    oc.put(filled, "old", ttl=60, epoch=epoch)
    assert oc.get(filled) == ((False, None) if stripe == "same" else (True, "old"))


def test_object_cache_fill_lands_while_other_stripes_are_invalidated_faster_than_it_runs() -> None:
    """A single-key delete reaches only its stripe, so a fill outpaced by deletes of other keys still lands."""
    (_, _), (filled, invalidated) = _stripe_pairs()
    oc = ObjectCache(max_entries=8)
    epoch = oc.invalidation_epoch
    for _ in range(100):
        oc.delete(invalidated)
    oc.put(filled, "v", ttl=60, epoch=epoch)
    assert oc.get(filled) == (True, "v")
    oc.clear()  # clear reaches every stripe
    epoch_before_clear = epoch
    oc.put(filled, "stale", ttl=60, epoch=epoch_before_clear)
    assert oc.get(filled) == (False, None)


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
    for x in range(4 * EPOCH_STRIPES):
        get(x)
    stripes = {int(stored.rsplit(":k", 1)[1]): epoch_stripe(stored) for stored in backend.store}
    get.cache_clear()
    backend.store.clear()
    # str hashes vary per process, so pick the pair from the keys' groups: 256 keys in 64 stripes put at
    # least two keys in one stripe, and in at least two stripes.
    by_stripe: dict[int, list[int]] = {}
    for x, s in stripes.items():
        by_stripe.setdefault(s, []).append(x)
    if stripe == "same":
        k, j = next(xs[:2] for xs in by_stripe.values() if len(xs) >= 2)
    else:
        k, j = (xs[0] for xs in list(by_stripe.values())[:2])

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


class TrackingBackend(HoldDeleteBackend):
    """HoldDeleteBackend with a key registry, so a whole-function invalidation drains it (_drain_all).

    hold_delete holds the drain before it deletes; fail_after_delete makes it raise once it has deleted,
    as a drain whose reply is lost does."""

    def __init__(self) -> None:
        super().__init__()
        self.registry: dict[str, set[str]] = {}
        self.fail_after_delete = False

    def track_key(self, registry_id: str, key: str) -> None:
        self.registry.setdefault(registry_id, set()).add(key)

    def drain_tracked(self, registry_id: str, local_keys: Any) -> set[str]:
        gate, self.hold_delete = self.hold_delete, None
        if gate is not None:
            gate.entered.set()
            assert gate.release.wait(_WAIT)
        keys = self.registry.pop(registry_id, set()) | set(local_keys)
        for key in keys:
            self.store.pop(key, None)
        if self.fail_after_delete:
            raise ConnectionError("reply lost after the drain deleted")
        return keys


@pytest.mark.parametrize(
    ("how", "backend_cls"),
    [("invalidate_key", HoldDeleteBackend), ("invalidate_all", HoldDeleteBackend), ("invalidate_all", TrackingBackend)],
    ids=["key", "all-local", "all-drain"],
)
def test_backed_read_during_the_invalidation_does_not_backfill_the_old_entry(
    how: str, backend_cls: type[HoldDeleteBackend]
) -> None:
    """A read that starts inside the invalidation, before its L2 delete, can fetch the old entry. Once the
    invalidation has returned, L1 must not hold it."""
    source, backend = _Source(), backend_cls()

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


@pytest.mark.parametrize("backend_cls", [DictBackend, TrackingBackend], ids=["local", "drain"])
def test_backed_sync_fill_after_whole_function_invalidation_writes_neither_tier(backend_cls: type[DictBackend]) -> None:
    source, backend = _Source(), backend_cls()

    @cache(backend=backend, ttl=300, namespace=_ns())
    def get(x: int) -> str:
        return source.read()

    assert get(2) == "old"  # something for the drain to drain
    assert _race_sync(lambda: get(1), source, lambda: get.invalidate_cache()) == "old"
    assert backend.store == {}
    assert get(1) == "new"


def test_backed_drain_that_raises_after_deleting_leaves_no_l1_copy() -> None:
    """A drain that deleted on the server and then raised falls back to the local invalidation. A read that
    fetched the old entry from L2 during the drain still leaves no L1 copy once the invalidation returns,
    even when this process recorded nothing for the fallback to delete."""
    source, backend = _Source(), TrackingBackend()
    namespace = _ns()

    def body(x: int) -> str:
        return source.read()

    writer = cache(backend=backend, ttl=300, namespace=namespace)(body)  # another decoration wrote the entry
    get = cache(backend=backend, ttl=300, namespace=namespace)(body)
    assert writer(1) == "old"
    writer._cachekit_evict(None)  # the shared L1 (one per namespace) empty, L2 holds "old"
    source.value = "new"
    backend.fail_after_delete = True
    backend.hold_delete = draining = _Source()
    invalidator = threading.Thread(target=get.invalidate_cache)
    invalidator.start()
    assert draining.entered.wait(_WAIT)  # epochs moved, the drain has not deleted yet
    backend.hold_get = reading = _Source()
    out: list[str] = []
    reader = threading.Thread(target=lambda: out.append(get(1)))
    reader.start()
    assert reading.entered.wait(_WAIT)  # the reader has fetched "old" from L2
    draining.release.set()
    invalidator.join(_WAIT)
    assert not invalidator.is_alive()
    reading.release.set()
    reader.join(_WAIT)
    assert out == ["old"]
    backend.fail_after_delete = False
    assert backend.store == {}
    assert get(1) == "new"


def test_backed_drain_evicts_while_other_threads_record(caplog: pytest.LogCaptureFixture) -> None:
    """Keys recorded while a drain runs are evicted from a copy of the drain's watch set: walking the live
    set while other threads add to it raises, and the drain would fall back to the local invalidation."""
    backend = TrackingBackend()

    @cache(backend=backend, ttl=300, namespace=_ns())
    def get(x: int) -> int:
        return x

    stop = threading.Event()
    counter = itertools.count()

    def record_forever() -> None:
        while not stop.is_set():
            get(next(counter))

    backend.hold_delete = draining = _Source()
    invalidator = threading.Thread(target=get.invalidate_cache)
    invalidator.start()
    assert draining.entered.wait(_WAIT)
    for x in range(5_000):  # watched by the drain
        get(-x - 1)
    recorders = [threading.Thread(target=record_forever) for _ in range(2)]
    for t in recorders:
        t.start()
    switch_interval = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)  # with the GIL, switch threads often enough to land inside a walk of the set
    try:
        with caplog.at_level("WARNING", logger="cachekit.decorators.wrapper"):
            draining.release.set()
            invalidator.join(_WAIT)
    finally:
        sys.setswitchinterval(switch_interval)
    stop.set()
    for t in recorders:
        t.join(_WAIT)
    assert not invalidator.is_alive()
    assert "Key registry drain failed" not in caplog.text
