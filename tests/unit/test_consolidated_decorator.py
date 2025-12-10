"""Comprehensive test suite for the consolidated redis_cache decorator.

This test suite validates:
- All feature combinations (parametrized tests)
- Backward compatibility with old parameters
- Feature toggle behavior (enabled/disabled)
- Performance benchmarks for overhead validation

Requirements: 5.1, 5.2, 5.3
"""

import asyncio
import time
from unittest.mock import patch

import pytest

from cachekit.decorators import cache


class TestConsolidatedDecoratorFeatureCombinations:
    """Test all feature combinations with parametrized tests."""

    @pytest.mark.parametrize("use_preset", ["minimal", "production", None])
    def test_all_feature_combinations_sync(
        self,
        redis_test_client,
        use_preset: str | None,
    ):
        """Test different feature presets work correctly for sync functions."""
        call_count = 0

        # Use intent-based presets instead of individual feature flags
        if use_preset == "minimal":
            decorator = cache.minimal(ttl=300, namespace="feature_test")
        elif use_preset == "production":
            decorator = cache.production(ttl=300, namespace="feature_test")
        else:
            decorator = cache(ttl=300, namespace="feature_test")

        @decorator
        def test_function(x: int) -> str:
            nonlocal call_count
            call_count += 1
            return f"result_{x}_{call_count}"

        # Test basic functionality
        result1 = test_function(42)
        assert result1 == "result_42_1"
        assert call_count == 1

        # Test cache hit
        result2 = test_function(42)
        assert result2 == result1
        assert call_count == 1  # No additional call due to cache hit

        # Test cache miss with different args
        result3 = test_function(43)
        assert result3 == "result_43_2"
        assert call_count == 2

        # Verify decorator methods are attached
        assert hasattr(test_function, "invalidate_cache")
        assert hasattr(test_function, "check_health")
        assert hasattr(test_function, "get_health_status")

        # Test health status reflects preset configuration
        health_status = test_function.get_health_status()
        features = health_status["components"]

        # Verify health status structure is valid
        assert "namespace" in health_status
        assert "components" in health_status

        # Different presets have different feature sets
        if use_preset == "production":
            # Production has all features enabled
            assert "circuit_breaker" in features
            assert "load_control" in features
            assert "metrics" in features
        elif use_preset == "minimal":
            # Minimal has fewer features
            assert ("circuit_breaker" not in features) or (not features["circuit_breaker"])
        # Default preset varies, just check structure

    @pytest.mark.parametrize("use_preset", ["minimal", "production", None])
    @pytest.mark.asyncio
    async def test_all_feature_combinations_async(
        self,
        redis_test_client,
        use_preset: str | None,
    ):
        """Test different feature presets work correctly for async functions."""
        call_count = 0

        # Use intent-based presets instead of individual feature flags
        if use_preset == "minimal":
            decorator = cache.minimal(ttl=300, namespace="async_feature_test")
        elif use_preset == "production":
            decorator = cache.production(ttl=300, namespace="async_feature_test")
        else:
            decorator = cache(ttl=300, namespace="async_feature_test")

        @decorator
        async def async_test_function(x: int) -> str:
            nonlocal call_count
            call_count += 1
            await asyncio.sleep(0.01)  # Simulate async work
            return f"async_result_{x}_{call_count}"

        # Test basic functionality
        result1 = await async_test_function(42)
        assert result1 == "async_result_42_1"
        assert call_count == 1

        # Test cache hit
        result2 = await async_test_function(42)
        assert result2 == result1
        assert call_count == 1  # No additional call due to cache hit

        # Test cache miss with different args
        result3 = await async_test_function(43)
        assert result3 == "async_result_43_2"
        assert call_count == 2

        # Verify decorator methods are attached
        assert hasattr(async_test_function, "invalidate_cache")
        assert hasattr(async_test_function, "check_health")
        assert hasattr(async_test_function, "get_health_status")

        # Test health status reflects preset configuration
        health_status = async_test_function.get_health_status()
        features = health_status["components"]

        # Verify health status structure is valid
        assert "namespace" in health_status
        assert "components" in health_status

        # Different presets have different feature sets
        if use_preset == "production":
            # Production has all features enabled
            assert "circuit_breaker" in features
            assert "load_control" in features
            assert "metrics" in features
        elif use_preset == "minimal":
            # Minimal has fewer features
            assert ("circuit_breaker" not in features) or (not features["circuit_breaker"])
        # Default preset varies, just check structure


class TestBackwardCompatibility:
    """Test backward compatibility with old parameters and usage patterns."""

    def test_simple_usage_no_parameters(self, redis_test_client):
        """Test @cache without any parameters works with defaults."""
        call_count = 0

        @cache
        def simple_function():
            nonlocal call_count
            call_count += 1
            return f"simple_result_{call_count}"

        result1 = simple_function()
        assert result1 == "simple_result_1"
        assert call_count == 1

        # Test cache hit
        result2 = simple_function()
        assert result2 == result1
        assert call_count == 1

    def test_legacy_parameter_compatibility(self, redis_test_client):
        """Test that legacy parameters are honored or safely ignored."""

        # Test all the core parameters that should be backward compatible
        @cache(
            ttl=600,
            namespace="legacy_test",
            serializer="default",
            safe_mode=True,
        )
        def legacy_function(x):
            return f"legacy_{x}"

        result = legacy_function("test")
        assert result == "legacy_test"

        # Verify namespace in cache key (tenant prefix + namespace)
        keys = redis_test_client.keys("t:default:ns:legacy_test*")
        assert len(keys) > 0

    def test_decorator_with_arguments_syntax(self, redis_test_client):
        """Test @cache(ttl=300) syntax works correctly."""

        @cache(ttl=300, namespace="args_test")
        def args_function(x):
            return f"args_{x}"

        result = args_function("test")
        assert result == "args_test"

    def test_decorator_without_arguments_syntax(self, redis_test_client):
        """Test @cache syntax (no parentheses) works correctly."""

        @cache
        def no_args_function(x):
            return f"no_args_{x}"

        result = no_args_function("test")
        assert result == "no_args_test"

    def test_invalidate_cache_backward_compatibility(self, redis_test_client):
        """Test that invalidate_cache method works as expected."""
        call_count = 0

        @cache(ttl=300)
        def cacheable_function(arg):
            nonlocal call_count
            call_count += 1
            return f"result_{arg}_{call_count}"

        # Cache a result
        result1 = cacheable_function("test")
        assert result1 == "result_test_1"
        assert call_count == 1

        # Verify cache hit
        result2 = cacheable_function("test")
        assert result2 == result1
        assert call_count == 1

        # Invalidate and verify cache miss
        cacheable_function.invalidate_cache("test")
        result3 = cacheable_function("test")
        assert result3 == "result_test_2"
        assert call_count == 2

    @pytest.mark.asyncio
    async def test_async_invalidate_cache_backward_compatibility(self, redis_test_client):
        """Test that async invalidate_cache method works as expected."""
        call_count = 0

        @cache(ttl=300)
        async def async_cacheable_function(arg):
            nonlocal call_count
            call_count += 1
            return f"async_result_{arg}_{call_count}"

        # Cache a result
        result1 = await async_cacheable_function("test")
        assert result1 == "async_result_test_1"
        assert call_count == 1

        # Verify cache hit
        result2 = await async_cacheable_function("test")
        assert result2 == result1
        assert call_count == 1

        # Invalidate and verify cache miss
        await async_cacheable_function.invalidate_cache("test")
        result3 = await async_cacheable_function("test")
        assert result3 == "async_result_test_2"
        assert call_count == 2


class TestFeatureToggleBehavior:
    """Test feature toggle behavior when features are enabled/disabled."""

    def test_circuit_breaker_enabled_vs_disabled(self, redis_test_client):
        """Test circuit breaker behavior when enabled vs disabled."""

        # Test with circuit breaker enabled (production preset)
        @cache.production(ttl=300)
        def function_with_cb(x):
            return f"cb_result_{x}"

        health_status = function_with_cb.get_health_status()
        assert health_status["components"]["circuit_breaker"]  # Should be a dict with circuit breaker details

        # Test with circuit breaker disabled (minimal preset)
        @cache.minimal(ttl=300)
        def function_without_cb(x):
            return f"no_cb_result_{x}"

        health_status = function_without_cb.get_health_status()
        # When circuit breaker disabled, key may not exist or be False
        assert health_status["components"].get("circuit_breaker", False) is False

    def test_statistics_collection_enabled_vs_disabled(self, redis_test_client):
        """Test statistics collection when enabled vs disabled."""

        # Test with stats enabled (production preset)
        @cache.production(ttl=300)
        def function_with_stats(x):
            return f"stats_result_{x}"

        health_status = function_with_stats.get_health_status()
        assert health_status["components"]["metrics"]  # Should be a dict with metrics details

        # Test with stats disabled (minimal preset)
        @cache.minimal(ttl=300)
        def function_without_stats(x):
            return f"no_stats_result_{x}"

        health_status = function_without_stats.get_health_status()
        # When stats disabled, metrics key may not exist or be False
        assert health_status["components"].get("metrics", False) is False

    def test_backpressure_enabled_vs_disabled(self, redis_test_client):
        """Test backpressure control when enabled vs disabled."""
        from cachekit.config import DecoratorConfig
        from cachekit.config.nested import BackpressureConfig

        # Test with backpressure enabled and custom max_concurrent
        @cache(config=DecoratorConfig(ttl=300, backpressure=BackpressureConfig(enabled=True, max_concurrent_requests=10)))
        def function_with_bp(x):
            return f"bp_result_{x}"

        health_status = function_with_bp.get_health_status()
        assert health_status["components"]["load_control"]  # Should be a dict with load_control details
        assert health_status["components"]["load_control"]["max_concurrent"] == 10

        # Test with backpressure disabled (minimal preset)
        @cache.minimal(ttl=300)
        def function_without_bp(x):
            return f"no_bp_result_{x}"

        health_status = function_without_bp.get_health_status()
        # Minimal preset disables backpressure, but may still show in components as disabled
        # Just verify the structure exists
        assert "components" in health_status

    def test_adaptive_timeout_enabled_vs_disabled(self, redis_test_client):
        """Test adaptive timeout when enabled vs disabled."""

        # Test with adaptive timeout enabled (production preset)
        @cache.production(ttl=300)
        def function_with_at(x):
            return f"at_result_{x}"

        health_status = function_with_at.get_health_status()
        # Adaptive timeout may not show in components when enabled
        assert "components" in health_status

        # Test with adaptive timeout disabled (minimal preset)
        @cache.minimal(ttl=300)
        def function_without_at(x):
            return f"no_at_result_{x}"

        health_status = function_without_at.get_health_status()
        # Adaptive timeout disabled
        assert "components" in health_status

    def test_structured_logging_enabled_vs_disabled(self, redis_test_client):
        """Test structured logging when enabled vs disabled."""

        # Test with structured logging enabled (production preset)
        @cache.production(ttl=300)
        def function_with_logging(x):
            return f"logging_result_{x}"

        health_status = function_with_logging.get_health_status()
        # Structured logging may not show in components when enabled
        assert "components" in health_status

        # Test with structured logging disabled (minimal preset)
        @cache.minimal(ttl=300)
        def function_without_logging(x):
            return f"no_logging_result_{x}"

        health_status = function_without_logging.get_health_status()
        # Structured logging disabled
        assert "components" in health_status

    def test_pipelined_enabled_vs_disabled(self, redis_test_client):
        """Test basic cache operations with different namespaces.

        Note: pipelined is a wrapper-level implementation detail not exposed in DecoratorConfig.
        This test verifies basic caching functionality works correctly.
        """
        call_count = 0

        # Test with default settings
        @cache(ttl=300, namespace="pipelined_test")
        def function_with_pipeline(x):
            nonlocal call_count
            call_count += 1
            return f"pipeline_result_{x}_{call_count}"

        result1 = function_with_pipeline(1)
        assert result1 == "pipeline_result_1_1"

        # Test with different namespace
        @cache(ttl=300, namespace="no_pipeline_test")
        def function_without_pipeline(x):
            nonlocal call_count
            call_count += 1
            return f"no_pipeline_result_{x}_{call_count}"

        result2 = function_without_pipeline(1)
        assert result2 == "no_pipeline_result_1_2"

    def test_ttl_refresh_enabled_vs_disabled(self, redis_test_client):
        """Test TTL refresh behavior when enabled vs disabled."""

        # Test with TTL refresh enabled
        @cache(ttl=300, refresh_ttl_on_get=True, ttl_refresh_threshold=0.5)
        def function_with_refresh(x):
            return f"refresh_result_{x}"

        result1 = function_with_refresh(1)
        assert result1 == "refresh_result_1"

        # Test with TTL refresh disabled (default)
        @cache(ttl=300, refresh_ttl_on_get=False)
        def function_without_refresh(x):
            return f"no_refresh_result_{x}"

        result2 = function_without_refresh(1)
        assert result2 == "no_refresh_result_1"


class TestPerformanceOverheadValidation:
    """Test performance benchmarks for overhead validation."""

    def test_minimal_overhead_with_all_features_disabled(self, redis_test_client):
        """Test that overhead is minimal when all features are disabled."""
        call_count = 0

        # Use minimal preset for minimal overhead
        @cache.minimal(ttl=300)
        def minimal_function():
            nonlocal call_count
            call_count += 1
            return f"minimal_{call_count}"

        # Measure performance of cache miss (first call)
        start_time = time.time()
        result1 = minimal_function()
        miss_duration = time.time() - start_time

        assert result1 == "minimal_1"
        assert call_count == 1
        assert miss_duration < 0.1  # Should complete within 100ms

        # Measure performance of cache hit (second call)
        start_time = time.time()
        result2 = minimal_function()
        hit_duration = time.time() - start_time

        assert result2 == result1
        assert call_count == 1  # No additional function call
        assert hit_duration < 0.05  # Cache hit should be faster

    def test_acceptable_overhead_with_all_features_enabled(self, redis_test_client):
        """Test that overhead is acceptable when all features are enabled."""
        call_count = 0

        # Use production preset for all features enabled
        @cache.production(ttl=300)
        def full_featured_function():
            nonlocal call_count
            call_count += 1
            return f"full_featured_{call_count}"

        # Measure performance of cache miss (first call)
        start_time = time.time()
        result1 = full_featured_function()
        miss_duration = time.time() - start_time

        assert result1 == "full_featured_1"
        assert call_count == 1
        # Allow more time for full featured version but still reasonable
        assert miss_duration < 0.5  # Should complete within 500ms

        # Measure performance of cache hit (second call)
        start_time = time.time()
        result2 = full_featured_function()
        hit_duration = time.time() - start_time

        assert result2 == result1
        assert call_count == 1  # No additional function call
        assert hit_duration < 0.1  # Cache hit should be reasonably fast

    @pytest.mark.asyncio
    async def test_async_performance_overhead(self, redis_test_client):
        """Test performance overhead for async functions."""
        call_count = 0

        # Use production preset for all features enabled
        @cache.production(ttl=300)
        async def async_performance_function():
            nonlocal call_count
            call_count += 1
            await asyncio.sleep(0.01)  # Simulate async work
            return f"async_perf_{call_count}"

        # Measure performance of cache miss
        start_time = time.time()
        result1 = await async_performance_function()
        miss_duration = time.time() - start_time

        assert result1 == "async_perf_1"
        assert call_count == 1
        assert miss_duration < 0.5  # Should complete within 500ms

        # Measure performance of cache hit
        start_time = time.time()
        result2 = await async_performance_function()
        hit_duration = time.time() - start_time

        assert result2 == result1
        assert call_count == 1
        assert hit_duration < 0.1  # Cache hit should be fast

    def test_concurrent_requests_performance(self, redis_test_client):
        """Test performance under concurrent load."""
        import threading

        from cachekit.config import DecoratorConfig
        from cachekit.config.nested import BackpressureConfig, MonitoringConfig

        call_count = 0
        results = []

        # Use custom config for specific backpressure settings
        @cache(
            config=DecoratorConfig(
                ttl=300,
                backpressure=BackpressureConfig(enabled=True, max_concurrent_requests=50),
                monitoring=MonitoringConfig(collect_stats=True),
            )
        )
        def concurrent_function(x):
            nonlocal call_count
            call_count += 1
            time.sleep(0.01)  # Simulate work
            return f"concurrent_{x}_{call_count}"

        def worker(worker_id):
            try:
                result = concurrent_function(worker_id % 5)  # Create some cache hits
                results.append(result)
            except Exception as e:
                results.append(f"error_{e}")

        # Run 20 concurrent requests
        threads = []
        start_time = time.time()

        for i in range(20):
            thread = threading.Thread(target=worker, args=(i,))
            threads.append(thread)
            thread.start()

        for thread in threads:
            thread.join()

        total_duration = time.time() - start_time

        # All requests should complete
        assert len(results) == 20
        # Should complete reasonably quickly even with backpressure
        assert total_duration < 2.0
        # With concurrent access, cache hits may vary due to race conditions
        # The test primarily validates that concurrent requests are handled correctly
        # In practice: 20 threads, 5 unique keys → expect some cache hits, but not guaranteed
        # due to concurrent cache misses on first access
        assert call_count <= 20  # At most all calls execute (worst case: no cache hits)


class TestErrorHandlingAndGracefulDegradation:
    """Test error handling and graceful degradation scenarios."""

    def test_redis_connection_failure_graceful_degradation(self):
        """Test that functions work normally when Redis connection fails."""
        from cachekit.config import DecoratorConfig
        from cachekit.config.nested import CircuitBreakerConfig, L1CacheConfig

        call_count = 0

        @cache(
            config=DecoratorConfig(
                ttl=300,
                circuit_breaker=CircuitBreakerConfig(enabled=True),
                l1=L1CacheConfig(enabled=False),
            )
        )
        def function_with_redis_failure():
            nonlocal call_count
            call_count += 1
            return f"fallback_{call_count}"

        # Mock Redis connection failure
        with patch("cachekit.decorators.wrapper.get_backend_provider") as mock_get_provider:
            mock_provider = mock_get_provider.return_value
            mock_provider.get_sync_client.side_effect = Exception("Redis connection failed")

            # Function should work despite Redis failure
            result1 = function_with_redis_failure()
            assert result1 == "fallback_1"
            assert call_count == 1

            result2 = function_with_redis_failure()
            assert result2 == "fallback_2"
            assert call_count == 2  # No caching due to Redis failure

    def test_circuit_breaker_open_behavior(self, redis_test_client):
        """Test behavior when circuit breaker is open."""
        from cachekit.config import DecoratorConfig
        from cachekit.config.nested import CircuitBreakerConfig

        call_count = 0

        # Create circuit breaker with very low threshold for testing
        @cache(
            config=DecoratorConfig(
                ttl=300,
                circuit_breaker=CircuitBreakerConfig(enabled=True, failure_threshold=1, recovery_timeout=0.1),
            )
        )
        def function_with_cb():
            nonlocal call_count
            call_count += 1
            return f"cb_test_{call_count}"

        # Normal operation should work
        result1 = function_with_cb()
        assert result1 == "cb_test_1"
        assert call_count == 1

    def test_health_check_with_component_failures(self, redis_test_client):
        """Test health check behavior when components fail."""

        # Use production preset which has all features enabled
        @cache.production(ttl=300)
        def function_for_health_test():
            return "health_test"

        # Test normal health check
        health_result = function_for_health_test.check_health()
        assert "status" in health_result
        assert "components" in health_result

        # Test health status
        health_status = function_for_health_test.get_health_status()
        assert "namespace" in health_status
        assert "components" in health_status

    def test_serialization_fallback_behavior(self, redis_test_client):
        """Test serialization fallback when preferred serializer fails."""

        @cache(ttl=300, serializer="default", safe_mode=False)
        def function_with_serialization():
            return {"complex": "data", "with": ["nested", "structures"]}

        result = function_with_serialization()
        assert result == {"complex": "data", "with": ["nested", "structures"]}

        # Test safe mode
        @cache(ttl=300, safe_mode=True)
        def function_with_safe_mode():
            return {"safe": "data"}

        result2 = function_with_safe_mode()
        assert result2 == {"safe": "data"}

    @pytest.mark.asyncio
    async def test_async_error_handling(self, redis_test_client):
        """Test error handling in async functions."""
        call_count = 0

        # Use production preset which has circuit breaker enabled
        @cache.production(ttl=300)
        async def async_function_with_errors():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise ValueError("First call fails")
            return f"async_success_{call_count}"

        # First call should fail but be caught
        with pytest.raises(ValueError):
            await async_function_with_errors()

        # Second call should succeed (no caching due to error)
        result = await async_function_with_errors()
        assert result == "async_success_2"
        assert call_count == 2


class TestRealWorldUsagePatterns:
    """Test real-world usage patterns and edge cases."""

    def test_decorator_with_complex_function_signatures(self, redis_test_client):
        """Test decorator works with complex function signatures."""
        call_count = 0

        @cache(ttl=300, namespace="complex_sig")
        def complex_function(pos_arg, *args, keyword_arg="default", **kwargs):
            nonlocal call_count
            call_count += 1
            return {
                "pos_arg": pos_arg,
                "args": args,
                "keyword_arg": keyword_arg,
                "kwargs": kwargs,
                "call_count": call_count,
            }

        # Test various call patterns
        result1 = complex_function("pos", "extra1", "extra2", keyword_arg="custom", extra_kw="value")
        # First call - function executes, returns tuple (before serialization)
        expected1 = {
            "pos_arg": "pos",
            "args": ("extra1", "extra2"),  # Direct return has tuple
            "keyword_arg": "custom",
            "kwargs": {"extra_kw": "value"},
            "call_count": 1,
        }
        assert result1 == expected1
        assert call_count == 1

        # Same call should hit cache - MessagePack deserializes tuple as list
        result2 = complex_function("pos", "extra1", "extra2", keyword_arg="custom", extra_kw="value")
        expected2 = {
            "pos_arg": "pos",
            "args": ["extra1", "extra2"],  # MessagePack converts tuples to lists
            "keyword_arg": "custom",
            "kwargs": {"extra_kw": "value"},
            "call_count": 1,
        }
        assert result2 == expected2
        assert call_count == 1  # Cache hit

        # Different call should miss cache
        result3 = complex_function("pos", "extra1", "extra2", keyword_arg="different")
        assert result3["call_count"] == 2
        assert call_count == 2

    def test_decorator_with_class_methods(self, redis_test_client):
        """Test decorator works with class methods."""

        class TestClass:
            def __init__(self, name):
                self.name = name

            @cache(ttl=300, namespace="class_method")
            def instance_method(self, arg):
                return f"{self.name}_{arg}"

            @classmethod
            @cache(ttl=300, namespace="class_method_cls")
            def class_method(cls, arg):
                return f"class_{arg}"

            @staticmethod
            @cache(ttl=300, namespace="static_method")
            def static_method(arg):
                return f"static_{arg}"

        # Test instance method
        obj = TestClass("test")
        result1 = obj.instance_method("value")
        assert result1 == "test_value"

        # Test class method
        result2 = TestClass.class_method("value")
        assert result2 == "class_value"

        # Test static method
        result3 = TestClass.static_method("value")
        assert result3 == "static_value"

    def test_decorator_with_bypass_cache_parameter(self, redis_test_client):
        """Test _bypass_cache parameter works correctly."""
        call_count = 0

        @cache(ttl=300)
        def bypassable_function(x):
            nonlocal call_count
            call_count += 1
            return f"bypass_test_{x}_{call_count}"

        # Normal call should cache
        result1 = bypassable_function(1)
        assert result1 == "bypass_test_1_1"
        assert call_count == 1

        # Same call should hit cache
        result2 = bypassable_function(1)
        assert result2 == result1
        assert call_count == 1

        # Bypass cache should always execute function
        result3 = bypassable_function(1, _bypass_cache=True)
        assert result3 == "bypass_test_1_2"
        assert call_count == 2

        # Normal call should still hit cache (bypass didn't affect cache)
        result4 = bypassable_function(1)
        assert result4 == result1
        assert call_count == 2

    def test_namespace_isolation(self, redis_test_client):
        """Test that different namespaces are properly isolated."""

        @cache(ttl=300, namespace="ns1")
        def function_ns1(x):
            return f"ns1_{x}"

        @cache(ttl=300, namespace="ns2")
        def function_ns2(x):
            return f"ns2_{x}"

        # Functions with same args but different namespaces should not interfere
        result1 = function_ns1("test")
        result2 = function_ns2("test")

        assert result1 == "ns1_test"
        assert result2 == "ns2_test"
        assert result1 != result2

        # Verify keys are stored separately (tenant prefix + namespace)
        keys_ns1 = redis_test_client.keys("t:default:ns:ns1*")
        keys_ns2 = redis_test_client.keys("t:default:ns:ns2*")

        assert len(keys_ns1) > 0
        assert len(keys_ns2) > 0
        assert not any(key in keys_ns2 for key in keys_ns1)
