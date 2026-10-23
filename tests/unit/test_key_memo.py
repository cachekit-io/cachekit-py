"""The generated-key memo and the L1-only fast path.

A decorated function remembers the key of each argument tuple it has generated one for, and an
L1-only hit on a remembered key returns without setting the stats context. These pin what must not
change: the key bytes, the counters, and which arguments may never share a memo entry.
"""

from __future__ import annotations

import asyncio
import enum
import threading
import types
from decimal import Decimal
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from cachekit import cache
from cachekit.decorators.stats_context import get_current_function_stats
from cachekit.decorators.wrapper import _MEMO_MAX_ARGS, _MEMO_MAX_ENTRIES, _MEMO_MAX_LEN, _FunctionStats, _memo_key
from cachekit.key_generator import CacheKeyGenerator
from cachekit.object_cache import ObjectCache
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
    "x" * (_MEMO_MAX_LEN + 1),
    b"x" * (_MEMO_MAX_LEN + 1),
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
        for value in (0, -1, 2**63, 1.5, -0.0, True, None, "", "x" * _MEMO_MAX_LEN, b"x" * _MEMO_MAX_LEN):
            assert _memo_key((value,), {}) is not None

    def test_a_str_subclass_keyword_name_is_not_memoised(self) -> None:
        assert _memo_key((), {_Str("x"): 1}) is None

    def test_types_are_tested_by_identity_never_hashed_or_compared(self) -> None:
        """A metaclass that defines __eq__ makes a class unhashable; one that hashes and compares
        like int would pass a set-membership test. Neither may reach the memo."""
        for value in (_Token(), _IntLike(1)):
            assert _memo_key((value,), {}) is None
            assert _memo_key((1, value), {}) is None
            assert _memo_key((1, 2, value), {}) is None
            assert _memo_key((), {"x": value}) is None

    def test_at_most_max_args_arguments_are_memoised(self) -> None:
        assert _memo_key(tuple(range(_MEMO_MAX_ARGS)), {}) is not None
        assert _memo_key(tuple(range(_MEMO_MAX_ARGS + 1)), {}) is None
        assert _memo_key(tuple(range(_MEMO_MAX_ARGS)), {"x": 1}) is None

    def test_shapes_never_collide(self) -> None:
        calls = [((1,), {}), ((1, 2), {}), ((1, 2, 3), {}), ((), {}), ((1,), {"b": 2}), ((1, 2), {"b": 2})]
        keys = [_memo_key(a, k) for a, k in calls]
        assert len(set(keys)) == len(calls)


def _key_now(fn: Any, args: tuple[Any, ...], kwargs: dict[str, Any], namespace: str) -> str:
    """The key the key generator builds for this call right now: the bytes the memo must reproduce."""
    return CacheKeyGenerator().generate_key(fn.__wrapped__, args, kwargs, namespace, True, serializer_type="default")


@pytest.mark.unit
class TestKeyBytesUnchanged:
    """With L1 off, every call reads L2 under the key it resolved, so the backend sees each key."""

    @pytest.mark.parametrize(
        ("args", "kwargs"),
        [
            ((1,), {}),
            ((1.0,), {}),
            ((True,), {}),
            ((-0.0,), {}),
            (("a",), {}),
            ((b"a",), {}),
            ((None,), {}),
            ((42, "user-profile"), {}),
            ((1, 2, 3), {}),
            (("q",), {"limit": 10}),
            (("q",), {"limit": 10, "offset": 5}),
            (("x" * (_MEMO_MAX_LEN + 1),), {}),
            ((_Color.RED,), {}),
            (([1, 2],), {}),
        ],
    )
    def test_a_memoised_call_reads_the_generated_key(self, args: tuple[Any, ...], kwargs: dict[str, Any]) -> None:
        backend = _RecordingBackend()

        @cache(backend=backend, l1_enabled=False, ttl=60, namespace="memo", encryption=False)
        def fn(*a: Any, **k: Any) -> int:
            return 1

        for _ in range(3):
            fn(*args, **kwargs)
        expected = _key_now(fn, args, kwargs, "memo")
        assert backend.reads == [expected] * 3

    # Values that compare equal across types (1, 1.0, True; 0, 0.0, -0.0, False), values just past
    # the length cap, and a few the memo never takes, so equal-but-different calls interleave.
    _ARGS = st.sampled_from(
        [0, 1, 1.0, True, False, 0.0, -0.0, None, "", "1", b"1", "x" * _MEMO_MAX_LEN, "x" * (_MEMO_MAX_LEN + 1), _Color.RED, [1]]
    )

    @settings(max_examples=200, deadline=None)
    @given(
        calls=st.lists(
            st.tuples(st.lists(_ARGS, max_size=3), st.dictionaries(st.sampled_from("ab"), _ARGS, max_size=2)), max_size=12
        )
    )
    def test_any_call_sequence_reads_the_generated_keys(self, calls: list[tuple[list[Any], dict[str, Any]]]) -> None:
        backend = _RecordingBackend()

        @cache(backend=backend, l1_enabled=False, ttl=60, namespace="memo", encryption=False)
        def fn(*a: Any, **k: Any) -> int:
            return 1

        for args, kwargs in calls:
            fn(*args, **kwargs)
            assert backend.reads[-1] == _key_now(fn, tuple(args), kwargs, "memo")

    def test_keyword_order_does_not_change_the_key(self) -> None:
        backend = _RecordingBackend()

        @cache(backend=backend, l1_enabled=False, ttl=60, namespace="memo", encryption=False)
        def fn(**k: Any) -> int:
            return 1

        fn(a=1, b=2)
        fn(b=2, a=1)
        fn(a=1, b=2)
        assert len(set(backend.reads)) == 1

    def test_a_rename_after_the_first_call_rekeys(self) -> None:
        backend = _RecordingBackend()

        @cache(backend=backend, l1_enabled=False, ttl=60, namespace="memo", encryption=False)
        def fn(x: int) -> int:
            return x

        fn(1)
        fn.__wrapped__.__qualname__ = "renamed"
        renamed = _key_now(fn, (1,), {}, "memo")
        fn(1)
        fn.__wrapped__.__module__ = "elsewhere"
        moved = _key_now(fn, (1,), {}, "memo")
        fn(1)
        fn(1)
        assert ":renamed:" not in backend.reads[0]
        assert backend.reads[1:] == [renamed, moved, moved]
        assert ":elsewhere.renamed:" in moved


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

    def test_the_memo_stays_bounded(self) -> None:
        @cache(backend=None, encryption=False)
        def fn(x: int) -> int:
            return x

        for i in range(_MEMO_MAX_ENTRIES + 10):
            fn(i)
        assert 0 < len(_closure_cell(fn, "_key_memo").cell_contents) <= _MEMO_MAX_ENTRIES

    def test_a_miss_still_runs_inside_the_stats_context(self) -> None:
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
        assert all(stats is not None for stats in seen)

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

        memo = _closure_cell(fn, "_key_memo").cell_contents
        fn("alice@example.com")
        fn("bob@example.com")
        assert len(memo) == 2
        fn.invalidate_cache("alice@example.com")
        assert list(memo) == [(str, "bob@example.com")]
        fn.cache_clear()
        assert memo == {}

    def test_an_encrypted_function_memoises_nothing(self) -> None:
        backend = _RecordingBackend()

        @cache.secure(master_key="ab" * 32, backend=backend, ttl=60)
        def fn(x: str) -> str:
            return x

        for _ in range(3):
            assert fn("alice@example.com") == "alice@example.com"
        assert _closure_cell(fn, "_key_memo").cell_contents == {}
        assert len(set(backend.reads)) == 1

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

    def test_async_hits_take_the_fast_path(self) -> None:
        calls = 0

        @cache(backend=None, encryption=False)
        async def fn(x: int) -> int:
            nonlocal calls
            calls += 1
            return x

        async def run() -> None:
            for _ in range(5):
                assert await fn(3) == 3

        asyncio.run(run())
        info = fn.cache_info()
        assert (calls, info.l1_hits, info.misses) == (1, 4, 1)


@pytest.mark.unit
def test_function_stats_count_l1_hits_from_many_threads_exactly() -> None:
    stats = _FunctionStats("memo.threads")
    threads, per_thread = 8, 10_000
    workers = [threading.Thread(target=lambda: [stats.record_l1_hit() for _ in range(per_thread)]) for _ in range(threads)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join()
    stats.record_miss()
    info = stats.get_info()
    assert (info.hits, info.l1_hits, info.misses) == (threads * per_thread, threads * per_thread, 1)
    stats.clear()
    assert stats.get_info().l1_hits == 0


@pytest.mark.unit
class TestSWRJitterDraw:
    """get_with_swr draws its ±10% jitter only where the draw can change the answer."""

    def test_no_draw_below_the_jittered_threshold_and_a_draw_above_it(self, monkeypatch: pytest.MonkeyPatch) -> None:
        clock = types.SimpleNamespace(monotonic=lambda: 1000.0)
        monkeypatch.setattr("cachekit.object_cache.time", clock)
        draws: list[tuple[float, float]] = []

        def uniform(a: float, b: float) -> float:
            draws.append((a, b))
            return 1.0

        monkeypatch.setattr("cachekit.object_cache.random.uniform", uniform)
        oc = ObjectCache(max_entries=4, swr_threshold_ratio=0.5)
        oc.put("k", "v", ttl=100)

        clock.monotonic = lambda: 1000.0 + 0.9 * 50  # the jitter's low end: never stale
        assert oc.get_with_swr("k", 100) == (True, "v", False, 1)
        assert draws == []

        clock.monotonic = lambda: 1000.0 + 50.5  # past the threshold at jitter 1.0
        assert oc.get_with_swr("k", 100)[2] is True
        assert draws == [(0.9, 1.1)]
