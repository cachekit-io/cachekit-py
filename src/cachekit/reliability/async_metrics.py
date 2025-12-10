"""Async metrics collection for high-performance reliability features.

This module provides asynchronous, batched metrics collection to eliminate
synchronous Prometheus updates from the hot path.
"""

import logging
import queue
import threading
import time
from collections import defaultdict
from typing import Any, Optional, Union

logger = logging.getLogger(__name__)

try:
    from prometheus_client import Counter, Gauge, Histogram  # type: ignore[assignment]

    PROMETHEUS_AVAILABLE = True
except ImportError:
    PROMETHEUS_AVAILABLE = False  # type: ignore[misc]

    # Mock classes for when Prometheus is not available
    class Counter:
        """Mock counter for when Prometheus is unavailable."""

        def labels(self, **kwargs):
            """Return self for method chaining (no-op)."""
            return self

        def inc(self, amount=1):
            """Increment counter (no-op)."""
            pass

    class Histogram:
        """Mock histogram for when Prometheus is unavailable."""

        def labels(self, **kwargs):
            """Return self for method chaining (no-op)."""
            return self

        def observe(self, amount):
            """Observe value (no-op)."""
            pass

    class Gauge:
        """Mock gauge for when Prometheus is unavailable."""

        def labels(self, **kwargs):
            """Return self for method chaining (no-op)."""
            return self

        def set(self, value):
            """Set gauge value (no-op)."""
            pass


class AsyncMetricsCollector:
    """High-performance metrics collector with sync/async modes.

    Features:
    - Sync mode: Direct Prometheus calls for low-frequency operations (≤4μs per op)
    - Async mode: Non-blocking batched updates for high-frequency scenarios
    - Automatic mode detection based on usage patterns
    - Zero-copy thread communication with memory pools
    - Graceful overflow handling (drops metrics under extreme load)

    Examples:
        Create collector in sync mode:

        >>> collector = AsyncMetricsCollector(sync_mode=True)
        >>> collector._sync_mode
        True

        Record cache operation:

        >>> collector.record_cache_operation(
        ...     operation="get",
        ...     namespace="test",
        ...     success=True,
        ...     duration_ms=1.5
        ... )

        Get stats:

        >>> stats = collector.get_stats()
        >>> stats["mode"]
        'sync'
        >>> stats["total_operations"] >= 1
        True

        Check dropped metrics count:

        >>> collector.get_dropped_metrics_count()
        0
    """

    def __init__(
        self,
        batch_size: int = 100,
        flush_interval: float = 0.1,
        max_queue_size: int = 10000,
        sync_mode: Union[bool, None] = None,
        auto_detect_mode: bool = True,
    ):
        """Initialize metrics collector with performance optimization.

        Args:
            batch_size: Number of metrics to batch before flushing (async mode only)
            flush_interval: Maximum time between flushes in seconds (async mode only)
            max_queue_size: Maximum queue size before dropping metrics (async mode only)
            sync_mode: Force sync mode (True) or async mode (False). None for auto-detect
            auto_detect_mode: Automatically switch between sync/async based on frequency
        """
        self.batch_size = batch_size
        self.flush_interval = flush_interval
        self.max_queue_size = max_queue_size
        self.auto_detect_mode = auto_detect_mode

        # Performance tracking for mode detection
        self._operation_count = 0
        self._start_time = time.time()
        self._last_mode_check = time.time()

        # Determine initial mode
        if sync_mode is None and auto_detect_mode:
            # Start in sync mode for low-frequency operations
            self._sync_mode = True
        else:
            self._sync_mode = sync_mode if sync_mode is not None else False

        # Cached metric instances (shared between modes)
        self._metrics_cache: dict[str, Any] = {}

        # Async mode components (lazy initialization)
        self._queue = None
        self._stopped = None
        self._worker_thread = None
        self._dropped_metrics = 0

        # Memory pool for reducing allocations
        self._metric_pool = []
        self._pool_lock = threading.Lock()

        # Initialize async mode if needed
        if not self._sync_mode:
            self._init_async_mode()

    def record_cache_operation(
        self,
        operation: str,
        namespace: str,
        success: bool,
        duration_ms: float,
        serializer: str = "unknown",
        size_bytes: int = 0,
        hit: Optional[bool] = None,
    ):
        """Record cache operation metric.

        Automatically uses sync or async mode based on configuration.
        """
        self._operation_count += 1

        # Auto-detect mode switch if enabled
        if self.auto_detect_mode and self._should_check_mode():
            self._maybe_switch_mode()

        if self._sync_mode:
            self._record_cache_operation_sync(operation, namespace, success, duration_ms, serializer, size_bytes, hit)
        else:
            self._record_cache_operation_async(operation, namespace, success, duration_ms, serializer, size_bytes, hit)

    def record_circuit_breaker_state(self, namespace: str, state: str, transitions: int = 0):
        """Record circuit breaker state change."""
        self._operation_count += 1

        if self.auto_detect_mode and self._should_check_mode():
            self._maybe_switch_mode()

        if self._sync_mode:
            self._record_circuit_breaker_sync(namespace, state, transitions)
        else:
            self._record_circuit_breaker_async(namespace, state, transitions)

    def record_counter(self, metric_name: str, labels: Optional[dict[str, Any]] = None, value: float = 1.0):
        """Record a counter metric.

        Args:
            metric_name: Name of the counter metric
            labels: Dictionary of labels for the metric
            value: Value to increment by (default: 1.0)
        """
        self._operation_count += 1

        if self.auto_detect_mode and self._should_check_mode():
            self._maybe_switch_mode()

        if self._sync_mode:
            self._record_counter_sync(metric_name, labels or {}, value)
        else:
            self._record_counter_async(metric_name, labels or {}, value)

    def record_histogram(self, metric_name: str, value: float, labels: Optional[dict[str, Any]] = None):
        """Record a histogram metric.

        Args:
            metric_name: Name of the histogram metric
            value: Value to observe
            labels: Dictionary of labels for the metric
        """
        self._operation_count += 1

        if self.auto_detect_mode and self._should_check_mode():
            self._maybe_switch_mode()

        if self._sync_mode:
            self._record_histogram_sync(metric_name, value, labels or {})
        else:
            self._record_histogram_async(metric_name, value, labels or {})

    def _worker_loop(self):
        """Background worker that processes metrics in batches."""
        assert self._stopped is not None, "Worker started before initialization"
        assert self._queue is not None, "Worker started before initialization"

        batch = []
        last_flush = time.time()

        while not self._stopped.is_set():
            try:
                # Wait for metric with timeout
                metric = self._queue.get(timeout=self.flush_interval)
                batch.append(metric)

                # Flush if batch is full
                if len(batch) >= self.batch_size:
                    self._flush_batch(batch)
                    batch = []
                    last_flush = time.time()

            except queue.Empty:
                # Timeout - flush any pending metrics
                if batch:
                    self._flush_batch(batch)
                    batch = []
                    last_flush = time.time()

            except Exception as e:
                logger.error(f"Error in metrics worker: {e}")

            # Force flush if too much time has passed
            if batch and (time.time() - last_flush) > self.flush_interval:
                self._flush_batch(batch)
                batch = []
                last_flush = time.time()

        # Final flush on shutdown
        if batch:
            self._flush_batch(batch)

    def _flush_batch(self, batch: list[dict[str, Any]]):
        """Process a batch of metrics and update Prometheus."""
        if not PROMETHEUS_AVAILABLE:
            # Return metrics to pool
            for metric in batch:
                self._return_to_pool(metric)
            return

        # Group metrics by type for efficient processing
        cache_ops = defaultdict(lambda: {"count": 0, "duration": 0, "size": 0})
        circuit_states = defaultdict(int)
        counters = defaultdict(lambda: defaultdict(float))  # {name: {labels_key: value}}
        histograms = defaultdict(list)  # {name: [(value, labels_key)]}

        for metric in batch:
            try:
                if metric["type"] == "cache_operation":
                    key = (metric["operation"], metric["namespace"], metric["success"], metric["serializer"])
                    cache_ops[key]["count"] += 1
                    cache_ops[key]["duration"] += metric["duration_ms"]
                    cache_ops[key]["size"] += metric["size_bytes"]

                elif metric["type"] == "circuit_breaker":
                    key = (metric["namespace"], metric["state"])
                    circuit_states[key] += 1

                elif metric["type"] == "counter":
                    name = metric["name"]
                    labels_key = tuple(sorted(metric["labels"].items()))
                    counters[name][labels_key] += metric["value"]

                elif metric["type"] == "histogram":
                    name = metric["name"]
                    labels_key = tuple(sorted(metric["labels"].items()))
                    histograms[name].append((metric["value"], labels_key))

            except Exception as e:
                logger.error(f"Error processing metric: {e}")
            finally:
                # Return metric data to pool for reuse
                self._return_to_pool(metric)

        # Batch update Prometheus metrics
        self._update_prometheus_metrics(cache_ops, circuit_states, counters, histograms)  # type: ignore[arg-type]

    def _update_prometheus_metrics(
        self,
        cache_ops: dict[tuple[Any, ...], dict[str, Any]],
        circuit_states: dict[tuple[Any, ...], int],
        counters: dict[str, dict[str, Any]],
        histograms: dict[str, list[Any]],
    ):
        """Update Prometheus metrics in batch."""
        # Get or create metric instances
        cache_counter = self._get_metric(
            "cache_operations_total", Counter, "Total cache operations", ["operation", "namespace", "success", "serializer"]
        )

        cache_duration = self._get_metric(
            "cache_operation_duration_ms", Histogram, "Cache operation duration", ["operation", "namespace", "serializer"]
        )

        cache_size = self._get_metric(
            "cache_operation_size_bytes", Histogram, "Cache operation size", ["operation", "namespace", "serializer"]
        )

        circuit_gauge = self._get_metric("circuit_breaker_state", Gauge, "Circuit breaker state", ["namespace", "state"])

        # Batch update cache metrics
        for (operation, namespace, success, serializer), stats in cache_ops.items():
            cache_counter.labels(operation=operation, namespace=namespace, success=str(success), serializer=serializer).inc(
                stats["count"]
            )

            if stats["duration"] > 0:
                # Record average duration for the batch
                avg_duration = stats["duration"] / stats["count"]
                cache_duration.labels(operation=operation, namespace=namespace, serializer=serializer).observe(avg_duration)

            if stats["size"] > 0:
                # Record average size for the batch
                avg_size = stats["size"] / stats["count"]
                cache_size.labels(operation=operation, namespace=namespace, serializer=serializer).observe(avg_size)

        # Update circuit breaker states
        for (namespace, state), count in circuit_states.items():
            circuit_gauge.labels(namespace=namespace, state=state).set(count)

        # Update generic counters
        for name, label_values in counters.items():
            # Extract label names from first entry
            if label_values:
                first_labels_key = next(iter(label_values.keys()))
                label_names = [k for k, v in first_labels_key] if first_labels_key else []

                counter_metric = self._get_metric(name, Counter, f"Counter metric {name}", label_names)
                for labels_key, value in label_values.items():
                    labels_dict = dict(labels_key)  # type: ignore[arg-type]
                    counter_metric.labels(**labels_dict).inc(value)  # type: ignore[arg-type]

        # Update generic histograms
        for name, observations in histograms.items():
            # Extract label names from first entry
            if observations:
                first_value, first_labels_key = observations[0]
                label_names = [k for k, v in first_labels_key] if first_labels_key else []

                histogram_metric = self._get_metric(name, Histogram, f"Histogram metric {name}", label_names)
                for value, labels_key in observations:
                    labels_dict = dict(labels_key)
                    histogram_metric.labels(**labels_dict).observe(value)

    def _get_metric(self, name: str, metric_class: type, description: str, labels: list[str]) -> Any:
        """Get or create a cached metric instance."""
        if name not in self._metrics_cache:
            try:
                self._metrics_cache[name] = metric_class(name, description, labels)
            except ValueError as e:
                if "Duplicated timeseries" in str(e):
                    # Use a unique name for this instance to avoid conflicts
                    import uuid

                    unique_name = f"{name}_{uuid.uuid4().hex[:8]}"
                    self._metrics_cache[name] = metric_class(unique_name, description, labels)
                else:
                    raise
        return self._metrics_cache[name]

    def get_dropped_metrics_count(self) -> int:
        """Get count of dropped metrics due to queue overflow."""
        return self._dropped_metrics

    def get_stats(self) -> dict[str, Any]:
        """Get comprehensive performance statistics."""
        elapsed = time.time() - self._start_time
        ops_per_second = self._operation_count / elapsed if elapsed > 0 else 0

        return {
            "mode": "sync" if self._sync_mode else "async",
            "total_operations": self._operation_count,
            "dropped_metrics": self._dropped_metrics,
            "uptime_seconds": elapsed,
            "ops_per_second": ops_per_second,
            "auto_detect_enabled": self.auto_detect_mode,
            "pool_size": len(self._metric_pool),
        }

    def shutdown(self, timeout: float = 5.0):
        """Gracefully shutdown the metrics collector."""
        if not self._sync_mode and self._stopped is not None:
            self._stopped.set()
            if self._worker_thread is not None:
                self._worker_thread.join(timeout)

    def _init_async_mode(self):
        """Initialize async mode components."""
        if self._queue is None:
            self._queue = queue.Queue(maxsize=self.max_queue_size)
            self._stopped = threading.Event()

            # Start worker thread
            self._worker_thread = threading.Thread(target=self._worker_loop, daemon=True, name="AsyncMetricsWorker")
            self._worker_thread.start()

    def _should_check_mode(self) -> bool:
        """Check if we should evaluate mode switching."""
        now = time.time()
        if now - self._last_mode_check > 5.0:  # Check every 5 seconds
            self._last_mode_check = now
            return True
        return False

    def _maybe_switch_mode(self):
        """Switch between sync and async mode based on usage patterns."""
        if self._operation_count < 10:  # Need minimum sample size
            return

        elapsed = time.time() - self._start_time
        if elapsed < 1.0:  # Need at least 1 second of data
            return

        ops_per_second = self._operation_count / elapsed

        # Switch to async mode if high frequency (>100 ops/sec)
        if self._sync_mode and ops_per_second > 100:
            logger.info(f"Switching to async mode due to high frequency: {ops_per_second:.1f} ops/sec")
            self._sync_mode = False
            self._init_async_mode()

        # Switch to sync mode if low frequency (<10 ops/sec) and currently async
        elif not self._sync_mode and ops_per_second < 10:
            logger.info(f"Switching to sync mode due to low frequency: {ops_per_second:.1f} ops/sec")
            self._sync_mode = True
            # Shutdown async components
            if self._stopped is not None:
                self._stopped.set()

    def _get_pooled_metric_data(self) -> dict[str, Any]:
        """Get a metric data dict from the pool to reduce allocations."""
        with self._pool_lock:
            if self._metric_pool:
                metric_data = self._metric_pool.pop()
                metric_data.clear()  # Reset for reuse
                return metric_data
        return {}  # Create new if pool empty

    def _return_to_pool(self, metric_data: dict[str, Any]):
        """Return metric data dict to pool for reuse."""
        with self._pool_lock:
            if len(self._metric_pool) < 100:  # Limit pool size
                self._metric_pool.append(metric_data)

    # Sync mode implementations (direct Prometheus calls)
    def _record_cache_operation_sync(
        self,
        operation: str,
        namespace: str,
        success: bool,
        duration_ms: float,
        serializer: str,
        size_bytes: int,
        hit: Optional[bool],
    ):
        """Record cache operation directly to Prometheus (sync mode)."""
        if not PROMETHEUS_AVAILABLE:
            return

        # Direct metric updates (≤4μs per operation)
        cache_counter = self._get_metric(
            "cache_operations_total", Counter, "Total cache operations", ["operation", "namespace", "success", "serializer"]
        )
        cache_counter.labels(operation=operation, namespace=namespace, success=str(success), serializer=serializer).inc()

        if duration_ms > 0:
            cache_duration = self._get_metric(
                "cache_operation_duration_ms", Histogram, "Cache operation duration", ["operation", "namespace", "serializer"]
            )
            cache_duration.labels(operation=operation, namespace=namespace, serializer=serializer).observe(duration_ms)

        if size_bytes > 0:
            cache_size = self._get_metric(
                "cache_operation_size_bytes", Histogram, "Cache operation size", ["operation", "namespace", "serializer"]
            )
            cache_size.labels(operation=operation, namespace=namespace, serializer=serializer).observe(size_bytes)

    def _record_circuit_breaker_sync(self, namespace: str, state: str, transitions: int):
        """Record circuit breaker state directly to Prometheus (sync mode)."""
        if not PROMETHEUS_AVAILABLE:
            return

        circuit_gauge = self._get_metric("circuit_breaker_state", Gauge, "Circuit breaker state", ["namespace", "state"])
        circuit_gauge.labels(namespace=namespace, state=state).set(transitions)

    def _record_counter_sync(self, metric_name: str, labels: dict[str, Any], value: float):
        """Record counter directly to Prometheus (sync mode)."""
        if not PROMETHEUS_AVAILABLE:
            return

        counter = self._get_metric(metric_name, Counter, f"Counter metric {metric_name}", list(labels.keys()))
        counter.labels(**labels).inc(value)

    def _record_histogram_sync(self, metric_name: str, value: float, labels: dict[str, Any]):
        """Record histogram directly to Prometheus (sync mode)."""
        if not PROMETHEUS_AVAILABLE:
            return

        histogram = self._get_metric(metric_name, Histogram, f"Histogram metric {metric_name}", list(labels.keys()))
        histogram.labels(**labels).observe(value)

    # Async mode implementations (queued processing)
    def _record_cache_operation_async(
        self,
        operation: str,
        namespace: str,
        success: bool,
        duration_ms: float,
        serializer: str,
        size_bytes: int,
        hit: Optional[bool],
    ):
        """Record cache operation to queue for async processing."""
        assert self._queue is not None, "Async metrics not initialized"

        metric_data = self._get_pooled_metric_data()
        metric_data.update(
            {
                "type": "cache_operation",
                "operation": operation,
                "namespace": namespace,
                "success": success,
                "duration_ms": duration_ms,
                "serializer": serializer,
                "size_bytes": size_bytes,
                "hit": hit,
                "timestamp": time.time(),
            }
        )

        try:
            self._queue.put_nowait(metric_data)
        except queue.Full:
            self._dropped_metrics += 1
            self._return_to_pool(metric_data)  # Return to pool if failed

    def _record_circuit_breaker_async(self, namespace: str, state: str, transitions: int):
        """Record circuit breaker state to queue for async processing."""
        assert self._queue is not None, "Async metrics not initialized"

        metric_data = self._get_pooled_metric_data()
        metric_data.update(
            {
                "type": "circuit_breaker",
                "namespace": namespace,
                "state": state,
                "transitions": transitions,
                "timestamp": time.time(),
            }
        )

        try:
            self._queue.put_nowait(metric_data)
        except queue.Full:
            self._dropped_metrics += 1
            self._return_to_pool(metric_data)

    def _record_counter_async(self, metric_name: str, labels: dict[str, Any], value: float):
        """Record counter to queue for async processing."""
        assert self._queue is not None, "Async metrics not initialized"

        metric_data = self._get_pooled_metric_data()
        metric_data.update(
            {
                "type": "counter",
                "name": metric_name,
                "labels": labels,
                "value": value,
                "timestamp": time.time(),
            }
        )

        try:
            self._queue.put_nowait(metric_data)
        except queue.Full:
            self._dropped_metrics += 1
            self._return_to_pool(metric_data)

    def _record_histogram_async(self, metric_name: str, value: float, labels: dict[str, Any]):
        """Record histogram to queue for async processing."""
        assert self._queue is not None, "Async metrics not initialized"

        metric_data = self._get_pooled_metric_data()
        metric_data.update(
            {
                "type": "histogram",
                "name": metric_name,
                "labels": labels,
                "value": value,
                "timestamp": time.time(),
            }
        )

        try:
            self._queue.put_nowait(metric_data)
        except queue.Full:
            self._dropped_metrics += 1
            self._return_to_pool(metric_data)


# Global instance for easy access
_global_collector: Optional[AsyncMetricsCollector] = None


def get_async_metrics_collector(sync_mode: Union[bool, None] = None, auto_detect_mode: bool = True) -> AsyncMetricsCollector:
    """Get or create the global async metrics collector.

    Args:
        sync_mode: Force sync mode (True) or async mode (False). None for auto-detect
        auto_detect_mode: Automatically switch between sync/async based on frequency

    Returns:
        AsyncMetricsCollector: Global collector instance optimized for current usage
    """
    global _global_collector
    if _global_collector is None:
        _global_collector = AsyncMetricsCollector(sync_mode=sync_mode, auto_detect_mode=auto_detect_mode)
    return _global_collector
