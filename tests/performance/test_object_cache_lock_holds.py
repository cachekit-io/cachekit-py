"""How long an L1-only cache's writes hold its lock, and how long a reader waits meanwhile.

Dead deadline and LRU records pile up as entries are removed and stored; once they outnumber the
entries, the cache drops them. These tests run that churn on 100,000 entries and time every lock hold
of the writing thread, and the gaps between a reader thread's gets.
"""

from __future__ import annotations

import gc
import statistics
import threading
import time
from collections import OrderedDict
from itertools import pairwise
from typing import Any

import pytest

from cachekit._rust_serializer import HitCounter, HitMemo
from cachekit.object_cache import ObjectCache, _ObjectCacheState

from .stats_utils import report

ENTRIES = 100_000


def _memo() -> HitMemo:
    def owner() -> None:
        pass

    return HitMemo(owner, HitCounter(), owner.__module__, owner.__qualname__)


@pytest.mark.performance
@pytest.mark.usefixtures("nothing_configured")  # an L1-only cache: no Redis
@pytest.mark.parametrize("with_memo", [True, False], ids=["memo", "no-memo"])
def test_object_cache_store_lock_holds(record_property, monkeypatch: pytest.MonkeyPatch, with_memo: bool) -> None:
    """Delete the oldest entry and store a new one, 2.5 times as often as there are entries.

    Each pair leaves a dead deadline record and, with a memo, a dead LRU record, so the dead records
    pass the live ones twice over, at about the 100,000th and the 200,000th pair. Holds are timed in
    thread CPU time, so a loaded host's descheduling does not count as lock work. The reader's gap is
    wall time from one get's start to the next: it includes waiting for the GIL, not only the lock,
    so freeing a large list in one go shows up there.
    """
    oc = ObjectCache(max_entries=2 * ENTRIES, memo=_memo() if with_memo else None)
    for i in range(ENTRIES):
        oc.put(f"churn:{i}", i, ttl=3600)
    oc.put("hot", "h", ttl=3600)

    state = oc._state
    inner, writer = state.lock, threading.get_ident()
    holds: list[int] = []
    kinds: list[str] = []  # per hold: "step" if it ran a compaction step, else "store" if it stored, else "other"

    # Count the calls themselves, not whether a compaction was in progress: a delete during one runs no
    # step, and a store outside one runs no step either, so each population is what its label says.
    calls = {"push": 0, "compact_step": 0}

    def counted(name: str) -> Any:
        method = getattr(_ObjectCacheState, name)

        def call(self: _ObjectCacheState, *args: Any, **kwargs: Any) -> None:
            calls[name] += 1
            method(self, *args, **kwargs)

        return call

    for name in calls:
        monkeypatch.setattr(_ObjectCacheState, name, counted(name))

    class TimedLock:
        depth = 0

        def __enter__(self):
            inner.acquire()
            if threading.get_ident() == writer:
                self.depth += 1
                if self.depth == 1:
                    self.calls = dict(calls)
                    self.start = time.thread_time_ns()
            return self

        def __exit__(self, *exc):
            if threading.get_ident() == writer:
                self.depth -= 1
                if self.depth == 0:
                    holds.append(time.thread_time_ns() - self.start)
                    if calls["compact_step"] != self.calls["compact_step"]:
                        kinds.append("step")
                    else:
                        kinds.append("store" if calls["push"] != self.calls["push"] else "other")
            inner.release()

    state.lock = TimedLock()
    starts: list[int] = []
    failures: list[BaseException] = []
    done = threading.Event()

    def reader() -> None:
        try:
            while not done.is_set():
                starts.append(time.perf_counter_ns())
                assert oc.get("hot") == (True, "h")
                time.sleep(0)
        except BaseException as exc:  # a thread's failure only warns: hand it to the test
            failures.append(exc)

    thread = threading.Thread(target=reader)
    thread.start()
    time.sleep(0.05)
    # A full collection over the 100k entries stalls every thread whatever the cache does, and lands
    # in whichever hold allocates when it comes due, so it runs before the measurement, not in it.
    gc.collect()
    gc.disable()
    try:
        begin = time.perf_counter_ns()
        for i in range(ENTRIES, ENTRIES + 5 * ENTRIES // 2):
            oc.delete(f"churn:{i - ENTRIES}")
            oc.put(f"churn:{i}", i, ttl=3600)
        end = time.perf_counter_ns()
        time.sleep(0.05)
    finally:
        done.set()
        thread.join()
        gc.enable()
    assert not failures, f"the reader failed during the writes: {failures[0]!r}"

    gaps = [b - a for a, b in pairwise(starts) if b >= begin and a <= end]
    label = f"{ENTRIES:,} entries, {'memo' if with_memo else 'no memo'}"
    report(record_property, f"ObjectCache write lock hold ({label})", holds, "hold_")
    report(record_property, "ObjectCache reader gap during the writes", gaps, "gap_")
    print(f"max hold {max(holds) / 1e6:.3f} ms over {len(holds):,} holds; max reader gap {max(gaps) / 1e6:.3f} ms")
    record_property("hold_max_ns", max(holds))
    record_property("gap_max_ns", max(gaps))
    print("longest holds, ms:", ", ".join(f"{h / 1e6:.3f}" for h in sorted(holds)[-5:]))
    stepped = [h for h, k in zip(holds, kinds, strict=True) if k == "step"]
    stores = [h for h, k in zip(holds, kinds, strict=True) if k == "store"]
    if stepped:
        print(
            f"{len(stepped):,} stores ran a compaction step: max {max(stepped) / 1e6:.3f} ms, median "
            f"{statistics.median(stepped) / 1e3:.2f} us; {len(stores):,} stores ran none: median "
            f"{statistics.median(stores) / 1e3:.2f} us"
        )
        record_property("step_hold_max_ns", max(stepped))

    # The store is a dict, which CPython resizes in one go as it grows or fills with deleted slots: the
    # same churn on a bare OrderedDict shows how much of the longest hold that resize alone takes.
    store = OrderedDict((f"churn:{i}", i) for i in range(ENTRIES))
    gc.disable()
    try:
        floor = 0
        for i in range(ENTRIES, ENTRIES + 5 * ENTRIES // 2):
            start = time.thread_time_ns()
            del store[f"churn:{i - ENTRIES}"]
            store[f"churn:{i}"] = i
            floor = max(floor, time.thread_time_ns() - start)
    finally:
        gc.enable()
    print(f"the same churn on a bare OrderedDict: max {floor / 1e6:.3f} ms")
    record_property("store_resize_max_ns", floor)

    assert oc.size == ENTRIES + 1
    assert oc.get("hot") == (True, "h")
    assert stepped  # the churn measured what it claims: stores that ran a compaction step
