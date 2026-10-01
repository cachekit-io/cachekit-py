"""Whenever the collector is batched, exactly one live worker of its own process must be reading its queue."""

import multiprocessing
import os
import queue
import signal
import sys
import threading
import time
import uuid
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

import pytest

from cachekit.reliability import async_metrics
from cachekit.reliability.async_metrics import PROMETHEUS_AVAILABLE, AsyncMetricsCollector

pytestmark = pytest.mark.skipif(not PROMETHEUS_AVAILABLE, reason="prometheus_client not installed")
needs_fork = pytest.mark.skipif(not hasattr(os, "fork"), reason="needs os.fork")


def _steer(collector: AsyncMetricsCollector, ops_per_second: float) -> None:
    # The rate is a lifetime average, so fix it through its inputs and make the next record run the mode check.
    collector._start_time = time.time() - 100.0
    collector._operation_count = int(ops_per_second * 100)
    collector._last_mode_check = 0.0


def _record(collector: AsyncMetricsCollector, namespace: str) -> None:
    collector.record_cache_operation(operation="get", namespace=namespace, success=True, duration_ms=1.0)


def _flushed(namespace: str) -> float:
    from prometheus_client import REGISTRY

    labels = {"operation": "get", "namespace": namespace, "success": "True", "serializer": "unknown"}
    return REGISTRY.get_sample_value("cache_operations_total", labels) or 0.0


def test_records_after_returning_to_batched_mode_reach_their_metric():
    n = 10
    namespace = f"mode-switch-cycle-{uuid.uuid4().hex}"
    other = f"mode-switch-cycle-other-{uuid.uuid4().hex}"
    collector = AsyncMetricsCollector(flush_interval=0.05, max_queue_size=n * 10)
    assert collector._sync_mode

    _steer(collector, 500)
    _record(collector, other)
    assert not collector._sync_mode
    first_worker, q = collector._worker_thread, collector._queue
    assert first_worker is not None and first_worker.is_alive()

    _steer(collector, 1)
    _record(collector, other)
    assert collector._sync_mode
    first_worker.join(timeout=5.0)
    assert not first_worker.is_alive()

    before = _flushed(namespace)
    _steer(collector, 500)
    for _ in range(n):
        _record(collector, namespace)

    assert not collector._sync_mode
    worker = collector._worker_thread
    assert worker is not None and worker is not first_worker and worker.is_alive()
    # The queue is reused, never replaced or cleared, so a producer never sees it missing.
    assert collector._queue is q
    collector.shutdown()
    assert not worker.is_alive()
    assert _flushed(namespace) - before == n
    assert collector.get_dropped_metrics_count() == 0


class _SignallingQueue(queue.Queue):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.entered_get = threading.Event()

    def get(self, *args, **kwargs):
        self.entered_get.set()
        return super().get(*args, **kwargs)


def test_no_new_worker_starts_while_the_previous_one_is_alive(monkeypatch):
    namespace = f"mode-switch-wait-{uuid.uuid4().hex}"
    fake_queue = SimpleNamespace(Queue=_SignallingQueue, Empty=queue.Empty, Full=queue.Full)
    monkeypatch.setattr(async_metrics, "queue", fake_queue)
    # A long get() timeout keeps the stopped worker alive until it is fed a record.
    collector = AsyncMetricsCollector(flush_interval=30.0, max_queue_size=100, sync_mode=False)
    old_worker, q = collector._worker_thread, collector._queue
    assert old_worker is not None and isinstance(q, _SignallingQueue)
    # Stop the worker only once it waits in get(). Stopped before its first loop check, it skips the loop and
    # snapshots an empty queue for its exit drain, so the record put below would be left behind.
    assert q.entered_get.wait(5)

    _steer(collector, 1)
    _record(collector, namespace)
    assert collector._sync_mode and old_worker.is_alive()

    _steer(collector, 500)
    _record(collector, namespace)
    # The old worker has not exited, so the collector stays synchronous and retries at the next check.
    assert collector._sync_mode
    assert collector._worker_thread is old_worker

    before = _flushed(namespace)
    q.put_nowait({"type": "cache_operation", "operation": "get", "namespace": namespace, "success": True,
                  "duration_ms": 1.0, "serializer": "unknown", "size_bytes": 0})  # fmt: skip
    old_worker.join(timeout=5.0)
    assert not old_worker.is_alive()
    # The old worker's exit drain flushed the record it was woken with.
    assert _flushed(namespace) - before == 1

    _steer(collector, 500)
    _record(collector, namespace)
    assert not collector._sync_mode
    assert collector._worker_thread is not old_worker and collector._worker_thread.is_alive()
    collector.shutdown()


def test_concurrent_switches_start_one_worker(monkeypatch):
    collector = AsyncMetricsCollector(flush_interval=0.05)
    assert collector._sync_mode
    started = []
    real_thread = threading.Thread

    class SlowStartThread(real_thread):
        def start(self):
            started.append(self)
            # Widen the window between creating the worker and it running, so an unlocked switch races.
            time.sleep(0.05)
            super().start()

    fake_threading = SimpleNamespace(Thread=SlowStartThread, Event=threading.Event, Lock=threading.Lock)
    monkeypatch.setattr(async_metrics, "threading", fake_threading)
    _steer(collector, 500)
    barrier = threading.Barrier(2)

    def switch():
        barrier.wait()
        collector._maybe_switch_mode()

    threads = [real_thread(target=switch) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5.0)

    assert not collector._sync_mode
    assert len(started) == 1
    collector.shutdown()


@needs_fork
def test_child_forked_mid_switch_can_still_switch(monkeypatch):
    namespace = f"mode-switch-fork-{uuid.uuid4().hex}"
    collector = AsyncMetricsCollector(flush_interval=0.05)
    assert collector._sync_mode
    parent_pid = os.getpid()
    inside, release = threading.Event(), threading.Event()

    class ThreadThatBlocksInParent(threading.Thread):
        def start(self):
            # Park the parent's switching thread inside the switch, holding the mode lock, while the process forks.
            # Park before the worker starts: a running worker can hold the queue's mutex across the fork, which
            # hangs the child on the queue instead of testing the mode lock.
            if os.getpid() == parent_pid:
                inside.set()
                release.wait(5)
            super().start()

    fake_threading = SimpleNamespace(Thread=ThreadThatBlocksInParent, Event=threading.Event, Lock=threading.Lock)
    monkeypatch.setattr(async_metrics, "threading", fake_threading)
    _steer(collector, 500)
    switcher = threading.Thread(target=_record, args=(collector, namespace), daemon=True)
    switcher.start()
    assert inside.wait(5)

    pid = os.fork()
    if pid == 0:  # child: SIGALRM kills it if the switch blocks on the lock the parked thread holds
        try:
            signal.alarm(5)
            _steer(collector, 500)
            _record(collector, namespace)
            os._exit(0 if not collector._sync_mode else 1)
        finally:
            os._exit(2)
    release.set()
    switcher.join(5)
    _, status = os.waitpid(pid, 0)
    assert os.waitstatus_to_exitcode(status) == 0
    collector.shutdown()


def test_mode_switch_after_shutdown_starts_no_worker():
    collector = AsyncMetricsCollector(flush_interval=0.05, sync_mode=False)
    namespace = f"mode-switch-after-shutdown-{uuid.uuid4().hex}"
    _steer(collector, 1)
    _record(collector, namespace)
    assert collector._sync_mode
    collector.shutdown()

    _steer(collector, 500)
    _record(collector, namespace)

    # shutdown() is final: a later rise in the rate must not bring a worker back.
    assert collector._sync_mode
    assert collector._worker_thread is not None and not collector._worker_thread.is_alive()


@pytest.mark.parametrize("ops_per_second", [None, 500], ids=["auto-detect-off", "high-rate"])
def test_records_after_shutdown_reach_their_metric(ops_per_second):
    n = 15
    namespace = f"after-shutdown-{uuid.uuid4().hex}"
    collector = AsyncMetricsCollector(
        flush_interval=0.05, max_queue_size=10, sync_mode=False, auto_detect_mode=ops_per_second is not None
    )
    collector.shutdown()

    # More records than the queue holds: queued behind the stopped worker, the last five would be dropped.
    for _ in range(n):
        if ops_per_second is not None:
            _steer(collector, ops_per_second)
        _record(collector, namespace)

    assert collector._sync_mode
    assert _flushed(namespace) == n
    assert collector.get_dropped_metrics_count() == 0


def test_sync_records_make_no_getpid_call(monkeypatch):
    collector = AsyncMetricsCollector(sync_mode=True, auto_detect_mode=False)
    namespace = f"sync-no-getpid-{uuid.uuid4().hex}"
    _record(collector, namespace)  # creating a metric looks up this process's lock; recording into it does not
    calls = []

    def getpid():
        calls.append(None)
        return os.getpid()

    monkeypatch.setattr(async_metrics, "os", SimpleNamespace(getpid=getpid))
    for _ in range(10):
        _record(collector, namespace)

    # The fork check costs a syscall, so it stays on the paths that touch batching state.
    assert calls == []


def _in_child_forked_holding(lock: Any, child: Callable[[], Any]) -> Any:
    """Return what ``child`` returns in a process forked while a thread of this one holds ``lock``."""
    ctx = multiprocessing.get_context("fork")
    results = ctx.Queue()
    with lock:
        process = ctx.Process(target=lambda: results.put(child()), daemon=True)
        process.start()
    try:
        return results.get(timeout=10)
    except queue.Empty:
        process.kill()
        pytest.fail("the forked child hung on a lock it inherited held")
    finally:
        process.join(timeout=5)


@needs_fork
@pytest.mark.parametrize("held", ["queue", "pool"])
def test_child_of_a_batched_parent_records_past_a_lock_held_at_fork(held):
    namespace = f"fork-batched-{held}-{uuid.uuid4().hex}"
    collector = AsyncMetricsCollector(flush_interval=0.05, sync_mode=False)
    assert collector._queue is not None
    lock = collector._queue.mutex if held == "queue" else collector._pool_lock

    def child():
        collector._last_mode_check = time.time()  # the first record takes the batched path, not a mode check
        _record(collector, namespace)
        first = _flushed(namespace)  # recorded synchronously: the child has no worker yet to flush a queued one
        _steer(collector, 500)
        _record(collector, namespace)  # batched again, on a queue and pool of the child's own
        batched = not collector._sync_mode
        collector.shutdown()
        return first, batched, _flushed(namespace)

    # The parent's worker did not survive the fork, and no thread in the child will ever release the lock.
    assert _in_child_forked_holding(lock, child) == (1, True, 2)
    collector.shutdown()


@needs_fork
@pytest.mark.parametrize("first_call", ["switch-to-sync", "shutdown"])
def test_child_of_a_batched_parent_stops_batching_past_a_held_stop_event(first_call):
    namespace = f"fork-stop-event-{first_call}-{uuid.uuid4().hex}"
    collector = AsyncMetricsCollector(flush_interval=0.05)
    _steer(collector, 500)
    _record(collector, f"{namespace}-parent")
    assert not collector._sync_mode and collector._stopped is not None

    def child():
        if first_call == "shutdown":
            collector.shutdown()
        else:
            _steer(collector, 1)
        _record(collector, namespace)
        return collector._sync_mode, _flushed(namespace)

    # Both calls set the stop event, whose Condition lock a parent thread holds at the fork.
    assert _in_child_forked_holding(collector._stopped._cond, child) == (True, 1)
    collector.shutdown()


@needs_fork
def test_child_of_a_parent_back_in_sync_mode_batches_on_a_queue_of_its_own():
    namespace = f"fork-old-queue-{uuid.uuid4().hex}"
    other = f"fork-old-queue-other-{uuid.uuid4().hex}"
    collector = AsyncMetricsCollector(flush_interval=0.05)
    _steer(collector, 500)
    _record(collector, other)
    worker = collector._worker_thread
    _steer(collector, 1)
    _record(collector, other)
    assert collector._sync_mode and worker is not None
    worker.join(timeout=5.0)
    assert not worker.is_alive() and collector._queue is not None

    def child():
        _steer(collector, 500)
        _record(collector, namespace)
        batched = not collector._sync_mode
        collector.shutdown()  # the child's own worker flushes the record on its way out
        return batched, _flushed(namespace)

    # The parent no longer batches but still owns its old queue, which a switch in the child would otherwise reuse.
    assert _in_child_forked_holding(collector._queue.mutex, child) == (True, 1)


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="needs glibc fork() via ctypes")
def test_child_of_a_hookless_fork_batches_with_a_worker_of_its_own():
    """A fork made from C skips Python's after-fork handling, as uWSGI's does without --py-call-osafterfork."""
    import ctypes

    namespace = f"fork-hookless-{uuid.uuid4().hex}"
    collector = AsyncMetricsCollector(flush_interval=0.05, sync_mode=False)
    inherited_worker, q = collector._worker_thread, collector._queue
    assert inherited_worker is not None and q is not None
    libc_fork = ctypes.PyDLL(None).fork  # PyDLL keeps the GIL through the call

    with q.mutex:
        pid = libc_fork()
        if pid == 0:  # child: SIGALRM kills it if it waits on the parent's queue
            try:
                signal.alarm(5)
                if not inherited_worker.is_alive():
                    os._exit(3)  # the dead worker must still look alive here, or this tests nothing
                _steer(collector, 500)
                _record(collector, namespace)
                batched = not collector._sync_mode
                collector.shutdown()
                os._exit(0 if batched and _flushed(namespace) == 1 else 1)
            finally:
                os._exit(2)
    _, status = os.waitpid(pid, 0)
    assert os.waitstatus_to_exitcode(status) == 0
    collector.shutdown()


def test_shutdown_during_a_switch_to_batched_stops_the_new_worker():
    namespace = f"mode-switch-shutdown-race-{uuid.uuid4().hex}"
    collector = AsyncMetricsCollector(flush_interval=0.05, sync_mode=False)
    _steer(collector, 1)
    _record(collector, namespace)
    assert collector._sync_mode and collector._worker_thread is not None
    collector._worker_thread.join(5)
    inside, release = threading.Event(), threading.Event()

    class ParkingEvent(threading.Event):
        def clear(self):
            # Park the restart between its checks and clearing the stop signal, where shutdown() must not slip in.
            inside.set()
            release.wait(5)
            super().clear()

    collector._stopped = ParkingEvent()
    _steer(collector, 500)
    switcher = threading.Thread(target=_record, args=(collector, namespace), daemon=True)
    switcher.start()
    assert inside.wait(5)

    stopper = threading.Thread(target=collector.shutdown, daemon=True)
    stopper.start()
    threading.Timer(0.2, release.set).start()
    switcher.join(5)
    stopper.join(5)

    assert not stopper.is_alive()
    assert not collector._worker_thread.is_alive()
