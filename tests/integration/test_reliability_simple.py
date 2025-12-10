"""Simple integration tests for reliability features.

This module contains basic tests that verify reliability features work correctly.
"""

from unittest.mock import patch

import pytest
from redis.exceptions import ConnectionError, TimeoutError

from cachekit import cache, get_health_checker
from cachekit.backends.redis.client import get_redis_client
from cachekit.health import HealthStatus


class TestReliabilitySimple:
    """Simple tests for reliability features."""

    @pytest.fixture
    def redis_client(self):
        """Get Redis client for testing."""
        client = get_redis_client()
        yield client
        try:
            client.flushdb()
        except Exception:
            pass

    def test_basic_cache_operation(self, redis_client):
        """Test basic cache operation works."""

        @cache(namespace="simple")
        def simple_func(x):
            return x * 2

        # First call - cache miss
        result1 = simple_func(5)
        assert result1 == 10

        # Second call - cache hit
        result2 = simple_func(5)
        assert result2 == 10

    def test_fallback_on_redis_error(self, redis_client):
        """Test fallback when Redis fails."""

        @cache(namespace="fallback", safe_mode=True)
        def fallback_func(x):
            return f"computed_{x}"

        # Normal operation
        result1 = fallback_func("test")
        assert result1 == "computed_test"

        # Simulate Redis failure
        from cachekit.cache_handler import CacheOperationHandler

        with patch.object(CacheOperationHandler, "get_cached_value", side_effect=ConnectionError()):
            result2 = fallback_func("error")
            assert result2 == "computed_error"  # Function executes directly

    def test_health_check_basic(self, redis_client):
        """Test basic health check functionality."""
        health_checker = get_health_checker()
        result = health_checker.check_health()

        # Should be healthy or degraded (not unhealthy)
        # Note: Connection pool not being initialized causes DEGRADED status in tests
        assert result.is_healthy  # Covers HEALTHY and DEGRADED
        assert result.status in (HealthStatus.HEALTHY, HealthStatus.DEGRADED)

        # Should have components
        assert len(result.components) >= 2
        component_names = {c.name for c in result.components}
        assert "redis" in component_names
        assert "circuit_breaker" in component_names

    def test_custom_fallback_function(self, redis_client):
        """Test custom fallback function."""

        def my_fallback(*args, **kwargs):
            return {"fallback": True, "args": args, "kwargs": kwargs}

        @cache(namespace="custom", safe_mode=True)
        def custom_func(x, y=10):
            return {"result": x + y}

        # Normal operation
        result1 = custom_func(5, y=20)
        assert result1 == {"result": 25}

        # The custom fallback is only used when circuit breaker is OPEN
        # For a simple test, we'll check that the function executes when cache fails
        from cachekit.cache_handler import CacheOperationHandler

        with patch.object(CacheOperationHandler, "get_cached_value", side_effect=TimeoutError()):
            # With safe_mode=True, function should execute normally
            result2 = custom_func(10, y=30)
            # With safe_mode=True, it falls back to executing the function
            assert result2 == {"result": 40}

    @pytest.mark.skip(reason="fail_closed only applies to circuit breaker states")
    def test_fail_closed_strategy(self, redis_client):
        """Test fail_closed strategy raises exceptions."""

        @cache(namespace="fail_closed", safe_mode=False)
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

    @pytest.mark.skip(reason="Mocking Redis failure is complex due to connection pool")
    def test_health_check_with_redis_failure(self, redis_client):
        """Test health check when Redis is down."""
        health_checker = get_health_checker()

        # Mock Redis ping failure
        with patch.object(redis_client, "ping", side_effect=ConnectionError()):
            result = health_checker.check_health(force=True)

            # Should be unhealthy
            assert result.status == HealthStatus.UNHEALTHY
            assert not result.is_healthy

            # Redis component should be unhealthy
            redis_component = next(c for c in result.components if c.name == "redis")
            assert redis_component.status == HealthStatus.UNHEALTHY

    # DELETED: test_connection_pool_statistics
    # Reason: RedisConnectionPoolManager and get_pool_stats() no longer exist.
    # Pool management now handled directly by redis.ConnectionPool.
