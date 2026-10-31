"""Cache operations record into per-thread cells that their Prometheus series sum when read.

A free-threaded build runs threads in parallel, so a record that writes shared state (a lock, a queue, a shared
counter) serialises them. Each thread writes only cells of its own instead. What a scrape, ``get_stats()`` and the mode
switch read must still be exact, immediate and monotonic, survive a thread's exit and a fork, and stay bounded under
thread churn.
"""

from __future__ import annotations

import gc
import math
import multiprocessing
import os
import queue
import random
import subprocess
import sys
import threading
import time
import uuid
import weakref
from collections.abc import Callable

import pytest

from cachekit.reliability import async_metrics
from cachekit.reliability.async_metrics import PROMETHEUS_AVAILABLE, AsyncMetricsCollector

pytestmark = pytest.mark.skipif(
    not (PROMETHEUS_AVAILABLE and async_metrics._PER_THREAD), reason="per-thread cells need in-process prometheus values"
)
needs_fork = pytest.mark.skipif(not hasattr(os, "fork"), reason="needs os.fork")

THREADS = 16


def _record(collector: AsyncMetricsCollector, namespace: str, duration_ms: float = 1.5, size_bytes: int = 100) -> None:
    collector.record_cache_operation(
        operation="get", namespace=namespace, success=True, duration_ms=duration_ms, serializer="rust", size_bytes=size_bytes
    )


def _sample(name: str, namespace: str) -> float:
    from prometheus_client import REGISTRY

    labels = {"operation": "get", "namespace": namespace, "serializer": "rust"}
    if name == "cache_operations_total":
        labels["success"] = "True"
    return REGISTRY.get_sample_value(name, labels) or 0.0


def _series_samples(namespace: str) -> tuple[float, ...]:
    """Every cumulative value one label tuple exports: count, then each histogram's count and sum."""
    return tuple(
        _sample(name, namespace)
        for name in (
            "cache_operations_total",
            "cache_operation_duration_ms_count",
            "cache_operation_duration_ms_sum",
            "cache_operation_size_bytes_count",
            "cache_operation_size_bytes_sum",
        )
    )


def _in_threads(target: Callable[[int], None], threads: int = THREADS) -> None:
    """Run ``target(i)`` on ``threads`` threads released together, and wait for all of them to exit."""
    barrier = threading.Barrier(threads)

    def run(i: int) -> None:
        barrier.wait()
        target(i)

    workers = [threading.Thread(target=run, args=(i,)) for i in range(threads)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join()


def _cells(namespace: str) -> int:
    """Return how many live per-thread cells the label tuple's three series hold."""
    total = 0
    for name, labels in (
        ("cache_operations_total", {"success": "True"}),
        ("cache_operation_duration_ms", {}),
        ("cache_operation_size_bytes", {}),
    ):
        child = async_metrics._metrics_cache[name].labels(operation="get", namespace=namespace, serializer="rust", **labels)
        value = child._value if name == "cache_operations_total" else child._sum
        total += len(value.shards.cells)
    return total


@pytest.mark.parametrize("sync_mode", [True, False], ids=["sync", "batched"])
def test_a_scrape_right_after_calls_on_16_threads_counts_every_one(sync_mode):
    namespace = f"per-thread-exact-{uuid.uuid4().hex}"
    collector = AsyncMetricsCollector(sync_mode=sync_mode, auto_detect_mode=False)
    per_thread = 250
    live = threading.Event()
    done = threading.Barrier(THREADS + 1)

    def record(_: int) -> None:
        for _ in range(per_thread):
            _record(collector, namespace)
        done.wait()  # still alive, so the scrape reads live cells, not retired ones
        live.wait()

    threads = threading.Thread(target=_in_threads, args=(record,))
    threads.start()
    done.wait()
    n = THREADS * per_thread
    try:
        assert _series_samples(namespace) == (n, n, n * 1.5, n, n * 100.0)
        assert collector.get_stats()["total_operations"] == n
    finally:
        live.set()
        threads.join()
        collector.shutdown()
    assert _series_samples(namespace) == (n, n, n * 1.5, n, n * 100.0)  # and once every thread has exited


def test_each_value_lands_in_the_bucket_prometheus_would_pick():
    namespace = f"per-thread-buckets-{uuid.uuid4().hex}"
    collector = AsyncMetricsCollector(sync_mode=True, auto_detect_mode=False)
    durations = [0.01, 0.010001, 0.5, 2.5, 7.0, 1000.0, 1e9, math.inf]
    for duration_ms in durations:
        _record(collector, namespace, duration_ms=duration_ms, size_bytes=0)

    from prometheus_client import REGISTRY, CollectorRegistry, Histogram

    expected = Histogram("reference", "", buckets=async_metrics.DURATION_BUCKETS_MS, registry=CollectorRegistry())
    for duration_ms in durations:
        expected.observe(duration_ms)
    want = {s.labels["le"]: s.value for s in expected.collect()[0].samples if s.name.endswith("_bucket")}
    labels = {"operation": "get", "namespace": namespace, "serializer": "rust"}
    got = {le: REGISTRY.get_sample_value("cache_operation_duration_ms_bucket", {**labels, "le": le}) for le in want}
    assert got == want
    assert _sample("cache_operation_size_bytes_count", namespace) == 0.0  # a zero size observes nothing


def test_get_stats_keeps_its_keys_and_sums_every_thread():
    collector = AsyncMetricsCollector(sync_mode=True, auto_detect_mode=False)
    namespace = f"per-thread-stats-{uuid.uuid4().hex}"
    _in_threads(lambda i: [_record(collector, namespace) for _ in range(10 + i)])
    collector.record_counter(f"per_thread_stats_{uuid.uuid4().hex}")

    stats = collector.get_stats()
    assert set(stats) == {
        "mode",
        "total_operations",
        "dropped_metrics",
        "uptime_seconds",
        "ops_per_second",
        "auto_detect_enabled",
        "pool_size",
    }
    assert stats["total_operations"] == sum(10 + i for i in range(THREADS)) + 1


def test_mode_switch_reads_the_count_summed_over_threads():
    collector = AsyncMetricsCollector(flush_interval=0.05)
    namespace = f"per-thread-mode-{uuid.uuid4().hex}"
    collector._last_mode_check = time.time()  # no check while the threads record
    _in_threads(lambda _: [_record(collector, namespace) for _ in range(20)])  # 20 per thread, 320 in all
    assert collector._sync_mode

    # Over 2 s, each thread's 20 alone is 10/s, below the 100/s switch; the 320 together are 160/s.
    collector._start_time = time.time() - 2.0
    collector._last_mode_check = 0.0
    _record(collector, namespace)
    assert not collector._sync_mode
    collector.shutdown()


def test_a_steady_record_writes_no_shared_state(monkeypatch):
    collector = AsyncMetricsCollector(sync_mode=False, auto_detect_mode=False)
    namespace = f"per-thread-no-shared-{uuid.uuid4().hex}"
    _record(collector, namespace)  # binds this thread's cells, the one time it takes a lock
    snapshot = dict(vars(collector))

    def forbidden(*args, **kwargs):
        raise AssertionError("a steady record took a shared lock or queued")

    monkeypatch.setattr(async_metrics, "_shard_lock", forbidden)
    monkeypatch.setattr(async_metrics, "_metrics_cache_lock", forbidden)
    monkeypatch.setattr(async_metrics, "_get_shared_metric", forbidden)
    monkeypatch.setattr(type(collector._queue), "put_nowait", forbidden)
    monkeypatch.setattr(AsyncMetricsCollector, "_get_pooled_metric_data", forbidden)
    for _ in range(100):
        _record(collector, namespace)

    monkeypatch.undo()
    assert vars(collector) == snapshot
    assert _sample("cache_operations_total", namespace) == 101
    collector.shutdown()


def test_reads_never_decrease_while_threads_record_and_exit():
    namespace = f"per-thread-monotonic-{uuid.uuid4().hex}"
    collector = AsyncMetricsCollector(sync_mode=True, auto_detect_mode=False)
    stop = threading.Event()
    decreases: list[str] = []

    def record(i: int) -> None:
        # Durations whose float sum rounds, so a retired cell would show any re-grouping. Short-lived threads
        # retire their cells while the readers run.
        rng = random.Random(i)
        while not stop.is_set():
            for _ in range(rng.randint(1, 200)):
                _record(collector, namespace, duration_ms=rng.uniform(0.001, 3.0), size_bytes=rng.randint(1, 10**6))
            if rng.random() < 0.3:
                return

    def churn(i: int) -> None:
        while not stop.is_set():
            worker = threading.Thread(target=record, args=(i,))
            worker.start()
            worker.join()

    def read() -> None:
        last = (_series_samples(namespace), collector.get_stats()["total_operations"])
        while not stop.is_set():
            now = (_series_samples(namespace), collector.get_stats()["total_operations"])
            if any(b < a for a, b in zip(last[0], now[0], strict=True)) or now[1] < last[1]:
                decreases.append(f"{last} -> {now}")
            last = now

    threads = [threading.Thread(target=churn, args=(i,)) for i in range(THREADS)]
    threads += [threading.Thread(target=read) for _ in range(2)]
    for thread in threads:
        thread.start()
    time.sleep(2.0)
    stop.set()
    for thread in threads:
        thread.join()
    assert decreases == []
    assert _sample("cache_operations_total", namespace) == collector._operation_count > 0


def test_an_exited_threads_counts_survive_and_its_cells_go():
    namespace = f"per-thread-exit-{uuid.uuid4().hex}"
    collector = AsyncMetricsCollector(sync_mode=True, auto_detect_mode=False)
    _in_threads(lambda _: [_record(collector, namespace) for _ in range(7)])

    assert _series_samples(namespace) == (7 * THREADS, 7 * THREADS, 7 * THREADS * 1.5, 7 * THREADS, 7 * THREADS * 100.0)
    assert collector._operation_count == 7 * THREADS
    assert _cells(namespace) == 0


def test_cells_stay_bounded_across_10000_thread_lifetimes():
    namespace = f"per-thread-churn-{uuid.uuid4().hex}"
    collector = AsyncMetricsCollector(sync_mode=True, auto_detect_mode=False)
    _record(collector, namespace)  # this thread's cells stay live
    states = len(async_metrics._thread_ops)
    for _ in range(10_000):
        thread = threading.Thread(target=_record, args=(collector, namespace))
        thread.start()
        thread.join()

    assert len(async_metrics._thread_ops) <= states
    assert _cells(namespace) == 3
    assert _sample("cache_operations_total", namespace) == collector._operation_count == 10_001


def test_dropped_collectors_leave_no_counts_behind():
    namespace = f"per-thread-drop-{uuid.uuid4().hex}"
    _record(AsyncMetricsCollector(sync_mode=True, auto_detect_mode=False), namespace)  # this thread's state exists
    gc.collect()
    retired, own = len(async_metrics._retired_ops), len(async_metrics._tls.state.ops)
    for _ in range(500):
        collector = AsyncMetricsCollector(sync_mode=True, auto_detect_mode=False)
        _record(collector, namespace)
        thread = threading.Thread(target=_record, args=(collector, namespace))  # retires its count on exit
        thread.start()
        thread.join()
        del collector, thread
    gc.collect()

    assert len(async_metrics._retired_ops) <= retired
    assert len(async_metrics._tls.state.ops) <= own
    assert _sample("cache_operations_total", namespace) == 1001  # the series keep every operation


def test_a_finalizer_that_runs_under_the_series_lock_does_not_deadlock():
    # A garbage collection can run a finalizer while its thread holds the lock (inside a scrape, a bind or a
    # thread's exit). A finalizer that records, reads or drops a collector then takes the lock again on that thread.
    namespace = f"per-thread-reenter-{uuid.uuid4().hex}"
    collector = AsyncMetricsCollector(sync_mode=True, auto_detect_mode=False)
    _record(collector, namespace)
    results: list[object] = []

    def nested() -> None:
        with async_metrics._shard_lock():
            _record(collector, f"{namespace}-new")  # binds a new label tuple
            dropped = AsyncMetricsCollector(sync_mode=True, auto_detect_mode=False)
            _record(dropped, namespace)
            del dropped
            gc.collect()  # finalizes the dropped collector under the lock
            results.append((collector.get_stats()["total_operations"], _sample("cache_operations_total", namespace)))

    thread = threading.Thread(target=nested, daemon=True)
    thread.start()
    thread.join(10)
    assert not thread.is_alive(), "deadlocked on the series lock"
    assert results == [(2, 2)]


def test_an_infinite_duration_stays_infinite_after_its_thread_exits():
    namespace = f"per-thread-inf-{uuid.uuid4().hex}"
    collector = AsyncMetricsCollector(sync_mode=True, auto_detect_mode=False)
    _in_threads(lambda i: _record(collector, namespace, duration_ms=math.inf if i == 0 else 1.0), threads=2)

    assert _sample("cache_operation_duration_ms_sum", namespace) == math.inf
    assert _sample("cache_operation_duration_ms_count", namespace) == 2


def test_a_retirement_inside_an_open_section_waits_until_the_section_closes():
    # A traceback in a reference cycle can keep an exited thread's state alive until a cyclic collection frees it,
    # on whatever thread runs that collection, possibly in the middle of a read there.
    namespace = f"per-thread-defer-{uuid.uuid4().hex}"
    collector = AsyncMetricsCollector(sync_mode=True, auto_detect_mode=False)
    kept: list[object] = []

    def record_and_keep_state() -> None:
        _record(collector, namespace)
        kept.append(async_metrics._tls.state)

    thread = threading.Thread(target=record_and_keep_state)
    thread.start()
    thread.join()
    (state,) = kept
    ops = state.ops  # type: ignore[attr-defined]
    del state

    with async_metrics._locked():
        before = (collector._operation_count, _sample("cache_operations_total", namespace))
        kept.clear()  # frees the state here, inside the section
        assert len(async_metrics._deferred) == 1 and id(ops) in async_metrics._thread_ops  # nothing moved yet
        assert (collector._operation_count, _sample("cache_operations_total", namespace)) == before == (1, 1)

    assert (collector._operation_count, _sample("cache_operations_total", namespace)) == (1, 1)  # applied by that read
    assert async_metrics._deferred == [] and id(ops) not in async_metrics._thread_ops
    assert _cells(namespace) == 0


def test_retiring_a_cell_never_changes_a_total():
    # A plain float total rounds differently once a cell joins it, and can read one ulp lower than before.
    rng = random.Random(7117)
    for _ in range(200):
        shards = async_metrics._Shards([rng.uniform(0, 1e6)])
        cells = [shards.add_cell() for _ in range(rng.randint(2, 30))]
        for cell in cells:
            cell[0] = sum(rng.uniform(0.001, 3.0) for _ in range(rng.randint(1, 50)))
        before = shards.total(0)
        for cell in rng.sample(cells, len(cells)):
            shards.retire(cell)
            assert shards.total(0) == before


def test_counter_reset_and_direct_updates_still_reach_the_series():
    namespace = f"per-thread-direct-{uuid.uuid4().hex}"
    collector = AsyncMetricsCollector(sync_mode=True, auto_detect_mode=False)
    _record(collector, namespace)
    child = async_metrics._metrics_cache["cache_operations_total"].labels(
        operation="get", namespace=namespace, success="True", serializer="rust"
    )
    child.inc(2)
    assert _sample("cache_operations_total", namespace) == 3
    child.reset()
    _record(collector, namespace)
    assert _sample("cache_operations_total", namespace) == 1


def test_multiprocess_mode_records_into_the_series_directly(monkeypatch):
    monkeypatch.setattr(async_metrics, "_PER_THREAD", False)
    namespace = f"per-thread-multiproc-{uuid.uuid4().hex}"
    collector = AsyncMetricsCollector(sync_mode=True, auto_detect_mode=False)
    _record(collector, namespace)

    child = async_metrics._metrics_cache["cache_operations_total"].labels(
        operation="get", namespace=namespace, success="True", serializer="rust"
    )
    assert not isinstance(child._value, async_metrics._ShardView)
    assert _series_samples(namespace) == (1, 1, 1.5, 1, 100.0)
    assert collector._operation_count == 1


@needs_fork
def test_a_forked_child_starts_from_its_parents_counts():
    namespace = f"per-thread-fork-{uuid.uuid4().hex}"
    collector = AsyncMetricsCollector(sync_mode=True, auto_detect_mode=False)
    _record(collector, namespace)
    hold = threading.Event()
    held = threading.Event()

    def record_and_hold() -> None:  # a live parent thread with cells of its own; the child does not inherit it
        _record(collector, namespace)
        held.set()
        hold.wait()

    other = threading.Thread(target=record_and_hold)
    other.start()
    held.wait()

    ctx = multiprocessing.get_context("fork")
    results = ctx.Queue()

    def child() -> None:
        first = (_series_samples(namespace), collector._operation_count)
        _record(collector, namespace)
        results.put((first, _series_samples(namespace), collector._operation_count))

    try:
        process = ctx.Process(target=child, daemon=True)
        process.start()
        try:
            got = results.get(timeout=10)
        except queue.Empty:
            process.kill()
            pytest.fail("the forked child hung")
        process.join(timeout=5)
    finally:
        hold.set()
        other.join()
    assert got == (((2, 2, 3.0, 2, 200.0), 2), (3, 3, 4.5, 3, 300.0), 3)
    assert _series_samples(namespace) == (2, 2, 3.0, 2, 200.0)  # the child's record stayed in the child


@needs_fork
def test_fork_waits_for_a_thread_mid_read_and_the_child_does_not_hang():
    namespace = f"per-thread-fork-held-{uuid.uuid4().hex}"
    collector = AsyncMetricsCollector(sync_mode=True, auto_detect_mode=False)
    _record(collector, namespace)
    entered, release = threading.Event(), threading.Event()

    def hold_the_series_lock() -> None:
        with async_metrics._shard_lock():
            entered.set()
            release.wait()

    holder = threading.Thread(target=hold_the_series_lock)
    holder.start()
    entered.wait()
    threading.Timer(0.2, release.set).start()  # the at-fork hook waits for the reader to finish

    ctx = multiprocessing.get_context("fork")
    results = ctx.Queue()
    process = ctx.Process(target=lambda: results.put((_record(collector, namespace), _series_samples(namespace))[1]))
    process.start()
    try:
        assert results.get(timeout=10) == (2, 2, 3.0, 2, 200.0)
    except queue.Empty:
        process.kill()
        pytest.fail("the forked child hung on the series lock")
    finally:
        process.join(timeout=5)
        holder.join()


def test_a_thread_state_freed_once_the_interpreter_is_exiting_retires_nothing(monkeypatch):
    # The atexit hook has emptied _live. The subprocess test below covers the teardown itself, which coverage cannot see.
    class State(async_metrics._ThreadState):
        __slots__ = ("__weakref__",)  # so the test can tell the state was freed, which runs its __del__

    monkeypatch.setattr(async_metrics, "_live", [])
    state = State()
    ops, freed = state.ops, weakref.ref(state)
    del state
    try:
        assert freed() is None
        assert async_metrics._deferred == [] and async_metrics._thread_ops.get(id(ops)) is ops
    finally:
        with async_metrics._locked():
            async_metrics._thread_ops.pop(id(ops), None)


def test_a_thread_state_freed_at_interpreter_teardown_exits_silently():
    # Pinning the module on sys keeps it alive until teardown clears its globals; clearing _tls then frees this
    # thread's state, whose __del__ runs with _retire_thread and its helpers already None.
    script = (
        "import sys\n"
        "from cachekit.reliability import async_metrics\n"
        "sys._pinned_async_metrics = async_metrics\n"
        "async_metrics._new_thread_state()\n"
    )
    out = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=60)  # noqa: S603 (trusted: sys.executable + literal code)
    assert out.returncode == 0, out.stderr
    assert "Exception ignored" not in out.stderr, out.stderr
