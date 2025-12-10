"""Smoke tests for reliability infrastructure modules.

These are MINIMAL smoke tests ensuring reliability infrastructure:
1. Classes instantiate without crashing
2. Basic methods execute without errors
3. Worker threads start/stop cleanly
4. No obvious exceptions in happy path

Coverage targets:
- async_metrics.py (AsyncMetricsCollector)
- error_classification.py (RedisErrorClassifier)
- lightweight_health.py (LightweightHealthChecker)
- metrics_collection.py (MetricsCollector, AsyncMetricsCollector)
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

import pytest

from cachekit.backends.errors import BackendError, BackendErrorType
from cachekit.health import HealthChecker, get_health_checker
from cachekit.reliability.error_classification import BackendErrorClassifier
from cachekit.reliability.metrics_collection import (
    AsyncMetricsCollector,
    MetricsCollector,
    clear_metrics,
    get_async_metrics_collector,
    record_async_metric,
)

if TYPE_CHECKING:
    pass


@pytest.mark.critical
class TestMetricsCollectorSmoke:
    """Smoke tests for MetricsCollector."""

    def test_metrics_collector_instantiates(self):
        """SMOKE: MetricsCollector can instantiate."""
        collector = MetricsCollector("test_metric")
        assert collector is not None
        assert collector.name == "test_metric"

    def test_metrics_collector_increment_without_crash(self):
        """SMOKE: Incrementing counter doesn't crash."""
        collector = MetricsCollector("test_counter")

        # Should not raise
        collector.inc()
        collector.inc()

        # Should be able to read back (incremented twice)
        assert collector.get() >= 2

    def test_metrics_collector_set_gauge_without_crash(self):
        """SMOKE: Setting gauge doesn't crash."""
        collector = MetricsCollector("test_gauge")

        # Should not raise
        collector.set(42.0)
        collector.set(100.0, labels={"status": "healthy"})

        # Should be able to read back
        assert collector.get() == 42.0
        assert collector.get(labels={"status": "healthy"}) == 100.0

    def test_metrics_collector_observe_without_crash(self):
        """SMOKE: Observing histogram doesn't crash."""
        collector = MetricsCollector("test_histogram")

        # Should not raise
        collector.observe(1.5)
        collector.observe(2.5, labels={"operation": "get"})

        # Should be able to read back
        assert collector.get() == 1.5

    def test_metrics_collector_labels_returns_instance(self):
        """SMOKE: Labels method returns new instance."""
        collector = MetricsCollector("test_metric")
        labeled = collector.labels(operation="get", status="success")

        assert labeled is not None
        assert labeled.name == "test_metric"
        assert labeled._labels == {"operation": "get", "status": "success"}

    def test_clear_metrics_doesnt_crash(self):
        """SMOKE: Clearing metrics doesn't crash."""
        collector = MetricsCollector("test_counter")
        collector.inc()

        # Should not raise
        clear_metrics()

        # Metrics should be cleared
        assert collector.get() == 0


@pytest.mark.critical
class TestAsyncMetricsCollectorSmoke:
    """Smoke tests for AsyncMetricsCollector."""

    def test_async_collector_instantiates(self):
        """SMOKE: AsyncMetricsCollector can instantiate."""
        collector = AsyncMetricsCollector()
        assert collector is not None
        collector.shutdown()

    def test_async_collector_worker_thread_starts(self):
        """SMOKE: Worker thread starts successfully."""
        collector = AsyncMetricsCollector()

        # Worker thread should be alive
        assert collector._worker_thread is not None
        assert collector._worker_thread.is_alive()

        collector.shutdown()

    def test_async_collector_records_counter_without_crash(self):
        """SMOKE: Recording counter doesn't crash."""
        collector = AsyncMetricsCollector()

        # Should not raise
        collector.record_counter("test_counter", labels={"foo": "bar"})
        collector.record_counter("test_counter", value=5.0)

        # Give worker time to process
        time.sleep(0.1)

        collector.shutdown()

    def test_async_collector_records_gauge_without_crash(self):
        """SMOKE: Recording gauge doesn't crash."""
        collector = AsyncMetricsCollector()

        # Should not raise
        collector.record_gauge("test_gauge", 42.0)
        collector.record_gauge("test_gauge", 100.0, labels={"status": "healthy"})

        # Give worker time to process
        time.sleep(0.1)

        collector.shutdown()

    def test_async_collector_records_histogram_without_crash(self):
        """SMOKE: Recording histogram doesn't crash."""
        collector = AsyncMetricsCollector()

        # Should not raise
        collector.record_histogram("test_histogram", 1.5)
        collector.record_histogram("test_histogram", 2.5, labels={"operation": "get"})

        # Give worker time to process
        time.sleep(0.1)

        collector.shutdown()

    def test_async_collector_shutdown_cleans_up(self):
        """SMOKE: Shutdown cleans up worker thread."""
        collector = AsyncMetricsCollector()
        assert collector._worker_thread.is_alive()

        collector.shutdown()

        # Give thread time to stop
        time.sleep(0.2)

        # Worker thread should stop
        assert not collector._worker_thread.is_alive()

    def test_async_collector_queue_full_drops_gracefully(self):
        """SMOKE: Full queue drops metrics gracefully without crashing."""
        collector = AsyncMetricsCollector(max_queue_size=2)

        # Fill queue + overflow
        for i in range(10):
            collector.record_counter("test_counter", value=float(i))

        # Should not crash
        stats = collector.get_stats()
        assert "dropped_count" in stats

        collector.shutdown()

    def test_async_collector_get_stats_without_crash(self):
        """SMOKE: Getting stats doesn't crash."""
        collector = AsyncMetricsCollector()

        # Should not raise
        stats = collector.get_stats()
        assert isinstance(stats, dict)
        assert "processed_count" in stats
        assert "dropped_count" in stats
        assert "queue_size" in stats

        collector.shutdown()

    def test_async_collector_clear_without_crash(self):
        """SMOKE: Clearing metrics doesn't crash."""
        collector = AsyncMetricsCollector()
        collector.record_counter("test_counter", value=10.0)

        # Should not raise
        collector.clear()

        stats = collector.get_stats()
        assert stats["processed_count"] == 0
        assert stats["dropped_count"] == 0

        collector.shutdown()

    def test_get_async_metrics_collector_returns_singleton(self):
        """SMOKE: Global collector returns singleton instance."""
        collector1 = get_async_metrics_collector()
        collector2 = get_async_metrics_collector()

        assert collector1 is collector2

        # Cleanup
        collector1.shutdown()

    def test_record_async_metric_without_crash(self):
        """SMOKE: Record async metric function doesn't crash."""
        # Should not raise
        record_async_metric("counter", "test_metric", 1.0, {"foo": "bar"})
        record_async_metric("histogram", "test_duration", 1.5)
        record_async_metric("gauge", "test_gauge", 42.0)

        # Give worker time to process
        time.sleep(0.1)

        # Cleanup
        get_async_metrics_collector().shutdown()


@pytest.mark.critical
class TestErrorClassifierSmoke:
    """Smoke tests for BackendErrorClassifier."""

    def test_error_classifier_handles_transient_errors(self):
        """SMOKE: Classifier handles transient errors without crashing."""
        # Should not raise
        transient_error = BackendError("Connection failed", error_type=BackendErrorType.TRANSIENT)
        timeout_error = BackendError("Timeout", error_type=BackendErrorType.TIMEOUT)
        assert BackendErrorClassifier.is_circuit_breaker_failure(transient_error) is True
        assert BackendErrorClassifier.is_circuit_breaker_failure(timeout_error) is True

    def test_error_classifier_handles_permanent_errors(self):
        """SMOKE: Classifier handles permanent errors without crashing."""
        # Should not raise
        auth_error = BackendError("Auth failed", error_type=BackendErrorType.AUTHENTICATION)
        perm_error = BackendError("Invalid config", error_type=BackendErrorType.PERMANENT)
        assert BackendErrorClassifier.is_circuit_breaker_failure(auth_error) is False
        assert BackendErrorClassifier.is_circuit_breaker_failure(perm_error) is False

    def test_error_classifier_handles_unknown_errors(self):
        """SMOKE: Classifier handles unknown errors without crashing."""
        # Should not raise
        unknown_error = BackendError("Unknown", error_type=BackendErrorType.UNKNOWN)
        assert BackendErrorClassifier.is_circuit_breaker_failure(unknown_error) is True
        # Non-BackendError exceptions return False
        assert BackendErrorClassifier.is_circuit_breaker_failure(ValueError()) is False
        assert BackendErrorClassifier.is_circuit_breaker_failure(RuntimeError()) is False

    def test_error_classifier_get_category_without_crash(self):
        """SMOKE: Getting error category doesn't crash."""
        # Should not raise
        transient_error = BackendError("Connection failed", error_type=BackendErrorType.TRANSIENT)
        auth_error = BackendError("Auth failed", error_type=BackendErrorType.AUTHENTICATION)
        assert BackendErrorClassifier.get_error_category(transient_error) == "transient"
        assert BackendErrorClassifier.get_error_category(auth_error) == "authentication"
        assert BackendErrorClassifier.get_error_category(ValueError()) == "application"


@pytest.mark.critical
class TestHealthCheckerSmoke:
    """Smoke tests for HealthChecker."""

    def test_health_checker_instantiates(self):
        """SMOKE: HealthChecker can instantiate."""
        checker = HealthChecker()
        assert checker is not None

    def test_get_health_checker_singleton(self):
        """SMOKE: Global health checker returns singleton."""
        checker1 = get_health_checker()
        checker2 = get_health_checker()

        assert checker1 is checker2
        assert isinstance(checker1, HealthChecker)
