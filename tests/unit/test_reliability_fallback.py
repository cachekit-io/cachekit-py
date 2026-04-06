"""Unit tests for reliability fallback features.

This module tests reliability features using mocked Redis failures.
All Redis dependencies are mocked — no live Redis connection required.
"""

from unittest.mock import MagicMock, patch

import pytest
from redis.exceptions import ConnectionError, TimeoutError

from cachekit import cache
from cachekit.health import HealthChecker, HealthLevel, HealthStatus


@pytest.mark.unit
class TestReliabilityFallback:
    """Unit tests for reliability fallback behavior."""

    def test_fallback_on_redis_error(self):
        """Test fallback when Redis fails — function executes directly."""

        @cache(namespace="fallback", backend=None)
        def fallback_func(x):
            return f"computed_{x}"

        # Normal operation (L1-only, no Redis needed)
        result1 = fallback_func("test")
        assert result1 == "computed_test"

        # Simulate cache failure via patched handler
        from cachekit.cache_handler import CacheOperationHandler

        with patch.object(CacheOperationHandler, "get_cached_value", side_effect=ConnectionError()):
            result2 = fallback_func("error")
            assert result2 == "computed_error"  # Function executes directly

    def test_health_check_basic(self):
        """Test basic health check with mocked Redis backend."""
        # Mock the DI container lookup so HealthChecker doesn't need real Redis
        mock_provider = MagicMock()
        mock_backend = MagicMock()
        mock_backend.health_check.return_value = (True, {"ping": "pong"})

        with (
            patch("cachekit.health.container") as mock_container,
            patch("cachekit.health.RedisBackend", return_value=mock_backend),
        ):
            mock_container.get.return_value = mock_provider

            checker = HealthChecker(timeout_seconds=5.0)
            result = checker.check_health()

        # Should be healthy or degraded (not unhealthy)
        assert result.is_healthy
        assert result.status in (HealthStatus.HEALTHY, HealthStatus.DEGRADED)

        # Should have components
        assert len(result.components) >= 2
        component_names = {c.name for c in result.components}
        assert "redis" in component_names
        assert "circuit_breaker" in component_names

    def test_health_check_redis_unhealthy(self):
        """Test health check reports UNHEALTHY when Redis is down."""
        with patch("cachekit.health.container") as mock_container:
            mock_container.get.side_effect = ValueError("Service CacheClientProvider not registered")

            checker = HealthChecker(timeout_seconds=5.0)
            result = checker.check_health(level=HealthLevel.PING)

        assert result.status == HealthStatus.UNHEALTHY
        assert not result.is_healthy

    def test_custom_fallback_function(self):
        """Test custom fallback function."""

        @cache(namespace="custom", backend=None)
        def custom_func(x, y=10):
            return {"result": x + y}

        # Normal operation (L1-only)
        result1 = custom_func(5, y=20)
        assert result1 == {"result": 25}

        # The custom fallback executes when cache handler fails
        from cachekit.cache_handler import CacheOperationHandler

        with patch.object(CacheOperationHandler, "get_cached_value", side_effect=TimeoutError()):
            result2 = custom_func(10, y=30)
            assert result2 == {"result": 40}

    @pytest.mark.skip(reason="fail_closed only applies to circuit breaker states")
    def test_fail_closed_strategy(self):
        """Test fail_closed strategy raises exceptions."""

        @cache(namespace="fail_closed", fallback_strategy="fail_closed", backend=None)
        def fail_closed_func(x):
            return x

        # Normal operation
        result = fail_closed_func("test")
        assert result == "test"

        # With Redis failure - should raise
        from cachekit.cache_handler import CacheOperationHandler

        with patch.object(
            CacheOperationHandler,
            "get_cached_value",
            side_effect=ConnectionError("Test error"),
        ):
            with pytest.raises(ConnectionError, match="Test error"):
                fail_closed_func("error")
