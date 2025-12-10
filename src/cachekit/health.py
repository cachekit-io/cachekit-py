"""Health check functionality for Redis cache system.

This module provides health check endpoints and utilities for monitoring
the overall health of the Redis cache system, including Redis connectivity,
connection pool status, and circuit breaker state.

Architecture: Uses explicit dependency injection - optional components (pool manager,
circuit breaker) are passed to the constructor. No global registry.
"""

import asyncio
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional

from cachekit.backends import RedisBackend
from cachekit.backends.provider import CacheClientProvider
from cachekit.di import DIContainer
from cachekit.imports import OptionalImport
from cachekit.logging import get_structured_logger
from cachekit.monitoring import CircuitBreakerProtocol, PoolManagerProtocol

# Optional imports for enhanced functionality
PROMETHEUS = OptionalImport("prometheus_client", "install with: pip install prometheus-client")

# Global DI container instance
container = DIContainer()

logger = get_structured_logger(__name__)

# Health monitoring thresholds
HIGH_MEMORY_USAGE_THRESHOLD = 0.8
LOW_HIT_RATE_THRESHOLD = 0.5


class HealthStatus(Enum):
    """Health check status levels."""

    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"


class HealthLevel(Enum):
    """Health check depth levels."""

    PING = "ping"  # Just Redis ping (fastest)
    BASIC = "basic"  # Ping + connection count
    FULL = "full"  # All components + detailed stats


@dataclass
class ComponentHealth:
    """Health status of a single component."""

    name: str
    status: HealthStatus
    message: str = ""
    details: dict[str, Any] = field(default_factory=dict)
    last_check: Optional[datetime] = None

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "name": self.name,
            "status": self.status.value,
            "message": self.message,
            "details": self.details,
            "last_check": (self.last_check.isoformat() if self.last_check else None),
        }


@dataclass
class HealthCheckResult:
    """Overall health check result."""

    status: HealthStatus
    components: list[ComponentHealth]
    duration_ms: float
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def is_healthy(self) -> bool:
        """Check if system is healthy or degraded (not unhealthy)."""
        return self.status != HealthStatus.UNHEALTHY

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "status": self.status.value,
            "timestamp": self.timestamp.isoformat(),
            "duration_ms": self.duration_ms,
            "components": [c.to_dict() for c in self.components],
            "healthy": self.is_healthy,
        }


class HealthChecker:
    """Performs health checks on Redis cache system components.

    Supports multiple checking levels:
    - PING: Fast ping-only check (~1ms)
    - BASIC: Ping + connection info (~5ms)
    - FULL: All components + detailed stats (~50ms)

    Dependencies are explicitly injected:
    - pool_manager: Optional pool for checking connection stats
    - circuit_breaker: Optional circuit breaker for state checks
    """

    def __init__(
        self,
        timeout_seconds: float = 5.0,
        pool_manager: Optional[PoolManagerProtocol] = None,
        circuit_breaker: Optional[CircuitBreakerProtocol] = None,
    ):
        """Initialize health checker.

        Args:
            timeout_seconds: Timeout for health check operations
            pool_manager: Optional pool manager for health checks
            circuit_breaker: Optional circuit breaker for health checks
        """
        self.timeout = timeout_seconds
        self._pool_manager = pool_manager
        self._circuit_breaker = circuit_breaker
        self._last_result: Optional[HealthCheckResult] = None
        self._last_check_time: Optional[float] = None
        self._cache_duration_seconds = 10.0

    def quick_ping(self) -> bool:
        """Fast health check - just Redis ping.

        Returns:
            True if Redis is responding, False otherwise
        """
        try:
            client_provider = container.get(CacheClientProvider)
            backend = RedisBackend(client_provider=client_provider)
            return backend.health_check()[0]
        except Exception:
            return False

    async def check_health_async(self, force: bool = False, level: HealthLevel = HealthLevel.FULL) -> HealthCheckResult:
        """Perform async health check on all components.

        Args:
            force: Force fresh check even if cached result exists
            level: Health check depth level (PING, BASIC, FULL)

        Returns:
            HealthCheckResult with overall and component status
        """
        # Return cached result if available and not forced
        if not force and self._is_cached_result_valid():
            if self._last_result is None:
                raise RuntimeError("Cached result should not be None after validation")
            return self._last_result

        start_time = time.time()
        components: list[ComponentHealth] = []

        # Check Redis connectivity
        redis_health = await self._check_redis_async()
        components.append(redis_health)

        if level == HealthLevel.PING:
            # Fast path: just redis ping
            overall_status = self._determine_overall_status(components)
            duration_ms = (time.time() - start_time) * 1000
            result = HealthCheckResult(
                status=overall_status,
                components=components,
                duration_ms=duration_ms,
            )
            self._cache_result(result)
            return result

        # Check connection pool
        pool_health = await self._check_connection_pool_async()
        components.append(pool_health)

        # Check circuit breaker
        circuit_breaker_health = self._check_circuit_breaker()
        components.append(circuit_breaker_health)

        if level == HealthLevel.BASIC:
            # Basic path: stop here
            overall_status = self._determine_overall_status(components)
            duration_ms = (time.time() - start_time) * 1000
            result = HealthCheckResult(
                status=overall_status,
                components=components,
                duration_ms=duration_ms,
            )
            self._cache_result(result)
            return result

        # Check Prometheus metrics if available (FULL level only)
        if PROMETHEUS.available:
            metrics_health = self._check_metrics_collection()
            components.append(metrics_health)

        # Determine overall status
        overall_status = self._determine_overall_status(components)

        duration_ms = (time.time() - start_time) * 1000
        result = HealthCheckResult(
            status=overall_status,
            components=components,
            duration_ms=duration_ms,
        )

        # Cache the result
        self._cache_result(result)

        # Log health check result
        logger.cache_operation(
            "health_check",
            "system",
            status=overall_status.value,
            duration_ms=duration_ms,
            component_count=len(components),
        )

        return result

    def check_health(self, force: bool = False, level: HealthLevel = HealthLevel.FULL) -> HealthCheckResult:
        """Perform synchronous health check on all components.

        Args:
            force: Force fresh check even if cached result exists
            level: Health check depth level (PING, BASIC, FULL)

        Returns:
            HealthCheckResult with overall and component status
        """
        # Return cached result if available and not forced
        if not force and self._is_cached_result_valid():
            if self._last_result is None:
                raise RuntimeError("Cached result should not be None after validation")
            return self._last_result

        start_time = time.time()
        components: list[ComponentHealth] = []

        # Check Redis connectivity
        redis_health = self._check_redis_sync()
        components.append(redis_health)

        if level == HealthLevel.PING:
            # Fast path: just redis ping
            overall_status = self._determine_overall_status(components)
            duration_ms = (time.time() - start_time) * 1000
            result = HealthCheckResult(
                status=overall_status,
                components=components,
                duration_ms=duration_ms,
            )
            self._cache_result(result)
            return result

        # Check connection pool
        pool_health = self._check_connection_pool_sync()
        components.append(pool_health)

        # Check circuit breaker
        circuit_breaker_health = self._check_circuit_breaker()
        components.append(circuit_breaker_health)

        if level == HealthLevel.BASIC:
            # Basic path: stop here
            overall_status = self._determine_overall_status(components)
            duration_ms = (time.time() - start_time) * 1000
            result = HealthCheckResult(
                status=overall_status,
                components=components,
                duration_ms=duration_ms,
            )
            self._cache_result(result)
            return result

        # Check Prometheus metrics if available (FULL level only)
        if PROMETHEUS.available:
            metrics_health = self._check_metrics_collection()
            components.append(metrics_health)

        # Determine overall status
        overall_status = self._determine_overall_status(components)

        duration_ms = (time.time() - start_time) * 1000
        result = HealthCheckResult(
            status=overall_status,
            components=components,
            duration_ms=duration_ms,
        )

        # Cache the result
        self._cache_result(result)

        # Log health check result
        logger.cache_operation(
            "health_check",
            "system",
            status=overall_status.value,
            duration_ms=duration_ms,
            component_count=len(components),
        )

        return result

    def _cache_result(self, result: HealthCheckResult) -> None:
        """Cache health check result."""
        self._last_result = result
        self._last_check_time = time.time()

    def _is_cached_result_valid(self) -> bool:
        """Check if cached result is still valid."""
        if not self._last_result or not self._last_check_time:
            return False
        return (time.time() - self._last_check_time) < self._cache_duration_seconds

    async def _check_redis_async(self) -> ComponentHealth:
        """Check Redis connectivity asynchronously."""
        try:
            client_provider = container.get(CacheClientProvider)
            backend = RedisBackend(client_provider=client_provider)
            is_healthy, details = await asyncio.wait_for(
                asyncio.to_thread(backend.health_check),
                timeout=self.timeout,
            )

            if is_healthy:
                return ComponentHealth(
                    name="redis",
                    status=HealthStatus.HEALTHY,
                    message="Redis is responsive",
                    details=details,
                    last_check=datetime.now(timezone.utc),
                )
            else:
                return ComponentHealth(
                    name="redis",
                    status=HealthStatus.UNHEALTHY,
                    message=f"Redis health check failed: {details.get('error', 'unknown')}",
                    details=details,
                    last_check=datetime.now(timezone.utc),
                )
        except asyncio.TimeoutError:
            return ComponentHealth(
                name="redis",
                status=HealthStatus.UNHEALTHY,
                message=f"Redis health check timeout after {self.timeout}s",
                last_check=datetime.now(timezone.utc),
            )
        except Exception as e:
            return ComponentHealth(
                name="redis",
                status=HealthStatus.UNHEALTHY,
                message=f"Redis health check failed: {e!s}",
                details={"error_type": type(e).__name__},
                last_check=datetime.now(timezone.utc),
            )

    def _check_redis_sync(self) -> ComponentHealth:
        """Check Redis connectivity synchronously."""
        try:
            client_provider = container.get(CacheClientProvider)
            backend = RedisBackend(client_provider=client_provider)
            is_healthy, details = backend.health_check()

            if is_healthy:
                return ComponentHealth(
                    name="redis",
                    status=HealthStatus.HEALTHY,
                    message="Redis is responsive",
                    details=details,
                    last_check=datetime.now(timezone.utc),
                )
            else:
                return ComponentHealth(
                    name="redis",
                    status=HealthStatus.UNHEALTHY,
                    message=f"Redis health check failed: {details.get('error', 'unknown')}",
                    details=details,
                    last_check=datetime.now(timezone.utc),
                )
        except Exception as e:
            return ComponentHealth(
                name="redis",
                status=HealthStatus.UNHEALTHY,
                message=f"Redis health check failed: {e!s}",
                details={"error_type": type(e).__name__},
                last_check=datetime.now(timezone.utc),
            )

    async def _check_connection_pool_async(self) -> ComponentHealth:
        """Check connection pool health asynchronously."""
        try:
            if not self._pool_manager:
                return ComponentHealth(
                    name="connection_pool",
                    status=HealthStatus.DEGRADED,
                    message="Connection pool not initialized",
                    last_check=datetime.now(timezone.utc),
                )

            stats = self._pool_manager.get_pool_statistics()
            utilization = stats.get("utilization_ratio", 0)

            # Determine pool health based on utilization
            if utilization > 0.95:
                status = HealthStatus.UNHEALTHY
                message = "Connection pool is nearly exhausted"
            elif utilization > 0.9:
                status = HealthStatus.DEGRADED
                message = "Connection pool utilization is very high"
            else:
                status = HealthStatus.HEALTHY
                message = "Connection pool is healthy"

            return ComponentHealth(
                name="connection_pool",
                status=status,
                message=message,
                details=stats,
                last_check=datetime.now(timezone.utc),
            )
        except Exception as e:
            return ComponentHealth(
                name="connection_pool",
                status=HealthStatus.UNHEALTHY,
                message=f"Failed to check connection pool: {e!s}",
                details={"error_type": type(e).__name__},
                last_check=datetime.now(timezone.utc),
            )

    def _check_connection_pool_sync(self) -> ComponentHealth:
        """Check connection pool health synchronously."""
        try:
            if not self._pool_manager:
                return ComponentHealth(
                    name="connection_pool",
                    status=HealthStatus.DEGRADED,
                    message="Connection pool not initialized",
                    last_check=datetime.now(timezone.utc),
                )

            stats = self._pool_manager.get_pool_statistics()
            utilization = stats.get("utilization_ratio", 0)

            # Determine pool health based on utilization
            if utilization > 0.95:
                status = HealthStatus.UNHEALTHY
                message = "Connection pool is nearly exhausted"
            elif utilization > 0.9:
                status = HealthStatus.DEGRADED
                message = "Connection pool utilization is very high"
            else:
                status = HealthStatus.HEALTHY
                message = "Connection pool is healthy"

            return ComponentHealth(
                name="connection_pool",
                status=status,
                message=message,
                details=stats,
                last_check=datetime.now(timezone.utc),
            )
        except Exception as e:
            return ComponentHealth(
                name="connection_pool",
                status=HealthStatus.UNHEALTHY,
                message=f"Failed to check connection pool: {e!s}",
                details={"error_type": type(e).__name__},
                last_check=datetime.now(timezone.utc),
            )

    def _check_circuit_breaker(self) -> ComponentHealth:
        """Check circuit breaker status."""
        try:
            if not self._circuit_breaker:
                return ComponentHealth(
                    name="circuit_breaker",
                    status=HealthStatus.HEALTHY,
                    message="Circuit breaker not in use",
                    last_check=datetime.now(timezone.utc),
                )

            state = self._circuit_breaker.state
            failure_count = self._circuit_breaker.failure_count
            success_count = self._circuit_breaker.success_count

            # Map circuit breaker state to health status
            if state == "OPEN":
                status = HealthStatus.UNHEALTHY
                message = "Circuit breaker is OPEN - Redis operations are bypassed"
            elif state == "HALF_OPEN":
                status = HealthStatus.DEGRADED
                message = "Circuit breaker is HALF_OPEN - testing recovery"
            else:  # CLOSED
                status = HealthStatus.HEALTHY
                message = "Circuit breaker is CLOSED - normal operation"

            return ComponentHealth(
                name="circuit_breaker",
                status=status,
                message=message,
                details={
                    "state": state,
                    "failure_count": failure_count,
                    "success_count": success_count,
                    "last_failure_time": (
                        self._circuit_breaker.last_failure_time.isoformat() if self._circuit_breaker.last_failure_time else None
                    ),
                },
                last_check=datetime.now(timezone.utc),
            )
        except Exception as e:
            return ComponentHealth(
                name="circuit_breaker",
                status=HealthStatus.UNHEALTHY,
                message=f"Failed to check circuit breaker: {e!s}",
                details={"error_type": type(e).__name__},
                last_check=datetime.now(timezone.utc),
            )

    def _check_metrics_collection(self) -> ComponentHealth:
        """Check if Prometheus metrics are being collected."""
        try:
            from cachekit.reliability.metrics_collection import get_all_metrics

            # Get total count from all cache operations
            all_metrics = get_all_metrics()
            cache_ops_data = all_metrics.get("cache_operations_total", {})
            cache_ops_total = sum(cache_ops_data.values()) if cache_ops_data else 0

            return ComponentHealth(
                name="metrics",
                status=HealthStatus.HEALTHY,
                message="Prometheus metrics are being collected",
                details={
                    "cache_operations_total": int(cache_ops_total),
                    "collectors_active": True,
                },
                last_check=datetime.now(timezone.utc),
            )
        except Exception as e:
            return ComponentHealth(
                name="metrics",
                status=HealthStatus.DEGRADED,
                message=f"Metrics collection issue: {e!s}",
                details={"error_type": type(e).__name__},
                last_check=datetime.now(timezone.utc),
            )

    def _determine_overall_status(self, components: list[ComponentHealth]) -> HealthStatus:
        """Determine overall health status from component statuses."""
        if any(c.status == HealthStatus.UNHEALTHY for c in components):
            return HealthStatus.UNHEALTHY
        elif any(c.status == HealthStatus.DEGRADED for c in components):
            return HealthStatus.DEGRADED
        return HealthStatus.HEALTHY


# Global health checker instance
_health_checker: Optional[HealthChecker] = None


def get_health_checker(timeout_seconds: float = 5.0) -> HealthChecker:
    """Get or create global health checker instance.

    Dependencies (pool_manager, circuit_breaker) are auto-discovered from DI container
    if available, enabling optional health checks.

    Args:
        timeout_seconds: Timeout for health check operations

    Returns:
        HealthChecker instance
    """
    global _health_checker
    if _health_checker is None:
        # Auto-discover optional dependencies from DI container
        pool_manager: Optional[PoolManagerProtocol] = None
        circuit_breaker: Optional[CircuitBreakerProtocol] = None

        try:
            pool_manager = container.get(PoolManagerProtocol)
        except (ValueError, KeyError):
            pass

        try:
            circuit_breaker = container.get(CircuitBreakerProtocol)
        except (ValueError, KeyError):
            pass

        _health_checker = HealthChecker(
            timeout_seconds=timeout_seconds,
            pool_manager=pool_manager,
            circuit_breaker=circuit_breaker,
        )

    return _health_checker


def health_check_handler(force: bool = False) -> dict[str, Any]:
    """Synchronous health check handler for web frameworks.

    Args:
        force: Force fresh check even if cached result exists

    Returns:
        Dictionary with health check results suitable for JSON response
    """
    checker = get_health_checker()
    result = checker.check_health(force=force)
    return result.to_dict()


async def async_health_check_handler(force: bool = False) -> dict[str, Any]:
    """Asynchronous health check handler for async web frameworks.

    Args:
        force: Force fresh check even if cached result exists

    Returns:
        Dictionary with health check results suitable for JSON response
    """
    checker = get_health_checker()
    result = await checker.check_health_async(force=force)
    return result.to_dict()


# Export convenience functions
__all__ = [
    "ComponentHealth",
    "HealthCheckResult",
    "HealthChecker",
    "HealthLevel",
    "HealthStatus",
    "async_health_check_handler",
    "get_health_checker",
    "health_check_handler",
]
