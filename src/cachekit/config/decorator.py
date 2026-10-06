"""Unified immutable configuration for cache decorator.

Simple frozen dataclass with nested configuration groups and validation via __post_init__.
"""

from __future__ import annotations

import enum
import math
from collections.abc import Callable
from dataclasses import dataclass, field, fields
from typing import TYPE_CHECKING, Any, Literal, Union

from .nested import (
    BackpressureConfig,
    CircuitBreakerConfig,
    EncryptionConfig,
    L1CacheConfig,
    MonitoringConfig,
)
from .validation import ConfigurationError, hide_any_secret, refuse_bytes_key, reveal_secret

if TYPE_CHECKING:
    from pydantic import SecretStr

    from cachekit.backends.base import BaseBackend
    from cachekit.decorators.tenant_context import TenantContextExtractor
    from cachekit.serializers.base import SerializerProtocol


# Backend Resolution Layer


class _Unset(enum.Enum):
    """Type of UNSET. An enum member stays one object through copy, deepcopy and pickle, so ``is`` holds."""

    UNSET = "UNSET"

    def __repr__(self) -> str:
        return "UNSET"

    def __bool__(self) -> bool:
        # Falsy like the None it replaced as the default, so `if config.backend:` still reads "no backend".
        return False


# DecoratorConfig.backend's default: no backend stated, so it resolves per "Backend Resolution Priority".
# None is not the default because None states one: L1-only, the meaning backend=None has as a @cache keyword.
UNSET = _Unset.UNSET

# Keywords that carry a key. Each is wrapped before anything can raise, so no frame on an error's traceback holds
# one raw (CWE-532), including where it was passed by mistake.
_SECRET_KWARGS = frozenset({"master_key", "api_key"})

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


@dataclass(frozen=True)
class DecoratorConfig:
    """Unified immutable configuration for cache decorator.

    Intent-based presets with kwargs overrides for customization.
    - Frozen dataclass ensures immutability
    - Nested configs group related settings
    - Backend resolves per docs/backends/README.md, "Backend Resolution Priority": explicit backend=,
      then set_default_backend(), then one env selector at first call

    Examples:
        # Zero-config (backend auto-detected from the environment at first call)
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
        ttl: Time-to-live in seconds (None = no expiration; every preset sets its own default — see each classmethod)
        namespace: Optional namespace prefix for cache keys
        serializer: Serializer instance or name. Accepts either:
                   - String name: "default" (MessagePack), "arrow" (DataFrame zero-copy)
                   - SerializerProtocol instance: Custom serializer implementing the protocol
                   Default: "default" (MessagePack+LZ4+xxHash3-64 via Rust)
                   An EncryptionWrapper instance or the "encrypted" name raises ConfigurationError when
                   the decorator is applied: encrypt with secure(master_key=..., serializer=<inner>).
        integrity_checking: Enable checksums for corruption detection (default: True)
                           All serializers use xxHash3-64 (8 bytes).
                           Set to False for @cache.minimal (speed-first, no integrity guarantee)
        key: Custom key function for complex types. Receives (*args, **kwargs) and returns str.
             Use for numpy arrays, DataFrames, or cross-language cache sharing.
             Example: @cache(key=lambda arr: hashlib.blake2b(arr.tobytes()).hexdigest())
        refresh_ttl_on_get: Extend TTL on cache hit
        ttl_refresh_threshold: Minimum remaining TTL fraction (0.0-1.0) to trigger refresh
        backend: L2 backend (RedisBackend, HTTPBackend). None means L1-only (in-process, no network), as
                 @cache(backend=None) does. The default, UNSET, resolves per "Backend Resolution Priority".
        l1: L1 in-memory cache configuration
        circuit_breaker: Circuit breaker configuration
        backpressure: Backpressure configuration
        monitoring: Monitoring and observability configuration
        encryption: Client-side encryption configuration
    """

    # Core settings (6 fields)
    ttl: int | None = None
    # Stale-while-revalidate stale-grace window in seconds past the fresh TTL
    # (LAB-381, protocol spec/saas-api.md#stale-while-revalidate). Requires a
    # positive ttl and an SWR-capable backend (CachekitIO). None = preset
    # decides (io() defaults it to ttl via swr_by_default); 0 = explicitly off.
    stale_ttl: int | None = None
    # Preset flag: default stale_ttl to ttl (capped to the shared 30-day bound)
    # when the backend supports SWR and the user didn't say otherwise.
    swr_by_default: bool = False
    namespace: str | None = None
    serializer: Union[str, SerializerProtocol] = "default"  # type: ignore[assignment]  # String name or protocol instance
    integrity_checking: bool = True  # Checksums for corruption detection (xxHash3-64 for all serializers)
    key: Callable[..., str] | None = None  # Custom key function (escape hatch for complex types)
    # Interop mode (interop/v1): explicit cross-SDK operation name. Opting in switches
    # this function to {namespace}:{operation}:{args_hash} keys and plain-MessagePack
    # values shared byte-identically with cachekit-rs / cachekit-ts. None = auto mode.
    interop: str | None = None

    # Performance (2 fields)
    refresh_ttl_on_get: bool = False
    ttl_refresh_threshold: float = 0.5

    # Backend abstraction (1 field)
    backend: BaseBackend | None | Literal[_Unset.UNSET] = UNSET  # L2 backend; None = L1-only; UNSET = resolve

    # Nested configuration groups (5 groups)
    l1: L1CacheConfig = field(default_factory=L1CacheConfig)
    circuit_breaker: CircuitBreakerConfig = field(default_factory=CircuitBreakerConfig)
    backpressure: BackpressureConfig = field(default_factory=BackpressureConfig)
    monitoring: MonitoringConfig = field(default_factory=MonitoringConfig)
    encryption: EncryptionConfig = field(default_factory=EncryptionConfig)

    # Set only by io(), whose CachekitIOBackend is the preset: @cache(config=...) refuses a backend= beside such a
    # config, as @cache.io(backend=...) is refused. Matching on the backend's type instead would also refuse it beside
    # a production config that holds a CachekitIOBackend, where a backend= keyword wins. A field, so
    # dataclasses.replace() keeps it.
    _from_io: bool = field(default=False, repr=False)

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
        if self.ttl is not None:
            if not math.isfinite(self.ttl):
                raise ValueError(f"ttl must be a finite number, got {self.ttl!r}")
            if self.ttl < 0:
                raise ValueError(f"ttl must be non-negative, got {self.ttl}")

        # TTL refresh threshold validation
        if not 0.0 <= self.ttl_refresh_threshold <= 1.0:
            raise ConfigurationError(f"ttl_refresh_threshold must be 0.0-1.0, got {self.ttl_refresh_threshold}")

        # Under encryption integrity_checking becomes the AAD's `compressed` component, whose
        # tokens are frozen as exactly True / False (spec/encryption.md). A truthy 1 would seal
        # entries under the token "1", which no other SDK's reader builds.
        if not isinstance(self.integrity_checking, bool):  # pyright: ignore[reportUnnecessaryIsInstance] — runtime kwarg, untyped
            raise ConfigurationError(f"integrity_checking must be a bool, got {type(self.integrity_checking).__name__}")

        # Interop mode validation (interop/v1, spec/interop-mode.md): loud at
        # decoration time, never silently normalized.
        if self.interop is not None:
            from cachekit.interop import InteropError, validate_interop_config

            try:
                validate_interop_config(self.interop, self.namespace, has_custom_key=self.key is not None)
            except InteropError as e:
                raise ConfigurationError(str(e)) from e
            # Serializer and tenant_extractor constraints are enforced by their
            # single authority, CacheSerializationHandler.__init__ (which owns
            # the alias map and the encryption config) — it runs at decoration
            # time on every path, so the error still fires before first use.

        # `encryption=False` is the explicit opt-out on every preset (protocol intent-presets.md
        # § Encryption Activation), and `encryption=True` the opt-in, though on a preset it still needs a
        # tenant mode: EncryptionConfig(enabled=True, single_tenant_mode=True). The field is an
        # EncryptionConfig and dataclasses do not coerce:
        # without this, `@cache.production(encryption=False)` dies in `.validate()` with AttributeError.
        # Bare `@cache` flattens the bool earlier (decorators/intent.py); presets reach here with it raw.
        if isinstance(self.encryption, bool):  # pyright: ignore[reportUnnecessaryIsInstance] — runtime kwarg, untyped
            object.__setattr__(self, "encryption", EncryptionConfig(enabled=self.encryption))

        # cachekit.CircuitBreakerConfig (the top-level export) is the reliability class, which
        # has no .validate(); name the class this field takes instead of an opaque AttributeError.
        if not isinstance(self.circuit_breaker, CircuitBreakerConfig):  # pyright: ignore[reportUnnecessaryIsInstance]
            raise TypeError(
                "circuit_breaker must be a cachekit.config.nested.CircuitBreakerConfig, "
                f"got {type(self.circuit_breaker).__module__}.{type(self.circuit_breaker).__qualname__}"
            )

        # Validate nested configs
        self.l1.validate()
        self.circuit_breaker.validate()
        self.backpressure.validate()
        self.monitoring.validate()
        self.encryption.validate()

    def to_dict(self) -> dict[str, object]:
        """Convert to dictionary for backward compatibility during migration.

        This method will be removed in Task 6 when wrapper accepts DecoratorConfig directly.

        Lossy, for display only: never feed its values back into a DecoratorConfig. ``backend`` is None both
        for an omitted backend (UNSET, which resolves a backend) and for an explicit None (L1-only), so
        ``replace(config, backend=config.to_dict()["backend"])`` would turn the first into L1-only. To derive
        a config, call ``dataclasses.replace`` on the DecoratorConfig itself.

        Returns:
            Dictionary representation with flattened nested configs

        Example:
            >>> DecoratorConfig().to_dict()["backend"] is DecoratorConfig(backend=None).to_dict()["backend"] is None
            True
        """
        return {
            # Core fields
            "ttl": self.ttl,
            "namespace": self.namespace,
            "serializer": self.serializer,
            "key": self.key,
            "refresh_ttl_on_get": self.refresh_ttl_on_get,
            "ttl_refresh_threshold": self.ttl_refresh_threshold,
            "backend": None if self.backend is UNSET else self.backend,  # the legacy dict's "unset" was None
            # L1 cache (flattened)
            "l1_enabled": self.l1.enabled,
            "l1_max_size_mb": self.l1.max_size_mb,
            # Circuit breaker (flattened)
            "circuit_breaker": self.circuit_breaker.enabled,
            "failure_threshold": self.circuit_breaker.failure_threshold,
            "success_threshold": self.circuit_breaker.success_threshold,
            "recovery_timeout": self.circuit_breaker.recovery_timeout,
            "half_open_requests": self.circuit_breaker.half_open_requests,
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
            "master_key": "[REDACTED]" if self.encryption.master_key else None,
            "tenant_extractor": self.encryption.tenant_extractor,
        }

    # Intent Presets (Class Methods)
    # Each preset builds its defaults and lets the caller's kwargs win (``defaults | kwargs``): an explicit
    # argument MUST override the preset default (protocol intent-presets.md § Explicit Configuration, rule 1).
    # A keyword that names no field is a ConfigurationError, and so is one naming a field the preset fixes
    # (secure's encryption, io's backend): rule 2.

    @classmethod
    def minimal(cls, **kwargs: Any) -> DecoratorConfig:
        """Minimal protections profile: Maximum throughput, minimal overhead.

        Use cases: Read-heavy workloads, non-critical caching, high-performance scenarios
        Trade-offs: Circuit breaker disabled, no monitoring, NO integrity checking

        Note: Backend resolves per docs/backends/README.md, "Backend Resolution Priority": explicit
              backend=, then set_default_backend(), then one env selector at first call.

        Args:
            **kwargs: Overrides; each wins over the preset's value (ttl, namespace, backend,
                integrity_checking=True to opt-in, l1, circuit_breaker, backpressure, monitoring, etc.)
                Default ttl=300 (protocol/spec/intent-presets.md); ttl=None = never expire.

        Returns:
            DecoratorConfig with minimal protections preset

        Example:
            >>> config = DecoratorConfig.minimal()
            >>> config.ttl
            300
            >>> config.circuit_breaker.enabled
            False
            >>> config.integrity_checking
            False
            >>> DecoratorConfig.minimal(ttl=None).ttl is None  # explicit no-expiry opt-in
            True
        """
        _reject_unsupported("The minimal preset", kwargs)
        defaults: dict[str, Any] = {
            "ttl": 300,
            "integrity_checking": False,  # Speed-first: no checksum overhead
            "l1": L1CacheConfig(
                enabled=True,
                swr_enabled=False,
            ),
            "circuit_breaker": CircuitBreakerConfig(enabled=False),
            "backpressure": BackpressureConfig(enabled=True),
            "monitoring": MonitoringConfig(
                collect_stats=False,
                enable_tracing=False,
                enable_structured_logging=False,
                enable_prometheus_metrics=False,
            ),
        }
        return cls(**(defaults | kwargs))

    @classmethod
    def production(cls, **kwargs: Any) -> DecoratorConfig:
        """Production profile: All protections enabled, full observability, integrity checking ON.

        Use cases: Payment systems, APIs, production services, critical workloads
        Trade-offs: Additional latency from circuit breaker, monitoring, integrity validation

        Note: Backend resolves per docs/backends/README.md, "Backend Resolution Priority": explicit
              backend=, then set_default_backend(), then one env selector at first call.

        Args:
            **kwargs: Overrides; each wins over the preset's value (ttl, namespace, backend,
                integrity_checking, l1, circuit_breaker, backpressure, monitoring, etc.)
                Default ttl=600 (protocol/spec/intent-presets.md); ttl=None = never expire.

        Returns:
            DecoratorConfig with production-grade protections

        Example:
            >>> config = DecoratorConfig.production()
            >>> config.ttl
            600
            >>> config.circuit_breaker.enabled
            True
            >>> config.integrity_checking
            True
            >>> from cachekit.config.nested import CircuitBreakerConfig
            >>> DecoratorConfig.production(circuit_breaker=CircuitBreakerConfig(failure_threshold=3)).circuit_breaker.failure_threshold
            3
        """
        _reject_unsupported("The production preset", kwargs)
        defaults: dict[str, Any] = {
            "ttl": 600,
            "integrity_checking": True,  # Production: integrity guarantee
            "l1": L1CacheConfig(
                enabled=True,
                swr_enabled=True,
            ),
            "circuit_breaker": CircuitBreakerConfig(enabled=True),
            "backpressure": BackpressureConfig(enabled=True),
            "monitoring": MonitoringConfig(
                collect_stats=True,
                enable_tracing=True,
                enable_structured_logging=True,
                enable_prometheus_metrics=True,
            ),
        }
        return cls(**(defaults | kwargs))

    @classmethod
    def secure(
        cls, master_key: str | SecretStr, tenant_extractor: TenantContextExtractor | None = None, **kwargs: Any
    ) -> DecoratorConfig:
        """Security profile: Encryption REQUIRED, encrypted-at-rest everywhere, full audit trail, integrity NON-NEGOTIABLE.

        Use cases: PII, medical data, financial records and other regulated data (encryption can support
                   a compliance scope-reduction argument; it is not a compliance guarantee)
        Architecture: Both L1 and L2 store encrypted bytes (encrypt-at-rest everywhere)

        Note: .secure does not pin a backend; it resolves like every preset (docs/backends/README.md,
              "Backend Resolution Priority"). With REDIS_URL set and CACHEKIT_API_KEY unset, the
              encrypted values go to Redis. Pass backend= when a particular backend is required.
              backend=None is L1-only, which secure refuses at decoration with ConfigurationError:
              L1-only stores raw objects, which cannot be ciphertext.
        Note: integrity_checking is forced to True (non-negotiable for security)

        Args:
            master_key: Encryption master key (hex-encoded; use exactly 32 bytes, 64 hex characters)
            tenant_extractor: Optional tenant ID extractor (an object with .extract(args, kwargs)) for
                per-tenant key derivation. Not a tenancy boundary: see docs/features/zero-knowledge-encryption.md
            **kwargs: Overrides (ttl, namespace, backend, l1, circuit_breaker, backpressure, monitoring, etc.)
                     - integrity_checking other than True is rejected, and encryption= is not an override.
                     Default ttl=600 (protocol/spec/intent-presets.md); ttl=None = never expire.
                     fail_closed=True raises DecryptionAuthenticationError to the caller on AES-GCM
                     auth failure / key-fingerprint mismatch instead of silently recomputing
                     (default None defers to CACHEKIT_ENCRYPTION_FAIL_CLOSED, which defaults to False)

        Returns:
            DecoratorConfig with encryption enabled and full security features

        Raises:
            ConfigurationError: If an ``integrity_checking`` other than ``True``, ``encryption=`` or a keyword that names no
                field is passed.
            TypeError: If master_key is bytes: pass ``key.hex()``.

        Example:
            >>> config = DecoratorConfig.secure(master_key="a" * 64)
            >>> config.ttl
            600
            >>> config.encryption.enabled
            True
            >>> config.integrity_checking
            True
        """
        master_key = hide_any_secret(master_key)  # unwrapped only into the EncryptionConfig (CWE-532)
        _reject_unsupported("The secure preset", kwargs, _FIELD_NAMES | _SECURE_ENCRYPTION_KWARGS)
        master_key = refuse_bytes_key(master_key)  # after the check above has wrapped every refused keyword
        # encryption= is a field, but not one this preset takes: the EncryptionConfig below is the preset.
        if "encryption" in kwargs:
            raise ConfigurationError(
                "The secure preset sets its own encryption; encryption= cannot override it. Pass any of "
                f"{_SECURE_ENCRYPTION_OPTIONS} to it directly."
            )
        # The EncryptionConfig settings this preset takes through **kwargs, passed through by name. An omitted
        # deployment_uuid or fail_closed keeps EncryptionConfig's default (fail_closed=None defers to
        # CACHEKIT_ENCRYPTION_FAIL_CLOSED, default fail open); single_tenant_mode is resolved below.
        encryption_kwargs = {k: kwargs.pop(k) for k in _SECURE_ENCRYPTION_KWARGS if k in kwargs}

        # SECURITY INVARIANT: integrity_checking is forced to True. A request to turn it off is
        # rejected, never silently dropped (protocol intent-presets.md § Explicit Configuration).
        integrity_checking = kwargs.pop("integrity_checking", True)
        if integrity_checking is not True:
            raise ConfigurationError(
                f"The secure preset does not accept integrity_checking={integrity_checking!r} — it forces "
                "integrity checking on. Omit integrity_checking."
            )

        # Normalize empty string to None (security: empty string treated as single-tenant)
        tenant_extractor = tenant_extractor or None

        # Tenant mode: an explicit non-None single_tenant_mode wins; otherwise derived from tenant_extractor
        if encryption_kwargs.get("single_tenant_mode") is None:
            encryption_kwargs["single_tenant_mode"] = tenant_extractor is None

        defaults: dict[str, Any] = {
            "ttl": 600,
            "integrity_checking": True,  # NON-NEGOTIABLE for encryption (security invariant)
            "l1": L1CacheConfig(
                enabled=True,  # L1 stores encrypted bytes. Enabled: ~50ns hits vs 2-7ms Redis
                swr_enabled=True,
            ),
            "circuit_breaker": CircuitBreakerConfig(enabled=True),
            "backpressure": BackpressureConfig(enabled=True),
            "monitoring": MonitoringConfig(
                collect_stats=True,
                enable_tracing=True,
                enable_structured_logging=True,
                enable_prometheus_metrics=True,
            ),
        }
        # encryption= stays outside the merge, so no override can replace the preset's EncryptionConfig.
        return cls(
            encryption=EncryptionConfig(
                enabled=True,
                master_key=reveal_secret(master_key),
                tenant_extractor=tenant_extractor,
                **encryption_kwargs,
            ),
            **(defaults | kwargs),
        )

    @classmethod
    def dev(cls, **kwargs: Any) -> DecoratorConfig:
        """Development profile: Verbose logging, easy debugging, no Prometheus except circuit_breaker_state, integrity checking ON.

        Use cases: Local development, debugging production issues
        Trade-offs: Verbose logs, Prometheus metrics disabled for simplicity except circuit_breaker_state

        Note: Backend resolves per docs/backends/README.md, "Backend Resolution Priority": explicit
              backend=, then set_default_backend(), then one env selector at first call.

        Args:
            **kwargs: Overrides; each wins over the preset's value (ttl, namespace, backend,
                integrity_checking, l1, circuit_breaker, backpressure, monitoring, etc.)
                Default ttl=300 (SDK-local preset; spec rule 4 forbids never-expire as a default); ttl=None = never expire.

        Returns:
            DecoratorConfig optimized for development

        Example:
            >>> config = DecoratorConfig.dev()
            >>> config.ttl
            300
            >>> config.monitoring.enable_prometheus_metrics
            False
            >>> config.integrity_checking
            True
        """
        _reject_unsupported("The dev preset", kwargs)
        defaults: dict[str, Any] = {
            "ttl": 300,
            "integrity_checking": True,  # Development: catch data corruption early
            "l1": L1CacheConfig(
                enabled=True,
                swr_enabled=True,
            ),
            "circuit_breaker": CircuitBreakerConfig(enabled=True),
            "backpressure": BackpressureConfig(enabled=True),
            "monitoring": MonitoringConfig(
                collect_stats=True,
                enable_tracing=True,
                enable_structured_logging=True,
                enable_prometheus_metrics=False,
            ),
        }
        return cls(**(defaults | kwargs))

    @classmethod
    def test(cls, **kwargs: Any) -> DecoratorConfig:
        """Testing profile: Deterministic, all protections disabled, no monitoring, no integrity checking.

        Use cases: Unit tests, integration tests (with fakeredis)
        Trade-offs: No circuit breaker, no stats, no integrity (reproducible, fast)

        Note: Backend resolves per docs/backends/README.md, "Backend Resolution Priority": explicit
              backend=, then set_default_backend(), then one env selector at first call.

        Args:
            **kwargs: Overrides; each wins over the preset's value (ttl, namespace, backend,
                integrity_checking, l1, circuit_breaker, backpressure, monitoring, etc.)
                Default ttl=300 (SDK-local preset; spec rule 4 forbids never-expire as a default); ttl=None = never expire.

        Returns:
            DecoratorConfig optimized for testing

        Example:
            >>> config = DecoratorConfig.test()
            >>> config.ttl
            300
            >>> config.circuit_breaker.enabled
            False
            >>> config.integrity_checking
            False
        """
        _reject_unsupported("The test preset", kwargs)
        defaults: dict[str, Any] = {
            "ttl": 300,
            "integrity_checking": False,  # Testing: fast deterministic behavior
            "l1": L1CacheConfig(
                enabled=True,
                swr_enabled=False,
            ),
            "circuit_breaker": CircuitBreakerConfig(enabled=False),
            "backpressure": BackpressureConfig(enabled=False),
            "monitoring": MonitoringConfig(
                collect_stats=False,
                enable_tracing=False,
                enable_structured_logging=False,
                enable_prometheus_metrics=False,
            ),
        }
        return cls(**(defaults | kwargs))

    @classmethod
    def io(cls, api_key: str | SecretStr | None = None, **kwargs: Any) -> DecoratorConfig:
        """cachekit.io SaaS backend profile: HTTP-based caching via api.cachekit.io.

        Use cases: Zero-infrastructure caching, edge caching, multi-region deployments
        Features: Full L1+L2 caching, circuit breaker, production-grade reliability

        Credentials: ``api_key`` argument, falling back to the ``CACHEKIT_API_KEY``
        environment variable. An explicit argument wins, so one process can hold
        two keys (multi-tenant services, test suites). Neither present is a
        ConfigurationError here, at construction — never on the first cache call.
        ``CACHEKIT_API_URL`` overrides the endpoint (default: https://api.cachekit.io).

        Encryption: opt in explicitly with encryption=EncryptionConfig(enabled=True,
        single_tenant_mode=True, master_key=...); omit master_key to use CACHEKIT_MASTER_KEY.
        The env var never activates encryption: with it set and no encryption= stated,
        construction raises ConfigurationError — protocol intent-presets.md § Encryption Activation.

        Args:
            api_key: cachekit.io API key (``ck_live_...``). Default: ``CACHEKIT_API_KEY``.
            **kwargs: Overrides (ttl, namespace, integrity_checking, swr_by_default, l1, circuit_breaker,
                backpressure, monitoring, etc.). ``backend`` is not one — io always
                caches through its own CachekitIOBackend and rejects ``backend=``.
                Default ttl=3600 (protocol/spec/intent-presets.md); ttl=None = never expire
                (and disables the stale_ttl SWR window, which needs a positive ttl).

        Returns:
            DecoratorConfig with CachekitIOBackend

        Raises:
            ConfigurationError: If the API key is missing, empty or not an RFC 6750 bearer token
                (argument and CACHEKIT_API_KEY), if CACHEKIT_API_URL fails validation, or if
                ``backend=`` or a keyword that names no field is passed. ``@cache(config=...)``
                refuses a ``backend=`` beside the returned config too.

        Example:
            >>> config = DecoratorConfig.io(api_key="ck_test_key")  # pragma: allowlist secret
            >>> config.ttl
            3600
            >>> DecoratorConfig.io(api_key="ck_test_key", ttl=300).ttl  # pragma: allowlist secret
            300
        """
        api_key = hide_any_secret(api_key)  # passed down wrapped, a bytes key too (CWE-532)
        # Before the backend is built: a rejected call builds none, and a misspelt keyword is not masked by a missing key.
        _reject_unsupported("The io preset", kwargs)
        # Lazy import to avoid circular dependency and keep SaaS backend optional
        from cachekit.backends.cachekitio import CachekitIOBackend

        if "backend" in kwargs:
            raise ConfigurationError(
                "@cache.io does not accept backend= — it always caches through CachekitIOBackend.\n\n"
                "To cache through another backend, use a different preset:\n"
                "  @cache.production(backend=my_backend)"
            )

        # io() never reads the env itself: CachekitIOBackendConfig resolves api_key (argument wins,
        # else CACHEKIT_API_KEY) and raises ConfigurationError on a missing or empty key, at construction.
        backend = CachekitIOBackend(api_key=api_key)

        # Use production-grade settings with SaaS backend
        # Encryption is opt-in via encryption=EncryptionConfig(...); a key with no stated intent raises
        defaults: dict[str, Any] = {
            "ttl": 3600,
            "integrity_checking": True,
            # SWR default-on for the managed backend (LAB-381 design decision):
            # boundary requests serve stale + revalidate in the background.
            # stale_ttl resolves to ttl (capped) at wrap time; pass stale_ttl=0
            # to opt out, or an explicit value to size the window.
            "swr_by_default": True,
            "l1": L1CacheConfig(
                enabled=True,
                swr_enabled=True,
            ),
            "circuit_breaker": CircuitBreakerConfig(enabled=True),
            "backpressure": BackpressureConfig(enabled=True),
            "monitoring": MonitoringConfig(
                collect_stats=True,
                enable_tracing=True,
                enable_structured_logging=True,
                enable_prometheus_metrics=True,
            ),
        }
        return cls(
            backend=backend,
            _from_io=True,
            **(defaults | kwargs),
        )


# The keywords a preset or a config= override may name: DecoratorConfig's fields, less the private ones.
_FIELD_NAMES = frozenset(f.name for f in fields(DecoratorConfig) if not f.name.startswith("_"))
# The EncryptionConfig settings bare @cache and @cache.secure take as flat keywords. The single list: each form folds
# every name here into its EncryptionConfig, so an option added here reaches both.
_ENCRYPTION_FLAT_KWARGS = frozenset({"master_key", "tenant_extractor", "single_tenant_mode", "deployment_uuid", "fail_closed"})
# The EncryptionConfig settings DecoratorConfig.secure() takes through **kwargs: all but its named parameters.
_SECURE_ENCRYPTION_KWARGS = _ENCRYPTION_FLAT_KWARGS - {"master_key", "tenant_extractor"}
# The options secure()'s encryption= refusal names instead: every flat keyword but master_key.
_SECURE_ENCRYPTION_OPTIONS = ", ".join(f"{k}=" for k in sorted(_ENCRYPTION_FLAT_KWARGS - {"master_key"}))
# The keywords a @cache.<preset> decorator takes beside the fields: its classmethod's own parameters, and secure's above.
_PRESET_EXTRA_KWARGS = {
    "io": frozenset({"api_key"}),
    "secure": _ENCRYPTION_FLAT_KWARGS,
}


def _reject_unsupported(
    where: str,
    kwargs: dict[str, Any],
    accepted: frozenset[str] = _FIELD_NAMES,
    *,
    held_by: tuple[dict[str, Any], ...] = (),
) -> None:
    """Raise ConfigurationError naming every keyword in ``kwargs`` that ``accepted`` lacks.

    An unsupported argument is a configuration error, never the dataclass's TypeError and never a silent drop
    (protocol intent-presets.md § Explicit Configuration, rule 2). ``held_by`` lists the caller's other dicts that
    hold the same keywords.
    """
    unsupported = sorted(kwargs.keys() - accepted)
    if not unsupported:
        return
    # A key passed where none is taken is still a key, and a refused value may be one under a misspelt name
    # (master_keey=): wrap both in every dict a frame on the traceback holds before raising.
    hidden = _SECRET_KWARGS | set(unsupported)
    for mapping in (kwargs, *held_by):
        for name in mapping.keys() & hidden:
            mapping[name] = hide_any_secret(mapping[name])
    raise ConfigurationError(f"{where} does not accept {', '.join(unsupported)}.")
