"""Reliability components for backend cache operations.

Provides async metrics collection, health checks, circuit breakers,
error classification and backpressure control.
"""

from .async_metrics import AsyncMetricsCollector, get_async_metrics_collector
from .circuit_breaker import (
    CacheOperationMetrics,
    CircuitBreaker,
    CircuitBreakerConfig,
    CircuitState,
)
from .error_classification import BackendErrorClassifier
from .load_control import BackpressureController

__all__ = [
    "AsyncMetricsCollector",
    "BackendErrorClassifier",
    "BackpressureController",
    "CacheOperationMetrics",
    "CircuitBreaker",
    "CircuitBreakerConfig",
    "CircuitState",
    "get_async_metrics_collector",
]
