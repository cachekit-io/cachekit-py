"""Test-only backend provider implementations for isolated testing.

This module contains test infrastructure that provides mock/test implementations
of backend providers for use in the test suite. These classes are NOT production
code and should only be imported in test fixtures and test utilities.
"""

from cachekit.backends.provider import BackendProviderInterface, CacheClientProvider


class TestCacheClientProvider(CacheClientProvider):
    """Test Redis client provider for isolated testing."""

    def __init__(self, sync_client=None, async_client=None):
        self.sync_client = sync_client
        self._async_client = async_client
        self._async_client_cache = None

    def get_sync_client(self):
        if self.sync_client is not None:
            return self.sync_client
        from cachekit.backends.redis.client import get_redis_client

        return get_redis_client()

    async def get_async_client(self):
        # If explicit async client provided, use it
        if self._async_client is not None:
            return self._async_client

        # If we have cached async client, return it
        if self._async_client_cache is not None:
            return self._async_client_cache

        # If we have a sync client from pytest-redis, create async client with same connection params
        if self.sync_client is not None:
            try:
                import redis.asyncio as redis_async

                # Extract connection parameters from sync client
                pool = self.sync_client.connection_pool
                connection_kwargs = pool.connection_kwargs.copy()

                # Handle Unix domain socket for async client
                if "path" in connection_kwargs:
                    unix_socket_path = connection_kwargs.pop("path")
                    # Create async client with Unix socket
                    self._async_client_cache = redis_async.Redis.from_url(f"unix://{unix_socket_path}")
                else:
                    # Create async client with TCP connection
                    self._async_client_cache = redis_async.Redis(**connection_kwargs)

                return self._async_client_cache
            except Exception as e:
                # BUG FIX: Never return sync client for async operations
                # This would cause await calls to fail silently
                raise RuntimeError(f"Failed to create async Redis client from sync client connection params: {e}") from e

        # Fallback to default async client
        from cachekit.backends.redis.client import get_async_redis_client

        return await get_async_redis_client()


class TestBackendProvider(BackendProviderInterface):
    """Test backend provider for isolated testing with pytest-redis.

    Accepts a pre-existing Redis client (from pytest-redis fixture) and
    wraps it with per-request backend pattern for tenant isolation.
    """

    def __init__(self, redis_client):
        """Initialize with pytest-redis isolated client.

        Args:
            redis_client: Redis client from pytest-redis fixture
        """
        self._client = redis_client

    def get_backend(self):
        """Get per-request backend wrapper with tenant isolation.

        Returns:
            PerRequestRedisBackend wrapping the test client
        """
        from cachekit.backends.redis.provider import PerRequestRedisBackend, tenant_context

        # Get tenant from ContextVar (defaults to "default" for single-tenant)
        tenant_id = tenant_context.get()
        if tenant_id is None:
            tenant_context.set("default")
            tenant_id = "default"

        return PerRequestRedisBackend(self._client, tenant_id)


__all__ = ["TestCacheClientProvider", "TestBackendProvider"]
