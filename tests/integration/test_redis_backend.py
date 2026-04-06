"""Unit tests for RedisBackend implementation.

Includes:
- Sync operations (get, set, delete, exists)
- Client management
- Error handling
- Async pool lifecycle (Fix 2, 3 from async implementation bugs)
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest
import redis

from cachekit.backends.base import BackendError, BaseBackend
from cachekit.backends.redis import RedisBackend


@pytest.mark.unit
class TestRedisBackendInitialization:
    """Test RedisBackend initialization and configuration."""

    @patch.dict("os.environ", {"REDIS_URL": "redis://localhost:6379"}, clear=True)
    def test_initialization_with_env_var(self):
        """RedisBackend should initialize with REDIS_URL from environment."""
        with patch("cachekit.backends.redis.backend.DIContainer") as mock_container_class:
            mock_container_instance = Mock()
            mock_provider = Mock()
            mock_container_class.return_value = mock_container_instance
            mock_container_instance.get.return_value = mock_provider

            backend = RedisBackend()
            assert backend._redis_url == "redis://localhost:6379"

    def test_initialization_with_explicit_url(self):
        """RedisBackend should accept explicit redis_url parameter."""
        with patch("cachekit.backends.redis.backend.DIContainer") as mock_container_class:
            mock_container_instance = Mock()
            mock_provider = Mock()
            mock_container_class.return_value = mock_container_instance
            mock_container_instance.get.return_value = mock_provider

            backend = RedisBackend(redis_url="redis://custom:1234")
            assert backend._redis_url == "redis://custom:1234"

    @patch.dict("os.environ", {}, clear=True)
    def test_initialization_without_redis_url(self):
        """RedisBackend should use default localhost URL when env not configured."""
        with patch("cachekit.backends.redis.backend.DIContainer") as mock_container_class:
            mock_container_instance = Mock()
            mock_provider = Mock()
            mock_container_class.return_value = mock_container_instance
            mock_container_instance.get.return_value = mock_provider

            backend = RedisBackend()
            # Should use default redis://localhost:6379 from config
            assert backend._redis_url == "redis://localhost:6379"

    # Test removed: relied on config cache clearing which doesn't work in unit tests
    # Alternative env vars are tested via config tests

    @patch.dict("os.environ", {"REDIS_URL": "redis://localhost:6379"}, clear=True)
    def test_protocol_compliance(self):
        """RedisBackend should implement BaseBackend protocol."""
        with patch("cachekit.backends.redis.backend.DIContainer") as mock_container_class:
            mock_container_instance = Mock()
            mock_provider = Mock()
            mock_container_class.return_value = mock_container_instance
            mock_container_instance.get.return_value = mock_provider

            backend = RedisBackend()
            assert isinstance(backend, BaseBackend)

    def test_explicit_client_provider_injection(self):
        """RedisBackend should accept explicit client_provider parameter."""
        from cachekit.backends.provider import CacheClientProvider

        mock_provider = Mock(spec=CacheClientProvider)
        backend = RedisBackend("redis://localhost:6379", client_provider=mock_provider)

        # Verify injected provider is used, not global container
        assert backend._client_provider is mock_provider

    def test_backward_compatibility_fallback_to_container(self):
        """RedisBackend should fallback to container when client_provider not provided."""
        with patch("cachekit.backends.redis.backend.DIContainer") as mock_container_class:
            mock_container_instance = Mock()
            mock_provider = Mock()
            mock_container_class.return_value = mock_container_instance
            mock_container_instance.get.return_value = mock_provider

            backend = RedisBackend("redis://localhost:6379")

            # Verify container was called for backward compatibility
            mock_container_instance.get.assert_called_once()
            assert backend._client_provider is mock_provider


@pytest.mark.unit
class TestRedisBackendGet:
    """Test RedisBackend.get() method."""

    @patch.dict("os.environ", {"REDIS_URL": "redis://localhost:6379"}, clear=True)
    def test_get_existing_key_returns_bytes(self):
        """get() should return bytes for existing key."""
        with patch("cachekit.backends.redis.backend.DIContainer") as mock_container_class:
            mock_container_instance = Mock()
            mock_client = Mock()
            mock_client.get.return_value = b"test_value"
            mock_provider = Mock()
            mock_provider.get_sync_client.return_value = mock_client
            mock_container_class.return_value = mock_container_instance
            mock_container_instance.get.return_value = mock_provider

            backend = RedisBackend()
            result = backend.get("test:key")

            assert result == b"test_value"
            mock_client.get.assert_called_once_with("test:key")

    @patch.dict("os.environ", {"REDIS_URL": "redis://localhost:6379"}, clear=True)
    def test_get_missing_key_returns_none(self):
        """get() should return None for missing key."""
        with patch("cachekit.backends.redis.backend.DIContainer") as mock_container_class:
            mock_container_instance = Mock()
            mock_client = Mock()
            mock_client.get.return_value = None
            mock_provider = Mock()
            mock_provider.get_sync_client.return_value = mock_client
            mock_container_class.return_value = mock_container_instance
            mock_container_instance.get.return_value = mock_provider

            backend = RedisBackend()
            result = backend.get("missing:key")

            assert result is None

    @patch.dict("os.environ", {"REDIS_URL": "redis://localhost:6379"}, clear=True)
    def test_get_handles_string_response(self):
        """get() should handle string response from decode_responses=True."""
        with patch("cachekit.backends.redis.backend.DIContainer") as mock_container_class:
            mock_container_instance = Mock()
            mock_container_class.return_value = mock_container_instance
            mock_client = Mock()
            # Redis with decode_responses=True returns str
            mock_client.get.return_value = "string_value"
            mock_provider = Mock()
            mock_provider.get_sync_client.return_value = mock_client
            mock_container_instance.get.return_value = mock_provider

            backend = RedisBackend()
            result = backend.get("test:key")

            # Should convert str to bytes
            assert result == b"string_value"
            assert isinstance(result, bytes)

    @patch.dict("os.environ", {"REDIS_URL": "redis://localhost:6379"}, clear=True)
    def test_get_wraps_redis_exceptions(self):
        """get() should wrap Redis exceptions in BackendError."""
        with patch("cachekit.backends.redis.backend.DIContainer") as mock_container_class:
            mock_container_instance = Mock()
            mock_container_class.return_value = mock_container_instance
            mock_client = Mock()
            mock_client.get.side_effect = redis.ConnectionError("Connection failed")
            mock_provider = Mock()
            mock_provider.get_sync_client.return_value = mock_client
            mock_container_instance.get.return_value = mock_provider

            backend = RedisBackend()
            with pytest.raises(BackendError) as exc_info:
                backend.get("test:key")

            error = exc_info.value
            assert "Redis GET failed" in str(error)
            assert error.operation == "get"
            assert error.key == "test:key"


@pytest.mark.unit
class TestRedisBackendSet:
    """Test RedisBackend.set() method."""

    @patch.dict("os.environ", {"REDIS_URL": "redis://localhost:6379"}, clear=True)
    def test_set_without_ttl(self):
        """set() should use SET command without TTL."""
        with patch("cachekit.backends.redis.backend.DIContainer") as mock_container_class:
            mock_container_instance = Mock()
            mock_container_class.return_value = mock_container_instance
            mock_client = Mock()
            mock_provider = Mock()
            mock_provider.get_sync_client.return_value = mock_client
            mock_container_instance.get.return_value = mock_provider

            backend = RedisBackend()
            backend.set("test:key", b"test_value")

            mock_client.set.assert_called_once_with("test:key", b"test_value")
            mock_client.setex.assert_not_called()

    @patch.dict("os.environ", {"REDIS_URL": "redis://localhost:6379"}, clear=True)
    def test_set_with_ttl(self):
        """set() should use SETEX command with TTL."""
        with patch("cachekit.backends.redis.backend.DIContainer") as mock_container_class:
            mock_container_instance = Mock()
            mock_container_class.return_value = mock_container_instance
            mock_client = Mock()
            mock_provider = Mock()
            mock_provider.get_sync_client.return_value = mock_client
            mock_container_instance.get.return_value = mock_provider

            backend = RedisBackend()
            backend.set("test:key", b"test_value", ttl=60)

            mock_client.setex.assert_called_once_with("test:key", 60, b"test_value")
            mock_client.set.assert_not_called()

    @patch.dict("os.environ", {"REDIS_URL": "redis://localhost:6379"}, clear=True)
    def test_set_with_zero_ttl(self):
        """set() should ignore TTL=0 and use SET."""
        with patch("cachekit.backends.redis.backend.DIContainer") as mock_container_class:
            mock_container_instance = Mock()
            mock_container_class.return_value = mock_container_instance
            mock_client = Mock()
            mock_provider = Mock()
            mock_provider.get_sync_client.return_value = mock_client
            mock_container_instance.get.return_value = mock_provider

            backend = RedisBackend()
            backend.set("test:key", b"test_value", ttl=0)

            # TTL=0 should use SET (not SETEX)
            mock_client.set.assert_called_once_with("test:key", b"test_value")
            mock_client.setex.assert_not_called()

    @patch.dict("os.environ", {"REDIS_URL": "redis://localhost:6379"}, clear=True)
    def test_set_wraps_redis_exceptions(self):
        """set() should wrap Redis exceptions in BackendError."""
        with patch("cachekit.backends.redis.backend.DIContainer") as mock_container_class:
            mock_container_instance = Mock()
            mock_container_class.return_value = mock_container_instance
            mock_client = Mock()
            mock_client.setex.side_effect = redis.TimeoutError("Timeout")
            mock_provider = Mock()
            mock_provider.get_sync_client.return_value = mock_client
            mock_container_instance.get.return_value = mock_provider

            backend = RedisBackend()
            with pytest.raises(BackendError) as exc_info:
                backend.set("test:key", b"value", ttl=60)

            error = exc_info.value
            assert "Redis SET failed" in str(error)
            assert error.operation == "set"
            assert error.key == "test:key"


@pytest.mark.unit
class TestRedisBackendDelete:
    """Test RedisBackend.delete() method."""

    @patch.dict("os.environ", {"REDIS_URL": "redis://localhost:6379"}, clear=True)
    def test_delete_existing_key_returns_true(self):
        """delete() should return True for existing key."""
        with patch("cachekit.backends.redis.backend.DIContainer") as mock_container_class:
            mock_container_instance = Mock()
            mock_container_class.return_value = mock_container_instance
            mock_client = Mock()
            mock_client.delete.return_value = 1  # Redis returns number of keys deleted
            mock_provider = Mock()
            mock_provider.get_sync_client.return_value = mock_client
            mock_container_instance.get.return_value = mock_provider

            backend = RedisBackend()
            result = backend.delete("test:key")

            assert result is True
            mock_client.delete.assert_called_once_with("test:key")

    @patch.dict("os.environ", {"REDIS_URL": "redis://localhost:6379"}, clear=True)
    def test_delete_missing_key_returns_false(self):
        """delete() should return False for missing key."""
        with patch("cachekit.backends.redis.backend.DIContainer") as mock_container_class:
            mock_container_instance = Mock()
            mock_container_class.return_value = mock_container_instance
            mock_client = Mock()
            mock_client.delete.return_value = 0  # Redis returns 0 for non-existent key
            mock_provider = Mock()
            mock_provider.get_sync_client.return_value = mock_client
            mock_container_instance.get.return_value = mock_provider

            backend = RedisBackend()
            result = backend.delete("missing:key")

            assert result is False

    @patch.dict("os.environ", {"REDIS_URL": "redis://localhost:6379"}, clear=True)
    def test_delete_wraps_redis_exceptions(self):
        """delete() should wrap Redis exceptions in BackendError."""
        with patch("cachekit.backends.redis.backend.DIContainer") as mock_container_class:
            mock_container_instance = Mock()
            mock_container_class.return_value = mock_container_instance
            mock_client = Mock()
            mock_client.delete.side_effect = redis.ConnectionError("Connection lost")
            mock_provider = Mock()
            mock_provider.get_sync_client.return_value = mock_client
            mock_container_instance.get.return_value = mock_provider

            backend = RedisBackend()
            with pytest.raises(BackendError) as exc_info:
                backend.delete("test:key")

            error = exc_info.value
            assert "Redis DELETE failed" in str(error)
            assert error.operation == "delete"
            assert error.key == "test:key"


@pytest.mark.unit
class TestRedisBackendExists:
    """Test RedisBackend.exists() method."""

    @patch.dict("os.environ", {"REDIS_URL": "redis://localhost:6379"}, clear=True)
    def test_exists_returns_true_for_existing_key(self):
        """exists() should return True for existing key."""
        with patch("cachekit.backends.redis.backend.DIContainer") as mock_container_class:
            mock_container_instance = Mock()
            mock_container_class.return_value = mock_container_instance
            mock_client = Mock()
            mock_client.exists.return_value = 1  # Redis returns number of keys that exist
            mock_provider = Mock()
            mock_provider.get_sync_client.return_value = mock_client
            mock_container_instance.get.return_value = mock_provider

            backend = RedisBackend()
            result = backend.exists("test:key")

            assert result is True
            mock_client.exists.assert_called_once_with("test:key")

    @patch.dict("os.environ", {"REDIS_URL": "redis://localhost:6379"}, clear=True)
    def test_exists_returns_false_for_missing_key(self):
        """exists() should return False for missing key."""
        with patch("cachekit.backends.redis.backend.DIContainer") as mock_container_class:
            mock_container_instance = Mock()
            mock_container_class.return_value = mock_container_instance
            mock_client = Mock()
            mock_client.exists.return_value = 0
            mock_provider = Mock()
            mock_provider.get_sync_client.return_value = mock_client
            mock_container_instance.get.return_value = mock_provider

            backend = RedisBackend()
            result = backend.exists("missing:key")

            assert result is False

    @patch.dict("os.environ", {"REDIS_URL": "redis://localhost:6379"}, clear=True)
    def test_exists_wraps_redis_exceptions(self):
        """exists() should wrap Redis exceptions in BackendError."""
        with patch("cachekit.backends.redis.backend.DIContainer") as mock_container_class:
            mock_container_instance = Mock()
            mock_container_class.return_value = mock_container_instance
            mock_client = Mock()
            mock_client.exists.side_effect = redis.RedisError("Redis error")
            mock_provider = Mock()
            mock_provider.get_sync_client.return_value = mock_client
            mock_container_instance.get.return_value = mock_provider

            backend = RedisBackend()
            with pytest.raises(BackendError) as exc_info:
                backend.exists("test:key")

            error = exc_info.value
            assert "Redis EXISTS failed" in str(error)
            assert error.operation == "exists"
            assert error.key == "test:key"


@pytest.mark.unit
class TestRedisBackendClientManagement:
    """Test RedisBackend client management."""

    @patch.dict("os.environ", {"REDIS_URL": "redis://localhost:6379"}, clear=True)
    def test_get_client_success(self):
        """_get_client() should return Redis client from provider."""
        with patch("cachekit.backends.redis.backend.DIContainer") as mock_container_class:
            mock_container_instance = Mock()
            mock_container_class.return_value = mock_container_instance
            mock_client = Mock()
            mock_provider = Mock()
            mock_provider.get_sync_client.return_value = mock_client
            mock_container_instance.get.return_value = mock_provider

            backend = RedisBackend()
            client = backend._get_client()

            assert client is mock_client
            mock_provider.get_sync_client.assert_called_once()

    @patch.dict("os.environ", {"REDIS_URL": "redis://localhost:6379"}, clear=True)
    def test_get_client_wraps_exceptions(self):
        """_get_client() should wrap client creation exceptions."""
        with patch("cachekit.backends.redis.backend.DIContainer") as mock_container_class:
            mock_container_instance = Mock()
            mock_container_class.return_value = mock_container_instance
            mock_provider = Mock()
            mock_provider.get_sync_client.side_effect = Exception("Client creation failed")
            mock_container_instance.get.return_value = mock_provider

            backend = RedisBackend()
            with pytest.raises(BackendError) as exc_info:
                backend._get_client()

            error = exc_info.value
            assert "Failed to create Redis client" in str(error)
            assert error.operation == "get_client"


@pytest.mark.unit
class TestRedisBackendErrorMessages:
    """Test RedisBackend error message quality."""

    # Test removed: localhost is now allowed (correct behavior)
    # Config has default redis://localhost:6379 which is valid for development and testing

    @patch.dict("os.environ", {"REDIS_URL": "redis://localhost:6379"}, clear=True)
    def test_operation_errors_include_context(self):
        """Operation errors should include operation type and key."""
        with patch("cachekit.backends.redis.backend.DIContainer") as mock_container_class:
            mock_container_instance = Mock()
            mock_container_class.return_value = mock_container_instance
            mock_client = Mock()
            mock_client.get.side_effect = redis.ConnectionError("Connection failed")
            mock_provider = Mock()
            mock_provider.get_sync_client.return_value = mock_client
            mock_container_instance.get.return_value = mock_provider

            backend = RedisBackend()
            with pytest.raises(BackendError) as exc_info:
                backend.get("cache:user:123")

            error = exc_info.value
            # Should include operation type
            assert error.operation == "get"
            # Should include key for debugging
            assert error.key == "cache:user:123"
            # Should include both in formatted message
            error_msg = str(error)
            assert "operation=get" in error_msg
            assert "cache:user:123" in error_msg


# =============================================================================
# Async Pool Lifecycle Tests
# =============================================================================


@pytest.mark.unit
class TestAsyncPoolClosure:
    """Test that async pool is properly closed in reset_global_pool().

    Bug: Lines 156-158 had a comment "would need await, skip for now"
    Fix: When event loop is running, schedule disconnect() as a task.
    """

    @pytest.mark.asyncio
    async def test_reset_global_pool_disconnects_async_pool_in_event_loop(self):
        """reset_global_pool() should schedule disconnect() when event loop exists.

        Bug: Lines 156-158 had a comment "would need await, skip for now"

        Fix: When event loop is running, schedule disconnect() as a task.
        """
        from cachekit.backends.redis.client import reset_global_pool

        # Create a mock async pool with async disconnect
        mock_async_pool = MagicMock()
        disconnect_called = False

        async def mock_disconnect():
            nonlocal disconnect_called
            disconnect_called = True

        mock_async_pool.disconnect = mock_disconnect

        with patch(
            "cachekit.backends.redis.client._async_pool_instance",
            mock_async_pool,
        ):
            with patch("cachekit.backends.redis.client._pool_instance", None):
                reset_global_pool()

        # Allow task to run
        await asyncio.sleep(0.01)

        # Async pool disconnect should have been called via create_task
        assert disconnect_called, "Async pool disconnect() should be scheduled when event loop exists"

    def test_reset_global_pool_handles_no_event_loop(self):
        """reset_global_pool() should handle case when no event loop is running.

        When called from sync context, it should log and rely on GC for cleanup.
        """
        from cachekit.backends.redis.client import reset_global_pool

        # Create a mock async pool
        mock_async_pool = MagicMock()
        mock_async_pool.disconnect = AsyncMock()

        with patch(
            "cachekit.backends.redis.client._async_pool_instance",
            mock_async_pool,
        ):
            with patch("cachekit.backends.redis.client._pool_instance", None):
                # Should NOT raise - handles missing event loop gracefully
                reset_global_pool()

        # In sync context without event loop, disconnect is NOT called
        # (relies on GC instead)
        # This is expected behavior


@pytest.mark.unit
class TestAsyncPoolRaceCondition:
    """Test that async pool initialization is protected by a lock.

    Bug: Lines 85-94 have no locking, allowing race conditions where
    multiple pools are created and only the last one is kept.

    Fix: Use asyncio.Lock() with double-checked locking pattern.
    """

    @pytest.mark.asyncio
    async def test_concurrent_pool_creation_uses_single_pool(self):
        """Concurrent calls to get_async_redis_client() should share one pool.

        Bug: Lines 85-94 have no locking, allowing race conditions where
        multiple pools are created and only the last one is kept.

        Fix: Use asyncio.Lock() with double-checked locking pattern.
        """
        from cachekit.backends.redis import client as client_module

        # Reset state
        client_module._async_pool_instance = None

        pools_created = []

        # Track pool creation
        def tracking_from_url(*args, **kwargs):
            pool = MagicMock()
            pools_created.append(pool)
            return pool

        with patch.object(
            client_module.redis_async.ConnectionPool,
            "from_url",
            side_effect=tracking_from_url,
        ):
            with patch(
                "cachekit.backends.redis.config.RedisBackendConfig.from_env",
                return_value=MagicMock(redis_url="redis://localhost:6379", connection_pool_size=10),
            ):
                # Simulate concurrent access
                async def get_client():
                    return await client_module.get_async_redis_client()

                # Launch multiple concurrent tasks
                tasks = [get_client() for _ in range(10)]
                clients = await asyncio.gather(*tasks)

        # All clients should be created (10 of them)
        assert len(clients) == 10

        # But only ONE pool should be created (due to locking)
        # Before fix: multiple pools created
        # After fix: exactly 1 pool created
        assert len(pools_created) == 1, f"Expected 1 pool, got {len(pools_created)} - race condition detected!"

        # Cleanup
        client_module._async_pool_instance = None

    @pytest.mark.asyncio
    async def test_async_lock_is_properly_initialized(self):
        """The async pool lock should be created on first use."""
        from cachekit.backends.redis.client import _get_async_pool_lock

        # Get the lock
        lock1 = _get_async_pool_lock()
        lock2 = _get_async_pool_lock()

        # Should return the same lock instance
        assert lock1 is lock2
        assert isinstance(lock1, asyncio.Lock)


# =============================================================================
# Sync Pool Lifecycle Tests
# =============================================================================


@pytest.mark.unit
class TestSyncPoolCreation:
    """Test sync pool creation and caching in get_redis_client().

    Coverage targets:
    - Lines 72-85: Sync pool creation with double-checked locking
    - Lines 154-156: get_cached_sync_redis_client()
    """

    def test_get_redis_client_creates_pool_once(self):
        """get_redis_client() should create pool only once (double-checked locking)."""
        from cachekit.backends.redis import client as client_module

        # Reset state
        client_module._pool_instance = None

        pools_created = []

        def tracking_from_url(*args, **kwargs):
            pool = MagicMock()
            pools_created.append(pool)
            return pool

        with patch.object(
            client_module.redis.ConnectionPool,
            "from_url",
            side_effect=tracking_from_url,
        ):
            with patch(
                "cachekit.backends.redis.config.RedisBackendConfig.from_env",
                return_value=MagicMock(redis_url="redis://localhost:6379", connection_pool_size=10),
            ):
                # Call multiple times
                client1 = client_module.get_redis_client()
                client2 = client_module.get_redis_client()
                client3 = client_module.get_redis_client()

        # All clients should be returned
        assert client1 is not None
        assert client2 is not None
        assert client3 is not None

        # But only ONE pool should be created
        assert len(pools_created) == 1, f"Expected 1 pool, got {len(pools_created)}"

        # Cleanup
        client_module._pool_instance = None

    def test_get_cached_redis_client_uses_thread_local(self):
        """get_cached_redis_client() should cache client per thread."""
        from cachekit.backends.redis import client as client_module

        # Reset state
        client_module._pool_instance = None
        if hasattr(client_module._thread_local, "sync_client"):
            delattr(client_module._thread_local, "sync_client")

        with patch.object(
            client_module.redis.ConnectionPool,
            "from_url",
            return_value=MagicMock(),
        ):
            with patch(
                "cachekit.backends.redis.config.RedisBackendConfig.from_env",
                return_value=MagicMock(redis_url="redis://localhost:6379", connection_pool_size=10),
            ):
                # First call creates client
                client1 = client_module.get_cached_redis_client()

                # Second call should return same instance
                client2 = client_module.get_cached_redis_client()

                assert client1 is client2, "Should return cached client instance"

        # Cleanup
        client_module._pool_instance = None
        if hasattr(client_module._thread_local, "sync_client"):
            delattr(client_module._thread_local, "sync_client")


@pytest.mark.unit
class TestAsyncCachedClient:
    """Test get_cached_async_redis_client() function.

    Coverage target: Line 169 - return await get_async_redis_client()
    """

    @pytest.mark.asyncio
    async def test_get_cached_async_redis_client_delegates(self):
        """get_cached_async_redis_client() should delegate to get_async_redis_client()."""
        from cachekit.backends.redis import client as client_module

        # Reset state
        client_module._async_pool_instance = None

        with patch.object(
            client_module.redis_async.ConnectionPool,
            "from_url",
            return_value=MagicMock(),
        ):
            with patch(
                "cachekit.backends.redis.config.RedisBackendConfig.from_env",
                return_value=MagicMock(redis_url="redis://localhost:6379", connection_pool_size=10),
            ):
                # Call the cached version (should delegate)
                client = await client_module.get_cached_async_redis_client()

                assert client is not None

        # Cleanup
        client_module._async_pool_instance = None

    @pytest.mark.asyncio
    async def test_get_cached_async_redis_client_returns_same_pool(self):
        """get_cached_async_redis_client() should return clients from the same pool."""
        from cachekit.backends.redis import client as client_module

        # Reset state
        client_module._async_pool_instance = None

        mock_pool = MagicMock()
        with patch.object(
            client_module.redis_async.ConnectionPool,
            "from_url",
            return_value=mock_pool,
        ):
            with patch(
                "cachekit.backends.redis.config.RedisBackendConfig.from_env",
                return_value=MagicMock(redis_url="redis://localhost:6379", connection_pool_size=10),
            ):
                client1 = await client_module.get_cached_async_redis_client()
                client2 = await client_module.get_cached_async_redis_client()

                # Both should use the same pool
                assert client1.connection_pool is client2.connection_pool

        # Cleanup
        client_module._async_pool_instance = None


@pytest.mark.unit
class TestResetGlobalPoolAsyncLockReset:
    """Test that reset_global_pool() also resets the async lock.

    Coverage target: Line 204 - _async_pool_lock = None
    """

    def test_reset_global_pool_resets_async_lock(self):
        """reset_global_pool() should reset the async lock for fresh state."""
        from cachekit.backends.redis import client as client_module
        from cachekit.backends.redis.client import reset_global_pool

        # Force creation of the async lock
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            lock_before = client_module._get_async_pool_lock()
            assert lock_before is not None
            assert client_module._async_pool_lock is not None

            # Reset should clear the lock
            reset_global_pool()

            # Lock should be None now
            assert client_module._async_pool_lock is None

            # Getting lock again should create a new one
            lock_after = client_module._get_async_pool_lock()
            assert lock_after is not None
            # New lock should be different instance
            assert lock_after is not lock_before
        finally:
            loop.close()
            asyncio.set_event_loop(None)
