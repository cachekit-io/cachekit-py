"""Async metrics collection for high-performance reliability features.

This module provides asynchronous, batched metrics collection to eliminate
synchronous Prometheus updates from the hot path.
"""

import logging
import numbers
import os
import queue
import threading
import time
from collections import defaultdict
from typing import Any, Optional, Union

from cachekit.hash_utils import redact_error_for_log

logger = logging.getLogger(__name__)

# Suffixes prometheus_client appends to a metric's base name to form its series names.
_SERIES_SUFFIXES = ("", "_total", "_created", "_bucket", "_count", "_sum")

# Every series name the collector's own metrics register. A caller-supplied metric whose series would include
# one of these could claim it: the built-in metric would then register under a renamed series, or break every
# later cache-operation update.
_BUILTIN_SERIES = frozenset(
    {"cache_operations", "cache_operations_total", "cache_operations_created", "circuit_breaker_state"}
    | {f"{h}{s}" for h in ("cache_operation_duration_ms", "cache_operation_size_bytes") for s in _SERIES_SUFFIXES}
)

try:
    from prometheus_client import REGISTRY, Counter, Gauge, Histogram  # type: ignore[assignment]

    PROMETHEUS_AVAILABLE = True
except ImportError:
    PROMETHEUS_AVAILABLE = False  # type: ignore[misc]
    REGISTRY = None  # type: ignore[assignment]

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


class _NoopMetric:
    """Stand-in for a metric whose name another library already registered."""

    def labels(self, **kwargs):
        return self

    def _ignore(self, amount=1):
        pass

    inc = observe = set = _ignore


# Metric objects are process-wide because prometheus_client's default registry is.
# Every collector (one per decorated function) must record into the same documented
# series; registering per instance collides on the second collector.
_metrics_cache: dict[str, Any] = {}
# One lock per process, keyed by pid: a C-level fork skips the at-fork hooks below, so a
# child must never take a lock it inherited, possibly held by a thread that is gone.
_metrics_locks: dict[int, threading.Lock] = {}


def _metrics_cache_lock() -> threading.Lock:
    """Return this process's metric-cache lock, creating it on first use."""
    pid = os.getpid()
    lock = _metrics_locks.get(pid)
    if lock is None:
        # setdefault is atomic, so threads racing here in a new child all get one lock.
        lock = _metrics_locks.setdefault(pid, threading.Lock())
    return lock


def _acquire_metrics_lock() -> None:
    _metrics_cache_lock().acquire()


def _release_metrics_lock() -> None:
    _metrics_cache_lock().release()


# The process that imported this module, the last process whose metric locks _reset_metric_locks replaced, and the
# last child the at-fork hook ran in.
_import_pid = os.getpid()
_metric_locks_pid = _import_pid
_hooked_fork_pid: Optional[int] = None
_LOCK_TYPE = type(threading.Lock())


def _replace_lock(obj: Any) -> None:
    if isinstance(getattr(obj, "_lock", None), _LOCK_TYPE):
        obj._lock = threading.Lock()


def _reset_metric_locks() -> None:
    """Give the default registry, every cached metric and each of its series fresh locks. Run only in a child.

    prometheus_client guards each with a plain ``Lock`` and resets none of them after a fork. A parent thread (the
    batching worker, for one) may hold one at the fork, and a child recording into that series would wait on it
    forever. These are prometheus_client's private attributes; the fork tests in
    ``tests/unit/test_async_metrics_mode_switch.py`` fail if a release renames them. In multiprocess mode
    (``PROMETHEUS_MULTIPROC_DIR``) values share one lock that this cannot reach.
    """
    global _metric_locks_pid
    _replace_lock(REGISTRY)
    for metric in list(_metrics_cache.values()):
        _replace_lock(metric)  # a labelled metric's lock over its series
        for series in [metric, *getattr(metric, "_metrics", {}).values()]:
            for value in (getattr(series, "_value", None), getattr(series, "_sum", None), *getattr(series, "_buckets", ())):
                _replace_lock(value)
    _metric_locks_pid = os.getpid()


def _reset_metric_locks_once() -> None:
    """Run ``_reset_metric_locks`` in a child the at-fork hook below did not reach: a fork made from C.

    A collector built in such a child calls this, and so does an inherited collector's take-over, on its first
    batched record or mode check. That leaves one case open: an inherited synchronous collector's records make no
    fork check, so until one of those runs they use the inherited locks, and for good with auto-detect off.
    """
    if _metric_locks_pid == os.getpid():
        return
    with _metrics_cache_lock():  # per PID, so no parent thread can have held it
        if _metric_locks_pid != os.getpid():
            _reset_metric_locks()


def _in_hookless_child() -> bool:
    """Return whether this process is the child of a fork made from C, which ran no at-fork hook.

    Such a fork, as uWSGI's is without ``--py-call-osafterfork``, also skips CPython's own after-fork repair, so a
    thread started in that child can hang or crash the interpreter. No collector starts one there.
    """
    pid = os.getpid()
    return pid != _import_pid and pid != _hooked_fork_pid


def _after_fork_in_child() -> None:
    """Repair the child of a fork made from Python, which runs this while it is single-threaded."""
    global _hooked_fork_pid
    _metrics_locks.clear()  # only frees the parent's entry: keyed by its PID, it is never looked up here
    _hooked_fork_pid = os.getpid()
    _reset_metric_locks()


if hasattr(os, "register_at_fork"):
    # Hold the lock across fork (as the logging module does) so no thread is between
    # registering a metric and caching it.
    os.register_at_fork(
        before=_acquire_metrics_lock,
        after_in_parent=_release_metrics_lock,
        after_in_child=_after_fork_in_child,
    )


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
            sync_mode: Force sync mode (True) or async mode (False). None for auto-detect. Whatever the mode, a
                collector records synchronously after ``shutdown()``. So does one inherited by a forked child,
                until a mode check starts a worker of the child's own, which never happens with auto-detect off.
                In the child of a fork made from C, every collector, inherited or built there, records
                synchronously for good.
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

        # Async mode components (lazy initialization)
        self._queue = None
        self._stopped = None
        self._worker_thread = None
        # The process that owns the queue, stop event, worker and pool lock; see _take_over_if_forked.
        self._owner_pid = os.getpid()
        self._dropped_metrics = 0
        # Keyed by pid, like _metrics_locks: a child forked while a thread was mid-switch must not wait
        # on the copy of the lock that thread still holds, because the thread does not exist in the child.
        self._mode_locks: dict[int, threading.Lock] = {}
        # Set by shutdown(), and in the child of a fork made from C: no mode switch may start a worker again.
        self._batching_disabled = False

        # Memory pool for reducing allocations
        self._metric_pool = []
        self._pool_lock = threading.Lock()

        if _in_hookless_child():
            # Built in the child of a fork made from C, so no take-over will ever run for it: apply its outcome now.
            self._batching_disabled = True
            self._sync_mode = True
            _reset_metric_locks_once()

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
    ):
        """Record cache operation metric.

        Automatically uses sync or async mode based on configuration.
        """
        self._operation_count += 1

        # Auto-detect mode switch if enabled
        if self.auto_detect_mode and self._should_check_mode():
            self._maybe_switch_mode()

        if self._sync_mode or not self._may_enqueue():
            self._record_cache_operation_sync(operation, namespace, success, duration_ms, serializer, size_bytes)
        else:
            self._record_cache_operation_async(operation, namespace, success, duration_ms, serializer, size_bytes)

    def record_circuit_breaker_state(self, namespace: str, state: str, transitions: int = 0):
        """Record circuit breaker state change."""
        self._operation_count += 1

        if self.auto_detect_mode and self._should_check_mode():
            self._maybe_switch_mode()

        if self._sync_mode or not self._may_enqueue():
            self._record_circuit_breaker_sync(namespace, state, transitions)
        else:
            self._record_circuit_breaker_async(namespace, state, transitions)

    def record_counter(self, metric_name: str, labels: Optional[dict[str, Any]] = None, value: float = 1.0):
        """Record a counter metric.

        Args:
            metric_name: Name of the counter metric
            labels: Dictionary of labels for the metric
            value: Value to increment by (default: 1.0)

        In sync mode the errors below reach the caller. In batched mode the same checks run in the worker,
        which logs the rejected record and skips it, so the call itself never raises.

        Raises:
            TypeError: If ``metric_name`` or a label name is not a str, or ``value`` is not a real number.
            ValueError: If ``metric_name`` would register a series the collector records itself (such as
                ``cache_operations_total``), if it is already registered as another metric kind, or if the
                label names differ from those the metric was first recorded with.
            OverflowError: If ``value`` is too large for a float.
        """
        self._operation_count += 1

        if self.auto_detect_mode and self._should_check_mode():
            self._maybe_switch_mode()

        if self._sync_mode or not self._may_enqueue():
            self._record_counter_sync(metric_name, labels or {}, value)
        else:
            self._record_counter_async(metric_name, labels or {}, value)

    def record_histogram(self, metric_name: str, value: float, labels: Optional[dict[str, Any]] = None):
        """Record a histogram metric.

        Args:
            metric_name: Name of the histogram metric
            value: Value to observe
            labels: Dictionary of labels for the metric

        In sync mode the errors below reach the caller. In batched mode the same checks run in the worker,
        which logs the rejected record and skips it, so the call itself never raises.

        Raises:
            TypeError: If ``metric_name`` or a label name is not a str, or ``value`` is not a real number.
            ValueError: If ``metric_name`` would register a series the collector records itself (such as
                ``cache_operations_total``), if it is already registered as another metric kind, or if the
                label names differ from those the metric was first recorded with.
            OverflowError: If ``value`` is too large for a float.
        """
        self._operation_count += 1

        if self.auto_detect_mode and self._should_check_mode():
            self._maybe_switch_mode()

        if self._sync_mode or not self._may_enqueue():
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
                logger.error(f"Error in metrics worker: {redact_error_for_log(e)}")

            # Force flush if too much time has passed
            if batch and (time.time() - last_flush) > self.flush_interval:
                self._flush_batch(batch)
                batch = []
                last_flush = time.time()

        # Stopping ends the loop with records still queued; drain them so shutdown() loses nothing.
        # Bounded by a snapshot so producers still recording cannot keep the worker alive, and
        # flushed in batch_size chunks so the backlog keeps normal batch granularity. This worker is
        # the queue's only consumer, so the snapshot never exceeds the records available.
        for _ in range(self._queue.qsize()):
            batch.append(self._queue.get_nowait())
            if len(batch) >= self.batch_size:
                self._flush_batch(batch)
                batch = []

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
                    value = self._check_generic_metric(metric["name"], metric["labels"], metric["value"])
                    name = metric["name"]
                    labels_key = tuple(sorted(metric["labels"].items()))
                    counters[name][labels_key] += value

                elif metric["type"] == "histogram":
                    value = self._check_generic_metric(metric["name"], metric["labels"], metric["value"])
                    name = metric["name"]
                    labels_key = tuple(sorted(metric["labels"].items()))
                    histograms[name].append((value, labels_key))

            except Exception as e:
                logger.error(f"Error processing metric: {redact_error_for_log(e)}")
            finally:
                # Return metric data to pool for reuse
                self._return_to_pool(metric)

        # Batch update Prometheus metrics. Bad caller input is rejected per record above, and the per-metric
        # handlers skip what prometheus_client rejects with ValueError. This is the thread boundary for anything
        # unforeseen: most worker call sites (the shutdown drain among them) have no handler above them, so an
        # escaping exception would end the worker and strand every record still queued.
        try:
            self._update_prometheus_metrics(cache_ops, circuit_states, counters, histograms)  # type: ignore[arg-type]
        except Exception as e:
            logger.error(f"Failed to update metrics batch: {redact_error_for_log(e)}")

    @staticmethod
    def _series(metric: Any, labels: dict[str, Any]) -> Any:
        """Return the series of ``metric`` that ``labels`` names.

        prometheus_client rejects ``.labels()`` on a metric built with no label names, so a label-less
        record updates the metric itself.
        """
        return metric.labels(**labels) if labels else metric

    @staticmethod
    def _check_generic_metric(name: Any, labels: dict[Any, Any], value: Any) -> float:
        """Validate a caller-supplied counter or histogram and return its value as a float.

        Rejects input that would make the batch update fail with an error other than ValueError, which is
        all the update step isolates per metric. In async mode a rejected record is logged and skipped on
        its own; in sync mode the error reaches the caller.

        Raises:
            TypeError: If the name or a label name is not a str, or the value is not a number.
            ValueError: If the name is reserved for a metric the collector records itself.
            OverflowError: If the value is too large for a float.
        """
        if not isinstance(name, str) or not all(isinstance(k, str) for k in labels):
            raise TypeError("metric name and label names must be str")
        # A counter's base drops "_total"; checking both forms against every suffix covers both metric kinds.
        bases = (name, name.removesuffix("_total"))
        if any(f"{b}{s}" in _BUILTIN_SERIES for b in bases for s in _SERIES_SUFFIXES):
            raise ValueError(f"metric name {name} is reserved")
        if not isinstance(value, numbers.Real):
            raise TypeError("metric value must be a number")
        return float(value)

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

                # Prometheus rejects caller-supplied names and labels with ValueError (reserved label
                # names, label names that differ from the first-seen schema). Skip the bad series and
                # log once per metric, so one bad record neither discards the batch nor floods the log.
                try:
                    counter_metric = self._get_metric(name, Counter, f"Counter metric {name}", label_names)
                except ValueError as e:
                    logger.error(f"Failed to create counter {name}: {redact_error_for_log(e)}")
                    continue
                failures, last_error = 0, None
                for labels_key, value in label_values.items():
                    labels_dict = dict(labels_key)  # type: ignore[arg-type]
                    try:
                        self._series(counter_metric, labels_dict).inc(value)  # type: ignore[arg-type]
                    except ValueError as e:
                        failures, last_error = failures + 1, e
                if last_error is not None:
                    logger.error(f"Failed to update counter {name} ({failures} series): {redact_error_for_log(last_error)}")

        # Update generic histograms
        for name, observations in histograms.items():
            # Extract label names from first entry
            if observations:
                first_value, first_labels_key = observations[0]
                label_names = [k for k, v in first_labels_key] if first_labels_key else []

                try:
                    histogram_metric = self._get_metric(name, Histogram, f"Histogram metric {name}", label_names)
                except ValueError as e:
                    logger.error(f"Failed to create histogram {name}: {redact_error_for_log(e)}")
                    continue
                failures, last_error = 0, None
                for value, labels_key in observations:
                    labels_dict = dict(labels_key)
                    try:
                        self._series(histogram_metric, labels_dict).observe(value)
                    except ValueError as e:
                        failures, last_error = failures + 1, e
                if last_error is not None:
                    logger.error(
                        f"Failed to update histogram {name} ({failures} observations): {redact_error_for_log(last_error)}"
                    )

    def _get_metric(self, name: str, metric_class: type, description: str, labels: list[str]) -> Any:
        """Get or create the process-wide metric instance for ``name``.

        Raises:
            ValueError: If ``name`` is already cached as a different metric kind, as prometheus_client
                does for a name registered twice. Otherwise the caller would call a method the cached
                metric lacks.
        """
        metric = _metrics_cache.get(name)
        if metric is None:
            with _metrics_cache_lock():
                metric = _metrics_cache.get(name)
                if metric is None:
                    try:
                        metric = metric_class(name, description, labels)
                    except ValueError as e:
                        if "Duplicated timeseries" not in str(e):
                            raise
                        # The host application owns this name. Telemetry must not break cache
                        # calls, and a renamed series would be invisible to the documented
                        # queries, so drop this metric loudly instead.
                        logger.warning(f"Metric {name!r} is already registered outside cachekit; not recording it")
                        metric = _NoopMetric()
                    _metrics_cache[name] = metric
        if not isinstance(metric, (metric_class, _NoopMetric)):
            raise ValueError(f"metric {name} is already a {type(metric).__name__}, not a {metric_class.__name__}")
        return metric

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
        """Stop the batching worker, waiting up to ``timeout`` seconds for it to flush every record queued so far.

        The collector then records synchronously: a later record reaches its metric on the caller's thread, and no
        mode check starts a worker again. A record that another thread was already queueing as this ran can land
        after the worker's final drain, and is never flushed; that loses at most one record per such thread.
        """
        # A forked child must not set its parent's stop event or join its parent's worker.
        self._take_over_if_forked()
        # Under the mode lock, so a switch back to batched mode cannot restart the worker after this stops it.
        with self._mode_lock():
            self._batching_disabled = True
            # Before stopping the worker, so a record made from here on cannot queue behind it.
            self._sync_mode = True
            if self._stopped is not None:
                self._stopped.set()
            worker = self._worker_thread
        # Join outside the lock: a producer's mode check must not wait on the worker's drain.
        if worker is not None:
            worker.join(timeout)

    def _init_async_mode(self) -> bool:
        """Start the batching worker, creating the queue on first use.

        Returns False, starting nothing, while the previous worker is still alive: a stopped worker keeps
        reading the queue until its exit drain finishes, and that drain assumes it is the only consumer.
        Within a process the queue is reused, never replaced, so producers always have one to put on. Only
        ``_take_over_if_forked`` replaces it, which a mode switch runs first.
        """
        if self._queue is None or self._stopped is None:
            self._queue = queue.Queue(maxsize=self.max_queue_size)
            self._stopped = threading.Event()
        elif self._worker_thread is not None and self._worker_thread.is_alive():
            return False

        self._stopped.clear()
        self._worker_thread = threading.Thread(target=self._worker_loop, daemon=True, name="AsyncMetricsWorker")
        self._worker_thread.start()
        return True

    def _mode_lock(self) -> threading.Lock:
        """Return this process's mode-switch lock, creating it on first use."""
        pid = os.getpid()
        lock = self._mode_locks.get(pid)
        if lock is None:
            # setdefault is atomic, so threads racing here in a new child all get one lock.
            lock = self._mode_locks.setdefault(pid, threading.Lock())
        return lock

    def _take_over_if_forked(self) -> None:
        """In a forked child, replace the batching state inherited from the parent and fall back to sync mode.

        Threads do not survive ``fork()``, so the parent's worker is gone from the child. A parent thread may
        also have held the queue's mutex, the stop event's lock or the pool lock at the fork, and the child
        would then wait on it forever. The child gets a fresh queue, stop event and pool lock, and no worker,
        and records synchronously until a mode check starts a worker of its own. With auto-detect off it stays
        synchronous. The metrics it now records into directly get fresh locks too (``_reset_metric_locks``).

        A changed PID is the signal. A fork made from C, as uWSGI's is without ``--py-call-osafterfork``, runs
        no at-fork hook, and the dead worker's ``Thread.is_alive()`` still returns True. It also skips CPython's
        own after-fork repair, so a thread started in that child can hang or crash the interpreter. A child the
        at-fork hook did not reach therefore never starts a worker, and records synchronously for good; ``__init__``
        does the same for a collector built there.
        """
        pid = os.getpid()
        if self._owner_pid == pid:
            return
        with self._mode_lock():  # per PID, so no parent thread can have held it
            if self._owner_pid == pid:
                return
            self._sync_mode = True
            self._queue = queue.Queue(maxsize=self.max_queue_size)
            self._stopped = threading.Event()
            self._worker_thread = None
            self._pool_lock = threading.Lock()
            if _in_hookless_child():
                self._batching_disabled = True
            _reset_metric_locks_once()
            # Last, so a thread that sees this process as the owner also sees its fresh state.
            self._owner_pid = pid

    def _may_enqueue(self) -> bool:
        """Take over the batching state if this is a forked child, then return whether a batched record may queue.

        A producer that read batched mode calls this; on False it records synchronously instead. Only the batched
        record path calls this, so the sync path pays no per-record ``getpid()``. The PID test is
        inlined because this runs on every batched record.
        """
        if self._owner_pid != os.getpid():
            self._take_over_if_forked()
        # Re-read: a take-over, by this thread or a racing one, leaves sync mode set and no worker running. Only a
        # mode switch clears it again, and that starts this process's worker first.
        return not self._sync_mode

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

        # Both switches touch the batching state: one reuses the queue, the other sets the stop event.
        self._take_over_if_forked()
        # Two producers can pass the mode check at once; the lock keeps them from starting two workers.
        with self._mode_lock():
            # Switch to async mode if high frequency (>100 ops/sec)
            if self._sync_mode and ops_per_second > 100 and not self._batching_disabled:
                # Never join the old worker here: this runs on the caller's thread. Stay synchronous and
                # retry at the next mode check instead.
                if not self._init_async_mode():
                    logger.debug("Previous metrics worker still draining; staying in sync mode")
                    return
                logger.info(f"Switching to async mode due to high frequency: {ops_per_second:.1f} ops/sec")
                self._sync_mode = False

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

        value = self._check_generic_metric(metric_name, labels, value)
        counter = self._get_metric(metric_name, Counter, f"Counter metric {metric_name}", list(labels.keys()))
        self._series(counter, labels).inc(value)

    def _record_histogram_sync(self, metric_name: str, value: float, labels: dict[str, Any]):
        """Record histogram directly to Prometheus (sync mode)."""
        if not PROMETHEUS_AVAILABLE:
            return

        value = self._check_generic_metric(metric_name, labels, value)
        histogram = self._get_metric(metric_name, Histogram, f"Histogram metric {metric_name}", list(labels.keys()))
        self._series(histogram, labels).observe(value)

    # Async mode implementations (queued processing)
    def _record_cache_operation_async(
        self,
        operation: str,
        namespace: str,
        success: bool,
        duration_ms: float,
        serializer: str,
        size_bytes: int,
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
