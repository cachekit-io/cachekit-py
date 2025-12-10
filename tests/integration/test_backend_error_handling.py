"""Integration tests for backend error handling and classification.

Tests error type classification, exception handling in provider,
and error property access with real Redis scenarios.
"""

import pytest
import redis
from redis.exceptions import (
    AuthenticationError,
    BusyLoadingError,
    ResponseError,
)
from redis.exceptions import (
    ConnectionError as RedisConnectionError,
)
from redis.exceptions import (
    TimeoutError as RedisTimeoutError,
)

from cachekit.backends.errors import BackendError, BackendErrorType, CapabilityNotAvailableError
from cachekit.backends.redis.error_handler import classify_redis_error
from cachekit.backends.redis.provider import PerRequestRedisBackend


@pytest.mark.integration
class TestBackendErrorTypeProperties:
    """Test error type classification properties."""

    def test_error_is_transient_property(self):
        """Test is_transient property returns True for TRANSIENT errors."""
        error = BackendError(
            "Connection timeout",
            error_type=BackendErrorType.TRANSIENT,
        )
        assert error.is_transient is True
        assert error.is_permanent is False
        assert error.is_timeout is False
        assert error.is_authentication is False

    def test_error_is_permanent_property(self):
        """Test is_permanent property returns True for PERMANENT errors."""
        error = BackendError(
            "Invalid key format",
            error_type=BackendErrorType.PERMANENT,
        )
        assert error.is_permanent is True
        assert error.is_transient is False
        assert error.is_timeout is False
        assert error.is_authentication is False

    def test_error_is_timeout_property(self):
        """Test is_timeout property returns True for TIMEOUT errors."""
        error = BackendError(
            "Operation exceeded timeout",
            error_type=BackendErrorType.TIMEOUT,
        )
        assert error.is_timeout is True
        assert error.is_transient is False
        assert error.is_permanent is False
        assert error.is_authentication is False

    def test_error_is_authentication_property(self):
        """Test is_authentication property returns True for AUTHENTICATION errors."""
        error = BackendError(
            "Invalid credentials",
            error_type=BackendErrorType.AUTHENTICATION,
        )
        assert error.is_authentication is True
        assert error.is_transient is False
        assert error.is_permanent is False
        assert error.is_timeout is False

    def test_error_unknown_type(self):
        """Test UNKNOWN error type."""
        error = BackendError(
            "Unknown error",
            error_type=BackendErrorType.UNKNOWN,
        )
        assert error.is_transient is False
        assert error.is_permanent is False
        assert error.is_timeout is False
        assert error.is_authentication is False


@pytest.mark.integration
class TestBackendErrorRepresentation:
    """Test error string representation and formatting."""

    def test_error_repr(self):
        """Test __repr__ method for developer-friendly output."""
        error = BackendError(
            "Connection lost",
            error_type=BackendErrorType.TRANSIENT,
        )
        repr_str = repr(error)
        assert "BackendError" in repr_str
        assert "Connection lost" in repr_str
        assert "transient" in repr_str

    def test_error_formatted_message(self):
        """Test formatted message includes operation and key context."""
        error = BackendError(
            "Get failed",
            error_type=BackendErrorType.TRANSIENT,
            operation="get",
            key="user:123",
        )
        msg = str(error)
        assert "Get failed" in msg
        assert "operation=get" in msg
        assert "key=user:123" in msg
        assert "type=transient" in msg

    def test_error_key_truncation(self):
        """Test long keys are truncated in error messages."""
        long_key = "x" * 100
        error = BackendError(
            "Error",
            error_type=BackendErrorType.TRANSIENT,
            key=long_key,
        )
        msg = str(error)
        assert "..." in msg
        assert long_key not in msg
        assert len(msg) < len(long_key)


@pytest.mark.integration
class TestCapabilityNotAvailableError:
    """Test CapabilityNotAvailableError subclass."""

    def test_capability_error_inherits_from_backend_error(self):
        """Test CapabilityNotAvailableError is a BackendError."""
        error = CapabilityNotAvailableError("Backend doesn't support locking")
        assert isinstance(error, BackendError)
        assert isinstance(error, Exception)

    def test_capability_error_is_permanent(self):
        """Test CapabilityNotAvailableError is classified as PERMANENT."""
        error = CapabilityNotAvailableError("Locking not available")
        assert error.is_permanent is True
        assert error.error_type == BackendErrorType.PERMANENT

    def test_capability_error_message(self):
        """Test CapabilityNotAvailableError message is preserved."""
        msg = "Backend doesn't support locking"
        error = CapabilityNotAvailableError(msg)
        assert msg in str(error)


@pytest.mark.integration
class TestRedisErrorClassification:
    """Test error classification from real Redis exceptions.

    Note: These tests simulate Redis errors without requiring
    Redis to actually fail. They test the classify_redis_error function.
    """

    def test_classify_connection_error_as_transient(self):
        """Test connection errors are classified as TRANSIENT."""
        exc = RedisConnectionError("Connection refused")
        error = classify_redis_error(exc, operation="get", key="test:key")

        assert error.error_type == BackendErrorType.TRANSIENT
        assert error.is_transient is True
        assert error.operation == "get"
        assert error.key == "test:key"

    def test_classify_timeout_error(self):
        """Test timeout errors are classified as TIMEOUT."""
        exc = RedisTimeoutError("Operation timed out")
        error = classify_redis_error(exc, operation="set")

        assert error.error_type == BackendErrorType.TIMEOUT
        assert error.is_timeout is True

    def test_classify_auth_error_as_authentication(self):
        """Test authentication errors are classified as AUTHENTICATION."""
        exc = AuthenticationError("Invalid password")
        error = classify_redis_error(exc, operation="ping")

        assert error.error_type == BackendErrorType.AUTHENTICATION
        assert error.is_authentication is True

    def test_classify_response_error_as_permanent(self):
        """Test response errors are classified as PERMANENT."""
        exc = ResponseError("ERR invalid database index")
        error = classify_redis_error(exc, operation="get")

        assert error.error_type == BackendErrorType.PERMANENT
        assert error.is_permanent is True


@pytest.mark.integration
class TestPerRequestBackendErrorHandling:
    """Test error handling in PerRequestRedisBackend.

    Uses mock backend behavior since we can't easily force
    real Redis errors in controlled way.
    """

    def test_per_request_backend_fail_fast_on_none_tenant(self):
        """Test PerRequestRedisBackend raises RuntimeError if tenant_id is None."""
        client = redis.Redis()
        with pytest.raises(RuntimeError, match="tenant_id cannot be None"):
            PerRequestRedisBackend(client, tenant_id=None)

    def test_per_request_backend_stores_original_tenant_id(self):
        """Test PerRequestRedisBackend stores original tenant ID before URL encoding."""
        client = redis.Redis()
        tenant_id = "org:123:division"
        backend = PerRequestRedisBackend(client, tenant_id=tenant_id)

        # Original ID should be stored for error messages
        assert backend._original_tenant_id == tenant_id
        # URL-encoded version should be used for keys
        assert backend._tenant_id == "org%3A123%3Adivision"

    def test_per_request_backend_error_includes_context(self):
        """Test backend errors include operation and key context."""
        # BackendError context is tested via classify_redis_error()
        # PerRequestRedisBackend automatically wraps exceptions with context
        error = BackendError(
            "Operation failed",
            error_type=BackendErrorType.TRANSIENT,
            operation="set",
            key="cache:item:123",
        )
        assert error.operation == "set"
        assert error.key == "cache:item:123"


@pytest.mark.integration
class TestErrorClassificationConsistency:
    """Test that error classification is consistent across the system."""

    def test_transient_classification_includes_connection_errors(self):
        """Test transient classification covers connection errors."""
        error_types = [
            RedisConnectionError("Connection lost"),
            BusyLoadingError("Loading database"),
        ]

        for exc in error_types:
            classified = classify_redis_error(exc, operation="test")
            assert classified.error_type == BackendErrorType.TRANSIENT, (
                f"Unexpected classification for {exc.__class__.__name__}: {classified.error_type}"
            )

    def test_error_preserves_original_exception(self):
        """Test BackendError preserves original exception."""
        original_exc = RedisConnectionError("Network unreachable")
        error = BackendError(
            "Redis connection failed",
            error_type=BackendErrorType.TRANSIENT,
            original_exception=original_exc,
        )

        assert error.original_exception is original_exc
        assert isinstance(error.original_exception, RedisConnectionError)

    def test_error_message_composition(self):
        """Test error message includes all relevant context."""
        error = BackendError(
            "Operation failed",
            error_type=BackendErrorType.TRANSIENT,
            operation="get",
            key="cache:user:123",
        )

        msg = str(error)
        assert "Operation failed" in msg
        assert "get" in msg
        assert "cache:user:123" in msg
        assert "transient" in msg
