"""The duration and size histograms resolve real cache latencies and payload sizes, in either recording mode.

prometheus_client's default buckets top out at 10, sized for seconds. In milliseconds and bytes that put every L2
round trip and every real payload in ``+Inf``, so the documented p99 queries returned the top finite bucket.
"""

from __future__ import annotations

import math
import uuid
from collections.abc import Iterator
from typing import Any

import pytest

prometheus_client = pytest.importorskip("prometheus_client")

from cachekit.reliability.async_metrics import (  # noqa: E402
    DURATION_BUCKETS_MS,
    SIZE_BUCKETS_BYTES,
    AsyncMetricsCollector,
    _metrics_cache,
)

DURATION = "cache_operation_duration_ms"
SIZE = "cache_operation_size_bytes"


def _samples(name: str, namespace: str) -> dict[tuple[Any, ...], float]:
    """Map each ``_bucket``/``_count``/``_sum`` sample of ``name`` in ``namespace`` to its value, namespace dropped."""
    out = {}
    for sample in _metrics_cache[name].collect()[0].samples:
        if sample.labels.get("namespace") != namespace or sample.name.endswith("_created"):
            continue
        labels = tuple(sorted((k, v) for k, v in sample.labels.items() if k != "namespace"))
        out[(sample.name, labels)] = sample.value
    return out


def _bucket_bounds(name: str, namespace: str) -> list[float]:
    bounds = {float(dict(labels)["le"]) for (series, labels) in _samples(name, namespace) if series.endswith("_bucket")}
    return sorted(bounds)


def _bucket_of(name: str, namespace: str) -> float:
    """Return the ``le`` of the lowest bucket holding the one observation recorded in ``namespace``."""
    hits = [
        float(dict(labels)["le"])
        for (series, labels), v in _samples(name, namespace).items()
        if series.endswith("_bucket") and v
    ]
    return min(hits)


@pytest.fixture
def fresh_histograms() -> Iterator[None]:
    """Unregister both histograms so the test's own collector is the first to register them, then restore them."""
    registry = prometheus_client.REGISTRY
    saved = {name: _metrics_cache.pop(name, None) for name in (DURATION, SIZE)}
    for metric in saved.values():
        if metric is not None:
            registry.unregister(metric)
    try:
        yield
    finally:
        for name, metric in saved.items():
            fresh = _metrics_cache.pop(name, None)
            if fresh is not None:
                registry.unregister(fresh)
            if metric is not None:
                registry.register(metric)
                _metrics_cache[name] = metric


def test_batched_path_registers_documented_buckets(fresh_histograms: None) -> None:
    # The first registration fixes a metric's buckets for the whole process, so the batched path must set them too.
    namespace = f"buckets-batched-{uuid.uuid4().hex}"
    collector = AsyncMetricsCollector(sync_mode=False, auto_detect_mode=False)
    collector.record_cache_operation("get", namespace, True, duration_ms=1.0, size_bytes=1)
    collector.shutdown()

    assert _bucket_bounds(DURATION, namespace) == [*DURATION_BUCKETS_MS, math.inf]
    assert _bucket_bounds(SIZE, namespace) == [*SIZE_BUCKETS_BYTES, math.inf]


def test_sync_path_registers_documented_buckets(fresh_histograms: None) -> None:
    namespace = f"buckets-sync-{uuid.uuid4().hex}"
    AsyncMetricsCollector(sync_mode=True).record_cache_operation("get", namespace, True, duration_ms=1.0, size_bytes=1)

    assert _bucket_bounds(DURATION, namespace) == [*DURATION_BUCKETS_MS, math.inf]
    assert _bucket_bounds(SIZE, namespace) == [*SIZE_BUCKETS_BYTES, math.inf]


@pytest.mark.parametrize("buckets", [DURATION_BUCKETS_MS, SIZE_BUCKETS_BYTES])
def test_bucket_count_is_bounded(buckets: tuple[float, ...]) -> None:
    # Every finite bound is one more _bucket series per label tuple.
    assert len(buckets) <= 14
    assert list(buckets) == sorted(set(buckets))


@pytest.mark.parametrize(
    ("name", "values", "kwarg"),
    [
        (DURATION, [0.03, 0.5, 5, 50, 900], "duration_ms"),  # L1 hit, local Redis, remote Redis, SaaS, slow SaaS
        (SIZE, [64, 4 * 1024, 64 * 1024, 1024 * 1024], "size_bytes"),
    ],
)
def test_real_values_land_in_distinct_finite_buckets(name: str, values: list[float], kwarg: str) -> None:
    collector = AsyncMetricsCollector(sync_mode=True)
    buckets = []
    for value in values:
        namespace = f"buckets-resolution-{uuid.uuid4().hex}"
        collector.record_cache_operation("get", namespace, True, **{"duration_ms": 1.0, "size_bytes": 1, kwarg: value})
        buckets.append(_bucket_of(name, namespace))

    assert all(math.isfinite(b) for b in buckets), buckets
    assert len(set(buckets)) == len(values), buckets


def test_batched_mode_observes_what_sync_mode_observes() -> None:
    # Zero values included: sync mode skips them, and batched mode must skip the same ones.
    records = [
        ("get", True, 0.03, 64),
        ("get", True, 0.0, 0),
        ("get", False, 12.5, 0),
        ("set", True, 900.0, 1024 * 1024),
        ("get", True, 50.0, 4096),
        ("set", True, 0.0, 300),
        ("get", True, 0.5, 70_000),
    ] * 5
    ns_sync, ns_batched = f"parity-sync-{uuid.uuid4().hex}", f"parity-batched-{uuid.uuid4().hex}"

    sync = AsyncMetricsCollector(sync_mode=True)
    # A small batch splits the records across several flushes, as a busy worker would.
    batched = AsyncMetricsCollector(batch_size=3, sync_mode=False, auto_detect_mode=False)
    for operation, success, duration_ms, size_bytes in records:
        sync.record_cache_operation(operation, ns_sync, success, duration_ms, serializer="rust", size_bytes=size_bytes)
        batched.record_cache_operation(operation, ns_batched, success, duration_ms, serializer="rust", size_bytes=size_bytes)
    batched.shutdown()

    assert batched.get_dropped_metrics_count() == 0
    for name in (DURATION, SIZE):
        expected = _samples(name, ns_sync)
        assert expected, name
        assert _samples(name, ns_batched) == expected, name
