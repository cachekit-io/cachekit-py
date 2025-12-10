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

from cachekit.config.decorator import DecoratorConfig
from cachekit.config.nested import (
    BackpressureConfig,
    CircuitBreakerConfig,
    EncryptionConfig,
    L1CacheConfig,
    MonitoringConfig,
    TimeoutConfig,
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
        assert config.safe_mode is False

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
        assert isinstance(config.timeout, TimeoutConfig)
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

    def test_validate_delegates_to_timeout_config(self) -> None:
        """Test validation delegates to TimeoutConfig."""
        with pytest.raises(ConfigurationError, match="min .* <= initial .* <= max"):
            DecoratorConfig(timeout=TimeoutConfig(min=5.0, initial=1.0, max=10.0))

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
        config = DecoratorConfig(ttl=300, namespace="test", serializer="msgpack", safe_mode=True)
        d = config.to_dict()
        assert d["ttl"] == 300
        assert d["namespace"] == "test"
        assert d["serializer"] == "msgpack"
        assert d["safe_mode"] is True

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
                excluded_exceptions=(ValueError,),
            )
        )
        d = config.to_dict()
        assert d["circuit_breaker"] is True
        assert d["failure_threshold"] == 10
        assert d["success_threshold"] == 5
        assert d["recovery_timeout"] == 60
        assert d["half_open_requests"] == 2
        assert d["excluded_exceptions"] == (ValueError,)

    def test_to_dict_flattens_timeout_config(self) -> None:
        """Test to_dict() flattens TimeoutConfig."""
        config = DecoratorConfig(
            timeout=TimeoutConfig(enabled=True, initial=2.0, min=0.5, max=10.0, window_size=500, percentile=99.0)
        )
        d = config.to_dict()
        assert d["adaptive_timeout"] is True
        assert d["initial_timeout"] == 2.0
        assert d["min_timeout"] == 0.5
        assert d["max_timeout"] == 10.0
        assert d["timeout_window_size"] == 500
        assert d["timeout_percentile"] == 99.0

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
        assert d["master_key"] == "a" * 64
        assert d["tenant_extractor"] is tenant_extractor

    def test_to_dict_complete_config(self) -> None:
        """Test to_dict() on complete config with all fields."""
        config = DecoratorConfig(
            ttl=600,
            namespace="prod",
            serializer="msgpack",
            safe_mode=True,
            refresh_ttl_on_get=True,
            ttl_refresh_threshold=0.8,
            backend=None,
            l1=L1CacheConfig(enabled=True, max_size_mb=150),
            circuit_breaker=CircuitBreakerConfig(enabled=True, failure_threshold=3),
            timeout=TimeoutConfig(enabled=True, initial=1.5),
            backpressure=BackpressureConfig(enabled=True, max_concurrent_requests=75),
            monitoring=MonitoringConfig(collect_stats=True, enable_prometheus_metrics=False),
            encryption=EncryptionConfig(enabled=True, master_key="a" * 64, single_tenant_mode=True),
        )
        d = config.to_dict()

        # Core fields
        assert d["ttl"] == 600
        assert d["namespace"] == "prod"
        assert d["serializer"] == "msgpack"
        assert d["safe_mode"] is True

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
        assert d["adaptive_timeout"] is True
        assert d["initial_timeout"] == 1.5
        assert d["backpressure"] is True
        assert d["max_concurrent_requests"] == 75
        assert d["collect_stats"] is True
        assert d["enable_prometheus_metrics"] is False
        assert d["encryption"] is True
        assert d["master_key"] == "a" * 64
