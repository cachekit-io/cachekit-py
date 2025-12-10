"""Metrics collection for reliability monitoring.

This module provides lightweight metrics collection for circuit breakers,
adaptive timeouts, and other reliability features.
"""

import logging
import threading
import time
from collections import defaultdict
from typing import Any, ClassVar, Optional

logger = logging.getLogger(__name__)

# Thread-safe metrics storage
_metrics_lock = threading.RLock()
_metrics_data = defaultdict(lambda: defaultdict(float))
_metrics_timestamps = defaultdict(float)


class MetricsCollector:
    """Simple metrics collector for reliability features.

    Examples:
        Create and use a counter:

        >>> counter = MetricsCollector("test_counter")
        >>> counter.inc()
        >>> counter.get()
        1.0
        >>> counter.inc()
        >>> counter.get()
        2.0

        Use with labels:

        >>> counter = MetricsCollector("labeled_counter")
        >>> counter.inc({"operation": "get", "status": "success"})
        >>> counter.get({"operation": "get", "status": "success"})
        1.0

        Set gauge value:

        >>> gauge = MetricsCollector("test_gauge")
        >>> gauge.set(42.0)
        >>> gauge.get()
        42.0

        Chain with labels() method:

        >>> counter = MetricsCollector("chained")
        >>> counter.labels(operation="set").inc()
        >>> counter.get({"operation": "set"})
        1.0
    """

    def __init__(self, name: str):
        self.name = name
        self._labels = {}

    def labels(self, **kwargs) -> "MetricsCollector":
        """Return a new collector instance with labels set."""
        instance = MetricsCollector(self.name)
        instance._labels = kwargs
        return instance

    def inc(self, labels: Optional[dict[str, str]] = None):
        """Increment a counter metric."""
        all_labels = {**self._labels, **(labels or {})}
        key = self._make_key(all_labels)
        with _metrics_lock:
            _metrics_data[self.name][key] += 1
            _metrics_timestamps[f"{self.name}:{key}"] = time.time()

    def set(self, value: float, labels: Optional[dict[str, str]] = None):
        """Set a gauge metric."""
        all_labels = {**self._labels, **(labels or {})}
        key = self._make_key(all_labels)
        with _metrics_lock:
            _metrics_data[self.name][key] = value
            _metrics_timestamps[f"{self.name}:{key}"] = time.time()

    def observe(self, value: float, labels: Optional[dict[str, str]] = None):
        """Observe a histogram value (alias for set for compatibility)."""
        self.set(value, labels)

    def get(self, labels: Optional[dict[str, str]] = None) -> float:
        """Get current metric value."""
        all_labels = {**self._labels, **(labels or {})}
        key = self._make_key(all_labels)
        with _metrics_lock:
            return _metrics_data[self.name].get(key, 0)

    def _make_key(self, labels: dict[str, str]) -> str:
        """Create a key from labels."""
        if not labels:
            return "default"
        return "|".join(f"{k}={v}" for k, v in sorted(labels.items()))


# Pre-defined metrics for common reliability patterns
cache_operations = MetricsCollector("cache_operations_total")
cache_latency = MetricsCollector("cache_operation_duration_seconds")  # Added for test compatibility
circuit_breaker_state = MetricsCollector("circuit_breaker_state")
adaptive_timeout_adjustments = MetricsCollector("adaptive_timeout_adjustments")
connection_pool_usage = MetricsCollector("connection_pool_usage")


def get_all_metrics() -> dict[str, dict[str, Any]]:
    """Get all collected metrics.

    Examples:
        >>> clear_metrics()  # Start fresh
        >>> counter = MetricsCollector("api_calls")
        >>> counter.inc()
        >>> metrics = get_all_metrics()
        >>> "api_calls" in metrics
        True
    """
    with _metrics_lock:
        return dict(_metrics_data)


def clear_metrics():
    """Clear all metrics (useful for testing).

    Examples:
        >>> counter = MetricsCollector("to_clear")
        >>> counter.inc()
        >>> clear_metrics()
        >>> counter.get()
        0
    """
    with _metrics_lock:
        _metrics_data.clear()
        _metrics_timestamps.clear()


# Compatibility aliases for legacy code
def record_cache_operation(operation: str, status: str, namespace: str = "default"):
    """Record a cache operation."""
    cache_operations.inc({"operation": operation, "status": status, "namespace": namespace})


def record_circuit_breaker_state(state: str, namespace: str = "default"):
    """Record circuit breaker state change."""
    circuit_breaker_state.set(1.0 if state == "OPEN" else 0.0, {"state": state, "namespace": namespace})


def record_timeout_adjustment(adjustment_type: str, value: float, namespace: str = "default"):
    """Record adaptive timeout adjustment."""
    adaptive_timeout_adjustments.inc({"type": adjustment_type, "namespace": namespace})


# Async metrics collector class
class AsyncMetricsCollector:
    """Async metrics collector for high-performance scenarios.

    This collector uses a background worker thread to process metrics asynchronously,
    preventing blocking of the main thread during metric recording.
    """

    def __init__(self, name: str = "async_metrics", max_queue_size: int = 1000, worker_timeout: float = 1.0):
        self.name = name
        self.max_queue_size = max_queue_size
        self.worker_timeout = worker_timeout

        # Metrics storage
        self._metrics = defaultdict(lambda: defaultdict(float))
        self._lock = threading.RLock()

        # Queue for async processing
        import queue

        self._metric_queue = queue.Queue(maxsize=max_queue_size)

        # Worker thread management
        self._shutdown_event = threading.Event()
        self._worker_thread = None
        self._start_worker()

        # Statistics tracking
        self._stats: dict[str, Any] = {"processed_count": 0, "dropped_count": 0, "queue_size": 0}
        self._stats_lock = threading.Lock()

    def _start_worker(self):
        """Start the background worker thread."""
        if self._worker_thread is None or not self._worker_thread.is_alive():
            self._shutdown_event.clear()
            self._worker_thread = threading.Thread(target=self._worker_loop, daemon=True)
            self._worker_thread.start()

    def _worker_loop(self):
        """Main worker loop for processing metrics."""
        import queue

        while not self._shutdown_event.is_set():
            try:
                # Wait for metric with timeout to allow shutdown checks
                metric_data = self._metric_queue.get(timeout=self.worker_timeout)

                # Process the metric
                self._process_metric(metric_data)

                # Update statistics
                with self._stats_lock:
                    self._stats["processed_count"] += 1
                    self._stats["queue_size"] = self._metric_queue.qsize()

                self._metric_queue.task_done()

            except queue.Empty:
                # Timeout - continue to check shutdown
                continue
            except Exception as e:
                # Log error but keep worker running
                logger.error(f"Error processing metric in worker thread: {e}")
                try:
                    self._metric_queue.task_done()
                except ValueError:
                    pass  # task_done() called more times than get()

    def _process_metric(self, metric_data: dict):
        """Process a single metric."""
        try:
            metric_type = metric_data["type"]
            name = metric_data["name"]
            value = metric_data.get("value", 1.0)
            labels = metric_data.get("labels", {})

            # Try to use Prometheus metrics if available
            if self._try_prometheus_metric(metric_type, name, value, labels):
                return

            # Fallback to local storage
            key = self._make_key(labels)
            with self._lock:
                if metric_type == "counter":
                    self._metrics[name][key] += value
                elif metric_type in ["histogram", "gauge"]:
                    self._metrics[name][key] = value

        except Exception as e:
            logger.debug(f"Failed to process metric {metric_data.get('name', 'unknown')}: {e}")

    def _try_prometheus_metric(self, metric_type: str, name: str, value: float, labels: dict) -> bool:
        """Try to record using Prometheus metrics if available."""
        try:
            # Map common metric names to Prometheus metrics
            if name == "redis_cache_operations_total":
                cache_operations.labels(**labels).inc(value)  # type: ignore[arg-type]
                return True
            elif name == "redis_cache_operation_duration_seconds":
                cache_latency.labels(**labels).observe(value)
                return True
            elif name == "redis_circuit_breaker_state":
                circuit_breaker_state.labels(**labels).set(value)
                return True
            else:
                # Log debug message for unknown metrics
                logger.debug(f"Unknown metric name: {name}, using local storage")
                return False

        except (ImportError, AttributeError, Exception) as e:
            logger.debug(f"Prometheus metric not available for {name}: {e}")

        return False

    def _enqueue_metric(self, metric_data: dict):
        """Add metric to processing queue."""
        import queue

        try:
            self._metric_queue.put_nowait(metric_data)
            with self._stats_lock:
                self._stats["queue_size"] = self._metric_queue.qsize()
        except queue.Full:
            # Queue is full - drop the metric
            with self._stats_lock:
                self._stats["dropped_count"] += 1
            logger.warning(f"Metrics queue full, dropped metric: {metric_data.get('name', 'unknown')}")

    def record_counter(self, name: str, labels: Optional[dict[str, str]] = None, value: float = 1.0):
        """Record a counter metric."""
        metric_data = {"type": "counter", "name": name, "value": value, "labels": labels or {}}
        self._enqueue_metric(metric_data)

    def record_histogram(self, name: str, value: float, labels: Optional[dict[str, str]] = None):
        """Record a histogram metric."""
        metric_data = {"type": "histogram", "name": name, "value": value, "labels": labels or {}}
        self._enqueue_metric(metric_data)

    def record_gauge(self, name: str, value: float, labels: Optional[dict[str, str]] = None):
        """Record a gauge metric."""
        metric_data = {"type": "gauge", "name": name, "value": value, "labels": labels or {}}
        self._enqueue_metric(metric_data)

    def record_operation(self, operation: str, duration: float, labels: Optional[dict[str, str]] = None):
        """Record an operation with duration."""
        self.record_histogram(f"{operation}_duration", duration, labels)

    def get_stats(self) -> dict[str, Any]:
        """Get all recorded statistics."""
        with self._stats_lock:
            stats = dict(self._stats)
            stats["queue_size"] = self._metric_queue.qsize()

        # Add local metrics
        with self._lock:
            stats["local_metrics"] = dict(self._metrics)

        return stats

    def clear(self):
        """Clear all metrics."""
        with self._lock:
            self._metrics.clear()

        with self._stats_lock:
            self._stats["processed_count"] = 0
            self._stats["dropped_count"] = 0

    def flush(self, timeout: float = 2.0):
        """Flush all pending metrics with timeout."""
        if self._worker_thread and self._worker_thread.is_alive():
            # Wait for queue to be processed
            start_time = time.time()
            while not self._metric_queue.empty() and (time.time() - start_time) < timeout:
                time.sleep(0.01)

    def shutdown(self, timeout: float = 2.0):
        """Shutdown the collector gracefully."""
        if self._shutdown_event.is_set():
            return  # Already shutdown

        # Signal shutdown
        self._shutdown_event.set()

        # Wait for worker to finish
        if self._worker_thread and self._worker_thread.is_alive():
            self._worker_thread.join(timeout=timeout)

    def _make_key(self, labels: dict[str, str]) -> str:
        """Create a key from labels."""
        if not labels:
            return "default"
        return "|".join(f"{k}={v}" for k, v in sorted(labels.items()))


# Prometheus metrics registry class
class PrometheusMetricsRegistry:
    """Prometheus-compatible metrics registry.

    This registry is designed to work with prometheus_client metric classes
    while maintaining compatibility with the existing codebase.
    """

    # Class-level registry to maintain singleton behavior
    _registry: ClassVar[dict[str, Any]] = {}
    _lock: ClassVar[threading.RLock] = threading.RLock()

    def __init__(self):
        # Instance-level metrics for backward compatibility
        self._instance_metrics = {}
        self._instance_lock = threading.RLock()

    @classmethod
    def get_or_create_metric(cls, metric_class, name: str, description: str = "", labelnames=None, **kwargs):
        """Get or create a Prometheus metric.

        Args:
            metric_class: Prometheus metric class (Counter, Gauge, Histogram, etc.)
            name: Metric name
            description: Metric description
            labelnames: List of label names
            **kwargs: Additional arguments for metric creation

        Returns:
            Prometheus metric instance
        """
        with cls._lock:
            if name not in cls._registry:
                try:
                    # Create the Prometheus metric
                    if labelnames:
                        metric = metric_class(name, description, labelnames, **kwargs)
                    else:
                        metric = metric_class(name, description, **kwargs)
                    cls._registry[name] = metric
                except Exception as e:
                    # If Prometheus metric creation fails, return a compatible mock
                    logger.warning(f"Failed to create Prometheus metric {name}: {e}")
                    cls._registry[name] = MetricsCollector(name)

            return cls._registry[name]

    def get_all_metrics(self):
        """Get all metrics from both class and instance registries."""
        with self._lock:
            all_metrics = dict(self._registry)

        with self._instance_lock:
            all_metrics.update(self._instance_metrics)

        return all_metrics


# Global async metrics collector instance
_global_async_collector = None
_collector_lock = threading.Lock()


def get_async_metrics_collector() -> AsyncMetricsCollector:
    """Get the global async metrics collector instance."""
    global _global_async_collector
    if _global_async_collector is None:
        with _collector_lock:
            if _global_async_collector is None:
                _global_async_collector = AsyncMetricsCollector()
    elif not _global_async_collector._worker_thread.is_alive():
        # Worker thread died, restart it
        logger.warning("Async metrics collector worker thread died, restarting...")
        with _collector_lock:
            _global_async_collector._start_worker()
    return _global_async_collector


def record_async_metric(metric_type: str, name: str, value: float = 1.0, labels: Optional[dict[str, str]] = None):
    """Record an async metric with proper type handling.

    Args:
        metric_type: Type of metric ("counter", "histogram", "gauge")
        name: Metric name
        value: Metric value
        labels: Optional labels dictionary
    """
    collector = get_async_metrics_collector()

    try:
        if metric_type == "counter":
            collector.record_counter(name, labels, value)
        elif metric_type == "histogram":
            collector.record_histogram(name, value, labels)
        elif metric_type == "gauge":
            collector.record_gauge(name, value, labels)
        else:
            logger.warning(f"Unknown metric type: {metric_type}")
            # Fallback to operation recording
            collector.record_operation(name, value, labels)
    except Exception as e:
        logger.error(f"Failed to record async metric {name}: {e}")
