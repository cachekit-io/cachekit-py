"""Per-operation logging and metrics cost nothing they do not emit.

Every L1 hit, L2 hit and store logs through ``FeatureOrchestrator.log_cache_operation`` and
``SimpleLogger``, and records three Prometheus series. Disabled log levels must skip key redaction
and formatting, enabled ones must emit the same redacted records as before, and each label tuple's
series must be bound once, with the same values recorded.
"""

import logging
import uuid
from unittest.mock import patch

import pytest

from cachekit.backends.provider import SimpleLogger
from cachekit.decorators.orchestrator import FeatureOrchestrator
from cachekit.hash_utils import redact_key_for_log
from cachekit.reliability import async_metrics
from cachekit.reliability.async_metrics import PROMETHEUS_AVAILABLE, AsyncMetricsCollector

RAW_KEY = "ns:tenant-42:func:app.f:args:email@test.com:v1"


def _orchestrator() -> FeatureOrchestrator:
    return FeatureOrchestrator(namespace="ns", circuit_breaker_enabled=False, backpressure_enabled=False, collect_stats=False)


@pytest.fixture
def orchestrator_logger():
    log = logging.getLogger("cachekit.decorators.orchestrator")
    level = log.level
    yield log
    log.setLevel(level)


def test_log_cache_operation_skips_redaction_when_info_is_disabled(orchestrator_logger):
    orchestrator_logger.setLevel(logging.WARNING)
    with patch("cachekit.decorators.orchestrator.redact_key_for_log") as redact:
        _orchestrator().log_cache_operation(operation="get", key=RAW_KEY, hit=True)
    redact.assert_not_called()


def test_log_cache_operation_emits_the_redacted_record_when_info_is_enabled(orchestrator_logger, caplog):
    orchestrator_logger.setLevel(logging.INFO)
    with caplog.at_level(logging.INFO, logger="cachekit.decorators.orchestrator"):
        _orchestrator().log_cache_operation(operation="get", key=RAW_KEY, hit=True, duration_ms=1.5)

    (record,) = caplog.records
    redacted = redact_key_for_log(RAW_KEY)
    assert record.getMessage() == "[ns] Cache operation: get"
    assert record.structured == {  # type: ignore[attr-defined]
        "namespace": "ns",
        "message": "Cache operation: get",
        "cache_key": redacted,
        "operation": "get",
        "key": redacted,
        "hit": True,
        "duration_ms": 1.5,
    }
    assert RAW_KEY not in str(record.__dict__)


@pytest.mark.parametrize(
    ("method", "args", "message"),
    [
        ("cache_hit", (RAW_KEY,), "Redis cache hit for key: {}"),
        ("cache_miss", (RAW_KEY,), "Cache miss for key: {}"),
        ("cache_stored", (RAW_KEY, 60), "Cached result for key: {} with TTL 60"),
    ],
)
def test_simple_logger_redacts_only_what_it_emits(method, args, message, caplog):
    name = f"cachekit.test.simple_logger.{uuid.uuid4().hex}"
    log = logging.getLogger(name)
    simple = SimpleLogger(log)

    log.setLevel(logging.INFO)
    with patch("cachekit.backends.provider.redact_key_for_log") as redact:
        getattr(simple, method)(*args)
    redact.assert_not_called()

    log.setLevel(logging.DEBUG)
    with caplog.at_level(logging.DEBUG, logger=name):
        getattr(simple, method)(*args)
    (record,) = caplog.records
    assert record.getMessage() == message.format(redact_key_for_log(RAW_KEY))


needs_prometheus = pytest.mark.skipif(not PROMETHEUS_AVAILABLE, reason="prometheus_client not installed")


def _sample(name: str, labels: dict[str, str]) -> float | None:
    from prometheus_client import REGISTRY

    return REGISTRY.get_sample_value(name, labels)


def _count_labels_calls(monkeypatch) -> dict[str, int]:
    """Count ``labels()`` calls on the three cache-operation metrics."""
    calls = dict.fromkeys(("cache_operations_total", "cache_operation_duration_ms", "cache_operation_size_bytes"), 0)
    AsyncMetricsCollector(sync_mode=True, auto_detect_mode=False).record_cache_operation(
        operation="warm", namespace=f"warm-{uuid.uuid4().hex}", success=True, duration_ms=1.0, size_bytes=1
    )
    for name in calls:
        metric = async_metrics._metrics_cache[name]
        real = metric.labels

        def counting(*args, _name=name, _real=real, **kwargs):
            calls[_name] += 1
            return _real(*args, **kwargs)

        monkeypatch.setattr(metric, "labels", counting)
    return calls


@needs_prometheus
@pytest.mark.parametrize("sync_mode", [True, False])
def test_each_label_tuple_binds_its_series_once_and_records_every_operation(monkeypatch, sync_mode):
    namespace = f"prebound-{uuid.uuid4().hex}"
    calls = _count_labels_calls(monkeypatch)
    collector = AsyncMetricsCollector(sync_mode=sync_mode, auto_detect_mode=False)

    for duration_ms in (1.0, 2.0, 3.0):
        collector.record_cache_operation(
            operation="get", namespace=namespace, success=True, duration_ms=duration_ms, serializer="rust", size_bytes=100
        )
    collector.shutdown()  # batched mode: drain the queue

    assert calls == dict.fromkeys(calls, 1)
    series = {"operation": "get", "namespace": namespace, "serializer": "rust"}
    assert _sample("cache_operations_total", {**series, "success": "True"}) == 3.0
    assert _sample("cache_operation_duration_ms_count", series) == 3.0
    assert _sample("cache_operation_duration_ms_sum", series) == 6.0
    assert _sample("cache_operation_size_bytes_sum", series) == 300.0


@needs_prometheus
def test_a_tuple_with_no_duration_or_size_gets_no_histogram_series():
    namespace = f"prebound-empty-{uuid.uuid4().hex}"
    AsyncMetricsCollector(sync_mode=True, auto_detect_mode=False).record_cache_operation(
        operation="set", namespace=namespace, success=False, duration_ms=0.0
    )

    series = {"operation": "set", "namespace": namespace, "serializer": "unknown"}
    assert _sample("cache_operations_total", {**series, "success": "False"}) == 1.0
    assert _sample("cache_operation_duration_ms_count", series) is None
    assert _sample("cache_operation_size_bytes_count", series) is None


@needs_prometheus
def test_a_replaced_metric_is_rebound_not_recorded_into_the_orphan(monkeypatch):
    from prometheus_client import CollectorRegistry, Counter

    namespace = f"prebound-rebind-{uuid.uuid4().hex}"
    collector = AsyncMetricsCollector(sync_mode=True, auto_detect_mode=False)
    collector.record_cache_operation(operation="get", namespace=namespace, success=True, duration_ms=1.0)

    replacement = Counter(
        "cache_operations", "test", ["operation", "namespace", "success", "serializer"], registry=CollectorRegistry()
    )
    monkeypatch.setitem(async_metrics._metrics_cache, "cache_operations_total", replacement)
    collector.record_cache_operation(operation="get", namespace=namespace, success=True, duration_ms=1.0)

    labels = {"operation": "get", "namespace": namespace, "success": "True", "serializer": "unknown"}
    assert replacement.labels(**labels)._value.get() == 1.0
    assert _sample("cache_operations_total", labels) == 1.0
