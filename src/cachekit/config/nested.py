"""Nested configuration classes for logical grouping of related settings.

This module provides frozen dataclass configuration groups that organize
cache decorator settings by their functional area (L1 cache, circuit breaker,
timeout, backpressure, monitoring, encryption).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from .validation import ConfigurationError


@dataclass(frozen=True)
class L1CacheConfig:
    """L1 (in-memory) cache configuration.

    L1 cache provides sub-microsecond latency by caching results in process memory,
    eliminating network round-trips for frequently accessed data.

    Attributes:
        enabled: Enable L1 in-memory cache (default: True)
        max_size_mb: Maximum L1 cache size in megabytes (default: 100)

    Examples:
        Create with defaults:

        >>> config = L1CacheConfig()
        >>> config.enabled
        True
        >>> config.max_size_mb
        100

        Custom configuration validates successfully:

        >>> custom = L1CacheConfig(enabled=True, max_size_mb=200)
        >>> custom.validate()  # No error = valid

        Invalid max_size_mb raises ConfigurationError:

        >>> L1CacheConfig(max_size_mb=0).validate()  # doctest: +IGNORE_EXCEPTION_DETAIL
        Traceback (most recent call last):
            ...
        cachekit.config.validation.ConfigurationError: L1 max_size_mb must be >= 1, got 0
    """

    enabled: bool = True
    max_size_mb: int = 100
    swr_enabled: bool = True
    swr_threshold_ratio: float = 0.5
    invalidation_enabled: bool = True
    namespace_index: bool = True

    def validate(self) -> None:
        """Validate L1 cache configuration.

        Raises:
            ConfigurationError: If max_size_mb < 1
        """
        if self.max_size_mb < 1:
            raise ConfigurationError(f"L1 max_size_mb must be >= 1, got {self.max_size_mb}")


@dataclass(frozen=True)
class CircuitBreakerConfig:
    """Circuit breaker configuration for graceful degradation.

    Circuit breaker prevents cascading failures by failing fast when backend is unhealthy.
    Transitions between CLOSED (normal), OPEN (failing fast), and HALF_OPEN (testing recovery).

    Attributes:
        enabled: Enable circuit breaker protection (default: True)
        failure_threshold: Consecutive failures before opening circuit (default: 5)
        success_threshold: Consecutive successes in HALF_OPEN to close circuit (default: 3)
        recovery_timeout: Seconds to wait before attempting recovery (default: 30)
        half_open_requests: Max concurrent requests during HALF_OPEN state (default: 3)
        excluded_exceptions: Exception types that don't trigger circuit breaker (default: ())

    Examples:
        Create with defaults:

        >>> config = CircuitBreakerConfig()
        >>> config.failure_threshold
        5
        >>> config.recovery_timeout
        30

        Custom thresholds:

        >>> strict = CircuitBreakerConfig(failure_threshold=3, success_threshold=5)
        >>> strict.validate()  # No error = valid
        >>> strict.failure_threshold
        3

        Invalid threshold raises ConfigurationError:

        >>> CircuitBreakerConfig(failure_threshold=0).validate()  # doctest: +IGNORE_EXCEPTION_DETAIL
        Traceback (most recent call last):
            ...
        cachekit.config.validation.ConfigurationError: failure_threshold must be >= 1, got 0
    """

    enabled: bool = True
    failure_threshold: int = 5
    success_threshold: int = 3
    recovery_timeout: int = 30
    half_open_requests: int = 3
    excluded_exceptions: tuple[type[Exception], ...] = ()

    def validate(self) -> None:
        """Validate circuit breaker configuration.

        Raises:
            ConfigurationError: If thresholds are invalid
        """
        if self.failure_threshold < 1:
            raise ConfigurationError(f"failure_threshold must be >= 1, got {self.failure_threshold}")
        if self.success_threshold < 1:
            raise ConfigurationError(f"success_threshold must be >= 1, got {self.success_threshold}")
        if self.half_open_requests < 1:
            raise ConfigurationError(f"half_open_requests must be >= 1, got {self.half_open_requests}")


@dataclass(frozen=True)
class TimeoutConfig:
    """Adaptive timeout configuration.

    Adaptive timeout dynamically adjusts request timeouts based on observed
    latency percentiles, preventing both premature timeouts and excessive waiting.

    Attributes:
        enabled: Enable adaptive timeout (default: True)
        initial: Initial timeout in seconds (default: 1.0)
        min: Minimum timeout in seconds (default: 0.1)
        max: Maximum timeout in seconds (default: 5.0)
        window_size: Number of requests in sliding window for percentile calculation (default: 1000)
        percentile: Target percentile for timeout calculation (default: 95.0)

    Examples:
        Create with defaults:

        >>> config = TimeoutConfig()
        >>> config.initial
        1.0
        >>> config.min
        0.1
        >>> config.max
        5.0

        Custom timeout range:

        >>> custom = TimeoutConfig(min=0.5, initial=2.0, max=10.0)
        >>> custom.validate()  # No error = valid

        Invalid range (initial must be between min and max):

        >>> TimeoutConfig(min=1.0, initial=0.5, max=5.0).validate()  # doctest: +IGNORE_EXCEPTION_DETAIL
        Traceback (most recent call last):
            ...
        cachekit.config.validation.ConfigurationError: Timeout must satisfy: min (1.0) <= initial (0.5) <= max (5.0)
    """

    enabled: bool = True
    initial: float = 1.0
    min: float = 0.1
    max: float = 5.0
    window_size: int = 1000
    percentile: float = 95.0

    def validate(self) -> None:
        """Validate adaptive timeout configuration.

        Raises:
            ConfigurationError: If timeout values are inconsistent
        """
        if not self.min <= self.initial <= self.max:
            raise ConfigurationError(f"Timeout must satisfy: min ({self.min}) <= initial ({self.initial}) <= max ({self.max})")
        if not 0.0 < self.percentile <= 100.0:
            raise ConfigurationError(f"percentile must be 0.0-100.0, got {self.percentile}")


@dataclass(frozen=True)
class BackpressureConfig:
    """Backpressure configuration for overload protection.

    Backpressure limits concurrent requests to prevent resource exhaustion.
    When limit is reached, new requests are queued or rejected based on queue capacity.

    Attributes:
        enabled: Enable backpressure protection (default: True)
        max_concurrent_requests: Maximum concurrent cache requests (default: 100)
        queue_size: Queue size for waiting requests (default: 1000)
        timeout: Seconds to wait in queue before giving up (default: 0.1)

    Examples:
        Create with defaults:

        >>> config = BackpressureConfig()
        >>> config.max_concurrent_requests
        100
        >>> config.queue_size
        1000

        Custom limits:

        >>> custom = BackpressureConfig(max_concurrent_requests=50, queue_size=500)
        >>> custom.validate()  # No error = valid

        Invalid concurrent requests:

        >>> BackpressureConfig(max_concurrent_requests=0).validate()  # doctest: +IGNORE_EXCEPTION_DETAIL
        Traceback (most recent call last):
            ...
        cachekit.config.validation.ConfigurationError: max_concurrent_requests must be >= 1, got 0
    """

    enabled: bool = True
    max_concurrent_requests: int = 100
    queue_size: int = 1000
    timeout: float = 0.1

    def validate(self) -> None:
        """Validate backpressure configuration.

        Raises:
            ConfigurationError: If capacity limits are invalid
        """
        if self.max_concurrent_requests < 1:
            raise ConfigurationError(f"max_concurrent_requests must be >= 1, got {self.max_concurrent_requests}")


@dataclass(frozen=True)
class MonitoringConfig:
    """Observability and monitoring configuration.

    Controls collection of statistics, tracing, structured logging, and metrics export.

    Attributes:
        collect_stats: Collect cache hit/miss statistics (default: True)
        enable_tracing: Enable distributed tracing (default: True)
        enable_structured_logging: Enable structured JSON logging (default: True)
        enable_prometheus_metrics: Export Prometheus metrics (default: True)

    Examples:
        Create with defaults (all monitoring enabled):

        >>> config = MonitoringConfig()
        >>> config.collect_stats
        True
        >>> config.enable_prometheus_metrics
        True

        Disable Prometheus for local development:

        >>> dev_config = MonitoringConfig(enable_prometheus_metrics=False)
        >>> dev_config.validate()  # No error = valid
        >>> dev_config.enable_prometheus_metrics
        False
    """

    collect_stats: bool = True
    enable_tracing: bool = True
    enable_structured_logging: bool = True
    enable_prometheus_metrics: bool = True

    def validate(self) -> None:
        """Validate monitoring configuration.

        Raises:
            ConfigurationError: If configuration is invalid (currently no constraints)
        """
        # No validation constraints currently - all boolean flags
        pass


@dataclass(frozen=True)
class EncryptionConfig:
    """Encryption configuration for PII/sensitive data.

    Enables client-side AES-256-GCM encryption of cached values.
    Both L1 and L2 store encrypted bytes (encrypt-at-rest everywhere).

    NOTE: Per backend abstraction spec, encryption stores encrypted bytes in BOTH L1 and L2.
    L1 can be enabled with encryption (stores encrypted bytes, not plaintext).

    Attributes:
        enabled: Enable client-side encryption (default: False)
        master_key: Hex-encoded master key for key derivation (required if enabled)
        tenant_extractor: Optional callable for per-tenant key derivation (default: None)
        single_tenant_mode: Explicitly enable single-tenant mode (default: False)
        deployment_uuid: Optional deployment-specific UUID for single-tenant mode (default: None)

    Examples:
        Disabled by default (no encryption):

        >>> config = EncryptionConfig()
        >>> config.enabled
        False
        >>> config.validate()  # No error when disabled

        Single-tenant encryption:

        >>> master_key = 'a' * 64  # 32 bytes hex-encoded
        >>> single = EncryptionConfig(enabled=True, master_key=master_key, single_tenant_mode=True)
        >>> single.validate()  # No error = valid

        Missing master_key raises ConfigurationError:

        >>> EncryptionConfig(enabled=True).validate()  # doctest: +IGNORE_EXCEPTION_DETAIL
        Traceback (most recent call last):
            ...
        cachekit.config.validation.ConfigurationError: encryption.enabled=True requires encryption.master_key...

        Must specify tenant mode (single or multi):

        >>> EncryptionConfig(enabled=True, master_key='a'*64).validate()  # doctest: +IGNORE_EXCEPTION_DETAIL
        Traceback (most recent call last):
            ...
        cachekit.config.validation.ConfigurationError: Encryption requires explicit tenant mode...
    """

    enabled: bool = False
    master_key: str | None = None
    tenant_extractor: Callable[..., str] | None = None
    single_tenant_mode: bool = False
    deployment_uuid: str | None = None

    def validate(self) -> None:
        """Validate encryption configuration.

        Raises:
            ConfigurationError: If encryption enabled but master_key not set
        """
        if self.enabled and not self.master_key:
            raise ConfigurationError(
                "encryption.enabled=True requires encryption.master_key. "
                "Set CACHEKIT_MASTER_KEY environment variable or pass master_key parameter."
            )

        # Validate tenant mode configuration
        if self.enabled:
            if not self.tenant_extractor and not self.single_tenant_mode:
                raise ConfigurationError(
                    "Encryption requires explicit tenant mode. "
                    "Provide tenant_extractor for multi-tenant OR "
                    "set single_tenant_mode=True for single-tenant."
                )
            if self.tenant_extractor and self.single_tenant_mode:
                raise ConfigurationError(
                    "Cannot use both tenant_extractor and single_tenant_mode. "
                    "Choose multi-tenant (tenant_extractor) OR single-tenant (single_tenant_mode=True)."
                )
