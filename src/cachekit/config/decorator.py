"""Unified immutable configuration for cache decorator.

Simple frozen dataclass with nested configuration groups and validation via __post_init__.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable, Union

from .nested import (
    BackpressureConfig,
    CircuitBreakerConfig,
    EncryptionConfig,
    L1CacheConfig,
    MonitoringConfig,
    TimeoutConfig,
)
from .validation import ConfigurationError

if TYPE_CHECKING:
    from cachekit.backends.base import BaseBackend
    from cachekit.serializers.base import SerializerProtocol


# Backend Resolution Layer

# Sentinel for unset explicit backend parameter
_UNSET = object()

# Module-level default backend (set via set_default_backend())
_default_backend: BaseBackend | None = None


def set_default_backend(backend: BaseBackend | None) -> None:
    """Set module-level default backend for all decorators.

    This allows DRY configuration across multiple decorators by setting
    the backend once at application startup instead of repeating it on
    each decorator.

    Args:
        backend: Backend instance (RedisBackend, HTTPBackend) or None to clear

    Examples:
        Set and clear default backend:

        >>> set_default_backend(None)  # Clear any existing default
        >>> get_default_backend() is None
        True

        Set a mock backend:

        >>> from unittest.mock import Mock
        >>> mock_backend = Mock()
        >>> set_default_backend(mock_backend)
        >>> get_default_backend() is mock_backend
        True
        >>> set_default_backend(None)  # Clean up
    """
    global _default_backend
    _default_backend = backend


def get_default_backend() -> BaseBackend | None:
    """Get current module-level default backend.

    Returns:
        Current default backend or None if not set

    Examples:
        Returns None when no backend set:

        >>> set_default_backend(None)
        >>> get_default_backend() is None
        True
    """
    return _default_backend


def _resolve_backend(explicit_backend: object = _UNSET) -> BaseBackend | None:
    """Resolve backend via three-tier lookup.

    Priority order:
    1. Explicit backend= kwarg (highest priority)
    2. Module-level default (set_default_backend)
    3. REDIS_URL environment variable (auto-create RedisBackend)

    Zero-config UX: If REDIS_URL is set, backend is auto-created.
    Fail-fast: If no backend configured, raise helpful ConfigurationError.

    Args:
        explicit_backend: Explicit backend from decorator kwarg (use UNSET sentinel for not provided)

    Returns:
        Resolved backend instance or None for explicit L1-only mode

    Raises:
        ConfigurationError: If no backend configured and REDIS_URL not set
    """
    # Tier 1: Explicit backend parameter (highest priority)
    if explicit_backend is not _UNSET:
        return explicit_backend  # type: ignore[return-value]

    # Tier 2: Module-level default
    if _default_backend is not None:
        return _default_backend

    # Tier 3: Auto-create from REDIS_URL env var
    redis_url = os.environ.get("REDIS_URL")
    if redis_url:
        # Lazy import to avoid circular dependency
        from cachekit.backends.provider import CacheClientProvider
        from cachekit.backends.redis import RedisBackend
        from cachekit.di import DIContainer

        # Inject client_provider explicitly (follows Dependency Injection Principle)
        container = DIContainer()
        client_provider = container.get(CacheClientProvider)
        return RedisBackend(redis_url, client_provider=client_provider)

    # No backend configured - fail fast with helpful message
    raise ConfigurationError(
        "No backend configured.\n\n"
        "Quick fix (90% of cases):\n"
        "  export REDIS_URL=redis://localhost:6379\n\n"
        "Or explicitly configure:\n"
        "  from cachekit import set_default_backend\n"
        "  from cachekit.backends import RedisBackend\n"
        "  set_default_backend(RedisBackend('redis://localhost:6379'))\n\n"
        "Or use L1-only mode (no Redis):\n"
        "  @cache(backend=None)  # In-memory cache only\n\n"
        "See: https://github.com/cachekit-io/cachekit-py/blob/main/docs/guides/backend-guide.md"
    )


@dataclass(frozen=True)
class DecoratorConfig:
    """Unified immutable configuration for cache decorator.

    Intent-based presets with kwargs overrides for customization.
    - Frozen dataclass ensures immutability
    - Nested configs group related settings
    - Backend auto-resolved from REDIS_URL env var, set_default_backend(), or explicit backend= kwarg

    Examples:
        # Zero-config (REDIS_URL env var)
        @cache.minimal(ttl=300)
        def fast_function():
            return "value"

        # Production preset with encryption
        @cache.secure(master_key="...", ttl=600)
        def secure_function():
            return "value"

        # Explicit L1-only mode
        @cache(backend=None, ttl=60)
        def local_function():
            return "value"

    Attributes:
        ttl: Time-to-live in seconds (None = no expiration)
        namespace: Optional namespace prefix for cache keys
        serializer: Serializer instance or name. Accepts either:
                   - String name: "default" (MessagePack), "arrow" (DataFrame zero-copy)
                   - SerializerProtocol instance: Custom serializer implementing the protocol
                   Default: "default" (MessagePack+LZ4+xxHash3-64 via Rust)
        safe_mode: Enable fail-open behavior (cache failures return None instead of raising)
        integrity_checking: Enable checksums for corruption detection (default: True)
                           All serializers use xxHash3-64 (8 bytes).
                           Set to False for @cache.minimal (speed-first, no integrity guarantee)
        refresh_ttl_on_get: Extend TTL on cache hit
        ttl_refresh_threshold: Minimum remaining TTL fraction (0.0-1.0) to trigger refresh
        backend: L2 backend (RedisBackend, HTTPBackend, None for L1-only)
        l1: L1 in-memory cache configuration
        circuit_breaker: Circuit breaker configuration
        timeout: Adaptive timeout configuration
        backpressure: Backpressure configuration
        monitoring: Monitoring and observability configuration
        encryption: Client-side encryption configuration
    """

    # Core settings (5 fields)
    ttl: int | None = None
    namespace: str | None = None
    serializer: Union[str, SerializerProtocol] = "default"  # type: ignore[assignment]  # String name or protocol instance
    safe_mode: bool = False
    integrity_checking: bool = True  # Checksums for corruption detection (xxHash3-64 for all serializers)

    # Performance (2 fields)
    refresh_ttl_on_get: bool = False
    ttl_refresh_threshold: float = 0.5

    # Backend abstraction (1 field)
    backend: BaseBackend | None = None  # L2 backend: RedisBackend, HTTPBackend (future), None (L1-only)

    # Nested configuration groups (6 groups)
    l1: L1CacheConfig = field(default_factory=L1CacheConfig)
    circuit_breaker: CircuitBreakerConfig = field(default_factory=CircuitBreakerConfig)
    timeout: TimeoutConfig = field(default_factory=TimeoutConfig)
    backpressure: BackpressureConfig = field(default_factory=BackpressureConfig)
    monitoring: MonitoringConfig = field(default_factory=MonitoringConfig)
    encryption: EncryptionConfig = field(default_factory=EncryptionConfig)

    def __post_init__(self) -> None:
        """Validate configuration after instance creation.

        Raises:
            ConfigurationError: If configuration is invalid
        """
        self._validate_config()

    def _validate_config(self) -> None:
        """Validate configuration consistency.

        Validates core fields and delegates to nested config validators.

        Raises:
            ConfigurationError: If configuration is invalid
        """
        # TTL validation
        if self.ttl is not None and self.ttl < 0:
            raise ValueError(f"ttl must be non-negative, got {self.ttl}")

        # TTL refresh threshold validation
        if not 0.0 <= self.ttl_refresh_threshold <= 1.0:
            raise ConfigurationError(f"ttl_refresh_threshold must be 0.0-1.0, got {self.ttl_refresh_threshold}")

        # Validate nested configs
        self.l1.validate()
        self.circuit_breaker.validate()
        self.timeout.validate()
        self.backpressure.validate()
        self.monitoring.validate()
        self.encryption.validate()

    def to_dict(self) -> dict[str, object]:
        """Convert to dictionary for backward compatibility during migration.

        This method will be removed in Task 6 when wrapper accepts DecoratorConfig directly.

        Returns:
            Dictionary representation with flattened nested configs
        """
        return {
            # Core fields
            "ttl": self.ttl,
            "namespace": self.namespace,
            "serializer": self.serializer,
            "safe_mode": self.safe_mode,
            "refresh_ttl_on_get": self.refresh_ttl_on_get,
            "ttl_refresh_threshold": self.ttl_refresh_threshold,
            "backend": self.backend,
            # L1 cache (flattened)
            "l1_enabled": self.l1.enabled,
            "l1_max_size_mb": self.l1.max_size_mb,
            # Circuit breaker (flattened)
            "circuit_breaker": self.circuit_breaker.enabled,
            "failure_threshold": self.circuit_breaker.failure_threshold,
            "success_threshold": self.circuit_breaker.success_threshold,
            "recovery_timeout": self.circuit_breaker.recovery_timeout,
            "half_open_requests": self.circuit_breaker.half_open_requests,
            "excluded_exceptions": self.circuit_breaker.excluded_exceptions,
            # Timeout (flattened)
            "adaptive_timeout": self.timeout.enabled,
            "initial_timeout": self.timeout.initial,
            "min_timeout": self.timeout.min,
            "max_timeout": self.timeout.max,
            "timeout_window_size": self.timeout.window_size,
            "timeout_percentile": self.timeout.percentile,
            # Backpressure (flattened)
            "backpressure": self.backpressure.enabled,
            "max_concurrent_requests": self.backpressure.max_concurrent_requests,
            "queue_size": self.backpressure.queue_size,
            "backpressure_timeout": self.backpressure.timeout,
            # Monitoring (flattened)
            "collect_stats": self.monitoring.collect_stats,
            "enable_tracing": self.monitoring.enable_tracing,
            "enable_structured_logging": self.monitoring.enable_structured_logging,
            "enable_prometheus_metrics": self.monitoring.enable_prometheus_metrics,
            # Encryption (flattened)
            "encryption": self.encryption.enabled,
            "master_key": self.encryption.master_key,
            "tenant_extractor": self.encryption.tenant_extractor,
        }

    # Intent Presets (Class Methods)

    @classmethod
    def minimal(cls, **kwargs: Any) -> DecoratorConfig:
        """Minimal protections profile: Maximum throughput, minimal overhead.

        Use cases: Read-heavy workloads, non-critical caching, high-performance scenarios
        Trade-offs: Circuit breaker disabled, adaptive timeout disabled, no monitoring, NO integrity checking

        Note: Backend resolved from REDIS_URL env var, set_default_backend(), or explicit backend= kwarg

        Args:
            **kwargs: Overrides (ttl, namespace, backend, integrity_checking=True to opt-in, etc.)

        Returns:
            DecoratorConfig with minimal protections preset

        Example:
            >>> config = DecoratorConfig.minimal(ttl=300)
            >>> config.circuit_breaker.enabled
            False
            >>> config.integrity_checking
            False
        """
        return cls(
            integrity_checking=False,  # Speed-first: no checksum overhead
            l1=L1CacheConfig(
                enabled=True,
                swr_enabled=False,
                invalidation_enabled=False,
                namespace_index=False,
            ),
            circuit_breaker=CircuitBreakerConfig(enabled=False),
            timeout=TimeoutConfig(enabled=False),
            backpressure=BackpressureConfig(enabled=True),
            monitoring=MonitoringConfig(
                collect_stats=False,
                enable_tracing=False,
                enable_structured_logging=False,
                enable_prometheus_metrics=False,
            ),
            **kwargs,
        )

    @classmethod
    def production(cls, **kwargs: Any) -> DecoratorConfig:
        """Production profile: All protections enabled, full observability, integrity checking ON.

        Use cases: Payment systems, APIs, production services, critical workloads
        Trade-offs: Additional latency from circuit breaker, timeout checks, monitoring, integrity validation

        Note: Backend resolved from REDIS_URL env var, set_default_backend(), or explicit backend= kwarg

        Args:
            **kwargs: Overrides (ttl, namespace, backend, etc.)

        Returns:
            DecoratorConfig with production-grade protections

        Example:
            >>> config = DecoratorConfig.production(ttl=600)
            >>> config.circuit_breaker.enabled
            True
            >>> config.integrity_checking
            True
        """
        return cls(
            integrity_checking=True,  # Production: integrity guarantee
            l1=L1CacheConfig(
                enabled=True,
                swr_enabled=True,
                invalidation_enabled=True,
                namespace_index=True,
            ),
            circuit_breaker=CircuitBreakerConfig(enabled=True),
            timeout=TimeoutConfig(enabled=True),
            backpressure=BackpressureConfig(enabled=True),
            monitoring=MonitoringConfig(
                collect_stats=True,
                enable_tracing=True,
                enable_structured_logging=True,
                enable_prometheus_metrics=True,
            ),
            **kwargs,
        )

    @classmethod
    def secure(cls, master_key: str, tenant_extractor: Callable[..., str] | None = None, **kwargs: Any) -> DecoratorConfig:
        """Security profile: Encryption REQUIRED, encrypted-at-rest everywhere, full audit trail, integrity NON-NEGOTIABLE.

        Use cases: PII, medical data, financial records, GDPR compliance
        Architecture: Both L1 and L2 store encrypted bytes (encrypt-at-rest everywhere)

        Note: Backend resolved from REDIS_URL env var, set_default_backend(), or explicit backend= kwarg
        Note: integrity_checking is forced to True (non-negotiable for security)

        Args:
            master_key: Encryption master key (hex-encoded, minimum 32 bytes for AES-256)
            tenant_extractor: Optional tenant ID extractor for multi-tenant encryption
            **kwargs: Overrides (ttl, namespace, backend, etc.) - integrity_checking cannot be overridden

        Returns:
            DecoratorConfig with encryption enabled and full security features

        Example:
            >>> config = DecoratorConfig.secure(master_key="a" * 64, ttl=600)
            >>> config.encryption.enabled
            True
            >>> config.integrity_checking
            True
        """
        # Extract encryption-specific params from kwargs
        explicit_single_tenant = kwargs.pop("single_tenant_mode", None)
        deployment_uuid = kwargs.pop("deployment_uuid", None)

        # SECURITY INVARIANT: Force integrity_checking=True (non-negotiable for encryption)
        # Remove any explicit integrity_checking override (if user tried to disable it)
        kwargs.pop("integrity_checking", None)

        # Normalize empty string to None (security: empty string treated as single-tenant)
        tenant_extractor = tenant_extractor or None

        # Determine tenant mode: explicit param > tenant_extractor check
        if explicit_single_tenant is not None:
            single_tenant_mode = explicit_single_tenant
        else:
            single_tenant_mode = tenant_extractor is None

        return cls(
            integrity_checking=True,  # NON-NEGOTIABLE for encryption (security invariant)
            l1=L1CacheConfig(
                enabled=True,  # L1 stores encrypted bytes. Enabled: ~50ns hits vs 2-7ms Redis
                swr_enabled=True,
                invalidation_enabled=True,
                namespace_index=True,
            ),
            encryption=EncryptionConfig(
                enabled=True,
                master_key=master_key,
                tenant_extractor=tenant_extractor,
                single_tenant_mode=single_tenant_mode,
                deployment_uuid=deployment_uuid,
            ),
            circuit_breaker=CircuitBreakerConfig(enabled=True),
            timeout=TimeoutConfig(enabled=True),
            backpressure=BackpressureConfig(enabled=True),
            monitoring=MonitoringConfig(
                collect_stats=True,
                enable_tracing=True,
                enable_structured_logging=True,
                enable_prometheus_metrics=True,
            ),
            **kwargs,
        )

    @classmethod
    def dev(cls, **kwargs: Any) -> DecoratorConfig:
        """Development profile: Verbose logging, easy debugging, no Prometheus, integrity checking ON.

        Use cases: Local development, debugging production issues
        Trade-offs: Verbose logs, Prometheus disabled for simplicity

        Note: Backend resolved from REDIS_URL env var, set_default_backend(), or explicit backend= kwarg

        Args:
            **kwargs: Overrides (ttl, namespace, backend, etc.)

        Returns:
            DecoratorConfig optimized for development

        Example:
            >>> config = DecoratorConfig.dev(ttl=60)
            >>> config.monitoring.enable_prometheus_metrics
            False
            >>> config.integrity_checking
            True
        """
        return cls(
            integrity_checking=True,  # Development: catch data corruption early
            l1=L1CacheConfig(
                enabled=True,
                swr_enabled=True,
                invalidation_enabled=False,
                namespace_index=False,
            ),
            circuit_breaker=CircuitBreakerConfig(enabled=True),
            timeout=TimeoutConfig(enabled=True),
            backpressure=BackpressureConfig(enabled=True),
            monitoring=MonitoringConfig(
                collect_stats=True,
                enable_tracing=True,
                enable_structured_logging=True,
                enable_prometheus_metrics=False,
            ),
            **kwargs,
        )

    @classmethod
    def test(cls, **kwargs: Any) -> DecoratorConfig:
        """Testing profile: Deterministic, all protections disabled, no monitoring, no integrity checking.

        Use cases: Unit tests, integration tests (with fakeredis)
        Trade-offs: No circuit breaker, no adaptive timeout, no stats, no integrity (reproducible, fast)

        Note: Backend resolved from REDIS_URL env var, set_default_backend(), or explicit backend= kwarg

        Args:
            **kwargs: Overrides (ttl, namespace, backend, etc.)

        Returns:
            DecoratorConfig optimized for testing

        Example:
            >>> config = DecoratorConfig.test(ttl=10)
            >>> config.circuit_breaker.enabled
            False
            >>> config.integrity_checking
            False
        """
        return cls(
            integrity_checking=False,  # Testing: fast deterministic behavior
            l1=L1CacheConfig(
                enabled=True,
                swr_enabled=False,
                invalidation_enabled=False,
                namespace_index=False,
            ),
            circuit_breaker=CircuitBreakerConfig(enabled=False),
            timeout=TimeoutConfig(enabled=False),
            backpressure=BackpressureConfig(enabled=False),
            monitoring=MonitoringConfig(
                collect_stats=False,
                enable_tracing=False,
                enable_structured_logging=False,
                enable_prometheus_metrics=False,
            ),
            **kwargs,
        )
