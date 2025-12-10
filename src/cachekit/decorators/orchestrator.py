import contextvars
import logging
import uuid
from typing import Any, Optional

from ..monitoring.correlation_tracking import CorrelationTracker
from ..monitoring.pool_monitor import OptimizedPoolMonitor

# Import EXISTING modules - no duplication
from ..reliability import (
    AdaptiveTimeout,
    AsyncMetricsCollector,
    BackpressureController,
    CircuitBreaker,
)

logger = logging.getLogger(__name__)

# Thread-local operation context for automatic operation tracking
_operation_context: contextvars.ContextVar[Optional[dict[str, Any]]] = contextvars.ContextVar("operation_context", default=None)


class FeatureOrchestrator:
    """Orchestrates existing reliability and monitoring features.

    Examples:
        Create minimal orchestrator:

        >>> orch = FeatureOrchestrator(
        ...     namespace="test",
        ...     circuit_breaker_enabled=False,
        ...     adaptive_timeout_enabled=False,
        ...     backpressure_enabled=False,
        ... )
        >>> orch.namespace
        'test'

        Check circuit breaker state (disabled returns True):

        >>> orch.should_allow_request()
        True

        Get health status:

        >>> status = orch.get_health_status()
        >>> status["namespace"]
        'test'
        >>> status["overall_healthy"]
        True

        Generate correlation ID:

        >>> import uuid
        >>> corr_id = orch.generate_correlation_id()
        >>> uuid.UUID(corr_id)  # doctest: +ELLIPSIS
        UUID('...')
    """

    def __init__(
        self,
        namespace: str,
        circuit_breaker_enabled: bool = True,
        adaptive_timeout_enabled: bool = True,
        backpressure_enabled: bool = True,
        collect_stats: bool = True,
        enable_structured_logging: bool = True,
        circuit_breaker_config: Optional[dict[str, Any]] = None,
        adaptive_timeout_config: Optional[dict[str, Any]] = None,
        backpressure_config: Optional[dict[str, Any]] = None,
    ):
        self.namespace = namespace
        self._circuit_breaker_enabled = circuit_breaker_enabled
        self._adaptive_timeout_enabled = adaptive_timeout_enabled
        self._backpressure_enabled = backpressure_enabled
        self._collect_stats = collect_stats
        self._enable_structured_logging = enable_structured_logging

        # Initialize EXISTING modules
        self._circuit_breaker = None
        self._adaptive_timeout = None
        self._load_control = None
        self._metrics_collector = None
        self._correlation_tracker = None
        self._pool_monitor = None

        if circuit_breaker_enabled:
            from ..reliability.circuit_breaker import CircuitBreakerConfig

            # Handle both dict and CircuitBreakerConfig objects
            if circuit_breaker_config is None:
                # Create default config
                config = CircuitBreakerConfig()
                self._circuit_breaker = CircuitBreaker(config=config, namespace=namespace)
            elif isinstance(circuit_breaker_config, dict):
                # Convert dict to CircuitBreakerConfig
                config = CircuitBreakerConfig(**circuit_breaker_config)
                self._circuit_breaker = CircuitBreaker(config=config, namespace=namespace)
            else:
                # Already a CircuitBreakerConfig object
                self._circuit_breaker = CircuitBreaker(config=circuit_breaker_config, namespace=namespace)

        if adaptive_timeout_enabled:
            self._adaptive_timeout = AdaptiveTimeout(**(adaptive_timeout_config or {}))

        if backpressure_enabled:
            self._load_control = BackpressureController(**(backpressure_config or {}))

        if collect_stats:
            self._metrics_collector = AsyncMetricsCollector()

        if enable_structured_logging:
            self._correlation_tracker = CorrelationTracker()

        # Pool monitoring - initialized when pool manager is available
        self._pool_monitor = None

    @property
    def circuit_breaker(self) -> Optional[CircuitBreaker]:
        """Get circuit breaker if enabled."""
        return self._circuit_breaker

    @property
    def adaptive_timeout(self) -> Optional[AdaptiveTimeout]:
        """Get adaptive timeout if enabled."""
        return self._adaptive_timeout

    @property
    def load_control(self) -> Optional[BackpressureController]:
        """Get load control if enabled."""
        return self._load_control

    @property
    def metrics_collector(self) -> Optional[AsyncMetricsCollector]:
        """Get metrics collector if enabled."""
        return self._metrics_collector

    @property
    def correlation_tracker(self) -> Optional[CorrelationTracker]:
        """Get correlation tracker if enabled."""
        return self._correlation_tracker

    @property
    def pool_monitor(self) -> Optional[OptimizedPoolMonitor]:
        """Get pool monitor."""
        return self._pool_monitor

    def set_pool_manager(self, pool_manager) -> None:
        """Initialize pool monitor with pool manager."""
        if pool_manager and not self._pool_monitor:
            self._pool_monitor = OptimizedPoolMonitor(pool_manager)

    def should_allow_request(self) -> bool:
        """Check if request should be allowed based on circuit breaker state."""
        # Guard clause: No circuit breaker means allow
        if not self._circuit_breaker:
            return True

        # Use the circuit breaker's call method or check state
        from ..reliability.circuit_breaker import CircuitState

        return self._circuit_breaker.get_state() != CircuitState.OPEN

    def can_accept_request(self) -> bool:
        """Check if system can accept new request based on load control."""
        # Guard clause: No load control means accept
        if not self._load_control:
            return True

        # BackpressureController uses context manager (acquire), not can_accept_request
        # For now, always return True and let acquire handle backpressure
        return True

    def get_timeout(self, operation_type: str = "default") -> float:
        """Get current timeout for operation type."""
        # Guard clause: No adaptive timeout means use default
        if not self._adaptive_timeout:
            return 5.0  # Default timeout

        # AdaptiveTimeout.get_timeout() doesn't take arguments
        return self._adaptive_timeout.get_timeout()

    def start_request(self) -> Optional[str]:
        """Start request tracking."""
        # Note: BackpressureController uses acquire() context manager, not start_request()
        # Load control is handled via acquire() in the wrapper

        if self._correlation_tracker:
            correlation_id = self._correlation_tracker.generate_correlation_id()
            self._correlation_tracker.set_correlation_id(correlation_id)
            return correlation_id

        return None

    def end_request(self, correlation_id: Optional[str] = None) -> None:
        """End request tracking."""
        # Note: BackpressureController uses acquire() context manager, not end_request()
        # Load control cleanup is automatic via context manager

        if self._correlation_tracker:
            self._correlation_tracker.clear_correlation_id()

    def log_structured(self, level: str, message: str, **kwargs) -> None:
        """Log with structured format if enabled."""
        # Guard clause: No structured logging means standard log
        if not self._enable_structured_logging:
            getattr(logger, level.lower())(message)
            return

        # Add structured fields
        structured_data = {"namespace": self.namespace, "message": message, **kwargs}

        getattr(logger, level.lower())(f"[{self.namespace}] {message}", extra={"structured": structured_data})

    def log_warning(self, message: str, **kwargs) -> None:
        """Convenience method for logging warnings."""
        self.log_structured("warning", message, **kwargs)

    def get_health_status(self) -> dict[str, Any]:
        """Get comprehensive health status from all modules."""
        components: dict[str, Any] = {}
        status: dict[str, Any] = {
            "namespace": self.namespace,
            "overall_healthy": True,
            "healthy": True,
            "components": components,
            "circuit_breaker": None,  # Will be populated if enabled
        }

        # Circuit breaker health
        if self._circuit_breaker:
            # Use get_stats instead of get_status
            cb_status = self._circuit_breaker.get_stats()
            components["circuit_breaker"] = cb_status
            status["circuit_breaker"] = cb_status  # Also at top level for compatibility
            # Get state separately if needed
            from ..reliability.circuit_breaker import CircuitState

            state = self._circuit_breaker.get_state()
            cb_status["state"] = state.name.lower()
            if state == CircuitState.OPEN:
                status["overall_healthy"] = False
                status["healthy"] = False

        # Load control health
        if self._load_control:
            lc_status = self._load_control.get_stats()
            components["load_control"] = lc_status

        # Pool monitor health
        if self._pool_monitor:
            pool_status = self._pool_monitor.get_pool_stats()
            components["pool_monitor"] = pool_status

        # Metrics collector health
        if self._metrics_collector:
            metrics_status = self._metrics_collector.get_stats()
            components["metrics"] = metrics_status

        return status

    def generate_correlation_id(self) -> str:
        """Generate a unique correlation ID for request tracking."""
        return str(uuid.uuid4())

    def create_correlation_id(self) -> str:
        """Alias for generate_correlation_id."""
        return self.generate_correlation_id()

    def set_correlation_id(self, correlation_id: str) -> None:
        """Set correlation ID for structured logging."""
        # Implementation depends on correlation tracker
        pass

    def clear_correlation_id(self) -> None:
        """Clear correlation ID."""
        # Implementation depends on correlation tracker
        pass

    def create_span(self, name: str, attributes: Optional[dict[str, Any]] = None):
        """Create a tracing span (no-op if tracing not available)."""

        # Simple no-op context manager for now
        class NoOpSpan:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

        return NoOpSpan()

    def set_span_attributes(self, span: Any, attributes: dict[str, Any]):
        """Set attributes on a span (no-op)."""
        pass

    def log_cache_operation(self, **kwargs):
        """Log cache operation with structured logging."""
        if self._enable_structured_logging and kwargs:
            operation = kwargs.get("operation", "unknown")
            key = kwargs.get("key", "unknown")
            self.log_structured("info", f"Cache operation: {operation}", cache_key=key, **kwargs)

    def record_exception(self, span, exception: Exception):
        """Record exception in span and metrics."""
        if self._metrics_collector:
            self._metrics_collector.record_cache_operation(
                operation="exception",
                namespace=self.namespace,
                success=False,
                duration_ms=0.0,
            )

    def set_operation_context(self, operation: str, duration_ms: float = 0.0):
        """Set operation context for automatic tracking in record_success/failure.

        Args:
            operation: Operation type (e.g., "get", "set", "delete")
            duration_ms: Operation duration in milliseconds (optional)

        This method sets thread-local context that will be automatically used by
        subsequent record_success() or record_failure() calls. Works across async
        boundaries thanks to contextvars.

        Example:
            features.set_operation_context("get", duration_ms=1.5)
            features.record_success()  # Automatically uses "get" and 1.5ms
        """
        _operation_context.set(
            {
                "operation": operation,
                "duration_ms": duration_ms,
            }
        )

    def record_failure(self, error: Exception):
        """Record operation failure with automatic context detection.

        Automatically uses operation type and duration from set_operation_context()
        if available, otherwise falls back to defaults.

        Args:
            error: The exception that caused the failure
        """
        # Get operation context (async-safe)
        ctx = _operation_context.get() or {}
        operation = ctx.get("operation", "cache_operation")
        duration_ms = ctx.get("duration_ms", 0.0)

        if self._circuit_breaker:
            self._circuit_breaker._on_failure(error)
        if self._metrics_collector:
            self._metrics_collector.record_cache_operation(
                operation=operation,
                namespace=self.namespace,
                success=False,
                duration_ms=duration_ms,
            )

    def record_success(self):
        """Record operation success with automatic context detection.

        Automatically uses operation type and duration from set_operation_context()
        if available, otherwise falls back to defaults.
        """
        # Get operation context (async-safe)
        ctx = _operation_context.get() or {}
        operation = ctx.get("operation", "cache_operation")
        duration_ms = ctx.get("duration_ms", 0.0)

        if self._circuit_breaker:
            self._circuit_breaker._on_success()
        if self._metrics_collector:
            self._metrics_collector.record_cache_operation(
                operation=operation,
                namespace=self.namespace,
                success=True,
                duration_ms=duration_ms,
            )

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
        """Record cache operation metrics."""
        if self._metrics_collector:
            self._metrics_collector.record_cache_operation(
                operation=operation,
                namespace=namespace,
                success=success,
                duration_ms=duration_ms,
                serializer=serializer,
                size_bytes=size_bytes,
                hit=hit,
            )

    def record_duration(self, duration: float):
        """Record operation duration."""
        # AdaptiveTimeout doesn't have record_success method, just skip for now
        pass

    def get_adaptive_lock_timeouts(self):
        """Get adaptive lock timeouts if available."""
        return None  # Return None to use defaults

    def record_lock_operation(self, duration: float, success: bool):
        """Record lock operation performance."""
        if self._metrics_collector:
            self._metrics_collector.record_cache_operation(
                operation="lock",
                namespace=self.namespace,
                serializer="rust",
                success=success,
                duration_ms=duration * 1000,
            )

    def check_health(self) -> dict[str, Any]:
        """Check health status - returns simplified status format."""
        full_status = self.get_health_status()
        return {
            "status": "healthy" if full_status["healthy"] else "unhealthy",
            "namespace": full_status["namespace"],
            "components": full_status["components"],
        }

    @property
    def collect_stats(self) -> bool:
        """Whether statistics collection is enabled."""
        return self._collect_stats

    @property
    def backpressure(self):
        """Get backpressure controller."""
        return self._load_control

    def handle_cache_error(
        self,
        error: Exception,
        operation: str,
        cache_key: str = "unknown",
        namespace: Optional[str] = None,
        span: Optional[Any] = None,
        duration_ms: float = 0.0,
        correlation_id: Optional[str] = None,
        **extra_context: Any,
    ) -> None:
        """Centralized error handler for all cache operations.

        Provides consistent error recording, logging, and monitoring across
        all error paths in sync and async wrappers. Eliminates duplication.

        Args:
            error: The exception that occurred
            operation: Operation type (e.g., "key_generation", "cache_get", "cache_set")
            cache_key: Cache key involved (use "unknown" if unavailable)
            namespace: Cache namespace (defaults to orchestrator namespace)
            span: Optional tracing span for recording
            duration_ms: Operation duration in milliseconds
            correlation_id: Optional correlation ID for distributed tracing
            **extra_context: Additional context to include in logs

        Example:
            features.handle_cache_error(
                error=e,
                operation="key_generation",
                cache_key=cache_key,
                span=span,
                duration_ms=0.0
            )
        """
        # Use orchestrator namespace if not provided
        namespace = namespace or self.namespace

        # 1. Record exception in span and metrics
        if span:
            self.record_exception(span, error)

        # 2. Set operation context for metrics
        self.set_operation_context(operation, duration_ms)

        # 3. Record failure in circuit breaker and metrics collector
        self.record_failure(error)

        # 4. Structured logging with full context
        self.log_cache_operation(
            operation=f"{operation}_failed",
            key=cache_key,
            namespace=namespace,
            error=str(error),
            error_type=type(error).__name__,
            duration_ms=duration_ms,
            correlation_id=correlation_id,
            **extra_context,
        )

        # 5. Also log via standard logger for backwards compatibility
        from ..cache_handler import get_logger_provider

        logger_instance = get_logger_provider().get_logger(__name__)
        logger_instance.warning(
            f"Cache operation '{operation}' failed for key '{cache_key}': {error!s} ({type(error).__name__})"
        )
