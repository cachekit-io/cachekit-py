"""Unit tests for DecoratorConfig intent presets.

Tests all 5 intent presets:
- .minimal(): circuit_breaker disabled, timeout disabled, monitoring off
- .production(): all protections enabled, full monitoring
- .secure(): encryption enabled, master_key required, L1 enabled (stores encrypted bytes for performance)
- .dev(): monitoring on, Prometheus off
- .test(): all protections disabled, no monitoring
- Test **kwargs overrides work for each preset
"""

from __future__ import annotations

import pytest

from cachekit.config.decorator import DecoratorConfig


@pytest.mark.unit
class TestMinimalPreset:
    """Test .minimal() preset for maximum throughput."""

    def test_minimal_defaults(self) -> None:
        """Test minimal preset disables protections and monitoring."""
        config = DecoratorConfig.minimal()

        # L1 enabled for speed
        assert config.l1.enabled is True

        # Circuit breaker disabled
        assert config.circuit_breaker.enabled is False

        # Timeout disabled
        assert config.timeout.enabled is False

        # Backpressure still enabled (basic protection)
        assert config.backpressure.enabled is True

        # Monitoring disabled
        assert config.monitoring.collect_stats is False
        assert config.monitoring.enable_tracing is False
        assert config.monitoring.enable_structured_logging is False
        assert config.monitoring.enable_prometheus_metrics is False

    def test_minimal_with_ttl_override(self) -> None:
        """Test minimal preset with TTL override."""
        config = DecoratorConfig.minimal(ttl=300)
        assert config.ttl == 300
        assert config.circuit_breaker.enabled is False

    def test_minimal_with_namespace_override(self) -> None:
        """Test minimal preset with namespace override."""
        config = DecoratorConfig.minimal(namespace="test")
        assert config.namespace == "test"
        assert config.circuit_breaker.enabled is False

    def test_minimal_with_backend_override(self) -> None:
        """Test minimal preset with backend override."""
        config = DecoratorConfig.minimal(backend=None)
        assert config.backend is None
        assert config.circuit_breaker.enabled is False


@pytest.mark.unit
class TestProductionPreset:
    """Test .production() preset for full protections."""

    def test_production_defaults(self) -> None:
        """Test production preset enables all protections and monitoring."""
        config = DecoratorConfig.production()

        # L1 enabled
        assert config.l1.enabled is True

        # All protections enabled
        assert config.circuit_breaker.enabled is True
        assert config.timeout.enabled is True
        assert config.backpressure.enabled is True

        # Full monitoring
        assert config.monitoring.collect_stats is True
        assert config.monitoring.enable_tracing is True
        assert config.monitoring.enable_structured_logging is True
        assert config.monitoring.enable_prometheus_metrics is True

    def test_production_with_ttl_override(self) -> None:
        """Test production preset with TTL override."""
        config = DecoratorConfig.production(ttl=600)
        assert config.ttl == 600
        assert config.circuit_breaker.enabled is True

    def test_production_with_safe_mode_override(self) -> None:
        """Test production preset with safe_mode override."""
        config = DecoratorConfig.production(safe_mode=True)
        assert config.safe_mode is True
        assert config.circuit_breaker.enabled is True


@pytest.mark.unit
class TestSecurePreset:
    """Test .secure() preset for encryption."""

    def test_secure_requires_master_key(self) -> None:
        """Test secure preset requires master_key parameter."""
        config = DecoratorConfig.secure(master_key="a" * 64)

        # Encryption enabled
        assert config.encryption.enabled is True
        assert config.encryption.master_key == "a" * 64

        # L1 enabled (stores encrypted bytes for ~50ns hits vs 2-7ms Redis)
        assert config.l1.enabled is True

        # All protections enabled
        assert config.circuit_breaker.enabled is True
        assert config.timeout.enabled is True
        assert config.backpressure.enabled is True

        # Full monitoring
        assert config.monitoring.collect_stats is True
        assert config.monitoring.enable_tracing is True
        assert config.monitoring.enable_structured_logging is True
        assert config.monitoring.enable_prometheus_metrics is True

    def test_secure_single_tenant_mode_default(self) -> None:
        """Test secure preset defaults to single-tenant mode when no tenant_extractor."""
        config = DecoratorConfig.secure(master_key="a" * 64)
        assert config.encryption.single_tenant_mode is True
        assert config.encryption.tenant_extractor is None

    def test_secure_multi_tenant_mode(self) -> None:
        """Test secure preset with tenant_extractor enables multi-tenant mode."""

        def tenant_extractor() -> str:
            return "tenant-123"

        config = DecoratorConfig.secure(master_key="a" * 64, tenant_extractor=tenant_extractor)
        assert config.encryption.enabled is True
        assert config.encryption.tenant_extractor is tenant_extractor
        assert config.encryption.single_tenant_mode is False

    def test_secure_with_deployment_uuid(self) -> None:
        """Test secure preset with deployment_uuid in single-tenant mode."""
        config = DecoratorConfig.secure(master_key="a" * 64, deployment_uuid="uuid-123")
        assert config.encryption.deployment_uuid == "uuid-123"
        assert config.encryption.single_tenant_mode is True

    def test_secure_with_ttl_override(self) -> None:
        """Test secure preset with TTL override."""
        config = DecoratorConfig.secure(master_key="a" * 64, ttl=600)
        assert config.ttl == 600
        assert config.encryption.enabled is True

    def test_secure_explicit_single_tenant_mode(self) -> None:
        """Test secure preset with explicit single_tenant_mode parameter."""
        config = DecoratorConfig.secure(master_key="a" * 64, single_tenant_mode=True)
        assert config.encryption.single_tenant_mode is True
        assert config.encryption.tenant_extractor is None


@pytest.mark.unit
class TestDevPreset:
    """Test .dev() preset for development."""

    def test_dev_defaults(self) -> None:
        """Test dev preset enables monitoring but disables Prometheus."""
        config = DecoratorConfig.dev()

        # Protections enabled for realistic testing
        assert config.circuit_breaker.enabled is True
        assert config.timeout.enabled is True
        assert config.backpressure.enabled is True

        # Monitoring enabled, Prometheus disabled
        assert config.monitoring.collect_stats is True
        assert config.monitoring.enable_tracing is True
        assert config.monitoring.enable_structured_logging is True
        assert config.monitoring.enable_prometheus_metrics is False

    def test_dev_with_ttl_override(self) -> None:
        """Test dev preset with TTL override."""
        config = DecoratorConfig.dev(ttl=60)
        assert config.ttl == 60
        assert config.monitoring.enable_prometheus_metrics is False

    def test_dev_with_namespace_override(self) -> None:
        """Test dev preset with namespace override."""
        config = DecoratorConfig.dev(namespace="dev")
        assert config.namespace == "dev"
        assert config.monitoring.enable_prometheus_metrics is False


@pytest.mark.unit
class TestTestPreset:
    """Test .test() preset for unit/integration tests."""

    def test_test_defaults(self) -> None:
        """Test test preset disables all protections and monitoring."""
        config = DecoratorConfig.test()

        # All protections disabled for determinism
        assert config.circuit_breaker.enabled is False
        assert config.timeout.enabled is False
        assert config.backpressure.enabled is False

        # All monitoring disabled
        assert config.monitoring.collect_stats is False
        assert config.monitoring.enable_tracing is False
        assert config.monitoring.enable_structured_logging is False
        assert config.monitoring.enable_prometheus_metrics is False

    def test_test_with_ttl_override(self) -> None:
        """Test test preset with TTL override."""
        config = DecoratorConfig.test(ttl=10)
        assert config.ttl == 10
        assert config.circuit_breaker.enabled is False

    def test_test_with_backend_override(self) -> None:
        """Test test preset with backend override."""
        config = DecoratorConfig.test(backend=None)
        assert config.backend is None
        assert config.circuit_breaker.enabled is False


@pytest.mark.unit
class TestPresetKwargsOverrides:
    """Test that **kwargs overrides work for all presets."""

    def test_minimal_multiple_overrides(self) -> None:
        """Test minimal preset with multiple kwargs overrides."""
        config = DecoratorConfig.minimal(
            ttl=300,
            namespace="test",
            serializer="msgpack",
            safe_mode=True,
        )
        assert config.ttl == 300
        assert config.namespace == "test"
        assert config.serializer == "msgpack"
        assert config.safe_mode is True
        assert config.circuit_breaker.enabled is False  # Preset behavior preserved

    def test_production_multiple_overrides(self) -> None:
        """Test production preset with multiple kwargs overrides."""
        config = DecoratorConfig.production(
            ttl=600,
            refresh_ttl_on_get=True,
            ttl_refresh_threshold=0.8,
        )
        assert config.ttl == 600
        assert config.refresh_ttl_on_get is True
        assert config.ttl_refresh_threshold == 0.8
        assert config.circuit_breaker.enabled is True  # Preset behavior preserved

    def test_secure_multiple_overrides(self) -> None:
        """Test secure preset with multiple kwargs overrides."""
        config = DecoratorConfig.secure(
            master_key="a" * 64,
            ttl=600,
            namespace="secure",
        )
        assert config.ttl == 600
        assert config.namespace == "secure"
        assert config.encryption.enabled is True  # Preset behavior preserved

    def test_dev_multiple_overrides(self) -> None:
        """Test dev preset with multiple kwargs overrides."""
        config = DecoratorConfig.dev(
            ttl=60,
            namespace="dev",
        )
        assert config.ttl == 60
        assert config.namespace == "dev"
        assert config.monitoring.enable_prometheus_metrics is False  # Preset behavior preserved

    def test_test_multiple_overrides(self) -> None:
        """Test test preset with multiple kwargs overrides."""
        config = DecoratorConfig.test(
            ttl=10,
            namespace="test",
        )
        assert config.ttl == 10
        assert config.namespace == "test"
        assert config.circuit_breaker.enabled is False  # Preset behavior preserved
