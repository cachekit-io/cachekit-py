"""Reliability components for backend cache operations.

Provides async metrics collection, health checks, circuit breakers,
adaptive timeouts, and reliability profiles.
"""

from .adaptive_timeout import AdaptiveTimeout, AdaptiveTimeoutManager
from .async_metrics import AsyncMetricsCollector, get_async_metrics_collector
from .circuit_breaker import (
    CacheOperationMetrics,
    CircuitBreaker,
    CircuitBreakerConfig,
    CircuitState,
)
from .error_classification import BackendErrorClassifier
from .load_control import BackpressureController
from .profiles import (
    ProfileConfig,
    ReliabilityProfile,
    create_optimized_decorator_config,
    get_profile_config,
    recommend_profile,
)

__all__ = [
    "AdaptiveTimeout",
    "AdaptiveTimeoutManager",
    "AsyncMetricsCollector",
    "BackendErrorClassifier",
    "BackpressureController",
    "CacheOperationMetrics",
    "CircuitBreaker",
    "CircuitBreakerConfig",
    "CircuitState",
    "ProfileConfig",
    "ReliabilityProfile",
    "create_optimized_decorator_config",
    "get_async_metrics_collector",
    "get_profile_config",
    "recommend_profile",
]
