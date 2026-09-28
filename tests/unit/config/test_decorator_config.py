"""Unit tests for DecoratorConfig core functionality.

Tests DecoratorConfig:
- __post_init__ validation (ttl_refresh_threshold 0.0-1.0)
- Nested config validation delegation
- Frozen immutability
- Defaults
- to_dict() method (temporary backward compatibility)
"""

from __future__ import annotations

import pytest

from cachekit import cache
from cachekit.backends.cachekitio import CachekitIOBackend
from cachekit.config.decorator import DecoratorConfig
from cachekit.config.nested import (
    BackpressureConfig,
    CircuitBreakerConfig,
    EncryptionConfig,
    L1CacheConfig,
    MonitoringConfig,
)
from cachekit.config.validation import ConfigurationError


@pytest.mark.unit
class TestDecoratorConfigDefaults:
    """Test DecoratorConfig default values."""

    def test_core_defaults(self) -> None:
        """Test core field defaults."""
        config = DecoratorConfig()
        assert config.ttl is None
        assert config.namespace is None
        assert config.serializer == "default"

    def test_performance_defaults(self) -> None:
        """Test performance field defaults."""
        config = DecoratorConfig()
        assert config.refresh_ttl_on_get is False
        assert config.ttl_refresh_threshold == 0.5

    def test_backend_default(self) -> None:
        """Test backend field default."""
        config = DecoratorConfig()
        assert config.backend is None

    def test_nested_config_defaults(self) -> None:
        """Test nested config groups use default factory."""
        config = DecoratorConfig()
        assert isinstance(config.l1, L1CacheConfig)
        assert isinstance(config.circuit_breaker, CircuitBreakerConfig)
        assert isinstance(config.backpressure, BackpressureConfig)
        assert isinstance(config.monitoring, MonitoringConfig)
        assert isinstance(config.encryption, EncryptionConfig)


@pytest.mark.unit
class TestDecoratorConfigFrozen:
    """Test frozen dataclass immutability."""

    def test_frozen_core_field(self) -> None:
        """Test frozen dataclass prevents mutation of core fields."""
        config = DecoratorConfig()
        with pytest.raises(AttributeError, match="cannot assign to field"):
            config.ttl = 100  # type: ignore[misc]

    def test_frozen_nested_config(self) -> None:
        """Test frozen dataclass prevents mutation of nested configs."""
        config = DecoratorConfig()
        with pytest.raises(AttributeError, match="cannot assign to field"):
            config.l1 = L1CacheConfig(enabled=False)  # type: ignore[misc]


@pytest.mark.unit
class TestDecoratorConfigValidation:
    """Test DecoratorConfig validation logic."""

    def test_validate_ttl_refresh_threshold_valid(self) -> None:
        """Test validation passes for valid ttl_refresh_threshold (0.0-1.0)."""
        config = DecoratorConfig(ttl_refresh_threshold=0.0)
        assert config.ttl_refresh_threshold == 0.0

        config = DecoratorConfig(ttl_refresh_threshold=0.5)
        assert config.ttl_refresh_threshold == 0.5

        config = DecoratorConfig(ttl_refresh_threshold=1.0)
        assert config.ttl_refresh_threshold == 1.0

    def test_validate_ttl_refresh_threshold_negative(self) -> None:
        """Test validation fails for negative ttl_refresh_threshold."""
        with pytest.raises(ConfigurationError, match="ttl_refresh_threshold must be 0.0-1.0, got -0.1"):
            DecoratorConfig(ttl_refresh_threshold=-0.1)

    @pytest.mark.parametrize("bad_ttl", [float("nan"), float("inf"), float("-inf")])
    def test_non_finite_ttl_rejected(self, bad_ttl: float) -> None:
        """Non-finite TTL (NaN/inf) must fail validation (#158: would create an immortal entry)."""
        with pytest.raises(ValueError, match="finite"):
            DecoratorConfig(ttl=bad_ttl)

    def test_validate_ttl_refresh_threshold_above_one(self) -> None:
        """Test validation fails for ttl_refresh_threshold > 1.0."""
        with pytest.raises(ConfigurationError, match="ttl_refresh_threshold must be 0.0-1.0, got 1.5"):
            DecoratorConfig(ttl_refresh_threshold=1.5)

    def test_validate_delegates_to_l1_config(self) -> None:
        """Test validation delegates to L1CacheConfig."""
        with pytest.raises(ConfigurationError, match="L1 max_size_mb must be >= 1, got 0"):
            DecoratorConfig(l1=L1CacheConfig(max_size_mb=0))

    def test_validate_delegates_to_circuit_breaker_config(self) -> None:
        """Test validation delegates to CircuitBreakerConfig."""
        with pytest.raises(ConfigurationError, match="failure_threshold must be >= 1, got 0"):
            DecoratorConfig(circuit_breaker=CircuitBreakerConfig(failure_threshold=0))

    def test_top_level_circuit_breaker_config_rejected_naming_the_nested_class(self) -> None:
        """cachekit.CircuitBreakerConfig is the reliability class; circuit_breaker= takes the nested one (LAB-5340)."""
        import cachekit

        with pytest.raises(TypeError, match=r"cachekit\.config\.nested\.CircuitBreakerConfig"):
            DecoratorConfig(circuit_breaker=cachekit.CircuitBreakerConfig(failure_threshold=1))  # type: ignore[arg-type]

    def test_validate_delegates_to_backpressure_config(self) -> None:
        """Test validation delegates to BackpressureConfig."""
        with pytest.raises(ConfigurationError, match="max_concurrent_requests must be >= 1, got 0"):
            DecoratorConfig(backpressure=BackpressureConfig(max_concurrent_requests=0))

    def test_validate_delegates_to_monitoring_config(self) -> None:
        """Test validation delegates to MonitoringConfig (no constraints)."""
        config = DecoratorConfig(monitoring=MonitoringConfig(collect_stats=False))
        assert config.monitoring.collect_stats is False

    def test_validate_delegates_to_encryption_config(self) -> None:
        """Test validation delegates to EncryptionConfig."""
        with pytest.raises(ConfigurationError, match="encryption.enabled=True requires encryption.master_key"):
            DecoratorConfig(encryption=EncryptionConfig(enabled=True, single_tenant_mode=True))


@pytest.mark.unit
class TestDecoratorConfigToDict:
    """Test to_dict() method for backward compatibility."""

    def test_to_dict_core_fields(self) -> None:
        """Test to_dict() includes core fields."""
        config = DecoratorConfig(ttl=300, namespace="test", serializer="msgpack")
        d = config.to_dict()
        assert d["ttl"] == 300
        assert d["namespace"] == "test"
        assert d["serializer"] == "msgpack"

    def test_to_dict_performance_fields(self) -> None:
        """Test to_dict() includes performance fields."""
        config = DecoratorConfig(refresh_ttl_on_get=True, ttl_refresh_threshold=0.8)
        d = config.to_dict()
        assert d["refresh_ttl_on_get"] is True
        assert d["ttl_refresh_threshold"] == 0.8

    def test_to_dict_backend_field(self) -> None:
        """Test to_dict() includes backend field."""
        config = DecoratorConfig(backend=None)
        d = config.to_dict()
        assert d["backend"] is None

    def test_to_dict_flattens_l1_config(self) -> None:
        """Test to_dict() flattens L1CacheConfig."""
        config = DecoratorConfig(l1=L1CacheConfig(enabled=False, max_size_mb=200))
        d = config.to_dict()
        assert d["l1_enabled"] is False
        assert d["l1_max_size_mb"] == 200

    def test_to_dict_flattens_circuit_breaker_config(self) -> None:
        """Test to_dict() flattens CircuitBreakerConfig."""
        config = DecoratorConfig(
            circuit_breaker=CircuitBreakerConfig(
                enabled=True,
                failure_threshold=10,
                success_threshold=5,
                recovery_timeout=60,
                half_open_requests=2,
            )
        )
        d = config.to_dict()
        assert d["circuit_breaker"] is True
        assert d["failure_threshold"] == 10
        assert d["success_threshold"] == 5
        assert d["recovery_timeout"] == 60
        assert d["half_open_requests"] == 2
        assert "excluded_exceptions" not in d

    def test_to_dict_flattens_backpressure_config(self) -> None:
        """Test to_dict() flattens BackpressureConfig."""
        config = DecoratorConfig(
            backpressure=BackpressureConfig(enabled=True, max_concurrent_requests=50, queue_size=500, timeout=0.5)
        )
        d = config.to_dict()
        assert d["backpressure"] is True
        assert d["max_concurrent_requests"] == 50
        assert d["queue_size"] == 500
        assert d["backpressure_timeout"] == 0.5

    def test_to_dict_flattens_monitoring_config(self) -> None:
        """Test to_dict() flattens MonitoringConfig."""
        config = DecoratorConfig(
            monitoring=MonitoringConfig(
                collect_stats=False,
                enable_tracing=False,
                enable_structured_logging=False,
                enable_prometheus_metrics=False,
            )
        )
        d = config.to_dict()
        assert d["collect_stats"] is False
        assert d["enable_tracing"] is False
        assert d["enable_structured_logging"] is False
        assert d["enable_prometheus_metrics"] is False

    def test_to_dict_flattens_encryption_config(self) -> None:
        """Test to_dict() flattens EncryptionConfig."""

        def tenant_extractor() -> str:
            return "tenant-123"

        config = DecoratorConfig(
            encryption=EncryptionConfig(enabled=True, master_key="a" * 64, tenant_extractor=tenant_extractor)
        )
        d = config.to_dict()
        assert d["encryption"] is True
        assert d["master_key"] == "[REDACTED]"  # master_key masked in to_dict (CWE-200)
        assert d["tenant_extractor"] is tenant_extractor

    def test_to_dict_complete_config(self) -> None:
        """Test to_dict() on complete config with all fields."""
        config = DecoratorConfig(
            ttl=600,
            namespace="prod",
            serializer="msgpack",
            refresh_ttl_on_get=True,
            ttl_refresh_threshold=0.8,
            backend=None,
            l1=L1CacheConfig(enabled=True, max_size_mb=150),
            circuit_breaker=CircuitBreakerConfig(enabled=True, failure_threshold=3),
            backpressure=BackpressureConfig(enabled=True, max_concurrent_requests=75),
            monitoring=MonitoringConfig(collect_stats=True, enable_prometheus_metrics=False),
            encryption=EncryptionConfig(enabled=True, master_key="a" * 64, single_tenant_mode=True),
        )
        d = config.to_dict()

        # Core fields
        assert d["ttl"] == 600
        assert d["namespace"] == "prod"
        assert d["serializer"] == "msgpack"

        # Performance
        assert d["refresh_ttl_on_get"] is True
        assert d["ttl_refresh_threshold"] == 0.8

        # Backend
        assert d["backend"] is None

        # Nested configs
        assert d["l1_enabled"] is True
        assert d["l1_max_size_mb"] == 150
        assert d["circuit_breaker"] is True
        assert d["failure_threshold"] == 3
        assert d["backpressure"] is True
        assert d["max_concurrent_requests"] == 75
        assert d["collect_stats"] is True
        assert d["enable_prometheus_metrics"] is False
        assert d["encryption"] is True
        assert d["master_key"] == "[REDACTED]"  # masked (CWE-200)


@pytest.mark.unit
class TestIoPreset:
    """DecoratorConfig.io / @cache.io credentials and argument rejection (LAB-4643).

    Contract (protocol spec, intent-presets.md § io Credentials / § Explicit Configuration):
    api_key argument OR CACHEKIT_API_KEY, argument wins, neither -> ConfigurationError at
    construction; an unsupported argument (backend=) is rejected, never silently dropped.
    """

    @staticmethod
    def _key_of(config: DecoratorConfig) -> str:
        assert isinstance(config.backend, CachekitIOBackend)
        return config.backend._config.api_key.get_secret_value()

    def test_api_key_argument_builds_backend_with_that_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("CACHEKIT_API_KEY", raising=False)
        assert self._key_of(DecoratorConfig.io(api_key="ck_arg")) == "ck_arg"  # pragma: allowlist secret

    def test_api_key_argument_beats_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CACHEKIT_API_KEY", "ck_env")  # pragma: allowlist secret
        assert self._key_of(DecoratorConfig.io(api_key="ck_arg")) == "ck_arg"  # pragma: allowlist secret

    def test_env_fallback_when_no_argument(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CACHEKIT_API_KEY", "ck_env")  # pragma: allowlist secret
        assert self._key_of(DecoratorConfig.io()) == "ck_env"

    def test_env_key_resolves_exactly_as_the_backend_does(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Regression: io() read CACHEKIT_API_KEY itself, case-sensitively, and rejected a key the
        backend's case-insensitive pydantic-settings config would have loaded."""
        monkeypatch.delenv("CACHEKIT_API_KEY", raising=False)
        monkeypatch.setenv("cachekit_api_key", "ck_env")  # pragma: allowlist secret
        assert self._key_of(DecoratorConfig.io()) == "ck_env"

    def test_missing_both_raises_at_construction(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("CACHEKIT_API_KEY", raising=False)
        with pytest.raises(ConfigurationError, match=r"api_key=.*CACHEKIT_API_KEY"):
            DecoratorConfig.io()

    def test_empty_argument_is_an_error_not_an_env_fallback(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """api_key=settings.tenant_key yielding "" must not silently cache under the env tenant's key."""
        monkeypatch.setenv("CACHEKIT_API_KEY", "ck_env")  # pragma: allowlist secret
        with pytest.raises(ConfigurationError, match="requires an API key"):
            DecoratorConfig.io(api_key="")

    def test_two_keys_in_one_process_reach_the_wire_separately(self) -> None:
        """Regression: the per-thread HTTP client was first-wins, so a second key's backend
        carried the right _config but every request left under the FIRST key's header."""
        a = DecoratorConfig.io(api_key="ck_tenant_a").backend  # pragma: allowlist secret
        b = DecoratorConfig.io(api_key="ck_tenant_b").backend  # pragma: allowlist secret
        assert isinstance(a, CachekitIOBackend) and isinstance(b, CachekitIOBackend)
        assert a._sync_client.headers["authorization"] == "Bearer ck_tenant_a"
        assert b._sync_client.headers["authorization"] == "Bearer ck_tenant_b"
        assert b._async_client.headers["authorization"] == "Bearer ck_tenant_b"

    @pytest.mark.parametrize("backend", [None, object()], ids=["none", "instance"])
    def test_backend_kwarg_rejected(self, backend: object) -> None:
        with pytest.raises(ConfigurationError, match="does not accept backend="):
            DecoratorConfig.io(api_key="ck_arg", backend=backend)  # pragma: allowlist secret

    def test_decorator_api_key_reaches_backend(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """@cache.io(api_key=...) hands the key to CachekitIOBackend (the decorator path, not just the classmethod)."""
        monkeypatch.setenv("CACHEKIT_API_KEY", "ck_env")  # pragma: allowlist secret
        seen: dict[str, object] = {}

        class Spy(CachekitIOBackend):
            def __init__(self, **kwargs: object) -> None:
                seen.update(kwargs)
                super().__init__(**kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr("cachekit.backends.cachekitio.CachekitIOBackend", Spy)

        @cache.io(api_key="ck_arg")  # pragma: allowlist secret
        def fn() -> int:
            return 1

        assert seen["api_key"] == "ck_arg"  # pragma: allowlist secret

    def test_decorator_config_kwarg_rejected(self) -> None:
        """config= would swap the whole preset (and its backend) in silently — reject it like backend=."""
        with pytest.raises(ConfigurationError, match="does not accept config="):

            @cache.io(config=DecoratorConfig.production(backend=None))
            def fn() -> int:
                return 1

    @pytest.mark.parametrize("backend", [None, object()], ids=["none", "instance"])
    def test_decorator_backend_kwarg_rejected(self, backend: object) -> None:
        """@cache.io(backend=...) is a ConfigurationError at decoration, not a silent drop."""
        with pytest.raises(ConfigurationError, match="does not accept backend="):

            @cache.io(api_key="ck_arg", backend=backend)  # pragma: allowlist secret
            def fn() -> int:
                return 1
