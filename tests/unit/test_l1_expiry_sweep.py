"""L1Cache.cleanup_expired: removes what expired, keeps what did not, and never holds the lock for an O(n) scan.

The sweep pops a deadline heap with lazy deletion, so a removed, evicted or overwritten entry leaves
a dead tuple behind. These tests pin that a dead tuple never removes a live entry, that compaction
keeps every live deadline, and that each lock hold handles a bounded batch.
"""

from __future__ import annotations

import ast
import sys
import threading

import pytest

from cachekit import cache, l1_cache
from cachekit.l1_cache import _INVALIDATE_BATCH, _SWEEP_BATCH, L1Cache
from tests.utils.fork_helpers import fork_warns, run_fresh


class _Clock:
    def __init__(self) -> None:
        self.now = 1_000_000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> _Clock:
    c = _Clock()
    monkeypatch.setattr(l1_cache.time, "time", c)
    return c


class _CountingLock:
    """Wraps a state's RLock, recording the heap length at each outermost acquire and release."""

    def __init__(self, cache: L1Cache, on_release=None) -> None:
        self.cache, self.state = cache, cache._state
        self.inner, self.depth = self.state.lock, 0
        self.spans: list[tuple[int, int]] = []
        self.on_release = on_release

    def __enter__(self):
        self.inner.acquire()
        self.depth += 1
        if self.depth == 1:
            self.start = len(self.state.expiry)
        return self

    def __exit__(self, *exc):
        self.depth -= 1
        outermost = self.depth == 0
        if outermost:
            self.spans.append((self.start, len(self.state.expiry)))
        self.inner.release()
        if outermost and self.on_release is not None:
            self.on_release(len(self.spans))


@pytest.mark.unit
class TestSweepRemoval:
    def test_removes_expired_and_keeps_live(self, clock: _Clock) -> None:
        l1 = L1Cache(ttl_buffer_seconds=0)
        l1.put("short", b"s", redis_ttl=10)
        l1.put("long", b"l", redis_ttl=100)
        clock.now += 10  # "short" is due exactly now: get treats now >= expires_at as expired

        assert l1.cleanup_expired() == 1
        assert "short" not in l1._state.cache
        assert l1.get("long") == (True, b"l")
        assert l1.get_stats()["expired_evictions"] == 1

    def test_nothing_due_touches_no_entry(self, clock: _Clock) -> None:
        l1 = L1Cache(ttl_buffer_seconds=0)
        for i in range(100):
            l1.put(f"k{i}", b"v", redis_ttl=60)

        assert l1.cleanup_expired() == 0
        assert len(l1._state.cache) == len(l1._state.expiry) == 100

    def test_overwritten_entry_keeps_its_later_deadline(self, clock: _Clock) -> None:
        """The first put's tuple is dead once the key is stored again; it must not remove the new entry."""
        l1 = L1Cache(ttl_buffer_seconds=0)
        l1.put("k", b"old", redis_ttl=10)
        l1.put("k", b"new", redis_ttl=100)
        clock.now += 50

        assert l1.cleanup_expired() == 0
        assert l1.get("k") == (True, b"new")
        clock.now += 50
        assert l1.cleanup_expired() == 1
        assert not l1._state.expiry

    def test_reput_after_invalidate_with_the_same_deadline_is_removed_once(self, clock: _Clock) -> None:
        l1 = L1Cache(ttl_buffer_seconds=0)
        deadline = clock.now + 10
        l1.put("k", b"1", expires_at=deadline)
        l1.invalidate("k")
        l1.put("k", b"2", expires_at=deadline)
        clock.now = deadline

        assert l1.cleanup_expired() == 1
        assert l1.get_stats()["expired_evictions"] == 1

    def test_lru_evicted_entry_leaves_only_a_dead_tuple(self, clock: _Clock) -> None:
        l1 = L1Cache(max_memory_mb=1, ttl_buffer_seconds=0)
        l1.put("a", b"x" * 100_000, redis_ttl=10)
        for i in range(12):  # 1.2 MB through a 1 MB budget: "a", the oldest, is evicted
            l1.put(f"b{i}", b"x" * 100_000, redis_ttl=100)
        assert "a" not in l1._state.cache
        clock.now += 10

        assert l1.cleanup_expired() == 0  # its tuple was due, but dead

    def test_clear_drops_the_heap(self, clock: _Clock) -> None:
        l1 = L1Cache(ttl_buffer_seconds=0)
        for i in range(10):
            l1.put(f"k{i}", b"v", redis_ttl=10)
        l1.clear()

        assert l1._state.expiry == []


@pytest.mark.unit
class TestSweepBatching:
    def test_each_hold_pops_at_most_one_batch(self, clock: _Clock) -> None:
        l1 = L1Cache(ttl_buffer_seconds=0)
        n = 10 * _SWEEP_BATCH + 7
        for i in range(n):
            l1.put(f"k{i}", b"v", redis_ttl=10)
        clock.now += 10
        lock = _CountingLock(l1)
        l1._state.lock = lock

        assert l1.cleanup_expired() == n
        assert all(start - end <= _SWEEP_BATCH for start, end in lock.spans)
        assert len(lock.spans) >= n // _SWEEP_BATCH
        assert not l1._state.cache and not l1._state.expiry

    @staticmethod
    def _with_dead_tuples(l1: L1Cache) -> int:
        """Store _INVALIDATE_BATCH live entries and leave three times as many dead tuples: past the
        sweep's two-to-one compaction, short of the eight to one at which a put rebuilds the heap."""
        live = _INVALIDATE_BATCH
        for i in range(live):
            l1.put(f"live{i}", b"v", redis_ttl=100)
        dead = 3 * live
        for i in range(dead):
            l1.put(f"gone{i}", b"v", redis_ttl=100)
        l1.invalidate_many(f"gone{i}" for i in range(dead))
        return live

    def test_compaction_drops_dead_tuples_and_keeps_live_deadlines(self, clock: _Clock) -> None:
        l1 = L1Cache(ttl_buffer_seconds=0)
        live = self._with_dead_tuples(l1)
        l1.put("due", b"v", redis_ttl=10)
        clock.now += 10
        lock = _CountingLock(l1)
        l1._state.lock = lock

        assert l1.cleanup_expired() == 1
        assert sorted(key for _, key in l1._state.expiry) == sorted(f"live{i}" for i in range(live))
        # One hold detaches the whole heap (an O(1) swap); every other hold handles one batch.
        assert [span for span in lock.spans if abs(span[0] - span[1]) > _INVALIDATE_BATCH] == [(4 * live, 0)]
        clock.now += 90
        assert l1.cleanup_expired() == live

    def test_compaction_drops_overwrites_with_the_same_deadline(self, clock: _Clock) -> None:
        """An overwrite with an equal deadline leaves a dead tuple: liveness is the entry's own record, not its deadline."""
        l1 = L1Cache(ttl_buffer_seconds=0)
        live = _INVALIDATE_BATCH
        deadline = clock.now + 100
        for _ in range(4):  # 4 tuples per entry: past compaction's 2:1, short of a put's 8:1 rebuild
            for i in range(live):
                l1.put(f"k{i}", b"v", expires_at=deadline)
        assert len(l1._state.expiry) == 4 * live

        assert l1.cleanup_expired() == 0
        assert len(l1._state.expiry) == live
        lock = _CountingLock(l1)
        l1._state.lock = lock
        assert l1.cleanup_expired() == 0
        assert len(lock.spans) == 2  # nothing due, nothing to compact: a later sweep repeats no work
        clock.now = deadline
        assert l1.cleanup_expired() == live

    def test_writes_during_compaction_keep_their_deadlines(self, clock: _Clock) -> None:
        """Compaction detaches the heap and pushes the live tuples back across many holds; writes land between them."""
        l1 = L1Cache(ttl_buffer_seconds=0)
        live = self._with_dead_tuples(l1)
        l1.put("dropped", b"v", redis_ttl=100)
        l1.put("moved", b"v", redis_ttl=100)

        def write_between_holds(holds: int) -> None:
            if holds == 3:  # compaction is filtering the detached heap
                l1.put("added", b"v", redis_ttl=50)
                l1.invalidate("dropped")
                l1.put("moved", b"v2", redis_ttl=20)

        lock = _CountingLock(l1, on_release=write_between_holds)
        l1._state.lock = lock
        assert l1.cleanup_expired() == 0
        assert len(lock.spans) > 4  # the writes landed mid-compaction, not after it
        assert set(l1._state.cache) == {f"live{i}" for i in range(live)} | {"added", "moved"}

        clock.now += 20
        assert l1.cleanup_expired() == 1  # "moved", at its new deadline
        clock.now += 30
        assert l1.cleanup_expired() == 1  # "added"
        clock.now += 50
        assert l1.cleanup_expired() == live
        assert not l1._state.cache


@pytest.mark.unit
def test_put_rebuilds_the_heap_where_no_sweep_runs(clock: _Clock) -> None:
    """Without a sweep, overwrites alone would grow the heap by a tuple each; a put rebuilds it at eight to one."""
    l1 = L1Cache(ttl_buffer_seconds=0)
    for i in range(10):
        l1.put(f"k{i}", b"v", redis_ttl=100)
    for round_ in range(1_000):
        for i in range(10):
            l1.put(f"k{i}", b"v", redis_ttl=100 + round_)
        assert len(l1._state.expiry) <= 8 * 10 + _INVALIDATE_BATCH

    clock.now += 100 + 999
    assert l1.cleanup_expired() == 10  # every entry kept its latest deadline through the rebuilds
    assert not l1._state.cache


def _fork_warns_after_a_minimal_miss() -> object:
    """Run fresh: does os.fork() warn after one default @cache.minimal miss?"""

    @cache.minimal(backend=None)
    def fn(x: int) -> int:
        return x

    fn(1)
    return {"threads": sorted(t.name for t in threading.enumerate()), "warned": fork_warns()}


@pytest.mark.unit
@pytest.mark.skipif(sys.version_info < (3, 12), reason="os.fork() warns about threads from 3.12")
def test_the_sweeper_makes_fork_warn_for_good() -> None:
    """A process with an L1 cache runs the sweeper thread for good, so os.fork() warns there whatever else runs.
    That is why write-behind's waiting threads need no before-fork hook (test_write_behind pins the L1-off case)."""
    run = run_fresh(_fork_warns_after_a_minimal_miss)
    assert run.returncode == 0, run.stderr
    outcome = ast.literal_eval(run.stdout)
    assert outcome["warned"] is True, outcome
    assert any("cleanup_worker" in name for name in outcome["threads"]), outcome  # the sweeper's target
