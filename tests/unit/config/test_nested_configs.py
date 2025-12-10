"""Unit tests for nested configuration classes.

Tests all 6 nested config classes:
- L1CacheConfig: max_size_mb >= 1, defaults, frozen immutability
- CircuitBreakerConfig: thresholds >= 1, defaults
- TimeoutConfig: min <= initial <= max, 0 < percentile <= 100
- BackpressureConfig: max_concurrent >= 1
- MonitoringConfig: boolean flags, no validation constraints
- EncryptionConfig: master_key required if enabled, tenant mode mutual exclusivity
"""

from __future__ import annotations

import pytest

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
class TestL1CacheConfig:
    """Test L1CacheConfig validation and defaults."""

    def test_defaults(self) -> None:
        """Test default values."""
        config = L1CacheConfig()
        assert config.enabled is True
        assert config.max_size_mb == 100

    def test_custom_values(self) -> None:
        """Test custom configuration."""
        config = L1CacheConfig(enabled=False, max_size_mb=200)
        assert config.enabled is False
        assert config.max_size_mb == 200

    def test_frozen_immutability(self) -> None:
        """Test frozen dataclass prevents mutation."""
        config = L1CacheConfig()
        with pytest.raises(AttributeError, match="cannot assign to field"):
            config.enabled = False  # type: ignore[misc]

    def test_validate_success(self) -> None:
        """Test validation passes for valid config."""
        config = L1CacheConfig(max_size_mb=1)
        config.validate()  # Should not raise

    def test_validate_max_size_mb_too_small(self) -> None:
        """Test validation fails when max_size_mb < 1."""
        config = L1CacheConfig(max_size_mb=0)
        with pytest.raises(ConfigurationError, match="L1 max_size_mb must be >= 1, got 0"):
            config.validate()

    def test_validate_max_size_mb_negative(self) -> None:
        """Test validation fails for negative max_size_mb."""
        config = L1CacheConfig(max_size_mb=-10)
        with pytest.raises(ConfigurationError, match="L1 max_size_mb must be >= 1, got -10"):
            config.validate()


@pytest.mark.unit
class TestCircuitBreakerConfig:
    """Test CircuitBreakerConfig validation and defaults."""

    def test_defaults(self) -> None:
        """Test default values."""
        config = CircuitBreakerConfig()
        assert config.enabled is True
        assert config.failure_threshold == 5
        assert config.success_threshold == 3
        assert config.recovery_timeout == 30
        assert config.half_open_requests == 3
        assert config.excluded_exceptions == ()

    def test_custom_values(self) -> None:
        """Test custom configuration."""
        config = CircuitBreakerConfig(
            enabled=False,
            failure_threshold=10,
            success_threshold=5,
            recovery_timeout=60,
            half_open_requests=1,
            excluded_exceptions=(ValueError, KeyError),
        )
        assert config.enabled is False
        assert config.failure_threshold == 10
        assert config.success_threshold == 5
        assert config.recovery_timeout == 60
        assert config.half_open_requests == 1
        assert config.excluded_exceptions == (ValueError, KeyError)

    def test_frozen_immutability(self) -> None:
        """Test frozen dataclass prevents mutation."""
        config = CircuitBreakerConfig()
        with pytest.raises(AttributeError, match="cannot assign to field"):
            config.enabled = False  # type: ignore[misc]

    def test_validate_success(self) -> None:
        """Test validation passes for valid config."""
        config = CircuitBreakerConfig(failure_threshold=1, success_threshold=1, half_open_requests=1)
        config.validate()  # Should not raise

    def test_validate_failure_threshold_too_small(self) -> None:
        """Test validation fails when failure_threshold < 1."""
        config = CircuitBreakerConfig(failure_threshold=0)
        with pytest.raises(ConfigurationError, match="failure_threshold must be >= 1, got 0"):
            config.validate()

    def test_validate_success_threshold_too_small(self) -> None:
        """Test validation fails when success_threshold < 1."""
        config = CircuitBreakerConfig(success_threshold=0)
        with pytest.raises(ConfigurationError, match="success_threshold must be >= 1, got 0"):
            config.validate()

    def test_validate_half_open_requests_too_small(self) -> None:
        """Test validation fails when half_open_requests < 1."""
        config = CircuitBreakerConfig(half_open_requests=0)
        with pytest.raises(ConfigurationError, match="half_open_requests must be >= 1, got 0"):
            config.validate()


@pytest.mark.unit
class TestTimeoutConfig:
    """Test TimeoutConfig validation and defaults."""

    def test_defaults(self) -> None:
        """Test default values."""
        config = TimeoutConfig()
        assert config.enabled is True
        assert config.initial == 1.0
        assert config.min == 0.1
        assert config.max == 5.0
        assert config.window_size == 1000
        assert config.percentile == 95.0

    def test_custom_values(self) -> None:
        """Test custom configuration."""
        config = TimeoutConfig(enabled=False, initial=2.0, min=0.5, max=10.0, window_size=500, percentile=99.0)
        assert config.enabled is False
        assert config.initial == 2.0
        assert config.min == 0.5
        assert config.max == 10.0
        assert config.window_size == 500
        assert config.percentile == 99.0

    def test_frozen_immutability(self) -> None:
        """Test frozen dataclass prevents mutation."""
        config = TimeoutConfig()
        with pytest.raises(AttributeError, match="cannot assign to field"):
            config.enabled = False  # type: ignore[misc]

    def test_validate_success(self) -> None:
        """Test validation passes for valid config."""
        config = TimeoutConfig(min=0.1, initial=1.0, max=5.0, percentile=95.0)
        config.validate()  # Should not raise

    def test_validate_initial_below_min(self) -> None:
        """Test validation fails when initial < min."""
        config = TimeoutConfig(min=1.0, initial=0.5, max=5.0)
        with pytest.raises(ConfigurationError, match="min .* <= initial .* <= max"):
            config.validate()

    def test_validate_initial_above_max(self) -> None:
        """Test validation fails when initial > max."""
        config = TimeoutConfig(min=0.1, initial=10.0, max=5.0)
        with pytest.raises(ConfigurationError, match="min .* <= initial .* <= max"):
            config.validate()

    def test_validate_percentile_zero(self) -> None:
        """Test validation fails when percentile = 0."""
        config = TimeoutConfig(percentile=0.0)
        with pytest.raises(ConfigurationError, match="percentile must be 0.0-100.0, got 0.0"):
            config.validate()

    def test_validate_percentile_negative(self) -> None:
        """Test validation fails when percentile < 0."""
        config = TimeoutConfig(percentile=-5.0)
        with pytest.raises(ConfigurationError, match="percentile must be 0.0-100.0, got -5.0"):
            config.validate()

    def test_validate_percentile_above_100(self) -> None:
        """Test validation fails when percentile > 100."""
        config = TimeoutConfig(percentile=101.0)
        with pytest.raises(ConfigurationError, match="percentile must be 0.0-100.0, got 101.0"):
            config.validate()

    def test_validate_percentile_boundary_valid(self) -> None:
        """Test validation passes at percentile boundary (0 < p <= 100)."""
        config = TimeoutConfig(percentile=100.0)
        config.validate()  # Should not raise

        config = TimeoutConfig(percentile=0.01)
        config.validate()  # Should not raise


@pytest.mark.unit
class TestBackpressureConfig:
    """Test BackpressureConfig validation and defaults."""

    def test_defaults(self) -> None:
        """Test default values."""
        config = BackpressureConfig()
        assert config.enabled is True
        assert config.max_concurrent_requests == 100
        assert config.queue_size == 1000
        assert config.timeout == 0.1

    def test_custom_values(self) -> None:
        """Test custom configuration."""
        config = BackpressureConfig(enabled=False, max_concurrent_requests=50, queue_size=500, timeout=0.5)
        assert config.enabled is False
        assert config.max_concurrent_requests == 50
        assert config.queue_size == 500
        assert config.timeout == 0.5

    def test_frozen_immutability(self) -> None:
        """Test frozen dataclass prevents mutation."""
        config = BackpressureConfig()
        with pytest.raises(AttributeError, match="cannot assign to field"):
            config.enabled = False  # type: ignore[misc]

    def test_validate_success(self) -> None:
        """Test validation passes for valid config."""
        config = BackpressureConfig(max_concurrent_requests=1)
        config.validate()  # Should not raise

    def test_validate_max_concurrent_requests_too_small(self) -> None:
        """Test validation fails when max_concurrent_requests < 1."""
        config = BackpressureConfig(max_concurrent_requests=0)
        with pytest.raises(ConfigurationError, match="max_concurrent_requests must be >= 1, got 0"):
            config.validate()

    def test_validate_max_concurrent_requests_negative(self) -> None:
        """Test validation fails for negative max_concurrent_requests."""
        config = BackpressureConfig(max_concurrent_requests=-10)
        with pytest.raises(ConfigurationError, match="max_concurrent_requests must be >= 1, got -10"):
            config.validate()


@pytest.mark.unit
class TestMonitoringConfig:
    """Test MonitoringConfig defaults (no validation constraints)."""

    def test_defaults(self) -> None:
        """Test default values."""
        config = MonitoringConfig()
        assert config.collect_stats is True
        assert config.enable_tracing is True
        assert config.enable_structured_logging is True
        assert config.enable_prometheus_metrics is True

    def test_custom_values(self) -> None:
        """Test custom configuration."""
        config = MonitoringConfig(
            collect_stats=False,
            enable_tracing=False,
            enable_structured_logging=False,
            enable_prometheus_metrics=False,
        )
        assert config.collect_stats is False
        assert config.enable_tracing is False
        assert config.enable_structured_logging is False
        assert config.enable_prometheus_metrics is False

    def test_frozen_immutability(self) -> None:
        """Test frozen dataclass prevents mutation."""
        config = MonitoringConfig()
        with pytest.raises(AttributeError, match="cannot assign to field"):
            config.collect_stats = False  # type: ignore[misc]

    def test_validate_no_constraints(self) -> None:
        """Test validation passes unconditionally (no constraints)."""
        config = MonitoringConfig()
        config.validate()  # Should not raise

        config = MonitoringConfig(collect_stats=False, enable_tracing=False)
        config.validate()  # Should not raise


@pytest.mark.unit
class TestEncryptionConfig:
    """Test EncryptionConfig validation (master_key + tenant mode)."""

    def test_defaults(self) -> None:
        """Test default values."""
        config = EncryptionConfig()
        assert config.enabled is False
        assert config.master_key is None
        assert config.tenant_extractor is None
        assert config.single_tenant_mode is False
        assert config.deployment_uuid is None

    def test_custom_values(self) -> None:
        """Test custom configuration."""

        def tenant_extractor() -> str:
            return "tenant-123"

        config = EncryptionConfig(
            enabled=True,
            master_key="a" * 64,
            tenant_extractor=tenant_extractor,
            single_tenant_mode=False,
            deployment_uuid="uuid-123",
        )
        assert config.enabled is True
        assert config.master_key == "a" * 64
        assert config.tenant_extractor is tenant_extractor
        assert config.single_tenant_mode is False
        assert config.deployment_uuid == "uuid-123"

    def test_frozen_immutability(self) -> None:
        """Test frozen dataclass prevents mutation."""
        config = EncryptionConfig()
        with pytest.raises(AttributeError, match="cannot assign to field"):
            config.enabled = True  # type: ignore[misc]

    def test_validate_disabled_no_error(self) -> None:
        """Test validation passes when encryption disabled."""
        config = EncryptionConfig(enabled=False)
        config.validate()  # Should not raise

    def test_validate_enabled_without_master_key(self) -> None:
        """Test validation fails when enabled=True but master_key=None."""
        config = EncryptionConfig(enabled=True, master_key=None, single_tenant_mode=True)
        with pytest.raises(ConfigurationError, match="encryption.enabled=True requires encryption.master_key"):
            config.validate()

    def test_validate_single_tenant_mode_success(self) -> None:
        """Test validation passes for single-tenant mode."""
        config = EncryptionConfig(enabled=True, master_key="a" * 64, single_tenant_mode=True)
        config.validate()  # Should not raise

    def test_validate_multi_tenant_mode_success(self) -> None:
        """Test validation passes for multi-tenant mode."""

        def tenant_extractor() -> str:
            return "tenant-123"

        config = EncryptionConfig(enabled=True, master_key="a" * 64, tenant_extractor=tenant_extractor)
        config.validate()  # Should not raise

    def test_validate_no_tenant_mode_specified(self) -> None:
        """Test validation fails when neither tenant mode is specified."""
        config = EncryptionConfig(enabled=True, master_key="a" * 64)
        with pytest.raises(ConfigurationError, match="Encryption requires explicit tenant mode"):
            config.validate()

    def test_validate_both_tenant_modes_specified(self) -> None:
        """Test validation fails when both tenant modes are specified."""

        def tenant_extractor() -> str:
            return "tenant-123"

        config = EncryptionConfig(
            enabled=True,
            master_key="a" * 64,
            tenant_extractor=tenant_extractor,
            single_tenant_mode=True,
        )
        with pytest.raises(ConfigurationError, match="Cannot use both tenant_extractor and single_tenant_mode"):
            config.validate()

    def test_validate_with_deployment_uuid(self) -> None:
        """Test validation passes with deployment_uuid in single-tenant mode."""
        config = EncryptionConfig(
            enabled=True,
            master_key="a" * 64,
            single_tenant_mode=True,
            deployment_uuid="deployment-uuid-123",
        )
        config.validate()  # Should not raise
