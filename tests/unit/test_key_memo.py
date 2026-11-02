"""The L1-only memo and its lock-free hit.

An L1-only function memoises, by argument tuple, the ObjectCache entry each call stored or found
(cachekit._rust_serializer.HitMemo), and a hit on a memoised call is one call into the extension that
takes no lock and sets no stats context. These pin what must not change: the key bytes, the counters,
which arguments may never share a memo slot, and which functions keep no memo.
"""

from __future__ import annotations

import asyncio
import enum
import sys
import threading
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from cachekit import cache
from cachekit._rust_serializer import MEMO_MAX_ARGS, MEMO_MAX_ENTRIES, MEMO_MAX_LEN
from cachekit.decorators import wrapper as wrapper_module
from cachekit.decorators.stats_context import get_current_function_stats
from cachekit.decorators.wrapper import _memo_key
from cachekit.key_generator import CacheKeyGenerator
from tests.unit.test_key_registry import _closure_cell


class _Color(enum.IntEnum):
    RED = 1


class _Str(str):
    pass


class _Int(int):
    pass


class _EqMeta(type):
    """A metaclass defining __eq__, which leaves its classes unhashable."""

    def __eq__(cls, other: object) -> bool:
        return NotImplemented


class _Token(metaclass=_EqMeta):
    pass


class _IntLikeMeta(type):
    """Makes its classes hash and compare equal to int."""

    def __hash__(cls) -> int:
        return hash(int)

    def __eq__(cls, other: object) -> bool:
        return other is int or type.__eq__(cls, other)


class _IntLike(metaclass=_IntLikeMeta):
    def __init__(self, v: int) -> None:
        self.v = v

    def __hash__(self) -> int:
        return hash(self.v)

    def __eq__(self, other: object) -> bool:
        return other == self.v


class _RecordingBackend:
    """In-process L2 that records the key of every read, so a test sees the key each call used."""

    def __init__(self) -> None:
        self.store: dict[str, bytes] = {}
        self.reads: list[str] = []

    def get(self, key: str) -> bytes | None:
        self.reads.append(key)
        return self.store.get(key)

    def set(self, key: str, value: bytes, ttl: int | None = None) -> None:
        self.store[key] = value

    def delete(self, key: str) -> bool:
        return self.store.pop(key, None) is not None

    def exists(self, key: str) -> bool:
        return key in self.store

    def health_check(self) -> tuple[bool, dict[str, Any]]:
        return True, {}


def _l1_reads(fn: Any) -> list[str]:
    """Record the key of every lookup an L1-only function makes under its cache's lock; returns them.

    A call the memo does not answer looks up once, under the key it built; a lock-free hit makes no
    lookup, and the key it was served under is the one its entry was memoised by (see _key_used)."""
    oc = _closure_cell(fn, "_object_cache").cell_contents
    reads: list[str] = []
    for name in ("get", "get_with_swr"):
        method = getattr(oc, name)

        def recording(key: str, *rest: Any, _method: Any = method, **kwargs: Any) -> Any:
            reads.append(key)
            return _method(key, *rest, **kwargs)

        setattr(oc, name, recording)  # an instance attribute, so the cache keeps its memo wiring
    return reads


def _memo(fn: Any) -> Any:
    """An L1-only function's HitMemo: len() counts its slots, keys() lists their memo keys."""
    return _closure_cell(fn, "_l1_memo").cell_contents


def _key_used(fn: Any, args: tuple[Any, ...], kwargs: dict[str, Any]) -> str | None:
    """The cache key a lock-free hit for this call is served under: the key of the stored entry its
    memo slot points at, or None when the call has no slot."""
    memo_key = _memo_key(args, kwargs)
    store = _closure_cell(fn, "_object_cache").cell_contents._state.store
    for key, entry in store.items():
        if entry.hit is not None and memo_key in entry.hit.memo_keys:
            return key
    return None


class _Gate:
    """An L1-only function's cache whose delete and clear wait for the test, so an invalidation (of
    one key, or of the whole function) can be held between deciding to erase entries and erasing
    them while the test makes another call."""

    def __init__(self, fn: Any) -> None:
        self.deleting = threading.Event()
        self.release = threading.Event()
        oc = _closure_cell(fn, "_object_cache").cell_contents

        def held(remove: Any) -> Any:
            def gated(*args: Any) -> Any:
                self.deleting.set()
                assert self.release.wait(10), "the test never released the delete"
                return remove(*args)

            return gated

        oc.delete, oc.clear = held(oc.delete), held(oc.clear)


def _gate(fn: Any) -> _Gate:
    """Gate an L1-only function's cache deletes, before its first call."""
    return _Gate(fn)


def _deep_size(obj: Any, seen: set[int] | None = None) -> int:
    """Bytes held by obj and every dict, tuple, str, bytes and number under it, each counted once."""
    seen = set() if seen is None else seen
    if id(obj) in seen or isinstance(obj, type):
        return 0
    seen.add(id(obj))
    size = sys.getsizeof(obj)
    if isinstance(obj, dict):
        size += sum(_deep_size(k, seen) + _deep_size(v, seen) for k, v in obj.items())
    elif isinstance(obj, tuple):
        size += sum(_deep_size(item, seen) for item in obj)
    return size


def _l1_only(is_async: bool) -> tuple[Any, Any]:
    """An L1-only function returning its argument, sync or async, and a plain function that calls it."""
    if is_async:

        @cache(backend=None, ttl=60, namespace="memo", encryption=False)
        async def afn(x: int) -> int:
            return x

        return afn, lambda x: asyncio.run(afn(x))

    @cache(backend=None, ttl=60, namespace="memo", encryption=False)
    def fn(x: int) -> int:
        return x

    return fn, fn


class _MutableName:
    """A __module__ whose text changes while it stays the same object."""

    text = "before"

    def __str__(self) -> str:
        return self.text


# Arguments no memo entry may answer for: containers, subclasses that compare equal to an admitted
# type, other numeric types, and str/bytes past the length cap.
_NOT_MEMOISED = [
    [1],
    (1,),
    {"a": 1},
    _Color.RED,
    _Str("a"),
    _Int(1),
    Decimal(1),
    frozenset(),
    "x" * (MEMO_MAX_LEN + 1),
    b"x" * (MEMO_MAX_LEN + 1),
]


@pytest.mark.unit
class TestMemoKey:
    def test_equal_values_of_different_types_never_share_an_entry(self) -> None:
        for shape in (lambda v: ((v,), {}), lambda v: ((v, "a"), {}), lambda v: ((v, 2, 3), {}), lambda v: ((), {"x": v})):
            keys = {_memo_key(*shape(v)) for v in (1, 1.0, True)}
            assert len(keys) == 3, keys

    @pytest.mark.parametrize("value", _NOT_MEMOISED, ids=lambda v: type(v).__name__)
    def test_only_exact_admitted_types_are_memoised(self, value: Any) -> None:
        assert _memo_key((value,), {}) is None
        assert _memo_key((1, value), {}) is None
        assert _memo_key((value, 1), {}) is None
        assert _memo_key((1, 2, value), {}) is None
        assert _memo_key((), {"x": value}) is None

    def test_admitted_types_at_the_length_cap_are_memoised(self) -> None:
        for value in (0, -1, 2**63, 1.5, -0.0, True, None, "", "x" * MEMO_MAX_LEN, b"x" * MEMO_MAX_LEN):
            assert _memo_key((value,), {}) is not None

    def test_a_str_subclass_keyword_name_is_not_memoised(self) -> None:
        assert _memo_key((), {_Str("x"): 1}) is None

    def test_strings_and_keyword_names_share_one_length_budget(self) -> None:
        half, rest = MEMO_MAX_LEN // 2, MEMO_MAX_LEN - MEMO_MAX_LEN // 2
        for extra in (0, 1):
            fits = extra == 0
            assert (_memo_key(("x" * half, "y" * (rest + extra)), {}) is not None) is fits
            assert (_memo_key((b"x" * half, 1, "y" * (rest + extra)), {}) is not None) is fits
            assert (_memo_key(("x" * half,), {"y" * (rest - 1 + extra): "z"}) is not None) is fits
            assert (_memo_key((), {"x": "y" * (MEMO_MAX_LEN - 2 + extra), "n": 1}) is not None) is fits

    def test_types_are_tested_by_identity_never_hashed_or_compared(self) -> None:
        """A metaclass that defines __eq__ makes a class unhashable; one that hashes and compares
        like int would pass a set-membership test. Neither may reach the memo."""
        for value in (_Token(), _IntLike(1)):
            assert _memo_key((value,), {}) is None
            assert _memo_key((1, value), {}) is None
            assert _memo_key((1, 2, value), {}) is None
            assert _memo_key((), {"x": value}) is None

    def test_at_most_max_args_arguments_are_memoised(self) -> None:
        assert _memo_key(tuple(range(MEMO_MAX_ARGS)), {}) is not None
        assert _memo_key(tuple(range(MEMO_MAX_ARGS + 1)), {}) is None
        assert _memo_key(tuple(range(MEMO_MAX_ARGS)), {"x": 1}) is None

    def test_shapes_never_collide(self) -> None:
        calls = [((1,), {}), ((1, 2), {}), ((1, 2, 3), {}), ((), {}), ((1,), {"b": 2}), ((1, 2), {"b": 2})]
        keys = [_memo_key(a, k) for a, k in calls]
        assert len(set(keys)) == len(calls)


def _key_now(fn: Any, args: tuple[Any, ...], kwargs: dict[str, Any], namespace: str) -> str:
    """The key the key generator builds for this call right now: the bytes the memo must reproduce."""
    return CacheKeyGenerator().generate_key(fn.__wrapped__, args, kwargs, namespace, True, serializer_type="default")


@pytest.mark.unit
class TestKeyBytesUnchanged:
    """Every L1-only call looks up its object cache once, under the key it used (see
    _RecordingObjectCache): a memoised call's must be the one the key generator builds now."""

    @pytest.mark.parametrize(
        ("args", "kwargs", "memoised"),
        [
            ((1,), {}, True),
            ((1.0,), {}, True),
            ((True,), {}, True),
            ((-0.0,), {}, True),
            (("a",), {}, True),
            ((b"a",), {}, True),
            ((None,), {}, True),
            ((42, "user-profile"), {}, True),
            ((1, 2, 3), {}, True),
            (("q",), {"limit": 10}, True),
            (("q",), {"limit": 10, "offset": 5}, True),
            (("x" * (MEMO_MAX_LEN + 1),), {}, False),
            ((_Color.RED,), {}, False),
            (([1, 2],), {}, False),
        ],
    )
    def test_every_call_reads_the_generated_key(self, args: tuple[Any, ...], kwargs: dict[str, Any], memoised: bool) -> None:
        """The second and third calls of a memoised row read the memo's key; the others build theirs."""

        @cache(backend=None, ttl=60, namespace="memo", encryption=False)
        def fn(*a: Any, **k: Any) -> int:
            return 1

        reads = _l1_reads(fn)
        hits = fn.cache_info().l1_hits  # counted per function identity, which every row's fn shares
        for _ in range(3):
            fn(*args, **kwargs)
        key = _key_now(fn, args, kwargs, "memo")
        if memoised:
            assert reads == [key]  # the second and third calls are lock-free hits on that entry
            assert _key_used(fn, args, kwargs) == key
            assert fn.cache_info().l1_hits - hits == 2
        else:
            assert reads == [key] * 3
        assert len(_memo(fn)) == (1 if memoised else 0)

    # Values that compare equal across types (1, 1.0, True; 0, 0.0, -0.0, False), values just past
    # the length cap, and a few the memo never takes, so equal-but-different calls interleave.
    _ARGS = st.sampled_from(
        [0, 1, 1.0, True, False, 0.0, -0.0, None, "", "1", b"1", "x" * MEMO_MAX_LEN, "x" * (MEMO_MAX_LEN + 1), _Color.RED, [1]]
    )

    @settings(max_examples=200, deadline=None)
    @given(
        calls=st.lists(
            st.tuples(st.lists(_ARGS, max_size=3), st.dictionaries(st.sampled_from("ab"), _ARGS, max_size=2)), max_size=12
        )
    )
    def test_any_call_sequence_reads_the_generated_keys(self, calls: list[tuple[list[Any], dict[str, Any]]]) -> None:
        @cache(backend=None, ttl=60, namespace="memo", encryption=False)
        def fn(*a: Any, **k: Any) -> int:
            return 1

        reads = _l1_reads(fn)
        for args, kwargs in calls:
            before = len(reads)
            fn(*args, **kwargs)
            key = _key_now(fn, tuple(args), kwargs, "memo")
            # A lookup under the lock reads the key the call built; a lock-free hit makes none and is
            # served under its memoised entry's key.
            assert (reads[-1] if len(reads) > before else _key_used(fn, tuple(args), kwargs)) == key

    def test_keyword_order_does_not_change_the_key(self) -> None:
        @cache(backend=None, ttl=60, namespace="memo", encryption=False)
        def fn(**k: Any) -> int:
            return 1

        reads = _l1_reads(fn)
        fn(a=1, b=2)
        fn(b=2, a=1)
        fn(a=1, b=2)
        key = _key_now(fn, (), {"a": 1, "b": 2}, "memo")
        assert reads == [key]  # one memo slot answers both orders
        assert _key_used(fn, (), {"b": 2, "a": 1}) == key

    @pytest.mark.parametrize("is_async", [False, True], ids=["sync", "async"])
    def test_a_rename_after_the_first_call_rekeys(self, is_async: bool) -> None:
        """The lock-free hit answers only while the function has the names its memo was built with."""
        fn, call = _l1_only(is_async)
        reads = _l1_reads(fn)
        call(1)
        fn.__wrapped__.__qualname__ = "renamed"
        renamed = _key_now(fn, (1,), {}, "memo")
        call(1)
        fn.__wrapped__.__module__ = "elsewhere"
        moved = _key_now(fn, (1,), {}, "memo")
        call(1)
        call(1)
        assert ":renamed:" not in reads[0]
        assert reads[1:] == [renamed, moved, moved]
        assert ":elsewhere.renamed:" in moved

    def test_a_rename_rekeys_a_callable_that_is_not_a_plain_function(self) -> None:
        """The memo reads every callable's names by attribute access, so a rename re-keys any callable,
        here a bound method reporting its function's names, as it re-keys a plain function."""

        class Service:
            def lookup(self, x: int) -> int:
                return x

        fn = cache(backend=None, ttl=60, namespace="memo", encryption=False)(Service().lookup)
        reads = _l1_reads(fn)
        fn(1)
        fn(1)  # a lock-free hit: no read under the lock
        Service.lookup.__qualname__ = "renamed"
        renamed = _key_now(fn, (1,), {}, "memo")
        fn(1)
        fn(1)
        assert ":renamed:" not in reads[0]
        assert reads[1:] == [renamed, renamed]  # the memo answers only for the names it was built with

    def test_names_that_are_not_exact_str_are_never_memoised(self) -> None:
        """The generator formats __module__ and __qualname__ on every call, and an object can format
        another way while it stays the same object, so no memo entry may answer for it."""

        @cache(backend=None, ttl=60, namespace="memo", encryption=False)
        def fn(x: int) -> int:
            return x

        reads = _l1_reads(fn)
        module = _MutableName()
        fn.__wrapped__.__module__ = module
        fn(1)
        before = _key_now(fn, (1,), {}, "memo")
        module.text = "after"
        fn(1)
        assert reads == [before, _key_now(fn, (1,), {}, "memo")]
        assert reads[0] != reads[1]
        fn.__wrapped__.__module__ = "plain"
        fn.__wrapped__.__qualname__ = _Str("fn")
        fn(1)
        assert len(_memo(fn)) == 0

    @pytest.mark.parametrize(
        "decorate",
        [
            lambda b: cache(backend=b, ttl=60, namespace="memo", encryption=False),
            lambda b: cache(backend=b, l1_enabled=False, ttl=60, namespace="memo", encryption=False),
            lambda b: cache.secure(master_key="ab" * 32, backend=b, ttl=60, namespace="memo"),
        ],
        ids=["l1", "l1-off", "encrypted"],
    )
    def test_a_backed_function_keeps_no_memo(self, decorate: Any) -> None:
        """Other processes invalidate a backed function's L1, and with L1 off or encryption on the
        process otherwise holds no plaintext argument, so a backed call builds its key every time."""
        backend = _RecordingBackend()

        @decorate(backend)
        def fn(x: str) -> str:
            return x

        for _ in range(3):
            assert fn("alice@example.com") == "alice@example.com"
        assert _memo(fn) is None
        assert len(set(backend.reads)) == 1


@pytest.mark.unit
class TestL1OnlyFastPath:
    def test_equal_arguments_of_different_types_cache_apart(self) -> None:
        calls = 0

        @cache(backend=None, encryption=False)
        def fn(x: Any) -> str:
            nonlocal calls
            calls += 1
            return repr(x)

        for _ in range(3):
            assert [fn(1), fn(1.0), fn(True)] == ["1", "1.0", "True"]
        assert calls == 3

    def test_hits_from_many_threads_are_counted_exactly(self) -> None:
        @cache(backend=None, encryption=False)
        def fn(x: int) -> int:
            return x

        fn(7)
        threads, per_thread = 8, 2_000
        barrier = threading.Barrier(threads)

        def hit() -> None:
            barrier.wait()
            for _ in range(per_thread):
                fn(7)

        workers = [threading.Thread(target=hit) for _ in range(threads)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join()
        info = fn.cache_info()
        assert (info.hits, info.l1_hits, info.misses) == (threads * per_thread, threads * per_thread, 1)
        fn.cache_clear()
        assert fn.cache_info().l1_hits == 0

    def test_the_memo_stays_bounded(self) -> None:
        @cache(backend=None, encryption=False)
        def fn(x: int) -> int:
            return x

        for i in range(MEMO_MAX_ENTRIES + 10):
            fn(i)
        assert 0 < len(_memo(fn)) <= MEMO_MAX_ENTRIES

    def test_a_miss_runs_outside_the_stats_context(self) -> None:
        """Only a backed request reads the stats context, so an L1-only miss runs the function without one."""
        seen: list[Any] = []

        @cache(backend=None, encryption=False)
        def fn(x: int) -> int:
            seen.append(get_current_function_stats())
            return x

        fn(1)
        fn(1)
        fn.invalidate_cache(1)
        fn(1)
        assert len(seen) == 2
        assert all(stats is None for stats in seen)

    def test_metaclass_arguments_run_uncached_as_before(self) -> None:
        calls = 0

        @cache(backend=None, encryption=False)
        def fn(x: Any) -> str:
            nonlocal calls
            calls += 1
            return type(x).__name__

        assert [fn(1), fn(1), fn(_IntLike(1)), fn(_Token()), fn(_Token())] == ["int", "int", "_IntLike", "_Token", "_Token"]
        assert calls == 4  # one miss for 1; the others cannot be keyed, so they run every time

    def test_invalidation_forgets_the_memoised_arguments(self) -> None:
        @cache(backend=None, encryption=False)
        def fn(x: str) -> str:
            return x

        memo = _memo(fn)
        fn("alice@example.com")
        fn("bob@example.com")
        assert len(memo) == 2
        fn.invalidate_cache("alice@example.com")
        assert memo.keys() == [_memo_key(("bob@example.com",), {})]
        fn.cache_clear()
        assert len(memo) == 0

    def test_invalidation_with_keywords_in_another_order_forgets_the_entry(self) -> None:
        @cache(backend=None, encryption=False)
        def fn(*, user: str, org: str) -> str:
            return user

        memo = _memo(fn)
        fn(user="alice@example.com", org="a")
        fn(org="a", user="alice@example.com")
        assert len(memo) == 1
        fn.invalidate_cache(org="a", user="alice@example.com")
        assert len(memo) == 0

    def test_invalidation_spelled_another_way_forgets_the_memoised_arguments(self) -> None:
        """A Path keys like its str, and invalidate_cache keys without _bypass_cache: each erases the
        entry, so each must also forget whatever arguments the memo kept for that key."""
        calls = 0

        @cache(backend=None, encryption=False)
        def fn(x: Any) -> int:
            nonlocal calls
            calls += 1
            return 1

        memo = _memo(fn)
        fn("alice@example.com")
        fn("bob@example.com")
        fn.invalidate_cache(Path("alice@example.com"))
        assert memo.keys() == [_memo_key(("bob@example.com",), {})]
        fn.invalidate_cache("bob@example.com", _bypass_cache=True)
        assert len(memo) == 0
        fn("alice@example.com")
        fn("bob@example.com")
        assert calls == 4

    @pytest.mark.parametrize("scope", ["one", "all"])
    def test_a_call_during_an_invalidation_leaves_no_arguments_behind(self, scope: str) -> None:
        """A call made while an invalidation is deleting its entry can still hit that entry. Had the
        memo already forgotten the call, it would memoise the arguments again and keep them after
        the entry is gone; the memo forgets after the delete, as the calls in flight do."""
        calls = 0

        @cache(backend=None, ttl=60, encryption=False)
        def fn(x: str) -> str:
            nonlocal calls
            calls += 1
            return x

        gated = _gate(fn)
        fn("alice@example.com")
        invalidate = (lambda: fn.invalidate_cache("alice@example.com")) if scope == "one" else fn.invalidate_cache
        worker = threading.Thread(target=invalidate)
        worker.start()
        assert gated.deleting.wait(10)
        assert fn("alice@example.com") == "alice@example.com"
        assert calls == 1  # a hit on the entry being deleted
        gated.release.set()
        worker.join(10)
        assert not worker.is_alive()
        assert len(_memo(fn)) == 0
        fn("alice@example.com")
        assert calls == 2

    def test_an_async_call_during_an_invalidation_leaves_no_arguments_behind(self) -> None:
        """ainvalidate_cache deletes in a worker thread, so the event loop serves calls meanwhile."""
        calls = 0

        @cache(backend=None, ttl=60, encryption=False)
        async def fn(x: str) -> str:
            nonlocal calls
            calls += 1
            return x

        gated = _gate(fn)

        async def run() -> None:
            await fn("bob@example.com")
            invalidation = asyncio.ensure_future(fn.ainvalidate_cache("bob@example.com"))
            assert await asyncio.to_thread(gated.deleting.wait, 10)
            assert await fn("bob@example.com") == "bob@example.com"
            gated.release.set()
            await invalidation

        asyncio.run(run())
        assert calls == 1
        assert len(_memo(fn)) == 0

    def test_a_full_memo_at_every_cap_stays_small(self) -> None:
        """Every call at every cap: eight keyword names and eight values of 4-byte characters, sharing
        MEMO_MAX_LEN between them, the most a call can make the memo keep. docs/performance.md
        states this bound: 3.7 to 4.0MiB measured on CPython 3.10, 3.12, 3.14 and 3.14t."""

        @cache(backend=None, encryption=False)
        def fn(**kwargs: Any) -> int:
            return 1

        wide = "\U0001f600"
        each = MEMO_MAX_LEN // (2 * MEMO_MAX_ARGS)
        for i in range(MEMO_MAX_ENTRIES):
            tag = f"{i:04x}"
            fn(**{tag + chr(97 + j) + wide * (each - 5): tag + wide * (each - 4) for j in range(MEMO_MAX_ARGS)})
        memo = _memo(fn)
        assert len(memo) == MEMO_MAX_ENTRIES
        assert _deep_size(memo.keys()) < 4.5 * 2**20  # what the memo keeps beyond the cache's entries

    def test_keyword_names_past_the_cap_cache_without_the_memo(self) -> None:
        calls = 0

        @cache(backend=None, encryption=False)
        def fn(**kwargs: Any) -> int:
            nonlocal calls
            calls += 1
            return 1

        name = "x" * (MEMO_MAX_LEN + 1)
        for _ in range(3):
            fn(**{name: 1})
        assert calls == 1
        assert len(_memo(fn)) == 0

    def test_bypass_and_invalidation_reach_a_memoised_call(self) -> None:
        calls = 0

        @cache(backend=None, encryption=False)
        def fn(x: int) -> int:
            nonlocal calls
            calls += 1
            return x

        fn(1)
        fn(1)
        fn(1, _bypass_cache=True)
        assert calls == 2
        fn.invalidate_cache(1)
        fn(1)
        assert calls == 3

    @pytest.mark.parametrize("is_async", [False, True], ids=["sync", "async"])
    def test_no_l1_only_call_sets_the_stats_context(self, is_async: bool, monkeypatch: pytest.MonkeyPatch) -> None:
        """_sync_call and _async_call set the stats context only past the backed L1 hit, so an L1-only call never does."""
        sets = 0
        set_stats = wrapper_module.set_current_function_stats

        def counting(stats: Any) -> Any:
            nonlocal sets
            sets += 1
            return set_stats(stats)

        monkeypatch.setattr(wrapper_module, "set_current_function_stats", counting)
        fn, call = _l1_only(is_async)
        fn.cache_clear()  # cache_info() counts per function identity, which _l1_only's functions share
        for _ in range(5):
            assert call(3) == 3
        info = fn.cache_info()
        assert (sets, info.l1_hits, info.misses) == (0, 4, 1)
