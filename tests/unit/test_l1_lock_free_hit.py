"""The L1-only hit answered without a lock: cachekit._rust_serializer.HitMemo (ObjectCache, "Lock-free hits").

Pins what the hit must never do: serve an entry once its removal has returned, by any of the five routes
(expiry, eviction, invalidation, cache_clear, the at-fork reset); keep a removed value referenced from the
extension; judge a deadline on another clock than ObjectCache's; evict out of LRU order; or key a call
differently from the memo-key rules the Python implementation had.
"""

from __future__ import annotations

import asyncio
import enum
import gc
import itertools
import math
import os
import random
import signal
import threading
import time
import weakref
from collections import Counter, OrderedDict
from decimal import Decimal
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from cachekit import cache, object_cache
from cachekit._rust_serializer import HitCounter, HitMemo, monotonic
from cachekit.config import L1CacheConfig
from cachekit.decorators.wrapper import _MEMO_MAX_ARGS, _MEMO_MAX_ENTRIES, _MEMO_MAX_LEN, _memo_key
from cachekit.object_cache import ObjectCache
from tests.unit.test_key_registry import _closure_cell
from tests.utils.clock import fake_clock, shared_clock
from tests.utils.fork_helpers import child_outcome, report

_MISS = object()


class _Value:
    """A cached value a test can hold a weak reference to."""

    def __init__(self, n: Any) -> None:
        self.n = n


def _cache_and_memo(fn: Any) -> tuple[ObjectCache, Any]:
    return _closure_cell(fn, "_object_cache").cell_contents, _closure_cell(fn, "_l1_memo").cell_contents


def _locked_hits(fn: Any) -> int:
    """Lookups the function's ObjectCache answered under its lock (a lock-free hit is not one)."""
    return _cache_and_memo(fn)[0].hits


def _gone(ref: weakref.ref[Any]) -> bool:
    gc.collect()
    return ref() is None


@pytest.mark.unit
class TestRemovalRoutes:
    """Each route: the call after the removal returned runs the function again, the memo has no slot
    for the removed entry, and once the test drops its own reference the removed value is freed, so
    nothing on the extension's side (the memo, a HitEntry) still holds it."""

    @pytest.mark.parametrize("swr", [False, True], ids=["swr-off", "swr-on"])
    def test_expiry(self, monkeypatch: pytest.MonkeyPatch, swr: bool) -> None:
        clock = fake_clock(monkeypatch)
        calls: list[int] = []

        @cache(backend=None, ttl=10, l1=L1CacheConfig(swr_enabled=swr))
        def fn(x: int) -> _Value:
            calls.append(x)
            return _Value(x)

        first = fn(1)
        locked = _locked_hits(fn)
        assert fn(1) is first
        assert _locked_hits(fn) == locked  # served by the lock-free hit
        ref = weakref.ref(first)
        del first
        clock.monotonic = lambda: 1010.0  # the hard deadline itself: expired for the cache and the hit alike
        fresh = fn(1)
        assert ref() is not fresh
        assert len(calls) == 2
        assert _gone(ref)

    def test_eviction_by_the_byte_bound(self) -> None:
        payload = b"x" * 400_000  # three do not fit the 1 MiB budget; counted per entry, shared in memory
        calls: list[int] = []

        @cache(backend=None, ttl=300, l1=L1CacheConfig(max_size_mb=1))
        def fn(x: int) -> tuple[_Value, bytes]:
            calls.append(x)
            return _Value(x), payload

        oc, memo = _cache_and_memo(fn)
        ref = weakref.ref(fn(1)[0])
        fn(2)
        fn(3)  # evicts 1, the least recently used
        assert oc.size == 2
        assert (_memo_key((1,), {}) in memo.keys(), len(memo)) == (False, 2)
        assert _gone(ref)
        fn(1)
        assert calls == [1, 2, 3, 1]

    def test_eviction_by_the_entry_bound(self) -> None:
        """The decorator bounds bytes only, so the entry bound is driven on an ObjectCache with a memo."""
        oc, memo = _memo_cache(max_entries=2)
        a = _Value("a")
        ref = weakref.ref(a)
        _put(oc, "a", a)
        _put(oc, "b", _Value("b"))
        assert _hit(memo, "a") is a
        del a
        _put(oc, "c", _Value("c"))  # evicts b: a was used after it
        _put(oc, "d", _Value("d"))  # evicts a
        assert (_hit(memo, "a"), _hit(memo, "b")) == (_MISS, _MISS)
        assert _hit(memo, "c").n == "c"
        assert sorted(memo.keys()) == sorted([_memo_key(("c",), {}), _memo_key(("d",), {})])
        assert _gone(ref)

    @pytest.mark.parametrize("is_async", [False, True], ids=["sync", "async"])
    def test_invalidation(self, is_async: bool) -> None:
        calls: list[int] = []
        if is_async:

            @cache(backend=None, ttl=300)
            async def afn(x: int) -> _Value:
                calls.append(x)
                return _Value(x)

            def call(x: int) -> _Value:
                return asyncio.run(afn(x))

            def invalidate(x: int) -> Any:
                return asyncio.run(afn.ainvalidate_cache(x))

            fn: Any = afn
        else:

            @cache(backend=None, ttl=300)
            def sfn(x: int) -> _Value:
                calls.append(x)
                return _Value(x)

            call, invalidate, fn = sfn, sfn.invalidate_cache, sfn

        _, memo = _cache_and_memo(fn)
        first = call(1)
        assert call(1) is first
        ref = weakref.ref(first)
        del first
        invalidate(1)
        assert len(memo) == 0
        assert _gone(ref)
        call(1)
        assert calls == [1, 1]

    def test_cache_clear(self) -> None:
        calls: list[int] = []

        @cache(backend=None, ttl=300)
        def fn(x: int) -> _Value:
            calls.append(x)
            return _Value(x)

        _, memo = _cache_and_memo(fn)
        refs = [weakref.ref(fn(x)) for x in (1, 2)]
        assert len(memo) == 2
        fn.cache_clear()
        assert len(memo) == 0
        assert all(_gone(ref) for ref in refs)
        fn(1)
        assert calls == [1, 2, 1]

    @pytest.mark.skipif(not hasattr(os, "fork"), reason="fork() not available on this platform")
    def test_the_at_fork_reset(self) -> None:
        """A parent thread holds the cache's lock at fork, so the child's cache starts empty: its memo
        must be empty too, and the entry the parent memoised dead, or the child would serve it."""
        calls: list[int] = []

        @cache(backend=None, ttl=300)
        def fn(x: int) -> int:
            calls.append(x)
            return x * 2

        oc, memo = _cache_and_memo(fn)
        assert fn(1) == 2
        hit = next(iter(oc._state.store.values())).hit
        held, release = threading.Event(), threading.Event()

        def hold() -> None:
            with oc._state.lock:
                held.set()
                release.wait()

        holder = threading.Thread(target=hold, daemon=True)
        holder.start()
        assert held.wait(5)
        r, w = os.pipe()
        try:
            child = os.fork()
            if child == 0:
                signal.alarm(5)
                try:
                    outcome: object = {"memo": len(memo), "alive": hit.alive, "value": fn(1), "calls": len(calls)}
                except BaseException as e:
                    outcome = {"error": repr(e)}
                report(w, outcome)
        finally:
            release.set()
            holder.join(5)
        os.close(w)

        assert child_outcome(child, r) == {"memo": 0, "alive": False, "value": 2, "calls": 2}
        assert (hit.alive, len(memo)) == (True, 1)  # the parent's own cache is untouched


@pytest.mark.unit
class TestTheCacheKeepsTheMemoInStep:
    """ObjectCache-level: every change to an entry reaches the memo before it returns."""

    def test_clear_ends_every_lock_free_hit(self) -> None:
        oc, memo = _memo_cache(max_entries=8)
        a, b = _Value("a"), _Value("b")
        refs = [weakref.ref(a), weakref.ref(b)]
        hits = [_put_and_hit_entry(oc, "a", a), _put_and_hit_entry(oc, "b", b)]
        del a, b
        oc.clear()
        assert (_hit(memo, "a"), _hit(memo, "b"), len(memo)) == (_MISS, _MISS, 0)
        assert not any(entry_hit.alive for entry_hit in hits)
        del hits
        assert all(_gone(ref) for ref in refs)

    def test_a_refresh_points_the_memo_at_the_new_value_and_ends_the_old(self, monkeypatch: pytest.MonkeyPatch) -> None:
        clock = fake_clock(monkeypatch)
        oc, memo = _memo_cache(max_entries=8)
        old = _Value("old")
        ref = weakref.ref(old)
        stale = _put_and_hit_entry(oc, "k", old, ttl=100)
        clock.monotonic = lambda: 1060.0  # in the refresh band: the lock-free hit leaves it to the lock
        assert _hit(memo, "k") is old  # fresh_until is the hard expiry here: this cache was built without swr
        hit, value, needs_refresh, version = oc.get_with_swr("key:k", 100, _memo_key(("k",), {}))
        assert (hit, value, needs_refresh) == (True, old, True)
        new = _Value("new")
        assert oc.complete_refresh("key:k", version, new, ttl=100) is True
        assert _hit(memo, "k") is new
        assert stale.alive is False
        del old, value, stale
        assert _gone(ref)

    def test_a_full_memo_takes_its_entries_key_lists_with_it(self) -> None:
        """A full memo empties before it takes a new key, and the entries it held drop their key lists
        with it: an entry keeps a call's arguments only while that call has a slot, so the memo bounds
        what all of them keep."""
        oc, memo = _memo_cache(max_entries=4 * _MEMO_MAX_ENTRIES)
        for i in range(_MEMO_MAX_ENTRIES + 100):
            _put_and_hit_entry(oc, f"k{i}", i)
        assert len(memo) == 100
        assert _keys_held_by_entries(oc) == Counter(memo.keys())

    def test_a_slot_moved_to_another_entry_leaves_the_first(self) -> None:
        oc, memo = _memo_cache(max_entries=8)
        first = _put_and_hit_entry(oc, "a", _Value("a"))
        oc.put("key:b", _Value("b"), 3600)
        key = _memo_key(("a",), {})
        assert oc.get("key:b", key)[0]  # what the decorator never does: one call's slot moved to another entry
        assert (first.memo_keys, oc._state.store["key:b"].hit.memo_keys) == ((), (key,))
        oc.delete("key:b")
        assert len(memo) == 0
        assert first.alive  # the entry that lost its slot stays stored, for the lock to serve

    def test_a_slot_left_on_a_removed_entry_still_misses(self) -> None:
        """The cache forgets a removed entry's slots; the hit's liveness check is the second guard, for
        a slot that outlived its entry."""
        oc, memo = _memo_cache(max_entries=8)
        entry_hit = _put_and_hit_entry(oc, "k", _Value("k"))
        oc.delete("key:k")
        memo.set(_memo_key(("k",), {}), entry_hit)  # what no cache does: a slot on a removed entry
        assert _hit(memo, "k") is _MISS


def _keys_held_by_entries(oc: ObjectCache) -> Counter[Any]:
    """The memo keys the stored entries list as theirs: equal to the memo's keys, if the cache kept both in step."""
    return Counter(key for entry in oc._state.store.values() if entry.hit is not None for key in entry.hit.memo_keys)


def _put_and_hit_entry(oc: ObjectCache, k: str, value: Any, ttl: int = 3600) -> Any:
    """Store value under k, memoised, and return the HitEntry the memo now holds for it."""
    oc.put(f"key:{k}", value, ttl, memo_key=_memo_key((k,), {}))
    return oc._state.store[f"key:{k}"].hit


@pytest.mark.unit
class TestOneClock:
    """ObjectCache sets the deadlines and the extension compares against them, on one clock."""

    def test_the_cache_reads_the_extensions_clock(self) -> None:
        assert object_cache._monotonic is monotonic

    @pytest.mark.parametrize("swr", [False, True], ids=["swr-off", "swr-on"])
    def test_no_entry_is_served_after_its_hard_expiry(self, monkeypatch: pytest.MonkeyPatch, swr: bool) -> None:
        """swr_threshold_ratio=1.0 puts the lock-free hit's deadline as late as it goes: the hard expiry
        itself without SWR, and 0.9 of the ttl with it. A hit reading another clock would serve on."""
        clock = fake_clock(monkeypatch)
        calls: list[int] = []

        @cache(backend=None, ttl=10, l1=L1CacheConfig(swr_threshold_ratio=1.0, swr_enabled=swr))
        def fn(x: int) -> _Value:
            calls.append(x)
            return _Value(x)

        first = fn(1)
        locked = _locked_hits(fn)
        for now in (1000.0, 1005.0, 1008.999):  # inside the lock-free window, short of any refresh
            clock.monotonic = lambda now=now: now
            assert fn(1) is first
        assert _locked_hits(fn) == locked
        clock.monotonic = lambda: 1010.0  # the hard expiry itself: only a recompute may answer
        second = fn(1)
        assert second is not first
        clock.monotonic = lambda: 1020.001  # just past the recomputed entry's expiry
        assert fn(1) is not second
        assert len(calls) == 3


@pytest.mark.unit
class TestTheHit:
    @pytest.mark.parametrize("is_async", [False, True], ids=["sync", "async"])
    def test_a_hit_returns_the_stored_object(self, is_async: bool) -> None:
        stored: list[_Value] = []

        if is_async:

            @cache(backend=None, ttl=300)
            async def afn(x: int) -> _Value:
                stored.append(_Value(x))
                return stored[-1]

            results = [asyncio.run(afn(7)) for _ in range(3)]
        else:

            @cache(backend=None, ttl=300)
            def fn(x: int) -> _Value:
                stored.append(_Value(x))
                return stored[-1]

            results = [fn(7) for _ in range(3)]
        assert len(stored) == 1
        assert all(result is stored[0] for result in results)

    def test_a_bypass_call_is_never_served_from_the_memo(self) -> None:
        calls: list[int] = []

        @cache(backend=None, ttl=300)
        def fn(x: int) -> int:
            calls.append(x)
            return x

        fn(1)
        fn(1)
        fn(1, _bypass_cache=True)
        assert calls == [1, 1]

    def test_the_hit_counts_on_cache_info(self) -> None:
        @cache(backend=None, ttl=300)
        def fn(x: int) -> int:
            return x

        fn.cache_clear()
        for _ in range(4):
            fn(5)
        info = fn.cache_info()
        assert (info.hits, info.l1_hits, info.misses) == (3, 3, 1)
        assert info.last_operation_at is not None and abs(info.last_operation_at - time.time()) < 60


# ------------------------------------------------------------------------------------------------
# The memo key: the extension's, against the Python implementation it replaced
# ------------------------------------------------------------------------------------------------


def _reference_memo_size(a: Any) -> int:
    t = type(a)
    if t is str or t is bytes:
        return len(a)
    return 0 if t is int or t is float or t is bool or a is None else -1


def _reference_memo_key(args: tuple[Any, ...], kwargs: dict[str, Any]) -> tuple[Any, ...] | None:
    """The Python _memo_key before the memo moved into the extension (cachekit 0.25.1), verbatim in its rules."""
    if not kwargs:
        n = len(args)
        if n == 1:
            a = args[0]
            t = type(a)
            if t is int or ((t is str or t is bytes) and len(a) <= _MEMO_MAX_LEN) or t is float or t is bool or a is None:
                return (t, a)
            return None
        if n == 2:
            a, b = args
            t = type(a)
            u = type(b)
            if t is int or t is float or t is bool or a is None:
                if u is int or ((u is str or u is bytes) and len(b) <= _MEMO_MAX_LEN) or u is float or u is bool or b is None:
                    return (t, a, u, b)
            elif t is str or t is bytes:
                if u is int or u is float or u is bool or b is None:
                    if len(a) <= _MEMO_MAX_LEN:
                        return (t, a, u, b)
                elif (u is str or u is bytes) and len(a) + len(b) <= _MEMO_MAX_LEN:
                    return (t, a, u, b)
            return None
    if len(args) + len(kwargs) > _MEMO_MAX_ARGS:
        return None
    size = 0
    for a in args:
        cost = _reference_memo_size(a)
        if cost < 0:
            return None
        size += cost
    if not kwargs:
        return (args, tuple(map(type, args))) if size <= _MEMO_MAX_LEN else None
    for name, a in kwargs.items():
        cost = _reference_memo_size(a)
        if type(name) is not str or cost < 0:
            return None
        size += len(name) + cost
    if size > _MEMO_MAX_LEN:
        return None
    items = tuple(sorted(kwargs.items()))
    return (args, tuple(map(type, args)), items, tuple([type(value) for _, value in items]))


class _Color(enum.IntEnum):
    RED = 1


class _Str(str):
    pass


_NAN = float("nan")
_VALUES = st.one_of(
    st.sampled_from([0, 1, -1, 2**64, 0.0, -0.0, 1.0, _NAN, True, False, None, "", "1", "a", b"", b"1", b"a"]),
    st.integers(),
    st.floats(allow_nan=False),
    st.text(max_size=140),
    st.binary(max_size=140),
    st.sampled_from([_Color.RED, _Str("a"), Decimal(1), [1], (1,), frozenset()]),
)
_CALLS = st.tuples(
    st.lists(_VALUES, max_size=_MEMO_MAX_ARGS + 1),
    st.dictionaries(st.sampled_from(["a", "b", "c", "x" * 100, _Str("s")]), _VALUES, max_size=3),
)


@pytest.mark.unit
class TestMemoKeyMatchesItsReference:
    @settings(max_examples=600, deadline=None)
    @given(call=_CALLS)
    def test_the_same_calls_are_memoised(self, call: tuple[list[Any], dict[str, Any]]) -> None:
        args, kwargs = tuple(call[0]), call[1]
        assert (_memo_key(args, kwargs) is None) == (_reference_memo_key(args, kwargs) is None)

    @settings(max_examples=600, deadline=None)
    @given(first=_CALLS, second=_CALLS)
    def test_two_calls_share_a_slot_exactly_when_they_did(
        self, first: tuple[list[Any], dict[str, Any]], second: tuple[list[Any], dict[str, Any]]
    ) -> None:
        keys = [(_memo_key(tuple(a), k), _reference_memo_key(tuple(a), k)) for a, k in (first, second)]
        (new_a, old_a), (new_b, old_b) = keys
        if old_a is None or old_b is None:
            return
        assert (new_a == new_b) == (old_a == old_b)

    def test_a_lone_int_or_str_argument_is_its_own_key(self) -> None:
        """The commonest call keys without a tuple to hash and compare on every hit; None cannot be its
        own key, as None means the memo does not take the call."""
        assert (_memo_key((7,), {}), _memo_key(("bob",), {}), _memo_key((None,), {})) == (7, "bob", (None,))

    def test_equal_calls_of_different_value_types_never_share_a_slot(self) -> None:
        for make in (lambda v: (v,), lambda v: (v, "a"), lambda v: ("a", v), lambda v: (v, 2, 3)):
            assert len({_memo_key(make(v), {}) for v in (1, 1.0, True)}) == 3
            assert _memo_key(make(b"a"), {}) != _memo_key(make("a"), {})


# ------------------------------------------------------------------------------------------------
# Exact LRU, with hits under the lock and without it
# ------------------------------------------------------------------------------------------------


class _Sized:
    """A value whose size estimate is whatever the test says: ObjectCache counts __sizeof__."""

    def __init__(self, n: int) -> None:
        self.n = n

    def __sizeof__(self) -> int:
        return self.n


def _memo_cache(**bounds: Any) -> tuple[ObjectCache, HitMemo]:
    def owner() -> None:
        pass

    memo = HitMemo(owner, HitCounter(), owner.__module__, owner.__qualname__)
    return ObjectCache(**bounds, memo=memo), memo


def _put(oc: ObjectCache, k: str, value: Any) -> None:
    oc.put(f"key:{k}", value, 3600, memo_key=_memo_key((k,), {}))


def _hit(memo: HitMemo, k: str) -> Any:
    return memo.get((k,), {}, _MISS)


_LRU_OPS = st.lists(
    st.tuples(
        st.sampled_from(["put", "get", "hit", "swr", "land", "delete"]),
        st.sampled_from("abcdefgh"),
        st.sampled_from([2, 3, 4, 5, 8, 16, 64]),
    ),
    min_size=60,  # long enough that evictions meet entries used since they were filed
    max_size=200,
)


@pytest.mark.unit
class TestExactLRU:
    """Drive an ObjectCache through puts, reads under its lock (get, get_with_swr), lock-free hits
    (HitMemo.get, with a memo), refreshes landed some operations after they were flagged, and deletes,
    against an OrderedDict LRU. Every access must hit or miss as the reference does, and the same keys
    must stay stored: no trace loses a hit to exact LRU. Deletes, replacements and refreshes leave
    dead records behind, so traces also run their rebuild. The clock advances on every read, as the OS
    monotonic clock does between two accesses."""

    @pytest.mark.parametrize("with_memo", [True, False], ids=["memo", "no-memo"])
    @pytest.mark.parametrize("budget", [1 << 20, 100 << 20], ids=["1MiB", "default-100MiB"])
    @settings(max_examples=150, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
    @given(ops=_LRU_OPS)
    def test_every_trace_matches_exact_lru(self, with_memo: bool, budget: int, ops: list[tuple[str, str, int]]) -> None:
        ticks = itertools.count()
        shared_clock.fake = lambda: 1000.0 + next(ticks) * 1e-6
        try:
            if with_memo:
                oc, memo = _memo_cache(max_entries=None, max_size_bytes=budget)
            else:
                oc, memo = ObjectCache(max_entries=None, max_size_bytes=budget), None
            reference: OrderedDict[str, int] = OrderedDict()
            values: dict[str, _Sized] = {}
            stored = itertools.count()
            stores: dict[str, int] = {}  # key -> which store of it the reference holds
            flagged: dict[str, tuple[int, int]] = {}  # key -> (version, store) of a refresh in flight

            def evict_over(extra: int) -> None:
                while reference and sum(reference.values()) + extra > budget:
                    reference.popitem(last=False)

            for op, k, divisor in ops:
                size = budget // divisor + 1  # no two fit at divisor 2
                if op == "put":
                    values[k] = value = _Sized(size)
                    _put(oc, k, value)
                    reference.pop(k, None)
                    evict_over(_sized(value))
                    reference[k] = _sized(value)
                    stores[k] = next(stored)
                elif op == "delete":
                    assert oc.delete(f"key:{k}") is (reference.pop(k, None) is not None)
                elif op == "land":
                    if k not in flagged:
                        continue
                    version, store = flagged.pop(k)
                    expected = k in reference and stores[k] == store  # the entry it was flagged on is stored
                    value = _Sized(size)
                    assert oc.complete_refresh(f"key:{k}", version, value, 3600) is expected, (op, k)
                    if expected:
                        values[k] = value
                        reference[k] = _sized(value)
                        reference.move_to_end(k)  # a refresh is a use; others go while over the budget
                        evict_over(0)
                else:
                    expected = k in reference
                    if expected:
                        reference.move_to_end(k)
                    if op == "hit" and memo is not None:
                        got = _hit(memo, k)
                        found = got is not _MISS
                    elif op == "swr":
                        found, got, _, version = oc.get_with_swr(f"key:{k}", 3600, _memo_key((k,), {}))
                        if found:
                            flagged[k] = (version, stores[k])
                    else:
                        found, got = oc.get(f"key:{k}", _memo_key((k,), {}))
                    assert found is expected, (op, k)
                    if found:
                        assert got is values[k]
                assert set(oc._state.store) == {f"key:{key}" for key in reference}
                if memo is not None:
                    assert _keys_held_by_entries(oc) == Counter(memo.keys())
        finally:
            shared_clock.fake = None


def _sized(value: _Sized) -> int:
    return object_cache._estimate_object_size(value)


# ------------------------------------------------------------------------------------------------
# Concurrency: hits, writes, invalidations and evictions on shared keys
# ------------------------------------------------------------------------------------------------


@pytest.mark.unit
def test_no_value_is_served_after_its_removal_returned(monkeypatch: pytest.MonkeyPatch) -> None:
    """Threads hit, fill, invalidate and clear a small key range whose values overflow the byte budget,
    so entries are also evicted all along. Every removal is stamped once it is done, on a counter each
    call reads before it starts: a hit that returns a value whose removal was stamped before the call
    started was served it after the removal returned. A call that joined a concurrent miss gets that
    miss's value, removed or not, by the single-flight contract, so it is not a hit and is not checked.
    Most telling with the GIL off (CPython 3.14t)."""
    from cachekit.decorators.single_flight import ThreadFlights

    joined = threading.local()
    run = ThreadFlights.run

    def noted_run(self: Any, *args: Any, **kwargs: Any) -> Any:
        result = run(self, *args, **kwargs)
        joined.flag = result[1]
        return result

    monkeypatch.setattr(ThreadFlights, "run", noted_run)
    tick_lock = threading.Lock()
    counter = itertools.count(1)

    def tick() -> int:
        with tick_lock:
            return next(counter)

    removed_at: dict[int, int] = {}
    remove = object_cache._ObjectCacheState.remove

    def stamped_remove(state: Any, key: str) -> None:
        entry = state.store.get(key)
        remove(state, key)
        if entry is not None:
            removed_at[id(entry.value)] = tick()  # under the cache's lock, once the removal is done

    monkeypatch.setattr(object_cache._ObjectCacheState, "remove", stamped_remove)
    payload = b"x" * 150_000  # about 7 fit the 1 MiB budget, of 16 keys
    created: list[Any] = []  # keeps every value alive, so no id() is reused

    @cache(backend=None, ttl=300, l1=L1CacheConfig(max_size_mb=1))
    def fn(k: int) -> tuple[int, bytes]:
        value = (k, payload)
        created.append(value)
        return value

    stop = threading.Event()
    violations: list[tuple[int, int, int]] = []
    served = itertools.count()

    def hitter(seed: int) -> None:
        rng = random.Random(seed)
        while not stop.is_set():
            k = rng.randrange(16)
            joined.flag = False
            start = tick()
            value = fn(k)
            next(served)
            at = removed_at.get(id(value))
            if at is not None and at < start and not joined.flag:
                violations.append((k, at, start))

    def invalidator(seed: int) -> None:
        rng = random.Random(seed)
        while not stop.is_set():
            if rng.random() < 0.05:
                fn.cache_clear()
            else:
                fn.invalidate_cache(rng.randrange(16))

    threads = [threading.Thread(target=hitter, args=(i,)) for i in range(6)]
    threads += [threading.Thread(target=invalidator, args=(100 + i,)) for i in range(2)]
    for thread in threads:
        thread.start()
    time.sleep(2.0)
    stop.set()
    for thread in threads:
        thread.join(30)
    assert not any(thread.is_alive() for thread in threads)
    assert not violations, violations[:5]
    assert next(served) > 1000 and len(removed_at) > 100  # the race was actually run
    assert math.isfinite(monotonic())
