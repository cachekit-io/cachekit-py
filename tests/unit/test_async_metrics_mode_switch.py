"""Auto mode switching must leave exactly one live worker on the queue whenever the collector is batched."""

import os
import signal
import threading
import time
import uuid
from types import SimpleNamespace

import pytest

from cachekit.reliability import async_metrics
from cachekit.reliability.async_metrics import PROMETHEUS_AVAILABLE, AsyncMetricsCollector

pytestmark = pytest.mark.skipif(not PROMETHEUS_AVAILABLE, reason="prometheus_client not installed")


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


def test_no_new_worker_starts_while_the_previous_one_is_alive():
    namespace = f"mode-switch-wait-{uuid.uuid4().hex}"
    # A long get() timeout keeps the stopped worker alive until it is fed a record.
    collector = AsyncMetricsCollector(flush_interval=30.0, max_queue_size=100, sync_mode=False)
    old_worker, q = collector._worker_thread, collector._queue
    assert old_worker is not None and q is not None

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


@pytest.mark.skipif(not hasattr(os, "fork"), reason="needs os.fork")
def test_child_forked_mid_switch_can_still_switch(monkeypatch):
    namespace = f"mode-switch-fork-{uuid.uuid4().hex}"
    collector = AsyncMetricsCollector(flush_interval=0.05)
    assert collector._sync_mode
    parent_pid = os.getpid()
    inside, release = threading.Event(), threading.Event()
    real_info = async_metrics.logger.info

    def info_that_blocks_in_parent(*args, **kwargs):
        # Park the parent's switching thread inside the switch, holding the mode lock, while the process forks.
        if os.getpid() == parent_pid:
            inside.set()
            release.wait(5)
        real_info(*args, **kwargs)

    monkeypatch.setattr(async_metrics.logger, "info", info_that_blocks_in_parent)
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
