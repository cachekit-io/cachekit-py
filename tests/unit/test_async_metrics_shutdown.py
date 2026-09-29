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
