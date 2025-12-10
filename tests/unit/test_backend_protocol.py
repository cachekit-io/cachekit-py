"""Unit tests for BaseBackend protocol and BackendError exception."""

from __future__ import annotations

from typing import Optional
from unittest.mock import Mock

import pytest

from cachekit.backends.base import BackendError, BaseBackend


@pytest.mark.unit
class TestBackendError:
    """Test BackendError exception functionality."""

    def test_basic_error_message(self):
        """BackendError should format basic error message."""
        error = BackendError("Something went wrong")
        assert "Something went wrong" in str(error)
        assert error.message == "Something went wrong"
        assert error.operation is None
        assert error.key is None

    def test_error_with_operation(self):
        """BackendError should include operation in formatted message."""
        error = BackendError("Failed to retrieve", operation="get")
        error_msg = str(error)
        assert "Failed to retrieve" in error_msg
        assert "operation=get" in error_msg
        assert error.operation == "get"

    def test_error_with_key(self):
        """BackendError should include key in formatted message."""
        error = BackendError("Failed to store", operation="set", key="cache:user:123")
        error_msg = str(error)
        assert "Failed to store" in error_msg
        assert "operation=set" in error_msg
        assert "key=cache:user:123" in error_msg
        assert error.key == "cache:user:123"

    def test_error_with_long_key_truncation(self):
        """BackendError should truncate long keys for readability."""
        long_key = "cache:" + "x" * 100
        error = BackendError("Failed", operation="get", key=long_key)
        error_msg = str(error)
        assert "..." in error_msg
        assert len(error_msg) < len(long_key) + 50  # Truncated

    def test_error_serializability(self):
        """BackendError should contain only serializable types."""
        error = BackendError("Test error", operation="set", key="test:key")
        # Verify all attributes are simple types
        assert isinstance(error.message, str)
        assert isinstance(error.operation, str) or error.operation is None
        assert isinstance(error.key, str) or error.key is None

    def test_error_inheritance(self):
        """BackendError should be a proper Exception subclass."""
        error = BackendError("Test")
        assert isinstance(error, Exception)
        assert isinstance(error, BackendError)


@pytest.mark.unit
class TestBaseBackendProtocol:
    """Test BaseBackend protocol contract."""

    def test_protocol_runtime_checkable(self):
        """BaseBackend should be runtime checkable."""
        # Create a mock implementation
        mock_backend = Mock(spec=["get", "set", "delete", "exists"])
        mock_backend.get = Mock(return_value=b"value")
        mock_backend.set = Mock(return_value=None)
        mock_backend.delete = Mock(return_value=True)
        mock_backend.exists = Mock(return_value=True)

        # Protocol should allow isinstance check
        # Note: Mock objects don't satisfy Protocol checks, so we test with a real class
        class MockBackend:
            def get(self, key: str) -> Optional[bytes]:
                return b"value"

            def set(self, key: str, value: bytes, ttl: Optional[int] = None) -> None:
                pass

            def delete(self, key: str) -> bool:
                return True

            def exists(self, key: str) -> bool:
                return True

            def health_check(self):
                return True, {"latency_ms": 1.0, "backend_type": "mock"}

        backend = MockBackend()
        assert isinstance(backend, BaseBackend)

    def test_protocol_requires_all_methods(self):
        """BaseBackend protocol should require all four methods."""

        # Missing delete method
        class IncompleteBackend:
            def get(self, key: str) -> Optional[bytes]:
                return None

            def set(self, key: str, value: bytes, ttl: Optional[int] = None) -> None:
                pass

            def exists(self, key: str) -> bool:
                return False

        backend = IncompleteBackend()
        # Protocol checking happens at runtime with isinstance()
        assert not isinstance(backend, BaseBackend)

    def test_protocol_signature_validation(self):
        """BaseBackend methods should have correct signatures."""

        class TestBackend:
            def get(self, key: str) -> Optional[bytes]:
                return b"test"

            def set(self, key: str, value: bytes, ttl: Optional[int] = None) -> None:
                pass

            def delete(self, key: str) -> bool:
                return True

            def exists(self, key: str) -> bool:
                return True

            def health_check(self):
                return True, {"latency_ms": 1.0, "backend_type": "test"}

        backend = TestBackend()
        assert isinstance(backend, BaseBackend)

        # Verify method signatures
        import inspect

        sig = inspect.signature(backend.get)
        assert "key" in sig.parameters
        # Return annotation is a string due to __future__ annotations
        assert sig.return_annotation in ("Optional[bytes]", Optional[bytes])

        sig = inspect.signature(backend.set)
        assert "key" in sig.parameters
        assert "value" in sig.parameters
        assert "ttl" in sig.parameters

    def test_mock_backend_implementation(self):
        """Mock backend implementation should work with protocol."""

        class MockBackend:
            def __init__(self):
                self.data = {}

            def get(self, key: str) -> Optional[bytes]:
                return self.data.get(key)

            def set(self, key: str, value: bytes, ttl: Optional[int] = None) -> None:
                self.data[key] = value

            def delete(self, key: str) -> bool:
                if key in self.data:
                    del self.data[key]
                    return True
                return False

            def exists(self, key: str) -> bool:
                return key in self.data

            def health_check(self):
                return True, {"latency_ms": 1.0, "backend_type": "mock"}

        backend = MockBackend()
        assert isinstance(backend, BaseBackend)

        # Test operations
        backend.set("key1", b"value1")
        assert backend.get("key1") == b"value1"
        assert backend.exists("key1") is True
        assert backend.delete("key1") is True
        assert backend.exists("key1") is False
        assert backend.get("key1") is None

    def test_protocol_with_ttl_handling(self):
        """Backend implementation should support optional TTL."""

        class TTLBackend:
            def __init__(self):
                self.data = {}
                self.ttls = {}

            def get(self, key: str) -> Optional[bytes]:
                return self.data.get(key)

            def set(self, key: str, value: bytes, ttl: Optional[int] = None) -> None:
                self.data[key] = value
                if ttl is not None:
                    self.ttls[key] = ttl

            def delete(self, key: str) -> bool:
                deleted = key in self.data
                self.data.pop(key, None)
                self.ttls.pop(key, None)
                return deleted

            def exists(self, key: str) -> bool:
                return key in self.data

            def health_check(self):
                return True, {"latency_ms": 1.0, "backend_type": "ttl"}

        backend = TTLBackend()
        assert isinstance(backend, BaseBackend)

        # Test with TTL
        backend.set("key1", b"value1", ttl=60)
        assert backend.ttls.get("key1") == 60

        # Test without TTL
        backend.set("key2", b"value2")
        assert "key2" not in backend.ttls

    def test_protocol_bytes_only_interface(self):
        """BaseBackend should only operate on bytes."""

        class StrictBackend:
            def __init__(self):
                self.data = {}

            def get(self, key: str) -> Optional[bytes]:
                value = self.data.get(key)
                if value is not None:
                    assert isinstance(value, bytes), "Storage must contain bytes only"
                return value

            def set(self, key: str, value: bytes, ttl: Optional[int] = None) -> None:
                assert isinstance(value, bytes), "Can only store bytes"
                self.data[key] = value

            def delete(self, key: str) -> bool:
                return self.data.pop(key, None) is not None

            def exists(self, key: str) -> bool:
                return key in self.data

            def health_check(self):
                return True, {"latency_ms": 1.0, "backend_type": "strict"}

        backend = StrictBackend()
        assert isinstance(backend, BaseBackend)

        # Bytes work correctly
        backend.set("key1", b"bytes_value")
        assert backend.get("key1") == b"bytes_value"

        # Non-bytes should raise assertion (type safety)
        with pytest.raises(AssertionError, match="Can only store bytes"):
            backend.set("key2", "string_value")  # type: ignore


@pytest.mark.unit
class TestBackendErrorContext:
    """Test BackendError operation context."""

    def test_error_context_for_get_operation(self):
        """BackendError should capture context for get operations."""
        error = BackendError(
            "Connection timeout",
            operation="get",
            key="cache:user:123",
        )
        assert error.operation == "get"
        assert error.key == "cache:user:123"
        assert "get" in str(error)
        assert "cache:user:123" in str(error)

    def test_error_context_for_set_operation(self):
        """BackendError should capture context for set operations."""
        error = BackendError(
            "Write failed",
            operation="set",
            key="cache:session:abc",
        )
        assert error.operation == "set"
        assert error.key == "cache:session:abc"

    def test_error_context_for_delete_operation(self):
        """BackendError should capture context for delete operations."""
        error = BackendError(
            "Delete failed",
            operation="delete",
            key="cache:temp:xyz",
        )
        assert error.operation == "delete"

    def test_error_context_for_exists_operation(self):
        """BackendError should capture context for exists operations."""
        error = BackendError(
            "Exists check failed",
            operation="exists",
            key="cache:check:key",
        )
        assert error.operation == "exists"

    def test_error_without_context(self):
        """BackendError should work without operation context."""
        error = BackendError("Generic error")
        assert error.operation is None
        assert error.key is None
        assert str(error) == "Generic error | type=unknown"
