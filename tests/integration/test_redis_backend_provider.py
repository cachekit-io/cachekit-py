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
        token = tenant_context.set("tenant1")
        backend1 = provider.get_backend()
        tenant_context.reset(token)

        token = tenant_context.set("tenant2")
        backend2 = provider.get_backend()
        tenant_context.reset(token)

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


@pytest.fixture
def env_resolved_redis(redis_isolated, monkeypatch):
    """Route the decorator's backend resolution through the production DefaultBackendProvider.

    The autouse DI fixture installs a test provider; this swaps in the real one, pointed at
    the isolated Redis via CACHEKIT_REDIS_URL, so the tests exercise the env-resolved path
    exactly as an application gets it.
    """
    from cachekit.backends.provider import BackendProviderInterface, DefaultBackendProvider
    from cachekit.config.decorator import set_default_backend
    from cachekit.di import DIContainer

    kwargs = redis_isolated.connection_pool.connection_kwargs
    db = kwargs.get("db", 0)
    # pytest-redis spawns a unix-socket server locally; CI points at a TCP service.
    url = f"unix://{kwargs['path']}?db={db}" if "path" in kwargs else f"redis://{kwargs['host']}:{kwargs['port']}/{db}"
    for var in ("CACHEKIT_API_KEY", "CACHEKIT_MEMCACHED_SERVERS", "CACHEKIT_FILE_CACHE_DIR"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("CACHEKIT_REDIS_URL", url)
    set_default_backend(None)
    DIContainer()._singletons[BackendProviderInterface] = DefaultBackendProvider()
    return redis_isolated


def _tenant_prefixes(client) -> set[str]:
    return {key.decode().split(":", 2)[1] for key in client.keys("t:*")}


@pytest.mark.integration
class TestTenantScopedPerOperation:
    """LAB-4773: the decorator resolves its backend once and keeps it, so the backend it
    holds must scope every operation to the tenant of the calling context — not to the
    tenant that happened to make the first call."""

    def test_tenants_calling_in_sequence_write_under_their_own_prefix(self, env_resolved_redis):
        from cachekit import cache

        calls = []

        @cache(ttl=60, l1_enabled=False)  # assert on the L2 keys themselves
        def lookup(x):
            calls.append(x)
            return {"x": x}

        for tenant in ("tenant-a", "org:b"):
            token = tenant_context.set(tenant)
            try:
                assert lookup(1) == {"x": 1}
            finally:
                tenant_context.reset(token)

        assert _tenant_prefixes(env_resolved_redis) == {"tenant-a", "org%3Ab"}
        assert calls == [1, 1], "the second tenant must miss, not read the first tenant's entry"

    def test_context_without_a_tenant_uses_default_not_the_first_caller(self, env_resolved_redis):
        import threading

        from cachekit import cache

        @cache(ttl=60, l1_enabled=False)
        def lookup(x):
            return x

        token = tenant_context.set("tenant-a")
        try:
            lookup(1)
        finally:
            tenant_context.reset(token)

        # A fresh thread starts with an empty context: tenant_context is unset there.
        worker = threading.Thread(target=lookup, args=(2,))
        worker.start()
        worker.join()

        assert _tenant_prefixes(env_resolved_redis) == {"tenant-a", "default"}

    async def test_concurrent_async_tenants_stay_isolated(self, env_resolved_redis):
        import asyncio

        from cachekit import cache

        @cache(ttl=60, l1_enabled=False)
        async def lookup(x):
            await asyncio.sleep(0)
            return x

        async def as_tenant(tenant):
            tenant_context.set(tenant)  # each task runs in its own copy of the context
            for x in range(3):
                await lookup(x)

        await asyncio.gather(*(as_tenant(t) for t in ("tenant-a", "tenant-b", "tenant-c")))

        assert _tenant_prefixes(env_resolved_redis) == {"tenant-a", "tenant-b", "tenant-c"}
        for tenant in ("tenant-a", "tenant-b", "tenant-c"):
            assert len(env_resolved_redis.keys(f"t:{tenant}:*")) == 3

    def test_invalidate_deletes_only_the_calling_tenants_entry(self, env_resolved_redis):
        from cachekit import cache

        @cache(ttl=60, l1_enabled=False)
        def lookup(x):
            return x

        for tenant in ("tenant-a", "tenant-b"):
            token = tenant_context.set(tenant)
            try:
                lookup(1)
            finally:
                tenant_context.reset(token)

        token = tenant_context.set("tenant-b")
        try:
            lookup.invalidate_cache(1)
        finally:
            tenant_context.reset(token)

        assert _tenant_prefixes(env_resolved_redis) == {"tenant-a"}

    async def test_every_operation_follows_the_calling_context(self, redis_isolated):
        """One shared instance: reads, writes, deletes, TTL ops and locks all re-scope per context."""
        backend = PerRequestRedisBackend(redis_isolated, "default", follow_context=True)
        for tenant, wire in (("tenant-a", "tenant-a"), ("org:b", "org%3Ab")):
            token = tenant_context.set(tenant)
            try:
                assert backend.key_prefix == f"t:{wire}:"
                backend.set("k", b"v-" + tenant.encode(), ttl=60)
                assert backend.get("k") == b"v-" + tenant.encode()
                assert backend.exists("k")
                assert await backend.refresh_ttl("k", 120)
                assert 60 < (await backend.get_ttl("k") or 0) <= 120
                async with backend.acquire_lock("k", timeout=5) as acquired:
                    assert acquired
                    assert redis_isolated.exists(f"t:{wire}:k:lock")
                backend.set("gone", b"x")
                assert backend.delete("gone")
            finally:
                tenant_context.reset(token)

        assert sorted(redis_isolated.keys("t:*")) == [b"t:org%3Ab:k", b"t:tenant-a:k"]

    def test_whole_function_invalidate_keeps_other_tenants_tracked(self, env_resolved_redis):
        """A no-args invalidate by one tenant must not untrack another tenant's entries."""
        from cachekit import cache

        @cache(ttl=60, l1_enabled=False)
        def lookup(x):
            return x

        def as_tenant(tenant, fn, *args):
            token = tenant_context.set(tenant)
            try:
                return fn(*args)
            finally:
                tenant_context.reset(token)

        as_tenant("tenant-a", lookup, 1)
        as_tenant("tenant-b", lookup, 1)
        as_tenant("tenant-b", lookup, 2)

        as_tenant("tenant-a", lookup.invalidate_cache)
        assert _tenant_prefixes(env_resolved_redis) == {"tenant-b"}

        as_tenant("tenant-b", lookup.invalidate_cache)
        assert env_resolved_redis.keys("t:*") == []

    async def test_non_str_tenant_id_is_scoped_not_raised(self, env_resolved_redis):
        """Apps set UUID / int tenant ids; the async miss path must cache, not raise."""
        import uuid

        from cachekit import cache

        @cache(ttl=60, l1_enabled=False)
        async def lookup(x):
            return x

        tenant = uuid.UUID("12345678-1234-5678-1234-567812345678")
        tenant_context.set(tenant)  # type: ignore[arg-type]  # async test: own context copy
        assert await lookup(1) == 1
        assert _tenant_prefixes(env_resolved_redis) == {str(tenant)}

    def test_provider_handing_out_get_backend_follows_each_calling_tenant(self, env_resolved_redis):
        """A custom provider returning RedisBackendProvider.get_backend() — the decorator caches it."""
        import os

        from cachekit import cache
        from cachekit.backends.provider import BackendProviderInterface
        from cachekit.di import DIContainer

        redis_provider = RedisBackendProvider(os.environ["CACHEKIT_REDIS_URL"])

        class GetBackendProvider:
            def get_backend(self):
                return redis_provider.get_backend()

        DIContainer()._singletons[BackendProviderInterface] = GetBackendProvider()

        @cache(ttl=60, l1_enabled=False)
        def lookup(x):
            return x

        try:
            for tenant in ("tenant-a", "tenant-b"):
                token = tenant_context.set(tenant)
                try:
                    lookup(1)
                finally:
                    tenant_context.reset(token)
        finally:
            redis_provider.close()

        assert _tenant_prefixes(env_resolved_redis) == {"tenant-a", "tenant-b"}

    def test_request_backend_handed_to_a_worker_thread_keeps_its_tenant(self, env_resolved_redis):
        """A fresh thread has no tenant set: get_backend()'s call-time tenant is the fallback."""
        import os
        import threading

        redis_provider = RedisBackendProvider(os.environ["CACHEKIT_REDIS_URL"])
        token = tenant_context.set("tenant-x")
        try:
            backend = redis_provider.get_backend()
        finally:
            tenant_context.reset(token)

        try:
            worker = threading.Thread(target=backend.set, args=("k", b"v"))
            worker.start()
            worker.join()
        finally:
            redis_provider.close()

        assert env_resolved_redis.keys("t:*") == [b"t:tenant-x:k"]

    def test_direct_binding_is_not_overridden_by_the_context(self, redis_isolated):
        """Explicit construction (admin / fan-out to another tenant) keeps its tenant."""
        backend = PerRequestRedisBackend(redis_isolated, "tenant-x")
        token = tenant_context.set("tenant-y")
        try:
            backend.set("k", b"v")
        finally:
            tenant_context.reset(token)

        assert redis_isolated.keys("t:*") == [b"t:tenant-x:k"]

    def test_tenant_ids_whose_str_is_not_canonical_are_refused(self, redis_isolated):
        """str() of an arbitrary object (default repr embeds id()) could merge two tenants."""
        import uuid

        backend = PerRequestRedisBackend(redis_isolated, "default", follow_context=True)
        for tenant, wire in ((7, "7"), (uuid.UUID(int=1), "00000000-0000-0000-0000-000000000001"), (b"acme", "acme")):
            token = tenant_context.set(tenant)  # type: ignore[arg-type]
            try:
                assert backend.key_prefix == f"t:{wire}:"
            finally:
                tenant_context.reset(token)

        with pytest.raises(TypeError):
            PerRequestRedisBackend(redis_isolated, object())  # type: ignore[arg-type]
        token = tenant_context.set(object())  # type: ignore[arg-type]
        try:
            with pytest.raises(TypeError):
                backend.get("k")
        finally:
            tenant_context.reset(token)
