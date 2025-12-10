"""
Pytest examples verifying @cache decorator intent-based patterns.

These tests document and verify the decorator API patterns from intent.py doctests.
Run with: pytest tests/test_decorator_intent_examples.py -v
"""

import pytest

from cachekit.config import DecoratorConfig
from cachekit.decorators import cache


class TestZeroConfig:
    """Zero-config L1-only caching (90% of use cases)."""

    def test_cache_decorator_basic(self):
        """Test basic @cache decorator with L1-only backend."""

        @cache(backend=None)
        def expensive_function() -> int:
            return 42

        result = expensive_function()
        assert result == 42

    def test_cache_decorator_with_ttl(self):
        """Test @cache decorator with TTL configuration."""

        @cache(backend=None, ttl=300)
        def compute_value() -> str:
            return "cached_result"

        result = compute_value()
        assert result == "cached_result"


class TestIntentBasedMinimal:
    """Minimal profile: Speed-critical (trading, gaming, real-time)."""

    def test_minimal_config_ttl(self):
        """Minimal preset has TTL configured."""
        config = DecoratorConfig.minimal(ttl=300, backend=None)
        assert config.ttl == 300

    def test_minimal_config_circuit_breaker_disabled(self):
        """Minimal preset disables circuit breaker for speed."""
        config = DecoratorConfig.minimal(backend=None)
        assert config.circuit_breaker.enabled is False

    def test_minimal_config_timeout_disabled(self):
        """Minimal preset disables adaptive timeout."""
        config = DecoratorConfig.minimal(backend=None)
        assert config.timeout.enabled is False

    def test_minimal_config_monitoring_disabled(self):
        """Minimal preset disables monitoring."""
        config = DecoratorConfig.minimal(backend=None)
        assert config.monitoring.collect_stats is False
        assert config.monitoring.enable_prometheus_metrics is False

    def test_minimal_decorator(self):
        """Minimal preset can be used as decorator."""

        @cache(config=DecoratorConfig.minimal(ttl=300, backend=None))
        def get_price(symbol: str) -> float:
            return 99.99

        assert get_price("AAPL") == 99.99


class TestIntentBasedProduction:
    """Production profile: Reliability-critical (payments, APIs)."""

    def test_production_config_circuit_breaker_enabled(self):
        """Production preset enables circuit breaker."""
        config = DecoratorConfig.production(backend=None)
        assert config.circuit_breaker.enabled is True

    def test_production_config_timeout_enabled(self):
        """Production preset enables adaptive timeout."""
        config = DecoratorConfig.production(backend=None)
        assert config.timeout.enabled is True

    def test_production_config_monitoring_enabled(self):
        """Production preset enables full monitoring."""
        config = DecoratorConfig.production(backend=None)
        assert config.monitoring.collect_stats is True
        assert config.monitoring.enable_tracing is True
        assert config.monitoring.enable_structured_logging is True

    def test_production_config_backpressure_enabled(self):
        """Production preset enables backpressure."""
        config = DecoratorConfig.production(backend=None)
        assert config.backpressure.enabled is True

    def test_production_decorator(self):
        """Production preset can be used as decorator."""

        @cache(config=DecoratorConfig.production(ttl=600, backend=None))
        def process_payment(amount: float) -> dict:
            return {"status": "processed", "amount": amount}

        result = process_payment(100.0)
        assert result["status"] == "processed"


class TestIntentBasedSecure:
    """Secure profile: Security-critical (PII, medical, financial)."""

    def test_secure_config_encryption_enabled(self):
        """Secure preset enables encryption."""
        config = DecoratorConfig.secure(master_key="a" * 64, backend=None)
        assert config.encryption.enabled is True

    def test_secure_config_l1_enabled_for_encrypted_hits(self):
        """Secure preset enables L1 cache (stores encrypted bytes for performance)."""
        config = DecoratorConfig.secure(master_key="a" * 64, backend=None)
        assert config.l1.enabled is True

    def test_secure_config_monitoring_enabled(self):
        """Secure preset enables full monitoring for audit trail."""
        config = DecoratorConfig.secure(master_key="a" * 64, backend=None)
        assert config.monitoring.collect_stats is True
        assert config.monitoring.enable_tracing is True

    def test_secure_decorator_requires_master_key(self):
        """@cache.secure decorator requires master_key parameter."""
        with pytest.raises(ValueError, match="master_key"):

            @cache.secure(backend=None)  # Missing master_key in secure context
            def secure_func():
                pass

    def test_secure_decorator_with_tenant_extractor(self):
        """Secure preset supports multi-tenant extraction."""

        def extract_tenant(user_id: int) -> str:
            return f"tenant_{user_id}"

        config = DecoratorConfig.secure(master_key="a" * 64, tenant_extractor=extract_tenant, backend=None)
        assert config.encryption.tenant_extractor is extract_tenant


class TestIntentBasedDev:
    """Dev profile: Verbose logging, easy debugging."""

    def test_dev_config_prometheus_disabled(self):
        """Dev preset disables Prometheus for local development."""
        config = DecoratorConfig.dev(backend=None)
        assert config.monitoring.enable_prometheus_metrics is False

    def test_dev_config_structured_logging_enabled(self):
        """Dev preset enables structured logging."""
        config = DecoratorConfig.dev(backend=None)
        assert config.monitoring.enable_structured_logging is True


class TestIntentBasedTest:
    """Test profile: Deterministic, all protections disabled."""

    def test_test_config_circuit_breaker_disabled(self):
        """Test preset disables circuit breaker for reproducibility."""
        config = DecoratorConfig.test(backend=None)
        assert config.circuit_breaker.enabled is False

    def test_test_config_timeout_disabled(self):
        """Test preset disables adaptive timeout."""
        config = DecoratorConfig.test(backend=None)
        assert config.timeout.enabled is False

    def test_test_config_backpressure_disabled(self):
        """Test preset disables backpressure."""
        config = DecoratorConfig.test(backend=None)
        assert config.backpressure.enabled is False

    def test_test_config_monitoring_disabled(self):
        """Test preset disables monitoring."""
        config = DecoratorConfig.test(backend=None)
        assert config.monitoring.collect_stats is False

    def test_test_decorator(self):
        """Test preset ensures deterministic behavior."""

        @cache(config=DecoratorConfig.test(ttl=10, backend=None))
        def deterministic_function() -> str:
            return "same_every_time"

        result = deterministic_function()
        assert result == "same_every_time"


class TestROROConfiguration:
    """RORO (Readable Object-Returning Object) configuration pattern."""

    def test_roro_with_minimal_preset(self):
        """RORO pattern with minimal preset."""

        @cache(config=DecoratorConfig.minimal(ttl=300, backend=None))
        def optimized_function() -> str:
            return "cached"

        assert optimized_function() == "cached"

    def test_roro_with_production_preset(self):
        """RORO pattern with production preset."""

        @cache(config=DecoratorConfig.production(ttl=600, backend=None))
        def reliable_function() -> dict:
            return {"result": "value"}

        assert reliable_function() == {"result": "value"}

    def test_roro_with_secure_preset(self):
        """RORO pattern with secure preset."""

        @cache(config=DecoratorConfig.secure(master_key="a" * 64, ttl=600, backend=None))
        def secure_function() -> str:
            return "encrypted"

        assert secure_function() == "encrypted"


class TestManualOverride:
    """Manual override pattern for custom configuration."""

    def test_manual_ttl_override(self):
        """Manual TTL override."""

        @cache(ttl=1800, backend=None)
        def custom_ttl() -> str:
            return "value"

        assert custom_ttl() == "value"

    def test_manual_namespace_override(self):
        """Manual namespace override."""

        @cache(ttl=1800, namespace="custom", backend=None)
        def custom_namespace() -> dict:
            return {"result": "value"}

        assert custom_namespace() == {"result": "value"}

    def test_manual_safe_mode_override(self):
        """Manual safe_mode override for graceful degradation."""

        @cache(ttl=300, safe_mode=True, backend=None)
        def fail_gracefully() -> str:
            return "cached"

        assert fail_gracefully() == "cached"


class TestDecoratorVariants:
    """Test intent-based decorator variants (@cache.minimal, @cache.production, etc)."""

    def test_cache_minimal_variant(self):
        """@cache.minimal variant."""

        @cache.minimal(backend=None)
        def minimal_cached() -> int:
            return 42

        assert minimal_cached() == 42

    def test_cache_production_variant(self):
        """@cache.production variant."""

        @cache.production(backend=None)
        def production_cached() -> int:
            return 100

        assert production_cached() == 100

    def test_cache_dev_variant(self):
        """@cache.dev variant."""

        @cache.dev(backend=None)
        def dev_cached() -> int:
            return 55

        assert dev_cached() == 55

    def test_cache_test_variant(self):
        """@cache.test variant."""

        @cache.test(backend=None)
        def test_cached() -> int:
            return 77

        assert test_cached() == 77
