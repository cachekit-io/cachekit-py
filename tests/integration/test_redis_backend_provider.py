"""Integration tests for RedisBackendProvider and PerRequestRedisBackend.

Tests the optional protocol implementations (TTL, locking, timeouts)
with a real Redis instance.
"""

import time

import pytest
import redis

from cachekit.backends.redis.provider import (
    PerRequestRedisBackend,
    RedisBackendProvider,
    tenant_context,
)


@pytest.mark.integration
class TestRedisBackendProviderHealthCheck:
    """Test health_check() method with real Redis."""

    def test_health_check_success(self, redis_client):
        """Test successful health check returns healthy status."""
        backend = PerRequestRedisBackend(redis_client, tenant_id="test:health")

        is_healthy, details = backend.health_check()

        assert is_healthy is True
        assert details["backend_type"] == "redis"
        assert details["latency_ms"] >= 0
        assert "version" in details
        assert "used_memory_human" in details
        assert "connected_clients" in details

    def test_health_check_latency_measurement(self, redis_client):
        """Test health check measures latency correctly."""
        backend = PerRequestRedisBackend(redis_client, tenant_id="test:latency")

        is_healthy, details = backend.health_check()

        # Latency should be small (< 100ms for localhost)
        assert is_healthy is True
        assert 0 <= details["latency_ms"] < 100


@pytest.mark.integration
class TestRedisBackendProviderTTLProtocol:
    """Test TTLInspectableBackend protocol implementation."""

    @pytest.mark.asyncio
    async def test_get_ttl_with_ttl_set(self, redis_client):
        """Test get_ttl returns correct TTL for key with expiry."""
        backend = PerRequestRedisBackend(redis_client, tenant_id="test:ttl:set")

        # Set key with 60 second TTL
        backend.set("mykey", b"data", ttl=60)

        # Get TTL (method is async but currently not truly async)
        ttl = await backend.get_ttl("mykey")

        assert ttl is not None
        # Should be close to 60 (accounting for execution time)
        assert 55 <= ttl <= 60

    @pytest.mark.asyncio
    async def test_get_ttl_no_expiry(self, redis_client):
        """Test get_ttl returns None for key without expiry."""
        backend = PerRequestRedisBackend(redis_client, tenant_id="test:ttl:no_exp")

        # Set key WITHOUT TTL
        backend.set("mykey", b"data", ttl=None)

        # Get TTL should return None (key exists but no expiry)
        ttl = await backend.get_ttl("mykey")

        assert ttl is None

    @pytest.mark.asyncio
    async def test_get_ttl_nonexistent_key(self, redis_client):
        """Test get_ttl returns None for nonexistent key."""
        backend = PerRequestRedisBackend(redis_client, tenant_id="test:ttl:missing")

        ttl = await backend.get_ttl("nonexistent:key")

        assert ttl is None

    @pytest.mark.asyncio
    async def test_refresh_ttl_success(self, redis_client):
        """Test refresh_ttl updates TTL on existing key."""
        backend = PerRequestRedisBackend(redis_client, tenant_id="test:ttl:refresh")

        # Set key with initial TTL
        backend.set("mykey", b"data", ttl=30)
        initial_ttl = await backend.get_ttl("mykey")
        assert initial_ttl is not None

        # Wait a bit then refresh with new TTL
        time.sleep(1)
        refreshed = await backend.refresh_ttl("mykey", 120)

        assert refreshed is True
        new_ttl = await backend.get_ttl("mykey")
        # New TTL should be significantly higher than initial (waited 1 sec)
        assert new_ttl is not None
        assert new_ttl > initial_ttl + 80  # At least 80+ seconds more

    @pytest.mark.asyncio
    async def test_refresh_ttl_nonexistent_key(self, redis_client):
        """Test refresh_ttl returns False for nonexistent key."""
        backend = PerRequestRedisBackend(redis_client, tenant_id="test:ttl:refresh:missing")

        refreshed = await backend.refresh_ttl("nonexistent:key", 60)

        assert refreshed is False


@pytest.mark.integration
class TestRedisBackendProviderLocking:
    """Test LockableBackend protocol implementation."""

    @pytest.mark.asyncio
    async def test_acquire_lock_success(self, redis_client):
        """Test successful lock acquisition."""
        backend = PerRequestRedisBackend(redis_client, tenant_id="test:lock:acquire")

        async with backend.acquire_lock("mylock", timeout=10) as acquired:
            assert acquired is True

    @pytest.mark.asyncio
    async def test_lock_auto_release(self, redis_client):
        """Test lock is released after context manager exit."""
        backend = PerRequestRedisBackend(redis_client, tenant_id="test:lock:release")

        # Acquire and release lock
        async with backend.acquire_lock("testlock", timeout=5) as acquired:
            assert acquired is True

        # Lock should be released, can acquire again
        async with backend.acquire_lock("testlock", timeout=5) as acquired:
            assert acquired is True


@pytest.mark.integration
class TestRedisBackendProviderTimeout:
    """Test TimeoutConfigurableBackend protocol implementation."""

    @pytest.mark.asyncio
    async def test_with_timeout_context_manager(self, redis_client):
        """Test with_timeout context manager works."""
        backend = PerRequestRedisBackend(redis_client, tenant_id="test:timeout:context")

        # Should not raise error for normal operation
        async with backend.with_timeout("test_op", timeout_ms=5000):
            value = backend.get("somekey")
            assert value is None  # Key doesn't exist, but no timeout

    @pytest.mark.asyncio
    async def test_with_timeout_restores_original(self, redis_client):
        """Test with_timeout restores original socket timeout."""
        backend = PerRequestRedisBackend(redis_client, tenant_id="test:timeout:restore")

        original = backend._client.connection_pool.connection_kwargs.get("socket_timeout")

        # Set timeout
        async with backend.with_timeout("op", timeout_ms=1000):
            modified = backend._client.connection_pool.connection_kwargs.get("socket_timeout")
            assert modified == 1.0  # 1000ms = 1.0 seconds

        # Should be restored
        restored = backend._client.connection_pool.connection_kwargs.get("socket_timeout")
        assert restored == original


@pytest.mark.integration
class TestRedisBackendProviderFactory:
    """Test RedisBackendProvider factory pattern."""

    def test_provider_creates_singleton_pool(self):
        """Test provider creates a singleton connection pool."""
        provider = RedisBackendProvider("redis://localhost:6379", pool_size=10)

        # Pool should be created once
        assert provider._pool is not None
        assert provider._client is not None

        # Multiple get_backend calls should return different backend instances
        # but sharing the same client
        tenant_context.set("tenant1")
        backend1 = provider.get_backend()

        tenant_context.set("tenant2")
        backend2 = provider.get_backend()

        # Different backend instances
        assert backend1 is not backend2

        # But same underlying client
        assert backend1._client is backend2._client

        provider.close()

    def test_provider_requires_tenant_context(self):
        """Test provider fails fast if tenant context not set."""
        provider = RedisBackendProvider("redis://localhost:6379")

        # Reset tenant context
        tenant_context.set(None)

        with pytest.raises(RuntimeError, match="tenant_id cannot be None"):
            provider.get_backend()

        provider.close()

    def test_provider_closeables(self):
        """Test provider cleanup with close()."""
        provider = RedisBackendProvider("redis://localhost:6379")

        # Should not raise error
        provider.close()


@pytest.mark.integration
class TestRedisBackendProviderErrorRecovery:
    """Test error handling in provider operations."""

    def test_get_error_handling(self, redis_client):
        """Test get() error handling."""
        backend = PerRequestRedisBackend(redis_client, tenant_id="test:error:get")

        # Normal operation should work
        backend.set("testkey", b"testvalue")
        value = backend.get("testkey")
        assert value == b"testvalue"

    def test_set_error_handling(self, redis_client):
        """Test set() error handling."""
        backend = PerRequestRedisBackend(redis_client, tenant_id="test:error:set")

        # Should handle large values
        large_value = b"x" * (1024 * 1024)  # 1MB
        backend.set("largekey", large_value)

        retrieved = backend.get("largekey")
        assert retrieved == large_value

    def test_delete_error_handling(self, redis_client):
        """Test delete() error handling."""
        backend = PerRequestRedisBackend(redis_client, tenant_id="test:error:delete")

        # Set and delete
        backend.set("delkey", b"data")
        deleted = backend.delete("delkey")
        assert deleted is True

        # Delete nonexistent key
        deleted = backend.delete("nonexistent")
        assert deleted is False

    def test_exists_error_handling(self, redis_client):
        """Test exists() error handling."""
        backend = PerRequestRedisBackend(redis_client, tenant_id="test:error:exists")

        # Set key
        backend.set("existkey", b"data")
        assert backend.exists("existkey") is True

        # Delete and check
        backend.delete("existkey")
        assert backend.exists("existkey") is False


@pytest.fixture
def redis_client(skip_if_no_redis):
    """Get a Redis client for integration tests."""
    client = redis.Redis(host="localhost", port=6379, db=15)
    # Flush test database before test
    client.flushdb()
    yield client
    # Cleanup after test
    try:
        client.flushdb()
        client.close()
    except Exception:
        pass
