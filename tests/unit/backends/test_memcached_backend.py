"""Unit tests for MemcachedBackend.

Tests for backends/memcached/backend.py covering:
- Protocol compliance with BaseBackend
- Basic operations (get, set, delete, exists)
- TTL behavior and 30-day clamping
- Key prefix application
- Error classification via classify_memcached_error
- Health check responses
"""

from __future__ import annotations

import socket
from unittest.mock import MagicMock, patch

import pytest

from cachekit.backends.base import BaseBackend
from cachekit.backends.errors import BackendErrorType
from cachekit.backends.memcached.backend import MemcachedBackend
from cachekit.backends.memcached.config import MAX_MEMCACHED_TTL, MemcachedBackendConfig
from cachekit.backends.memcached.error_handler import classify_memcached_error


@pytest.fixture
def config() -> MemcachedBackendConfig:
    """Create MemcachedBackendConfig with defaults."""
    return MemcachedBackendConfig()


@pytest.fixture
def mock_hash_client():
    """Patch HashClient and return the mock instance."""
    with patch("pymemcache.client.hash.HashClient") as mock_cls:
        mock_instance = MagicMock()
        mock_cls.return_value = mock_instance
        yield mock_instance


@pytest.fixture
def backend(config: MemcachedBackendConfig, mock_hash_client: MagicMock) -> MemcachedBackend:
    """Create MemcachedBackend with mocked HashClient."""
    return MemcachedBackend(config)


@pytest.mark.unit
class TestProtocolCompliance:
    """Test BaseBackend protocol compliance."""

    def test_implements_base_backend_protocol(self, backend: MemcachedBackend) -> None:
        """Verify MemcachedBackend satisfies BaseBackend protocol."""
        assert isinstance(backend, BaseBackend)

    def test_has_required_methods(self, backend: MemcachedBackend) -> None:
        """Verify all required methods exist and are callable."""
        assert callable(backend.get)
        assert callable(backend.set)
        assert callable(backend.delete)
        assert callable(backend.exists)
        assert callable(backend.health_check)


@pytest.mark.unit
class TestBasicOperations:
    """Test basic get/set/delete/exists operations."""

    def test_get_returns_bytes(self, backend: MemcachedBackend, mock_hash_client: MagicMock) -> None:
        """Test get returns bytes when key exists."""
        mock_hash_client.get.return_value = b"cached_value"
        result = backend.get("mykey")
        assert result == b"cached_value"
        assert isinstance(result, bytes)

    def test_get_returns_none_for_missing_key(self, backend: MemcachedBackend, mock_hash_client: MagicMock) -> None:
        """Test get returns None when key does not exist."""
        mock_hash_client.get.return_value = None
        result = backend.get("missing")
        assert result is None

    def test_set_stores_value(self, backend: MemcachedBackend, mock_hash_client: MagicMock) -> None:
        """Test set calls client.set with correct arguments."""
        backend.set("mykey", b"myvalue", ttl=60)
        mock_hash_client.set.assert_called_once_with("mykey", b"myvalue", expire=60)

    def test_delete_returns_true_when_key_exists(self, backend: MemcachedBackend, mock_hash_client: MagicMock) -> None:
        """Test delete returns True when key existed."""
        mock_hash_client.delete.return_value = True
        result = backend.delete("mykey")
        assert result is True

    def test_delete_returns_false_when_key_missing(self, backend: MemcachedBackend, mock_hash_client: MagicMock) -> None:
        """Test delete returns False when key did not exist."""
        mock_hash_client.delete.return_value = False
        result = backend.delete("mykey")
        assert result is False

    def test_delete_passes_noreply_false(self, backend: MemcachedBackend, mock_hash_client: MagicMock) -> None:
        """Test delete passes noreply=False for synchronous response."""
        mock_hash_client.delete.return_value = True
        backend.delete("mykey")
        mock_hash_client.delete.assert_called_once_with("mykey", noreply=False)

    def test_exists_returns_true_when_key_exists(self, backend: MemcachedBackend, mock_hash_client: MagicMock) -> None:
        """Test exists returns True when get returns a value."""
        mock_hash_client.get.return_value = b"some_value"
        result = backend.exists("mykey")
        assert result is True

    def test_exists_returns_false_when_key_missing(self, backend: MemcachedBackend, mock_hash_client: MagicMock) -> None:
        """Test exists returns False when get returns None."""
        mock_hash_client.get.return_value = None
        result = backend.exists("mykey")
        assert result is False


@pytest.mark.unit
class TestTTLBehavior:
    """Test TTL handling and Memcached's 30-day maximum."""

    def test_ttl_none_passes_expire_zero(self, backend: MemcachedBackend, mock_hash_client: MagicMock) -> None:
        """Test ttl=None passes expire=0 (no expiry)."""
        backend.set("key", b"val", ttl=None)
        mock_hash_client.set.assert_called_once_with("key", b"val", expire=0)

    def test_ttl_zero_passes_expire_zero(self, backend: MemcachedBackend, mock_hash_client: MagicMock) -> None:
        """Test ttl=0 passes expire=0 (no expiry)."""
        backend.set("key", b"val", ttl=0)
        mock_hash_client.set.assert_called_once_with("key", b"val", expire=0)

    def test_ttl_positive_passes_expire(self, backend: MemcachedBackend, mock_hash_client: MagicMock) -> None:
        """Test ttl=100 passes expire=100."""
        backend.set("key", b"val", ttl=100)
        mock_hash_client.set.assert_called_once_with("key", b"val", expire=100)

    def test_ttl_exceeding_30_days_gets_clamped(self, backend: MemcachedBackend, mock_hash_client: MagicMock) -> None:
        """Test TTL > 30 days gets clamped to MAX_MEMCACHED_TTL (2592000)."""
        huge_ttl = MAX_MEMCACHED_TTL + 1000
        backend.set("key", b"val", ttl=huge_ttl)
        mock_hash_client.set.assert_called_once_with("key", b"val", expire=MAX_MEMCACHED_TTL)

    def test_ttl_exactly_30_days_not_clamped(self, backend: MemcachedBackend, mock_hash_client: MagicMock) -> None:
        """Test TTL exactly at 30-day max passes through unchanged."""
        backend.set("key", b"val", ttl=MAX_MEMCACHED_TTL)
        mock_hash_client.set.assert_called_once_with("key", b"val", expire=MAX_MEMCACHED_TTL)

    def test_negative_ttl_passes_expire_zero(self, backend: MemcachedBackend, mock_hash_client: MagicMock) -> None:
        """Test negative TTL is treated as no expiry."""
        backend.set("key", b"val", ttl=-5)
        mock_hash_client.set.assert_called_once_with("key", b"val", expire=0)


@pytest.mark.unit
class TestKeyPrefix:
    """Test key prefix application to all operations."""

    @pytest.fixture
    def prefixed_config(self) -> MemcachedBackendConfig:
        """Config with key_prefix set."""
        return MemcachedBackendConfig(key_prefix="app:")

    @pytest.fixture
    def prefixed_backend(self, prefixed_config: MemcachedBackendConfig, mock_hash_client: MagicMock) -> MemcachedBackend:
        """Backend with key prefix configured."""
        return MemcachedBackend(prefixed_config)

    def test_get_applies_prefix(self, prefixed_backend: MemcachedBackend, mock_hash_client: MagicMock) -> None:
        """Test get prepends prefix to key."""
        mock_hash_client.get.return_value = None
        prefixed_backend.get("mykey")
        mock_hash_client.get.assert_called_once_with("app:mykey")

    def test_set_applies_prefix(self, prefixed_backend: MemcachedBackend, mock_hash_client: MagicMock) -> None:
        """Test set prepends prefix to key."""
        prefixed_backend.set("mykey", b"val", ttl=60)
        mock_hash_client.set.assert_called_once_with("app:mykey", b"val", expire=60)

    def test_delete_applies_prefix(self, prefixed_backend: MemcachedBackend, mock_hash_client: MagicMock) -> None:
        """Test delete prepends prefix to key."""
        mock_hash_client.delete.return_value = True
        prefixed_backend.delete("mykey")
        mock_hash_client.delete.assert_called_once_with("app:mykey", noreply=False)

    def test_exists_applies_prefix(self, prefixed_backend: MemcachedBackend, mock_hash_client: MagicMock) -> None:
        """Test exists prepends prefix to key."""
        mock_hash_client.get.return_value = None
        prefixed_backend.exists("mykey")
        mock_hash_client.get.assert_called_once_with("app:mykey")

    def test_no_prefix_when_empty(self, backend: MemcachedBackend, mock_hash_client: MagicMock) -> None:
        """Test no prefix applied when key_prefix is empty."""
        mock_hash_client.get.return_value = None
        backend.get("mykey")
        mock_hash_client.get.assert_called_once_with("mykey")


@pytest.mark.unit
class TestErrorClassification:
    """Test classify_memcached_error maps pymemcache exceptions correctly."""

    def test_socket_timeout_maps_to_timeout(self) -> None:
        """Test socket.timeout is classified as TIMEOUT."""
        exc = socket.timeout("timed out")
        error = classify_memcached_error(exc, operation="get", key="k1")
        assert error.error_type == BackendErrorType.TIMEOUT

    def test_timeout_error_maps_to_timeout(self) -> None:
        """Test TimeoutError is classified as TIMEOUT."""
        exc = TimeoutError("operation timed out")
        error = classify_memcached_error(exc, operation="set", key="k2")
        assert error.error_type == BackendErrorType.TIMEOUT

    def test_unexpected_close_maps_to_transient(self) -> None:
        """Test MemcacheUnexpectedCloseError is classified as TRANSIENT."""
        from pymemcache.exceptions import MemcacheUnexpectedCloseError

        exc = MemcacheUnexpectedCloseError()
        error = classify_memcached_error(exc, operation="get", key="k3")
        assert error.error_type == BackendErrorType.TRANSIENT

    def test_server_error_maps_to_transient(self) -> None:
        """Test MemcacheServerError is classified as TRANSIENT."""
        from pymemcache.exceptions import MemcacheServerError

        exc = MemcacheServerError("SERVER_ERROR out of memory")
        error = classify_memcached_error(exc, operation="set")
        assert error.error_type == BackendErrorType.TRANSIENT

    def test_connection_error_maps_to_transient(self) -> None:
        """Test ConnectionError is classified as TRANSIENT."""
        exc = ConnectionError("Connection refused")
        error = classify_memcached_error(exc, operation="get")
        assert error.error_type == BackendErrorType.TRANSIENT

    def test_os_error_maps_to_transient(self) -> None:
        """Test OSError is classified as TRANSIENT."""
        exc = OSError("Network unreachable")
        error = classify_memcached_error(exc, operation="get")
        assert error.error_type == BackendErrorType.TRANSIENT

    def test_illegal_input_maps_to_permanent(self) -> None:
        """Test MemcacheIllegalInputError is classified as PERMANENT."""
        from pymemcache.exceptions import MemcacheIllegalInputError

        exc = MemcacheIllegalInputError("Key too long")
        error = classify_memcached_error(exc, operation="set", key="k4")
        assert error.error_type == BackendErrorType.PERMANENT

    def test_client_error_maps_to_permanent(self) -> None:
        """Test MemcacheClientError is classified as PERMANENT."""
        from pymemcache.exceptions import MemcacheClientError

        exc = MemcacheClientError("CLIENT_ERROR bad data")
        error = classify_memcached_error(exc, operation="set")
        assert error.error_type == BackendErrorType.PERMANENT

    def test_unknown_exception_maps_to_unknown(self) -> None:
        """Test unrecognized exception is classified as UNKNOWN."""
        exc = RuntimeError("something unexpected")
        error = classify_memcached_error(exc, operation="get", key="k5")
        assert error.error_type == BackendErrorType.UNKNOWN

    def test_error_preserves_operation(self) -> None:
        """Test that operation context is preserved in BackendError."""
        exc = RuntimeError("fail")
        error = classify_memcached_error(exc, operation="delete", key="k6")
        assert error.operation == "delete"
        assert error.key == "k6"

    def test_error_preserves_original_exception(self) -> None:
        """Test that original exception is preserved in BackendError."""
        exc = RuntimeError("original")
        error = classify_memcached_error(exc, operation="get")
        assert error.original_exception is exc


@pytest.mark.unit
class TestHealthCheck:
    """Test health_check method."""

    def test_healthy_returns_true_with_details(self, backend: MemcachedBackend, mock_hash_client: MagicMock) -> None:
        """Test health_check returns (True, details) when server responds."""
        mock_hash_client.stats.return_value = {("127.0.0.1", 11211): {"pid": "1234"}}
        is_healthy, details = backend.health_check()

        assert is_healthy is True
        assert details["backend_type"] == "memcached"
        assert "latency_ms" in details
        assert isinstance(details["latency_ms"], float)
        assert details["servers"] == 1
        assert details["configured_servers"] == 1

    def test_unhealthy_on_empty_stats(self, backend: MemcachedBackend, mock_hash_client: MagicMock) -> None:
        """Test health_check returns (False, ...) when no servers respond."""
        mock_hash_client.stats.return_value = {}
        is_healthy, details = backend.health_check()

        assert is_healthy is False
        assert details["backend_type"] == "memcached"
        assert details["servers"] == 0

    def test_unhealthy_on_exception(self, backend: MemcachedBackend, mock_hash_client: MagicMock) -> None:
        """Test health_check returns (False, details) on exception."""
        mock_hash_client.stats.side_effect = ConnectionError("Connection refused")
        is_healthy, details = backend.health_check()

        assert is_healthy is False
        assert details["backend_type"] == "memcached"
        assert "latency_ms" in details
        assert isinstance(details["latency_ms"], float)
        assert "error" in details
        assert details["servers"] == 0
        assert details["configured_servers"] == 1
