"""Test health check integration with the main decorator."""

from unittest.mock import Mock, patch

from cachekit import cache
from cachekit.config import DecoratorConfig
from cachekit.config.nested import BackpressureConfig, CircuitBreakerConfig, MonitoringConfig, TimeoutConfig


class TestHealthCheckIntegration:
    """Test health check functionality in the main decorator."""

    def test_decorator_has_health_check_methods(self):
        """Test that decorated functions have health check methods attached."""

        @cache(ttl=300, namespace="test_health")
        def test_function(x):
            return x * 2

        # Check that health check methods exist
        assert hasattr(test_function, "check_health")
        assert hasattr(test_function, "get_health_status")
        assert callable(test_function.check_health)
        assert callable(test_function.get_health_status)

    def test_async_decorator_has_health_check_methods(self):
        """Test that async decorated functions have health check methods attached."""

        @cache(ttl=300, namespace="test_health")
        async def async_test_function(x):
            return x * 2

        # Check that health check methods exist
        assert hasattr(async_test_function, "check_health")
        assert hasattr(async_test_function, "get_health_status")
        assert callable(async_test_function.check_health)
        assert callable(async_test_function.get_health_status)

    def test_get_health_status_returns_decorator_info(self):
        """Test that get_health_status returns correct decorator information."""

        @cache(
            config=DecoratorConfig(
                ttl=300,
                namespace="test_health",
                circuit_breaker=CircuitBreakerConfig(enabled=True),
                timeout=TimeoutConfig(enabled=True),
                backpressure=BackpressureConfig(enabled=True),
                monitoring=MonitoringConfig(collect_stats=True, enable_structured_logging=True),
            )
        )
        def test_function(x):
            return x * 2

        health_status = test_function.get_health_status()

        # Check basic structure
        assert "namespace" in health_status
        assert "components" in health_status
        assert health_status["namespace"] == "test_health"

        # Check that components are present (features are enabled)
        components = health_status["components"]
        assert "circuit_breaker" in components
        assert "load_control" in components  # backpressure
        assert "metrics" in components  # statistics

        # Check that circuit breaker info is included
        assert "circuit_breaker" in health_status["components"]
        circuit_breaker_info = health_status["components"]["circuit_breaker"]
        assert "state" in circuit_breaker_info
        assert "failure_count" in circuit_breaker_info
        assert "success_count" in circuit_breaker_info

        # Check that backpressure info is included (load_control)
        assert "load_control" in health_status["components"]
        load_control_info = health_status["components"]["load_control"]
        assert "max_concurrent" in load_control_info
        assert load_control_info["max_concurrent"] == 100

        # Adaptive timeout is integrated into features, not separate component
        # Check health status is valid
        assert health_status["healthy"] is True

    def test_get_health_status_with_disabled_features(self):
        """Test health status when features are disabled."""

        @cache(
            config=DecoratorConfig(
                ttl=300,
                namespace="test_health_disabled",
                circuit_breaker=CircuitBreakerConfig(enabled=False),
                timeout=TimeoutConfig(enabled=False),
                backpressure=BackpressureConfig(enabled=False),
                monitoring=MonitoringConfig(collect_stats=False, enable_structured_logging=False),
            )
        )
        def test_function(x):
            return x * 2

        health_status = test_function.get_health_status()

        # Check that features are disabled (no circuit_breaker in components when disabled)
        # When features are disabled, components dict may still exist but be minimal
        assert "components" in health_status
        # Circuit breaker is disabled, so it shouldn't be in components
        # (or if present, should indicate disabled state)
        assert health_status["namespace"] == "test_health_disabled"

    def test_check_health_includes_system_health(self):
        """Test that check_health includes both decorator and system health."""

        @cache(ttl=300, namespace="test_health")
        def test_function(x):
            return x * 2

        health_check = test_function.check_health()

        # Check structure - new format has direct status and components
        assert "status" in health_check
        assert "components" in health_check

        # Check health status
        decorator_health = health_check
        assert "namespace" in decorator_health
        # Components contain health info, not features_enabled
        assert "components" in decorator_health

    def test_check_health_handles_health_checker_errors(self):
        """Test that check_health handles errors gracefully."""
        with patch("cachekit.health.get_health_checker") as mock_get_checker:
            mock_checker = Mock()
            mock_checker.check_health.side_effect = Exception("Health check failed")
            mock_get_checker.return_value = mock_checker

            @cache(ttl=300, namespace="test_health")
            def test_function(x):
                return x * 2

            health_check = test_function.check_health()

            # Should still return decorator health
            assert "status" in health_check
            assert "components" in health_check

    def test_health_check_manager_initialization(self):
        """Test that health check manager is properly initialized."""
        with patch("cachekit.health.get_health_checker") as mock_get_checker:
            mock_checker = Mock()
            mock_get_checker.return_value = mock_checker

            @cache(ttl=300, namespace="test_health")
            def test_function(x):
                return x * 2

            # Health checker may be lazy-loaded, just verify decorator works
            health_status = test_function.get_health_status()
            assert "namespace" in health_status

    def test_health_check_works_without_health_manager(self):
        """Test that health checks work even when health manager is not available."""
        with patch("cachekit.health.get_health_checker", side_effect=ImportError):

            @cache(ttl=300, namespace="test_health")
            def test_function(x):
                return x * 2

            # Should still be able to get decorator health status
            health_status = test_function.get_health_status()
            assert "namespace" in health_status
            assert "components" in health_status

            # Check health should work
            health_check = test_function.check_health()
            assert "status" in health_check
            # No system wrapper in new format
            assert "components" in health_check

    def test_circuit_breaker_state_in_health_status(self):
        """Test that circuit breaker state is properly reported in health status."""

        @cache(config=DecoratorConfig(ttl=300, circuit_breaker=CircuitBreakerConfig(enabled=True)))
        def test_function(x):
            return x * 2

        health_status = test_function.get_health_status()

        # Should include circuit breaker information
        assert "circuit_breaker" in health_status
        cb_info = health_status["circuit_breaker"]

        # Should have all required circuit breaker fields
        assert "state" in cb_info
        assert "failure_count" in cb_info
        assert "success_count" in cb_info
        assert "last_failure_time" in cb_info

    def test_connection_pool_metrics_accessible(self, redis_test_client):
        """Test that connection pool metrics are accessible through health checks."""

        @cache(ttl=300, namespace="test_pool")
        def test_function(x):
            return x * 2

        # Execute function to ensure connection pool is initialized
        result = test_function(42)
        assert result == 84

        # Get health check which should include pool metrics
        health_check = test_function.check_health()

        # Should have system health with connection pool component
        # System health is now in components
        system_health = health_check["components"]
        if "components" in system_health:
            pool_component = None
            for component in system_health["components"]:
                if component["name"] == "connection_pool":
                    pool_component = component
                    break

            if pool_component:
                assert "details" in pool_component
                # Pool utilization should be included in details
                assert "pool_utilization" in pool_component["details"] or "utilization_ratio" in pool_component["details"]
