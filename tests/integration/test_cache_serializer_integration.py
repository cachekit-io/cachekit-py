"""Test CacheSerializer integration with Redis cache decorator."""

from uuid import UUID

import numpy as np

from cachekit import cache


class TestCacheSerializerIntegration:
    """Test cache serializer integration with Redis operations."""

    def test_api_response_caching(self, redis_test_client):
        """Test caching API responses with cache serializer."""

        @cache(ttl=60, namespace="api", serializer="default")
        def get_user_data(user_id: int) -> dict:
            return {
                "id": user_id,
                "name": f"User {user_id}",
                "email": f"user{user_id}@example.com",
                "active": True,
                "scores": [85, 92, 78, 90],
            }

        # First call - should compute
        result1 = get_user_data(123)
        assert result1["id"] == 123
        assert result1["name"] == "User 123"

        # Second call - should be cached
        result2 = get_user_data(123)
        assert result1 == result2

    def test_numpy_array_caching(self, redis_test_client):
        """Test caching NumPy arrays with cache serializer."""

        @cache(ttl=60, namespace="numpy", serializer="default")
        def generate_array(size: int) -> np.ndarray:
            np.random.seed(42)  # Fixed seed for reproducibility
            return np.random.rand(size)

        # First call
        arr1 = generate_array(10)
        assert arr1.shape == (10,)

        # Second call - should be cached
        arr2 = generate_array(10)
        assert np.array_equal(arr1, arr2)

    def test_primitive_caching(self, redis_test_client):
        """Test caching primitives with cache serializer."""

        @cache(ttl=60, namespace="calc", serializer="default")
        def calculate(x: int, y: int) -> int:
            return x * y + x + y

        # Various primitive types
        result1 = calculate(5, 7)
        assert result1 == 47  # 5*7 + 5 + 7

        result2 = calculate(5, 7)
        assert result1 == result2

        @cache(ttl=60, namespace="str", serializer="default")
        def format_string(prefix: str, suffix: str) -> str:
            return f"{prefix}_{suffix}"

        str1 = format_string("hello", "world")
        assert str1 == "hello_world"

        str2 = format_string("hello", "world")
        assert str1 == str2

    def test_mixed_data_caching(self, redis_test_client):
        """Test caching mixed data types with cache serializer."""

        @cache(ttl=60, namespace="mixed", serializer="default")
        def get_mixed_data(key: str) -> list:
            return [1, 2.5, "text", {"nested": True}, [1, 2, 3], None, True]

        data1 = get_mixed_data("test")
        assert len(data1) == 7
        assert data1[0] == 1
        assert data1[1] == 2.5
        assert data1[2] == "text"

        data2 = get_mixed_data("test")
        assert data1 == data2

    def test_binary_data_caching(self, redis_test_client):
        """Test caching binary data with cache serializer."""

        @cache(ttl=60, namespace="binary", serializer="default")
        def get_binary_data(seed: int) -> bytes:
            return f"Binary data {seed}".encode()

        bin1 = get_binary_data(42)
        assert bin1 == b"Binary data 42"

        bin2 = get_binary_data(42)
        assert bin1 == bin2

    def test_complex_object_caching(self, redis_test_client):
        """Test that non-serializable objects are NOT cached (correct security behavior)."""

        class CustomObject:
            def __init__(self, value):
                self.value = value

            def __str__(self):
                return f"CustomObject({self.value})"

        call_count = 0

        @cache(ttl=60, namespace="complex", serializer="default")
        def get_complex_object(value: int) -> CustomObject:
            nonlocal call_count
            call_count += 1
            return CustomObject(value)

        # First call - Function executes, serialization fails, nothing is cached
        obj1 = get_complex_object(123)
        assert isinstance(obj1, CustomObject)
        assert obj1.value == 123
        assert call_count == 1

        # Second call - Cache miss (serialization failed, so nothing was cached)
        # Function executes again. This is CORRECT behavior - we don't use pickle fallback
        obj2 = get_complex_object(123)
        assert isinstance(obj2, CustomObject)
        assert obj2.value == 123
        assert call_count == 2  # Function executed twice - no caching when serialization fails

    def test_cache_invalidation(self, redis_test_client):
        """Test cache invalidation with cache serializer."""
        call_count = 0

        @cache(ttl=60, namespace="inval", serializer="default")
        def get_data(key: str) -> dict:
            nonlocal call_count
            call_count += 1
            return {"key": key, "count": call_count}

        # First call
        result1 = get_data("test")
        assert result1["count"] == 1

        # Second call - cached
        result2 = get_data("test")
        assert result2["count"] == 1

        # Force bypass cache
        result3 = get_data("test", _bypass_cache=True)
        assert result3["count"] == 2

        # Regular call should still get original cached value
        result4 = get_data("test")
        assert result4["count"] == 1


class TestNewTypesRedisIntegration:
    """Test Redis integration for new type support (UUID, set, frozenset)."""

    def test_uuid_roundtrip_through_redis(self, redis_test_client):
        """Test UUID serialization and deserialization through Redis caching."""
        call_count = 0

        @cache(ttl=60, namespace="uuid", serializer="auto")
        def get_user_id() -> dict:
            nonlocal call_count
            call_count += 1
            return {
                "user_id": UUID("12345678-1234-5678-1234-567812345678"),
                "session_id": UUID("87654321-4321-8765-4321-876543218765"),
            }

        # First call - cache miss
        result1 = get_user_id()
        assert isinstance(result1["user_id"], UUID)
        assert result1["user_id"] == UUID("12345678-1234-5678-1234-567812345678")
        assert isinstance(result1["session_id"], UUID)
        assert call_count == 1

        # Second call - cache hit
        result2 = get_user_id()
        assert isinstance(result2["user_id"], UUID)
        assert isinstance(result2["session_id"], UUID)
        assert result1 == result2
        assert call_count == 1  # Not called again - served from cache

    def test_set_roundtrip_through_redis(self, redis_test_client):
        """Test set serialization and deserialization through Redis caching."""
        call_count = 0

        @cache(ttl=60, namespace="sets", serializer="auto")
        def get_tags(user_id: str) -> dict:
            nonlocal call_count
            call_count += 1
            return {
                "permissions": {"read", "write", "delete"},
                "roles": {"admin", "user"},
            }

        # First call - cache miss
        result1 = get_tags("user123")
        assert isinstance(result1["permissions"], set)
        assert result1["permissions"] == {"read", "write", "delete"}
        assert isinstance(result1["roles"], set)
        assert call_count == 1

        # Second call - cache hit
        result2 = get_tags("user123")
        assert isinstance(result2["permissions"], set)
        assert isinstance(result2["roles"], set)
        assert result1 == result2
        assert call_count == 1  # Not called again

    def test_frozenset_type_preservation_through_redis(self, redis_test_client):
        """Test that frozenset type is preserved through Redis roundtrip."""
        call_count = 0

        @cache(ttl=60, namespace="frozensets", serializer="auto")
        def get_config(app: str) -> dict:
            nonlocal call_count
            call_count += 1
            return {
                "locked_features": frozenset(["theme", "language", "timezone"]),
                "allowed_regions": frozenset(["us", "eu", "apac"]),
            }

        # First call - cache miss
        result1 = get_config("web")
        assert isinstance(result1["locked_features"], frozenset)
        assert result1["locked_features"] == frozenset(["theme", "language", "timezone"])
        assert isinstance(result1["allowed_regions"], frozenset)
        assert call_count == 1

        # Second call - cache hit
        result2 = get_config("web")
        assert isinstance(result2["locked_features"], frozenset)
        assert isinstance(result2["allowed_regions"], frozenset)
        assert result1 == result2
        assert call_count == 1  # Not called again

    def test_mixed_new_types_through_redis(self, redis_test_client):
        """Test caching complex objects with mixed new types through Redis."""
        from datetime import datetime

        call_count = 0

        @cache(ttl=60, namespace="mixed_types", serializer="auto")
        def get_user_profile(user_id: str) -> dict:
            nonlocal call_count
            call_count += 1
            return {
                "id": UUID("12345678-1234-5678-1234-567812345678"),
                "name": "Alice",
                "created_at": datetime(2025, 11, 14, 10, 30, 0),
                "permissions": {"read", "write"},
                "locked_settings": frozenset(["theme", "language"]),
                "tags": ["vip", "verified"],
            }

        # First call - cache miss
        result1 = get_user_profile("alice")
        assert isinstance(result1["id"], UUID)
        assert isinstance(result1["created_at"], datetime)
        assert isinstance(result1["permissions"], set)
        assert isinstance(result1["locked_settings"], frozenset)
        assert call_count == 1

        # Second call - cache hit with type preservation
        result2 = get_user_profile("alice")
        assert isinstance(result2["id"], UUID)
        assert isinstance(result2["created_at"], datetime)
        assert isinstance(result2["permissions"], set)
        assert isinstance(result2["locked_settings"], frozenset)
        assert result1 == result2
        assert call_count == 1  # Not called again

    def test_uuid_in_nested_list_through_redis(self, redis_test_client):
        """Test UUIDs in nested lists through Redis caching."""
        call_count = 0

        @cache(ttl=60, namespace="nested_uuid", serializer="auto")
        def get_related_ids(primary_id: str) -> list:
            nonlocal call_count
            call_count += 1
            return [
                UUID("12345678-1234-5678-1234-567812345678"),
                UUID("87654321-4321-8765-4321-876543218765"),
                UUID("abcdefab-cdef-abcd-efab-cdefabcdefab"),
            ]

        # First call - cache miss
        result1 = get_related_ids("id1")
        assert all(isinstance(u, UUID) for u in result1)
        assert len(result1) == 3
        assert call_count == 1

        # Second call - cache hit
        result2 = get_related_ids("id1")
        assert all(isinstance(u, UUID) for u in result2)
        assert result1 == result2
        assert call_count == 1  # Not called again
