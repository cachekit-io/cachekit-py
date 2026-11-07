"""@cache.local()'s lock-free hit: the HitMemo @cache(backend=None) has (test_l1_lock_free_hit.py), on the reference cache.

A default-key function answers a hit in one HitMemo.get call that builds no key and takes no lock; a key=
function keeps the locked path. Pins, for @cache.local(): the key each call is served under, the counts,
and that no route serves an entry once its removal has returned (expiry, eviction, invalidation, a
whole-function invalidation, the at-fork reset). The concurrent removal race runs in
test_l1_lock_free_hit.py, for both decorators.
"""

from __future__ import annotations

import asyncio
import os
import signal
import threading
import weakref
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from cachekit import cache
from cachekit._rust_serializer import MEMO_MAX_LEN
from cachekit.decorators.wrapper import _memo_key
from cachekit.key_generator import CacheKeyGenerator
from tests.unit.test_key_registry import _closure_cell
from tests.unit.test_l1_lock_free_hit import _gone, _Value
from tests.utils.clock import fake_clock
from tests.utils.fork_helpers import child_outcome, report


def _cache(fn: Any) -> Any:
    return _closure_cell(fn, "object_cache").cell_contents


def _memo(fn: Any) -> Any:
    return _closure_cell(fn, "memo").cell_contents


def _reads(fn: Any) -> list[str]:
    """Record the key of every lookup the function's cache makes under its lock; a lock-free hit makes none."""
    oc = _cache(fn)
    reads: list[str] = []
    get = oc.get

    def recording(key: str, *rest: Any) -> Any:
        reads.append(key)
        return get(key, *rest)

    oc.get = recording  # an instance attribute, so the cache keeps its memo wiring
    return reads


def _key_now(fn: Any, args: tuple[Any, ...], kwargs: dict[str, Any]) -> str:
    """The key @cache.local() builds for this call right now: the bytes the memo must reproduce."""
    return CacheKeyGenerator().generate_key(
        fn.__wrapped__, args, kwargs, namespace=None, integrity_checking=False, serializer_type="local"
    )


def _key_used(fn: Any, args: tuple[Any, ...], kwargs: dict[str, Any]) -> str | None:
    """The cache key a lock-free hit for this call is served under, or None when the call has no memo slot."""
    memo_key = _memo_key(args, kwargs)
    for key, entry in _cache(fn)._state.store.items():
        if entry.hit is not None and memo_key in entry.hit.memo_keys:
            return key
    return None


def _local(is_async: bool, **options: Any) -> tuple[Any, Any, list[Any]]:
    """A @cache.local() function returning a new _Value per run, a plain function that calls it, and its runs."""
    runs: list[Any] = []
    if is_async:

        @cache.local(**options)
        async def afn(x: Any) -> _Value:
            runs.append(x)
            return _Value(x)

        return afn, lambda x: asyncio.run(afn(x)), runs

    @cache.local(**options)
    def fn(x: Any) -> _Value:
        runs.append(x)
        return _Value(x)

    return fn, fn, runs


_SYNC_ASYNC = pytest.mark.parametrize("is_async", [False, True], ids=["sync", "async"])


@pytest.mark.unit
class TestTheHit:
    @_SYNC_ASYNC
    def test_a_hit_builds_no_key_and_takes_no_lock(self, is_async: bool) -> None:
        fn, call, runs = _local(is_async)
        key_gen = _closure_cell(fn, "key_gen").cell_contents
        built: list[str] = []
        generate = key_gen.generate_key

        def counting(*args: Any, **kwargs: Any) -> str:
            built.append(generate(*args, **kwargs))
            return built[-1]

        key_gen.generate_key = counting
        reads = _reads(fn)
        results = [call(7) for _ in range(4)]
        assert all(result is results[0] for result in results)
        assert runs == [7]
        assert built == reads == [_key_now(fn, (7,), {})]  # the miss alone builds a key and takes the lock
        assert _cache(fn).hits == 0

    @_SYNC_ASYNC
    def test_cache_info_counts_the_lock_free_hits(self, is_async: bool) -> None:
        fn, call, _ = _local(is_async)
        for _ in range(4):
            call(5)
        info = fn.cache_info()
        assert (info.hits, info.l1_hits, info.misses, info.currsize) == (3, 3, 1, 1)
        assert _cache(fn).hits == 0  # all three were lock-free
        fn.cache_clear()
        call(5)
        call(5)
        assert (fn.cache_info().hits, fn.cache_info().misses) == (4, 2)  # lifetime counts, as before

    def test_a_custom_key_function_keeps_the_locked_path(self) -> None:
        """key= can read more than the arguments (a context variable, say), so no memo answers for it."""

        @cache.local(key=lambda x: f"k:{x}")
        def fn(x: int) -> _Value:
            return _Value(x)

        assert _memo(fn) is None
        first = fn(1)
        assert fn(1) is first and fn(1) is first
        assert (_cache(fn).hits, fn.cache_info().hits) == (2, 2)

    def test_names_that_are_not_exact_str_are_never_memoised(self) -> None:
        def fn(x: int) -> int:
            return x

        fn.__qualname__ = type("_Name", (str,), {})("fn")
        assert _memo(cache.local()(fn)) is None

    @_SYNC_ASYNC
    def test_a_rename_after_the_first_call_rekeys(self, is_async: bool) -> None:
        """The lock-free hit answers only while the function has the names its memo was built with."""
        fn, call, _ = _local(is_async)
        reads = _reads(fn)
        call(1)
        fn.__wrapped__.__qualname__ = "renamed"
        renamed = _key_now(fn, (1,), {})
        call(1)
        fn.__wrapped__.__module__ = "elsewhere"
        moved = _key_now(fn, (1,), {})
        call(1)
        call(1)
        assert ":renamed:" not in reads[0]
        assert reads[1:] == [renamed, moved, moved]
        assert ":elsewhere.renamed:" in moved

    @_SYNC_ASYNC
    def test_a_rename_undone_while_the_key_is_built_memoises_no_other_identity(self, is_async: bool) -> None:
        """A rename landing between the call's read of the names and its key build (another thread's), then
        undone, must not leave the memo answering for an entry stored under the renamed identity: that
        entry's key is not the one invalidate_cache() builds, so the stale value would outlive it."""
        fn, call, runs = _local(is_async)
        key_gen = _closure_cell(fn, "key_gen").cell_contents
        generate = key_gen.generate_key
        original = fn.__wrapped__.__qualname__

        def renaming(*args: Any, **kwargs: Any) -> str:
            fn.__wrapped__.__qualname__ = "renamed"
            return generate(*args, **kwargs)

        key_gen.generate_key = renaming
        first = call(1)
        del key_gen.generate_key
        fn.__wrapped__.__qualname__ = original
        assert list(_cache(fn)._state.store) == [_key_now(fn, (1,), {})]  # stored under the names it read
        assert call(1) is first
        if is_async:
            asyncio.run(fn.ainvalidate_cache(1))
        else:
            fn.invalidate_cache(1)
        assert call(1) is not first
        assert runs == [1, 1]

    # Values that compare equal across types (1, 1.0, True; 0, 0.0, -0.0, False), values just past
    # the length cap, and a few the memo never takes, so equal-but-different calls interleave.
    _ARGS = st.sampled_from(
        [0, 1, 1.0, True, False, 0.0, -0.0, None, "", "1", b"1", "x" * MEMO_MAX_LEN, "x" * (MEMO_MAX_LEN + 1), [1]]
    )

    @settings(max_examples=200, deadline=None)
    @given(
        calls=st.lists(
            st.tuples(st.lists(_ARGS, max_size=3), st.dictionaries(st.sampled_from("ab"), _ARGS, max_size=2)), max_size=12
        )
    )
    def test_any_call_sequence_is_served_under_the_generated_key(self, calls: list[tuple[list[Any], dict[str, Any]]]) -> None:
        @cache.local(ttl=60)
        def fn(*a: Any, **k: Any) -> int:
            return 1

        reads = _reads(fn)
        for args, kwargs in calls:
            before = len(reads)
            fn(*args, **kwargs)
            key = _key_now(fn, tuple(args), kwargs)
            assert (reads[-1] if len(reads) > before else _key_used(fn, tuple(args), kwargs)) == key


@pytest.mark.unit
class TestRemovalRoutes:
    """Each route: the call after the removal returned runs the function again, the memo has no slot
    for the removed entry, and once the test drops its own reference the removed value is freed."""

    def test_expiry(self, monkeypatch: pytest.MonkeyPatch) -> None:
        clock = fake_clock(monkeypatch)
        fn, call, runs = _local(False, ttl=10)
        first = call(1)
        assert call(1) is first
        assert _cache(fn).hits == 0  # served by the lock-free hit
        ref = weakref.ref(first)
        del first
        clock.monotonic = lambda: 1010.0  # the hard deadline itself
        assert call(1) is not ref()
        assert runs == [1, 1]
        assert _gone(ref)

    def test_eviction_by_the_entry_bound(self) -> None:
        """Eviction is least recently used, a lock-free hit counting as a use."""
        fn, call, runs = _local(False, max_entries=2)
        memo = _memo(fn)
        ref = weakref.ref(call(1))
        call(2)
        call(1)  # a lock-free hit: 1 is now used after 2
        call(3)  # evicts 2
        assert sorted(memo.keys()) == [_memo_key((1,), {}), _memo_key((3,), {})]
        call(4)  # evicts 1
        assert sorted(memo.keys()) == [_memo_key((3,), {}), _memo_key((4,), {})]
        assert _gone(ref)
        call(1)
        assert runs == [1, 2, 3, 4, 1]

    @_SYNC_ASYNC
    def test_invalidation(self, is_async: bool) -> None:
        fn, call, runs = _local(is_async)
        memo = _memo(fn)
        first = call(1)
        call(2)
        assert call(1) is first
        ref = weakref.ref(first)
        del first
        if is_async:
            asyncio.run(fn.ainvalidate_cache(1))
        else:
            fn.invalidate_cache(1)
        assert memo.keys() == [_memo_key((2,), {})]
        assert _gone(ref)
        call(1)
        call(2)
        assert runs == [1, 2, 1]

    @pytest.mark.parametrize("clear", ["cache_clear", "invalidate_cache", "ainvalidate_cache"])
    def test_a_whole_function_invalidation(self, clear: str) -> None:
        """cache_clear() and a no-args invalidation of a function that takes parameters remove every entry."""
        fn, call, runs = _local(False)
        memo = _memo(fn)
        refs = [weakref.ref(call(x)) for x in (1, 2)]
        assert len(memo) == 2
        outcome = getattr(fn, clear)()
        if asyncio.iscoroutine(outcome):
            asyncio.run(outcome)
        assert (len(memo), _cache(fn).size) == (0, 0)
        assert all(_gone(ref) for ref in refs)
        call(1)
        assert runs == [1, 2, 1]

    @pytest.mark.skipif(not hasattr(os, "fork"), reason="fork() not available on this platform")
    def test_the_at_fork_reset(self) -> None:
        """A parent thread holds the cache's lock at fork, so the child's cache starts empty: its memo
        must be empty too, and the entry the parent memoised dead, or the child would serve it."""
        runs: list[int] = []

        @cache.local()
        def fn(x: int) -> int:
            runs.append(x)
            return x * 2

        oc, memo = _cache(fn), _memo(fn)
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
                    outcome: object = {"memo": len(memo), "alive": hit.alive, "value": fn(1), "runs": len(runs)}
                except BaseException as e:
                    outcome = {"error": repr(e)}
                report(w, outcome)
        finally:
            release.set()
            holder.join(5)
        os.close(w)

        assert child_outcome(child, r) == {"memo": 0, "alive": False, "value": 2, "runs": 2}
        assert (hit.alive, len(memo)) == (True, 1)  # the parent's own cache is untouched
