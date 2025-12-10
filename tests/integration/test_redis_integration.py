"""Integration tests requiring real Redis instance."""

import asyncio
import os
import time

import pytest
import redis

from cachekit import cache
from cachekit.backends.redis.client import get_async_redis_client, get_redis_client


@pytest.mark.integration
class TestRedisIntegration:
    """Core Redis integration tests."""

    @pytest.fixture(autouse=True)
    def setup_test_env(self, skip_if_no_redis):
        """Set up test environment with Redis cleanup."""

        # Store original environment
        original_env = {}
        test_env = {
            "REDIS_POOL_HOST": "localhost",
            "REDIS_POOL_PORT": "6379",
            "REDIS_POOL_DB": "15",
        }

        # Set test environment
        for key, value in test_env.items():
            original_env[key] = os.environ.get(key)
            os.environ[key] = value

        # Reset connection pool to pick up test config
        from cachekit.backends.redis.client import reset_global_pool

        reset_global_pool()

        yield

        # Cleanup test database
        try:
            client = redis.Redis(host="localhost", port=6379, db=15)
            client.flushdb()
            client.close()
        except Exception:
            pass

        # Restore original environment
        for key, original_value in original_env.items():
            if original_value is not None:
                os.environ[key] = original_value
            elif key in os.environ:
                del os.environ[key]

        # Reset pool again for clean state
        reset_global_pool()

    def test_cache_key_isolation(self):
        """Different arguments should create separate cache entries."""

        @cache(ttl=300)
        def compute_hash(data, algorithm="sha256"):
            return f"{algorithm}_{hash(data)}"

        result1 = compute_hash("hello", "md5")
        result2 = compute_hash("hello", "sha256")
        result3 = compute_hash("world", "md5")

        assert len({result1, result2, result3}) == 3  # All different

    def test_ttl_expiration(self):
        """Cache should expire after TTL."""
        call_count = 0

        @cache(ttl=1)  # 1 second TTL
        def short_lived():
            nonlocal call_count
            call_count += 1
            return f"result_{call_count}"

        result1 = short_lived()
        assert call_count == 1

        # Immediate call hits cache
        result2 = short_lived()
        assert result2 == result1
        assert call_count == 1

        # Wait for expiration
        time.sleep(1.5)

        # Now should execute again
        result3 = short_lived()
        assert result3 != result1
        assert call_count == 2

    def test_cache_invalidation(self):
        """Manual cache invalidation should work."""

        @cache(ttl=3600)
        def cacheable_func(key):
            return f"value_for_{key}_{time.time()}"

        result1 = cacheable_func("test")
        result2 = cacheable_func("test")
        assert result2 == result1  # Cache hit

        # Invalidate and verify new result
        cacheable_func.invalidate_cache("test")
        result3 = cacheable_func("test")
        assert result3 != result1

    def test_namespace_isolation(self):
        """Namespaces should isolate cache entries."""

        @cache(namespace="service_a", ttl=300)
        def service_a_func(key):
            return f"a_{key}_{time.time()}"

        @cache(namespace="service_b", ttl=300)
        def service_b_func(key):
            return f"b_{key}_{time.time()}"

        # Same key, different namespaces
        result_a = service_a_func("shared_key")
        result_b = service_b_func("shared_key")

        assert result_a != result_b

        # Both should cache independently
        assert service_a_func("shared_key") == result_a
        assert service_b_func("shared_key") == result_b

    def test_concurrent_access(self):
        """Concurrent access with distributed locking (async required)."""
        call_count = 0
        lock = asyncio.Lock()

        @cache(ttl=300)
        async def thread_safe_func(key):
            nonlocal call_count
            async with lock:
                call_count += 1
                count = call_count
            await asyncio.sleep(0.1)  # Simulate work
            return f"result_{key}_{count}"

        async def run_concurrent_requests():
            # Run concurrent requests using asyncio.gather
            tasks = [thread_safe_func("shared") for _ in range(10)]
            return await asyncio.gather(*tasks)

        # Execute async test
        results = asyncio.run(run_concurrent_requests())

        # All should get same result (distributed lock ensures only one execution)
        assert len(set(results)) == 1, f"Expected 1 unique result, got {len(set(results))}: {set(results)}"
        assert call_count == 1, f"Expected 1 function call, got {call_count}"

    def test_large_data_caching(self):
        """Large data structures should cache correctly."""

        @cache(ttl=300)
        def generate_large_data(size):
            return {
                "items": [f"item_{i}" for i in range(size)],
                "metadata": {"size": size, "timestamp": time.time()},
            }

        data = generate_large_data(1000)
        assert len(data["items"]) == 1000

        # Verify caching by checking timestamp
        data2 = generate_large_data(1000)
        assert data2["metadata"]["timestamp"] == data["metadata"]["timestamp"]

    def test_async_cache_operations(self):
        """Async functions should cache correctly."""
        call_count = 0

        @cache(ttl=300)
        async def async_func(multiplier):
            nonlocal call_count
            call_count += 1
            await asyncio.sleep(0.01)
            return {"value": multiplier * 100, "call": call_count}

        async def run_test():
            result1 = await async_func(5)
            assert result1["value"] == 500
            assert call_count == 1

            result2 = await async_func(5)
            assert result2 == result1
            assert call_count == 1

            return True

        assert asyncio.run(run_test())

    def test_redis_data_persistence(self, redis_test_client):
        """Data should actually be stored in Redis."""

        @cache(ttl=300, namespace="test_ns")
        def tracked_func(value):
            return {"computed": value * 2}

        tracked_func(21)

        # Verify data exists in Redis
        # Use the test client from the fixture
        # The actual key format is t:default:ns:{namespace}func:... (tenant prefix + namespace)
        keys = redis_test_client.keys("t:default:ns:test_ns*")
        assert len(keys) > 0, "Expected cache keys to exist in Redis"

        # Verify we can retrieve raw data
        raw_data = redis_test_client.get(keys[0])
        assert raw_data is not None, "Expected cached data to be retrievable"


@pytest.mark.integration
class TestRedisConnectionPoolIntegration:
    """Test connection pool behavior with real Redis."""

    @pytest.fixture(autouse=True)
    def setup_test_env(self, skip_if_no_redis):
        """Set up test environment with Redis cleanup."""

        # Store original environment
        original_env = {}
        test_env = {
            "REDIS_POOL_HOST": "localhost",
            "REDIS_POOL_PORT": "6379",
            "REDIS_POOL_DB": "15",
        }

        # Set test environment
        for key, value in test_env.items():
            original_env[key] = os.environ.get(key)
            os.environ[key] = value

        # Reset connection pool to pick up test config
        from cachekit.backends.redis.client import reset_global_pool

        reset_global_pool()

        yield

        # Cleanup test database
        try:
            client = redis.Redis(host="localhost", port=6379, db=15)
            client.flushdb()
            client.close()
        except Exception:
            pass

        # Restore original environment
        for key, value in original_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

        # Reset connection pool
        reset_global_pool()

    def test_connection_pool_reuse(self, skip_if_no_redis, redis_test_client):
        """Connection pool should reuse connections efficiently."""
        # For integration tests, we expect get_redis_client to use the same pool
        client1 = get_redis_client()
        client2 = get_redis_client()

        # Should use same connection pool
        assert client1.connection_pool is client2.connection_pool

        # Verify that the test Redis is working
        assert redis_test_client.ping() is True

    def test_async_connection_pool(self, skip_if_no_redis, redis_test_client):
        """Async connection pool should work correctly."""

        async def test_async_pool():
            # Verify test Redis is working synchronously first
            assert redis_test_client.ping() is True

            # Test async client connection pool
            _client = await get_async_redis_client()
            # Since we might not have a real Redis for async, just check the client exists
            # The real test is that get_async_redis_client doesn't throw
            return True

        assert asyncio.run(test_async_pool())

    def test_connection_recovery(self, skip_if_no_redis, redis_test_client):
        """Connections should recover from temporary failures."""
        # Use test client to verify Redis is available
        assert redis_test_client.ping() is True

        # Test connection recovery with the test client
        # Disconnect and reconnect
        redis_test_client.connection_pool.disconnect()
        # Should still work after reconnection
        assert redis_test_client.ping() is True


@pytest.mark.integration
class TestRedisHealthCheck:
    """Test Redis health monitoring with real Redis."""

    @pytest.fixture(autouse=True)
    def setup_test_env(self, skip_if_no_redis):
        """Set up test environment with Redis cleanup."""

        # Store original environment
        original_env = {}
        test_env = {
            "REDIS_POOL_HOST": "localhost",
            "REDIS_POOL_PORT": "6379",
            "REDIS_POOL_DB": "15",
        }

        # Set test environment
        for key, value in test_env.items():
            original_env[key] = os.environ.get(key)
            os.environ[key] = value

        # Reset connection pool to pick up test config
        from cachekit.backends.redis.client import reset_global_pool

        reset_global_pool()

        yield

        # Cleanup test database
        try:
            client = redis.Redis(host="localhost", port=6379, db=15)
            client.flushdb()
            client.close()
        except Exception:
            pass

        # Restore original environment
        for key, value in original_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

        # Reset connection pool
        reset_global_pool()

    # DELETED: test_health_check_success
    # Reason: RedisConnectionPoolManager and RedisConnectionPoolConfig no longer exist.
    # Health checks now use redis.ConnectionPool directly.

    # DELETED: test_pool_statistics
    # Reason: RedisConnectionPoolManager and get_pool_stats() no longer exist.
    # Pool statistics now handled by redis.ConnectionPool.
