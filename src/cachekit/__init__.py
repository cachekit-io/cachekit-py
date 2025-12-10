r"""cachekit - Caching decorator for Python applications.

A robust, production-ready Python library that provides intelligent Redis caching
capabilities with advanced features like chunked data handling, multi-serialization
support, distributed locking, and automatic corruption detection.

Key Features:
- **Intelligent @cache decorator** with auto-detection and intent-based optimization
- **Circuit breaker protection** against cascading failures
- **Adaptive timeout adjustment** based on historical Redis latency patterns
- **Backpressure control** to prevent Redis overload
- **Connection pooling** for optimized performance
- **Health check methods** for comprehensive monitoring
- **Structured logging** with correlation IDs and distributed tracing
- **Statistics collection** for Prometheus metrics integration

Architecture Overview:
cachekit v0.1.0 provides a modular decorator architecture with intelligent
auto-detection and intent-based optimization. The v0.1 architecture includes:

- FeatureOrchestrator: Manages enterprise-grade reliability and monitoring features
- Flexible configuration interface with intelligent auto-detection
- Enhanced error handling with comprehensive safety checks
- Streamlined connection management with optimized components

Born from production debugging of Redis caching failures:
- UTF-8 corruption prevention with intelligent binary data handling
- Binary data magic byte detection and validation
- Chunked storage for large objects with atomic operations
- Comprehensive checksum validation and recovery

Example Usage:
    ```python
    from cachekit import cache

    # Intelligent cache with zero configuration (90% of use cases)
    @cache
    def expensive_function():
        return compute_result()

    # Intent-based optimization (9% of use cases)
    @cache.minimal      # Speed-critical functions
    def get_price(symbol: str):
        return fetch_price(symbol)

    @cache.production      # Reliability-critical functions
    def process_payment(amount: Decimal):
        return payment_gateway.charge(amount)

    @cache.secure    # Security-critical functions
    def get_user_data(user_id: int) -> UserProfile:
        return db.fetch_user(user_id)

    # Manual configuration when needed (1% of use cases)
    @cache(ttl=3600, namespace="custom", circuit_breaker=True)
    def custom_function():
        return special_computation()

    # Health monitoring
    health = custom_function.get_health_status()
    full_health = custom_function.check_health()
    ```
"""

__version__ = "0.1.0"

from typing import Any, Callable, TypeVar

# Configure hiredis compatibility BEFORE any Redis imports
# This prevents GIL warnings in Python 3.13+ free-threading mode
try:
    from . import hiredis_compat
except ImportError:
    pass

# Import the configuration classes
# Redis client is automatically fast - no special import needed
from .config import DecoratorConfig

# Import the intelligent cache decorator and CacheInfo
from .decorators import cache
from .decorators.wrapper import CacheInfo

# Import health check functionality
from .health import (
    HealthCheckResult,
    HealthLevel,
    HealthStatus,
    async_health_check_handler,
    get_health_checker,
    health_check_handler,
)

# L1/L2 architecture integrated into standard cache interface
# No separate imports needed - cache.minimal/.production/.secure handle L1+L2 transparently
# Import reliability configuration
from .reliability import CircuitBreakerConfig

F = TypeVar("F", bound=Callable[..., Any])


__all__ = [
    "__version__",
    "async_health_check_handler",
    "cache",
    "CacheInfo",
    "CircuitBreakerConfig",
    "DecoratorConfig",
    "get_health_checker",
    "health_check_handler",
    "HealthCheckResult",
    "HealthLevel",
    "HealthStatus",
]
