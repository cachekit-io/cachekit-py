"""A generic counter or histogram recorded without labels records its value.

``labels`` is optional on ``record_counter`` and ``record_histogram``, but a metric built with no label
names rejects ``.labels()``, so a label-less call used to raise in sync mode and be logged and skipped in
batched mode. Values are read from the Prometheus registry, and every metric name is unique, because the
metric cache is process-wide and a reused name keeps its first-seen label schema.
"""

import uuid

import pytest

from cachekit.reliability.async_metrics import PROMETHEUS_AVAILABLE, AsyncMetricsCollector

pytestmark = pytest.mark.skipif(not PROMETHEUS_AVAILABLE, reason="prometheus_client not installed")


def _sample(name: str, labels: dict[str, str] | None = None) -> float | None:
    from prometheus_client import REGISTRY

    return REGISTRY.get_sample_value(name, labels or {})


def test_label_less_metrics_record_in_sync_mode():
    counter, histogram = f"label_less_sync_c_{uuid.uuid4().hex}", f"label_less_sync_h_{uuid.uuid4().hex}"
    collector = AsyncMetricsCollector(sync_mode=True, auto_detect_mode=False)

    collector.record_counter(counter)
    collector.record_counter(counter, value=2.0)
    collector.record_histogram(histogram, 5.0)

    assert _sample(f"{counter}_total") == 3.0
    assert _sample(f"{histogram}_count") == 1.0
    assert _sample(f"{histogram}_sum") == 5.0


def test_label_less_metrics_record_in_batched_mode_alongside_labelled():
    counter, histogram = f"label_less_async_c_{uuid.uuid4().hex}", f"label_less_async_h_{uuid.uuid4().hex}"
    # A namespace unique to this test isolates its cache-operation series from any other collector.
    namespace = f"label-less-{uuid.uuid4().hex}"
    collector = AsyncMetricsCollector(sync_mode=False, auto_detect_mode=False)

    collector.record_counter(counter)
    collector.record_counter(counter, value=2.0)
    collector.record_histogram(histogram, 5.0)
    collector.record_cache_operation(operation="get", namespace=namespace, success=True, duration_ms=1.0)
    collector.shutdown()

    assert _sample(f"{counter}_total") == 3.0
    assert _sample(f"{histogram}_count") == 1.0
    assert _sample(f"{histogram}_sum") == 5.0
    cache_op_labels = {"operation": "get", "namespace": namespace, "success": "True", "serializer": "unknown"}
    assert _sample("cache_operations_total", cache_op_labels) == 1.0
