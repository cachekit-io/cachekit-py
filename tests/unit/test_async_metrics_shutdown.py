"""shutdown() must flush every record queued before it, not just the worker's in-flight batch."""

import logging
import uuid

import pytest

from cachekit.reliability.async_metrics import PROMETHEUS_AVAILABLE, AsyncMetricsCollector

pytestmark = pytest.mark.skipif(not PROMETHEUS_AVAILABLE, reason="prometheus_client not installed")


def test_shutdown_flushes_queued_backlog():
    n = 1_000
    # A namespace unique to this test isolates its series from any other collector in the process.
    namespace = f"shutdown-drain-{uuid.uuid4().hex}"
    collector = AsyncMetricsCollector(sync_mode=False, auto_detect_mode=False)

    for _ in range(n):
        collector.record_cache_operation(operation="get", namespace=namespace, success=True, duration_ms=1.0)

    collector.shutdown()

    assert collector._worker_thread is not None and not collector._worker_thread.is_alive()
    assert collector._queue is not None and collector._queue.qsize() == 0
    counter = collector._metrics_cache["cache_operations_total"]
    flushed = counter.labels(operation="get", namespace=namespace, success="True", serializer="unknown")._value.get()
    assert flushed == n
    assert collector.get_dropped_metrics_count() == 0


def test_shutdown_drains_in_batch_size_chunks():
    collector = AsyncMetricsCollector(batch_size=100, sync_mode=False, auto_detect_mode=False)
    flushed_sizes = []
    original_flush = collector._flush_batch

    def spy(batch):
        flushed_sizes.append(len(batch))
        original_flush(batch)

    collector._flush_batch = spy
    name = f"shutdown_drain_chunks_{uuid.uuid4().hex}"
    for _ in range(1_000):
        collector.record_counter(name)

    collector.shutdown()

    assert sum(flushed_sizes) == 1_000
    assert max(flushed_sizes) <= 100


def test_shutdown_drain_is_bounded_while_producers_keep_recording():
    collector = AsyncMetricsCollector(sync_mode=False, auto_detect_mode=False)
    assert collector._queue is not None
    q = collector._queue
    name = f"shutdown_drain_producer_{uuid.uuid4().hex}"
    for _ in range(10):
        collector.record_counter(name)
    original_get_nowait = q.get_nowait

    def get_nowait_with_refill():
        # Every dequeue is matched by a new record, as if producers never stop recording.
        item = original_get_nowait()
        q.put_nowait({"type": "counter", "name": name, "labels": {}, "value": 1.0})
        return item

    q.get_nowait = get_nowait_with_refill
    collector.shutdown(timeout=2.0)

    # The drain is bounded by a snapshot, so the worker exits even though the queue never empties.
    assert collector._worker_thread is not None and not collector._worker_thread.is_alive()


def test_shutdown_flush_survives_failing_series(caplog):
    n = 1_000
    suffix = uuid.uuid4().hex
    counter_name = f"shutdown_drain_bad_counter_{suffix}"
    bad_histogram_name = f"shutdown_drain_bad_histogram_{suffix}"
    histogram_name = f"shutdown_drain_histogram_{suffix}"
    collector = AsyncMetricsCollector(sync_mode=False, auto_detect_mode=False)

    # Later records whose label names differ from the first record's make their Prometheus updates raise.
    collector.record_counter(counter_name, {"a": "one"})
    collector.record_counter(counter_name, {"b": "two"})
    collector.record_histogram(bad_histogram_name, 1.0, {"a": "one"})
    for _ in range(50):
        collector.record_histogram(bad_histogram_name, 1.0, {"b": "two"})
    # Prometheus refuses to create metrics with reserved label names.
    collector.record_counter(f"shutdown_drain_reserved_counter_{suffix}", {"__reserved": "x"})
    collector.record_histogram(f"shutdown_drain_reserved_histogram_{suffix}", 1.0, {"le": "x"})
    for _ in range(n):
        collector.record_histogram(histogram_name, 1.0, {"op": "get"})

    with caplog.at_level(logging.ERROR, logger="cachekit.reliability.async_metrics"):
        collector.shutdown()

    assert collector._worker_thread is not None and not collector._worker_thread.is_alive()
    assert collector._queue is not None and collector._queue.qsize() == 0
    assert collector._metrics_cache[counter_name].labels(a="one")._value.get() == 1
    assert collector._metrics_cache[bad_histogram_name].labels(a="one")._sum.get() == 1
    assert collector._metrics_cache[histogram_name].labels(op="get")._sum.get() == n
    # One log line per failing metric, not one per failing record.
    messages = [r.getMessage() for r in caplog.records]
    assert sum(bad_histogram_name in m for m in messages) == 1
    assert sum(counter_name in m for m in messages) == 1
    assert sum("Failed to create" in m for m in messages) == 2


def test_shutdown_flush_skips_records_that_break_the_type_contract(caplog):
    n = 1_000
    histogram_name = f"shutdown_drain_typed_histogram_{uuid.uuid4().hex}"
    collector = AsyncMetricsCollector(batch_size=100, sync_mode=False, auto_detect_mode=False)

    # prometheus_client fails on these with AttributeError or TypeError, not ValueError.
    collector.record_counter(f"shutdown_drain_int_label_{uuid.uuid4().hex}", {1: "one"})  # type: ignore[dict-item]
    collector.record_counter(123, {"a": "one"})  # type: ignore[arg-type]
    collector.record_histogram(f"shutdown_drain_str_value_{uuid.uuid4().hex}", "x", {"a": "one"})  # type: ignore[arg-type]
    for _ in range(n):
        collector.record_histogram(histogram_name, 1.0, {"op": "get"})

    with caplog.at_level(logging.ERROR, logger="cachekit.reliability.async_metrics"):
        collector.shutdown()

    assert collector._worker_thread is not None and not collector._worker_thread.is_alive()
    assert collector._queue is not None and collector._queue.qsize() == 0
    assert collector._metrics_cache[histogram_name].labels(op="get")._sum.get() == n
    assert sum("Error processing metric" in r.getMessage() for r in caplog.records) == 3
