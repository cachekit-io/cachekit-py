"""Unit tests for RedisBackend implementation."""

from __future__ import annotations

from unittest.mock import Mock, patch

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
