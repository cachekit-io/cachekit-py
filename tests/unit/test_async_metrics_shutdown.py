"""shutdown() must flush every record queued before it, not just the worker's in-flight batch."""

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


def test_shutdown_flush_survives_a_failing_series():
    n = 1_000
    suffix = uuid.uuid4().hex
    counter_name = f"shutdown_drain_bad_counter_{suffix}"
    histogram_name = f"shutdown_drain_histogram_{suffix}"
    collector = AsyncMetricsCollector(sync_mode=False, auto_detect_mode=False)

    # The second record's label names differ from the first's, so its Prometheus update raises.
    collector.record_counter(counter_name, {"a": "one"})
    collector.record_counter(counter_name, {"b": "two"})
    # Prometheus refuses to create a metric with a reserved label name.
    collector.record_counter(f"shutdown_drain_reserved_label_{suffix}", {"__reserved": "x"})
    for _ in range(n):
        collector.record_histogram(histogram_name, 1.0, {"op": "get"})

    collector.shutdown()

    assert collector._worker_thread is not None and not collector._worker_thread.is_alive()
    assert collector._queue is not None and collector._queue.qsize() == 0
    counter = collector._metrics_cache[counter_name]
    assert counter.labels(a="one")._value.get() == 1
    histogram = collector._metrics_cache[histogram_name]
    assert histogram.labels(op="get")._sum.get() == n
