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
from collections.abc import Callable, Iterable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest

from cachekit import cache
from cachekit.cache_handler import CacheSerializationHandler
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

    def take_hold(self) -> bool:
        """Whether the caller is the one hold_next armed; disarms it."""
        held, self._hold = self._hold, False
        return held

    def pause(self) -> None:
        """Signal entered, then block until release."""
        self.entered.set()
        assert self.release.wait(_WAIT)

    def read(self) -> str:
        value = self.value
        self.reads += 1
        if self.take_hold():
            self.pause()
        return value

    async def aread(self) -> str:
        value = self.value
        self.reads += 1
        if self.take_hold():
            await asyncio.to_thread(self.pause)
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
_LOCAL_INVALIDATIONS = ("cache_clear", "invalidate_all", "invalidate_key")


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


def _stripe_pairs(keys: list[str]) -> tuple[tuple[str, str], tuple[str, str]]:
    """Two pairs of keys: one sharing an epoch stripe, one not. str hashes vary per process, so the keys are
    grouped by stripe: 4 * EPOCH_STRIPES keys put at least two in one stripe, and use at least two stripes."""
    assert len(keys) >= 4 * EPOCH_STRIPES
    by_stripe: dict[int, list[str]] = {}
    for key in keys:
        by_stripe.setdefault(epoch_stripe(key), []).append(key)
    same = next(group[:2] for group in by_stripe.values() if len(group) >= 2)
    first, second = list(by_stripe.values())[:2]
    return (same[0], same[1]), (first[0], second[0])


@pytest.mark.parametrize("how", ["same", "other", "clear"])
def test_object_cache_invalidation_drops_another_keys_fill_only_in_its_stripe(how: str) -> None:
    """A delete reaches only its key's stripe, so a fill outpaced by 100 deletes of a key in another stripe
    still lands; clear reaches every stripe."""
    same, other = _stripe_pairs([f"k{x}" for x in range(4 * EPOCH_STRIPES)])
    invalidated, filled = same if how == "same" else other
    oc = ObjectCache(max_entries=8)
    epoch = oc.invalidation_epoch  # the fill of `filled` starts
    for _ in range(100):
        oc.delete(invalidated)
    if how == "clear":
        oc.clear()
    oc.put(filled, "old", ttl=60, epoch=epoch)
    assert oc.get(filled) == ((True, "old") if how == "other" else (False, None))


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
            gate.pause()
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
    same, other = _stripe_pairs(list(backend.store))
    k, j = (int(stored.rsplit(":k", 1)[1]) for stored in (same if stripe == "same" else other))
    get.cache_clear()
    backend.store.clear()

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
            gate.pause()
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

    def drain_tracked(self, registry_id: str, local_keys: Iterable[str]) -> set[str]:
        gate, self.hold_delete = self.hold_delete, None
        if gate is not None:
            gate.pause()
        keys = self.registry.pop(registry_id, set()) | set(local_keys)
        for key in keys:
            self.store.pop(key, None)
        if self.fail_after_delete:
            raise ConnectionError("reply lost after the drain deleted")
        return keys


@pytest.mark.parametrize(
    ("how", "backend_cls"),
    [("invalidate_key", DictBackend), ("invalidate_all", DictBackend), ("invalidate_all", TrackingBackend)],
    ids=["key", "all-local", "all-drain"],
)
def test_backed_sync_fill_after_invalidation_writes_neither_tier(how: str, backend_cls: type[DictBackend]) -> None:
    source, backend = _Source(), backend_cls()

    @cache(backend=backend, ttl=300, namespace=_ns())
    def get(x: int) -> str:
        return source.read()

    assert _race_sync(lambda: get(1), source, lambda: _SYNC_INVALIDATIONS[how](get)) == "old"
    assert backend.store == {}  # the L2 write was skipped
    assert get(1) == "new"  # and the L1 put: an L1 copy of "old" would be served here
    assert source.reads == 2


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


@pytest.fixture
def joined(monkeypatch: pytest.MonkeyPatch) -> threading.Event:
    """Set when a caller joins a sync backed miss in flight (ThreadFlights)."""
    event = threading.Event()
    run = ThreadFlights.run

    def run_signalling_joins(self: ThreadFlights, key: Any, call: Any, recheck: Any, on_join: Any = None) -> Any:
        def hook() -> None:
            if on_join is not None:
                on_join()
            event.set()

        return run(self, key, call, recheck, hook)

    monkeypatch.setattr(ThreadFlights, "run", run_signalling_joins)
    return event


def _arrow_value(value: Any) -> str:
    """The one cell of an arrow result: a Table for the caller that ran, a decoded DataFrame otherwise."""
    first = value["v"][0]
    return first.as_py() if hasattr(first, "as_py") else first


@pytest.mark.parametrize("serializer", ["default", "arrow"])
def test_backed_callers_that_joined_a_skipped_fill_share_its_value(joined: threading.Event, serializer: str) -> None:
    """A skipped fill still hands its value to the callers that joined it before the invalidation: they
    do not each run the function again. Arrow can stream, but not to this backend, so it shares too."""
    if serializer == "arrow":
        pa = pytest.importorskip("pyarrow")

        def wrap(value: str) -> Any:
            return pa.table({"v": [value]})

        unwrap = _arrow_value
    else:

        def wrap(value: str) -> Any:
            return value

        def unwrap(value: Any) -> str:
            return value

    source, backend = _Source(), DictBackend()

    @cache(backend=backend, ttl=300, namespace=_ns(), serializer=serializer)
    def get(x: int) -> Any:
        return wrap(source.read())

    source.hold_next()
    out: list[str] = []
    first = threading.Thread(target=lambda: out.append(unwrap(get(1))))
    first.start()
    assert source.entered.wait(_WAIT)
    second = threading.Thread(target=lambda: out.append(unwrap(get(1))))
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
    assert unwrap(get(1)) == "new"


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
    """A stress test, not an event-ordered one: keys recorded while a drain runs are evicted from a copy of
    the drain's watch set. Walking the live set while other threads add to it raises, and the drain would
    fall back to the local invalidation. A tiny switch interval makes that walk race the recorders."""
    backend = TrackingBackend()
    calls: dict[int, int] = {}

    @cache(backend=backend, ttl=300, namespace=_ns())
    def get(x: int) -> int:
        calls[x] = calls.get(x, 0) + 1
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
    sample = range(-1, -5_001, -500)  # watched keys: the drain deleted them from both tiers
    for x in sample:
        get(x)
    assert [calls[x] for x in sample] == [2] * len(sample)


@pytest.fixture
def hold_serialize(monkeypatch: pytest.MonkeyPatch) -> _Source:
    """A gate that holds the next serialize_data call after it has serialized, before its caller writes."""
    gate = _Source()
    serialize = CacheSerializationHandler.serialize_data

    def held(self: CacheSerializationHandler, *args: Any, **kwargs: Any) -> Any:
        data = serialize(self, *args, **kwargs)
        if gate.take_hold():
            gate.pause()
        return data

    monkeypatch.setattr(CacheSerializationHandler, "serialize_data", held)
    return gate


# path: (backend, coroutine function, stale_ttl refresh)
_WRITE_POINTS = {
    "sync-miss": (DictBackend, False, False),
    "sync-swr": (SWRBackend, False, True),
    "async-miss-unlocked": (DictBackend, True, False),
    "async-miss-locked": (LockingBackend, True, False),
    "async-swr": (SWRBackend, True, True),
}


@pytest.mark.parametrize("path", sorted(_WRITE_POINTS))
def test_backed_write_is_checked_after_serialization(path: str, hold_serialize: _Source) -> None:
    """An invalidation that runs while the value is serialized, after the fill's source read, skips the L2
    write: every buffered write site checks after serialize_data, not before it. The async calls run on an
    event loop in the caller's thread, which hold_serialize blocks."""
    backend_cls, is_async, swr = _WRITE_POINTS[path]
    source, backend = _Source(), backend_cls()
    ttls: dict[str, Any] = {"ttl": 60, "stale_ttl": 120} if swr else {"ttl": 60}

    if is_async:

        @cache(backend=backend, namespace=_ns(), **ttls)
        async def aget(x: int) -> str:
            return source.read()

        async def call_and_settle() -> str:
            value = await aget(1)
            pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
            await asyncio.gather(*pending)  # a stale hit's revalidation, before the loop closes
            return value

        fn: Any = aget
        call: Callable[[], str] = lambda: asyncio.run(call_and_settle())  # noqa: E731
        invalidate: Callable[[], Any] = lambda: asyncio.run(aget.ainvalidate_cache(1))  # noqa: E731
    else:

        @cache(backend=backend, namespace=_ns(), **ttls)
        def get(x: int) -> str:
            return source.read()

        fn, call, invalidate = get, lambda: get(1), lambda: get.invalidate_cache(1)

    if swr:
        assert call() == "old"
        fn._cachekit_evict(None)
        backend.stale = True  # the next call is a stale hit, which revalidates in the background
    hold_serialize.hold_next()
    assert _race_sync(call, source, invalidate, hold_serialize) == "old"
    backend.stale = False
    _await_threads("cachekit-swr-revalidate")
    assert backend.store == {}
    assert call() == "new"


def test_streamed_write_is_skipped_when_invalidated_before_its_stream(tmp_path: Path, joined: threading.Event) -> None:
    """A streamed value serializes inside its stream, so the check runs before the stream starts. A fill
    invalidated before then streams nothing and has no envelope to share: the caller that joined it takes
    its own trip, which streams the value read after the invalidation."""
    pa = pytest.importorskip("pyarrow")
    from cachekit.backends.file import FileBackend
    from cachekit.backends.file.config import FileBackendConfig

    class CountingFileBackend(FileBackend):
        streams = 0

        def set_streaming(self, *args: Any, **kwargs: Any) -> Any:
            CountingFileBackend.streams += 1
            return super().set_streaming(*args, **kwargs)

    backend = CountingFileBackend(FileBackendConfig(cache_dir=str(tmp_path), max_size_mb=64, max_value_mb=32))
    source = _Source()

    @cache(backend=backend, ttl=300, namespace=_ns(), serializer="arrow")
    def get(x: int) -> Any:
        return pa.table({"v": [source.read()]})

    source.hold_next()
    out: list[str] = []
    first = threading.Thread(target=lambda: out.append(_arrow_value(get(1))))
    first.start()
    assert source.entered.wait(_WAIT)
    second = threading.Thread(target=lambda: out.append(_arrow_value(get(1))))
    second.start()
    assert joined.wait(_WAIT)
    source.value = "new"
    get.invalidate_cache(1)
    source.release.set()
    for t in (first, second):
        t.join(_WAIT)
        assert not t.is_alive()
    assert sorted(out) == ["new", "old"]  # the joined caller took its own trip
    assert source.reads == 2
    assert CountingFileBackend.streams == 1  # only that trip streamed
    assert _arrow_value(get(1)) == "new"
    assert source.reads == 2
