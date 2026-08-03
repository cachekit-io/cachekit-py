"""Nested configuration classes for logical grouping of related settings.

This module provides frozen dataclass configuration groups that organize
cache decorator settings by their functional area (L1 cache, circuit breaker,
timeout, backpressure, monitoring, encryption).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from .validation import ConfigurationError


@dataclass(frozen=True)
class L1CacheConfig:
    """L1 (in-memory) cache configuration.

    L1 cache provides sub-microsecond latency by caching results in process memory,
    eliminating network round-trips for frequently accessed data.

    Attributes:
        enabled: Enable L1 in-memory cache (default: True)
        max_size_mb: Per-namespace L1 budget in MB. None (default) inherits the global
            CACHEKIT_L1_MAX_SIZE_MB setting (issue #163); an int overrides it per decorator.
            In L1-only mode (backend=None) this is a best-effort byte bound on raw object sizes.
        swr_enabled: Enable stale-while-revalidate background refresh (default: True).
            Requires a ttl; in L1-only mode async functions refresh via an asyncio
            task and sync functions via a daemon thread.
        swr_threshold_ratio: Fraction of TTL after which a hit triggers a background
            refresh, in (0.0, 1.0] (default: 0.5)
        namespace_index: Enable fast namespace-based invalidation (default: True)

    Examples:
        Create with defaults:

        >>> config = L1CacheConfig()
        >>> config.enabled
        True
        >>> config.max_size_mb is None  # inherits CACHEKIT_L1_MAX_SIZE_MB
        True

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
    max_size_mb: int | None = None
    swr_enabled: bool = True
    swr_threshold_ratio: float = 0.5
    namespace_index: bool = True

    def validate(self) -> None:
        """Validate L1 cache configuration.

        Raises:
            ConfigurationError: If max_size_mb < 1 or swr_threshold_ratio is
                outside (0.0, 1.0]
        """
        if self.max_size_mb is not None and self.max_size_mb < 1:
            raise ConfigurationError(f"L1 max_size_mb must be >= 1, got {self.max_size_mb}")
        if not (0.0 < self.swr_threshold_ratio <= 1.0):
            raise ConfigurationError(f"L1 swr_threshold_ratio must be in (0.0, 1.0], got {self.swr_threshold_ratio}")


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

    Tenant mode is required: set single_tenant_mode=True for single-tenant or provide
    a tenant_extractor callable for multi-tenant key isolation. @cache.secure() sets
    single_tenant_mode automatically; if using EncryptionConfig directly (e.g. with
    @cache.io), you must set it explicitly.

    Tri-state ``enabled`` (issue #128): a plain bool cannot tell "user left it unset"
    from "user explicitly disabled", so a deliberate opt-out was silently overridden by
    fleet-wide CACHEKIT_MASTER_KEY auto-detection. ``enabled`` is therefore None/True/False:
        - None (default): unset — defer to CACHEKIT_MASTER_KEY auto-detection downstream.
        - True: force client-side encryption ON (requires master_key + tenant mode).
        - False: explicit hard opt-out — never encrypt, even when a master key is present.

    Attributes:
        enabled: Tri-state encryption flag (default: None = unset/auto-detect).
                 True = force-on, False = explicit opt-out.
        master_key: Hex-encoded master key for key derivation (required if enabled=True)
        tenant_extractor: Optional callable for per-tenant key derivation (default: None)
        single_tenant_mode: Explicitly enable single-tenant mode (default: False)
        deployment_uuid: Optional deployment-specific UUID for single-tenant mode (default: None)
        fail_closed: Tri-state tamper-failure policy (default: None = defer to the
                 CACHEKIT_ENCRYPTION_FAIL_CLOSED env setting, which defaults to False).
                 True = raise DecryptionAuthenticationError to the caller on AES-GCM
                 authentication failure or key-fingerprint mismatch instead of silently
                 recomputing (fail closed). False = explicit per-decorator opt-out of a
                 fleet-wide fail-closed setting (fail open: warn, record the
                 cachekit_decrypt_failures_total metric, recompute).

    Examples:
        Unset by default (defers to auto-detection, no encryption forced):

        >>> config = EncryptionConfig()
        >>> config.enabled is None
        True
        >>> config.validate()  # No error when unset

        Explicit opt-out (never encrypts, even with CACHEKIT_MASTER_KEY set):

        >>> off = EncryptionConfig(enabled=False)
        >>> off.enabled
        False
        >>> off.validate()  # No error when explicitly disabled

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

    enabled: bool | None = None
    master_key: str | None = field(default=None, repr=False)
    tenant_extractor: Callable[..., str] | None = None
    single_tenant_mode: bool = False
    deployment_uuid: str | None = None
    fail_closed: bool | None = None

    def validate(self) -> None:
        """Validate encryption configuration.

        Only the explicit force-on state (enabled=True) requires a master key. The
        unset (None) and explicit opt-out (False) states are both falsy and skip
        validation — None defers to downstream auto-detection, False never encrypts.

        The master key may be supplied inline or via the CACHEKIT_MASTER_KEY env var
        (resolved here so force-on works fleet-wide without inlining the key, matching
        the handler's own resolution).

        Raises:
            ConfigurationError: If encryption enabled but no master_key (inline or env)
        """
        if self.enabled and not self._resolve_master_key():
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

    def _resolve_master_key(self) -> str | None:
        """Resolve the master key from the inline value, falling back to env settings.

        Inline master_key takes precedence; otherwise read CACHEKIT_MASTER_KEY via the
        settings singleton. Mirrors validate_encryption_config so the config-level and
        handler-level views of "is a key available?" stay consistent.
        """
        if self.master_key:
            return self.master_key
        from cachekit.config.singleton import get_settings

        settings = get_settings()
        return settings.master_key.get_secret_value() if settings.master_key else None
