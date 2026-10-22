"""Unit tests for nested configuration classes.

Tests all 5 nested config classes:
- L1CacheConfig: max_size_mb >= 1, defaults, frozen immutability
- CircuitBreakerConfig: thresholds >= 1, defaults
- BackpressureConfig: max_concurrent >= 1
- MonitoringConfig: boolean flags, no validation constraints
- EncryptionConfig: master_key required if enabled, tenant mode mutual exclusivity
"""

from __future__ import annotations

import threading
from decimal import Decimal
from fractions import Fraction

import pytest

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
class TestL1CacheConfig:
    """Test L1CacheConfig validation and defaults."""

    def test_defaults(self) -> None:
        """Test default values."""
        config = L1CacheConfig()
        assert config.enabled is True
        assert config.max_size_mb is None  # None inherits CACHEKIT_L1_MAX_SIZE_MB (issue #163)

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
        """max_size_mb=1, the smallest budget, passes the validation DecoratorConfig runs and is kept as given."""
        config = L1CacheConfig(max_size_mb=1)
        assert DecoratorConfig(l1=config).l1 is config

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

    def test_swr_retry_interval_default_is_non_zero(self) -> None:
        assert L1CacheConfig().swr_retry_interval == 10.0

    def test_swr_retry_interval_zero_is_valid(self) -> None:
        config = L1CacheConfig(swr_retry_interval=0)  # 0 = retry on the next stale read
        assert DecoratorConfig(l1=config).l1.swr_retry_interval == 0

    @pytest.mark.parametrize("bad", [-1, -0.5, float("nan")])
    def test_validate_swr_retry_interval_negative_or_nan(self, bad: float) -> None:
        with pytest.raises(ConfigurationError, match="L1 swr_retry_interval must be >= 0"):
            L1CacheConfig(swr_retry_interval=bad).validate()


@pytest.mark.unit
class TestCircuitBreakerConfig:
    """Test CircuitBreakerConfig validation and defaults."""

    def test_defaults(self) -> None:
        """Test default values."""
        config = CircuitBreakerConfig()
        assert config.enabled is True
        assert config.failure_threshold == 5
        assert config.success_threshold == 3
        assert config.recovery_timeout == 30.0
        # Matches the live breaker's default (reliability.CircuitBreakerConfig), which these knobs now configure
        assert config.half_open_requests == 3

    def test_custom_values(self) -> None:
        """Test custom configuration."""
        config = CircuitBreakerConfig(
            enabled=False,
            failure_threshold=10,
            success_threshold=5,
            recovery_timeout=60.5,
            half_open_requests=6,
        )
        config.validate()
        assert config.enabled is False
        assert config.failure_threshold == 10
        assert config.success_threshold == 5
        assert config.recovery_timeout == 60.5
        assert config.half_open_requests == 6

    def test_excluded_exceptions_removed(self) -> None:
        """excluded_exceptions was read by nothing; passing it is now a TypeError."""
        with pytest.raises(TypeError, match="excluded_exceptions"):
            CircuitBreakerConfig(excluded_exceptions=(ValueError,))  # type: ignore[call-arg]

    def test_frozen_immutability(self) -> None:
        """Test frozen dataclass prevents mutation."""
        config = CircuitBreakerConfig()
        with pytest.raises(AttributeError, match="cannot assign to field"):
            config.enabled = False  # type: ignore[misc]

    def test_validate_success(self) -> None:
        """Every threshold at its minimum of 1 passes the validation DecoratorConfig runs."""
        config = CircuitBreakerConfig(failure_threshold=1, success_threshold=1, half_open_requests=1)
        assert DecoratorConfig(circuit_breaker=config).circuit_breaker is config

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

    @pytest.mark.parametrize("bad", [0, 0.0, -1.0, float("nan"), float("inf")])
    def test_validate_recovery_timeout_rejects_zero_negative_and_non_finite(self, bad: float) -> None:
        """NaN or inf could never half-open; 0 would restart spent probe cycles on every clock tick."""
        config = CircuitBreakerConfig(recovery_timeout=bad)
        with pytest.raises(ConfigurationError, match="recovery_timeout must be a finite number > 0"):
            config.validate()

    def test_validate_half_open_requests_below_success_threshold(self) -> None:
        """A cycle admits at most half_open_requests probes, so fewer than success_threshold can never close."""
        config = CircuitBreakerConfig(success_threshold=3, half_open_requests=2)
        with pytest.raises(ConfigurationError, match=r"half_open_requests \(2\) must be >= success_threshold \(3\)"):
            config.validate()

    def test_validate_half_open_requests_equal_to_success_threshold(self) -> None:
        """Equal is the smallest probe budget that can still close a cycle; one fewer is refused above."""
        config = CircuitBreakerConfig(success_threshold=4, half_open_requests=4)
        assert DecoratorConfig(circuit_breaker=config).circuit_breaker is config


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
        """max_concurrent_requests=1, the minimum, passes the validation DecoratorConfig runs."""
        config = BackpressureConfig(max_concurrent_requests=1)
        assert DecoratorConfig(backpressure=config).backpressure is config

    @pytest.mark.parametrize("max_concurrent", [0, -10, 1.5, float("nan"), float("inf"), True])
    def test_validate_max_concurrent_requests_invalid(self, max_concurrent: float) -> None:
        """Below 1 nothing runs; from NaN, inf or a fraction threading.Semaphore never reaches 0, so nothing is limited."""
        config = BackpressureConfig(max_concurrent_requests=max_concurrent)  # pyright: ignore[reportArgumentType]
        with pytest.raises(
            ConfigurationError, match=f"max_concurrent_requests must be a whole number >= 1, got {max_concurrent!r}"
        ):
            config.validate()

    def test_validate_limit_minimums(self) -> None:
        """queue_size=1 and timeout=0 (no wait for a permit), the minimums, pass."""
        config = BackpressureConfig(queue_size=1, timeout=0.0)
        assert DecoratorConfig(backpressure=config).backpressure is config

    def test_validate_numpy_values(self) -> None:
        """A NumPy integer is a whole number (it has __index__), and np.float64 subclasses float, so both pass."""
        np = pytest.importorskip("numpy")
        config = BackpressureConfig(max_concurrent_requests=np.int64(8), queue_size=np.int32(16), timeout=np.float64(0.5))
        assert DecoratorConfig(backpressure=config).backpressure is config

    @pytest.mark.parametrize("queue_size", [0, -5, 1.5, float("nan"), float("inf"), True])
    def test_validate_queue_size_invalid(self, queue_size: float) -> None:
        """Below 1 every operation is rejected (each joins the queue); NaN or inf would remove the cap."""
        with pytest.raises(ConfigurationError, match=f"queue_size must be a whole number >= 1, got {queue_size!r}"):
            BackpressureConfig(queue_size=queue_size).validate()  # pyright: ignore[reportArgumentType]

    @pytest.mark.parametrize(
        "timeout",
        [-0.1, 1e10, float("inf"), float("nan"), True, None, "0.1", Decimal("0.1"), Fraction(1, 10)],
    )
    def test_validate_timeout_invalid(self, timeout: object) -> None:
        """Outside 0 to TIMEOUT_MAX NaN never returns from a wait and inf or 1e10 raises OverflowError; a value
        that is not an int or float (None, a str, a Decimal, a Fraction) is refused before it reaches the wait."""
        with pytest.raises(ConfigurationError, match="timeout must be a number from 0 to"):
            BackpressureConfig(timeout=timeout).validate()  # pyright: ignore[reportArgumentType]

    def test_validate_timeout_max_accepted(self) -> None:
        """threading.TIMEOUT_MAX itself is a valid permit wait."""
        config = BackpressureConfig(timeout=threading.TIMEOUT_MAX)
        assert DecoratorConfig(backpressure=config).backpressure is config


@pytest.mark.unit
class TestMonitoringConfig:
    """Test MonitoringConfig defaults (no validation constraints)."""

    def test_defaults(self) -> None:
        """Test default values."""
        config = MonitoringConfig()
        assert config.collect_stats is True
        assert config.enable_structured_logging is True
        assert config.enable_prometheus_metrics is True

    def test_custom_values(self) -> None:
        """Test custom configuration."""
        config = MonitoringConfig(
            collect_stats=False,
            enable_structured_logging=False,
            enable_prometheus_metrics=False,
        )
        assert config.collect_stats is False
        assert config.enable_structured_logging is False
        assert config.enable_prometheus_metrics is False

    def test_frozen_immutability(self) -> None:
        """Test frozen dataclass prevents mutation."""
        config = MonitoringConfig()
        with pytest.raises(AttributeError, match="cannot assign to field"):
            config.collect_stats = False  # type: ignore[misc]

    @pytest.mark.parametrize("enabled", [True, False])
    def test_validate_no_constraints(self, enabled: bool) -> None:
        """The flags carry no cross-constraint: all on and all off both pass DecoratorConfig's validation."""
        config = MonitoringConfig(collect_stats=enabled, enable_structured_logging=enabled, enable_prometheus_metrics=enabled)
        assert DecoratorConfig(monitoring=config).monitoring is config

    def test_enable_tracing_removed(self) -> None:
        """enable_tracing was a no-op and is gone: passing it fails loudly."""
        with pytest.raises(TypeError):
            MonitoringConfig(enable_tracing=True)  # type: ignore[call-arg]


@pytest.mark.unit
class TestEncryptionConfig:
    """Test EncryptionConfig validation (master_key + tenant mode)."""

    def test_defaults(self) -> None:
        """Test default values.

        enabled defaults to None (tri-state, issue #128): unset means no intent stated
        (plaintext without a master key, an error with one), distinct from an explicit False opt-out.
        """
        config = EncryptionConfig()
        assert config.enabled is None
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
        """enabled=False skips the tenant-mode check that enabled=True fails on the same fields."""
        config = EncryptionConfig(enabled=False, master_key="a" * 64)
        assert DecoratorConfig(encryption=config).encryption is config
        with pytest.raises(ConfigurationError, match="Encryption requires explicit tenant mode"):
            EncryptionConfig(enabled=True, master_key="a" * 64).validate()

    def test_validate_enabled_without_master_key(self) -> None:
        """Test validation fails when enabled=True but master_key=None."""
        config = EncryptionConfig(enabled=True, master_key=None, single_tenant_mode=True)
        with pytest.raises(ConfigurationError, match="encryption.enabled=True requires encryption.master_key"):
            config.validate()

    def test_validate_single_tenant_mode_success(self) -> None:
        """Test validation passes for single-tenant mode."""
        config = EncryptionConfig(enabled=True, master_key="a" * 64, single_tenant_mode=True)
        assert DecoratorConfig(encryption=config).encryption is config

    def test_validate_multi_tenant_mode_success(self) -> None:
        """Test validation passes for multi-tenant mode."""

        def tenant_extractor() -> str:
            return "tenant-123"

        config = EncryptionConfig(enabled=True, master_key="a" * 64, tenant_extractor=tenant_extractor)
        assert DecoratorConfig(encryption=config).encryption.tenant_extractor is tenant_extractor

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
        assert DecoratorConfig(encryption=config).encryption.deployment_uuid == "deployment-uuid-123"

    def test_master_key_not_in_repr(self) -> None:
        """master_key must not appear in repr (CWE-532: sensitive data in logs)."""
        key = "ab" * 32  # pragma: allowlist secret
        config = EncryptionConfig(enabled=True, master_key=key, single_tenant_mode=True)
        assert key not in repr(config)
