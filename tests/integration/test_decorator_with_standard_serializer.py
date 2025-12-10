"""Integration tests for @cache() decorator with StandardSerializer.

Tests verify that StandardSerializer is the default serializer and generates
correct cache keys with proper suffixes.
"""

import time

import pytest

from cachekit import cache
from cachekit.key_generator import CacheKeyGenerator


@pytest.mark.integration
class TestDecoratorWithStandardSerializer:
    """Integration tests verifying @cache() uses StandardSerializer by default."""

    def test_default_serializer_is_standard(self, redis_test_client):
        """Verify @cache() uses StandardSerializer by default without explicit serializer parameter."""
        call_count = 0

        @cache(ttl=60)
        def get_user(user_id: int) -> dict:
            nonlocal call_count
            call_count += 1
            return {"id": user_id, "name": "Alice", "email": "alice@example.com"}

        # First call - cache miss, function executes
        result1 = get_user(123)
        assert result1 == {"id": 123, "name": "Alice", "email": "alice@example.com"}
        assert call_count == 1

        # Second call - cache hit, function does NOT execute
        result2 = get_user(123)
        assert result2 == result1
        assert call_count == 1  # Function not called again

        # Verify cache key exists in Redis
        keys = redis_test_client.keys("t:default:*")
        assert len(keys) > 0, "Expected cache keys to exist in Redis"

    def test_cache_key_format_with_integrity_checking_enabled(self, redis_test_client):
        """Verify cache keys use :1s suffix when integrity checking is enabled (default)."""

        @cache(ttl=60, namespace="ic_test")
        def calc(x: int) -> int:
            return x * 2

        # Execute function
        result = calc(5)
        assert result == 10

        # Verify the raw key format by generating it directly
        gen = CacheKeyGenerator()
        raw_key = gen.generate_key(calc, (5,), {}, namespace="ic_test", integrity_checking=True, serializer_type="std")

        # The raw key (before normalization) should end with "1s" (integrity=1, serializer=s)
        assert raw_key.endswith("1s") or ":1s" in raw_key, f"Expected ':1s' in raw key, got: {raw_key}"

        # Verify cache key exists in Redis
        keys = redis_test_client.keys("t:default:ns:ic_test*")
        assert len(keys) > 0, "Expected cache key to exist"

    def test_cache_key_format_with_integrity_checking_disabled(self, redis_test_client):
        """Verify cache keys use :0s suffix when integrity checking is disabled."""

        @cache(ttl=60, namespace="no_ic", integrity_checking=False)
        def calc(x: int) -> int:
            return x * 3

        # Execute function
        result = calc(10)
        assert result == 30

        # Verify the raw key format
        gen = CacheKeyGenerator()
        raw_key = gen.generate_key(calc, (10,), {}, namespace="no_ic", integrity_checking=False, serializer_type="std")

        # The raw key should end with "0s" (integrity=0, serializer=s)
        assert raw_key.endswith("0s") or ":0s" in raw_key, f"Expected ':0s' in raw key, got: {raw_key}"

        # Verify cache key exists in Redis
        keys = redis_test_client.keys("t:default:ns:no_ic*")
        assert len(keys) > 0, "Expected cache key to exist"

    def test_explicit_std_serializer_selection(self, redis_test_client):
        """Verify explicit serializer='std' works and generates :1s suffix."""
        call_count = 0

        @cache(ttl=60, serializer="std", namespace="explicit_std")
        def get_data(key: str) -> dict:
            nonlocal call_count
            call_count += 1
            return {"key": key, "value": "test_data", "count": call_count}

        # First call - cache miss
        result1 = get_data("test_key")
        assert result1["count"] == 1
        assert call_count == 1

        # Second call - cache hit
        result2 = get_data("test_key")
        assert result2["count"] == 1  # Same cached result
        assert call_count == 1  # Function not called again

        # Verify raw key uses StandardSerializer suffix
        gen = CacheKeyGenerator()
        raw_key = gen.generate_key(
            get_data, ("test_key",), {}, namespace="explicit_std", integrity_checking=True, serializer_type="std"
        )
        assert raw_key.endswith("1s") or ":1s" in raw_key, f"Expected ':1s' in raw key, got: {raw_key}"

    def test_explicit_auto_serializer_selection(self, redis_test_client):
        """Verify explicit serializer='auto' uses AutoSerializer with :1a suffix."""
        call_count = 0

        @cache(ttl=60, serializer="auto", namespace="explicit_auto")
        def get_data(key: str) -> dict:
            nonlocal call_count
            call_count += 1
            return {"key": key, "value": "auto_data", "count": call_count}

        # First call - cache miss
        result1 = get_data("auto_key")
        assert result1["count"] == 1
        assert call_count == 1

        # Second call - cache hit
        result2 = get_data("auto_key")
        assert result2["count"] == 1
        assert call_count == 1

        # Verify raw key uses AutoSerializer suffix (:1a)
        gen = CacheKeyGenerator()
        raw_key = gen.generate_key(
            get_data, ("auto_key",), {}, namespace="explicit_auto", integrity_checking=True, serializer_type="auto"
        )
        assert raw_key.endswith("1a") or ":1a" in raw_key, f"Expected ':1a' in raw key for AutoSerializer, got: {raw_key}"

    def test_cross_serializer_isolation(self, redis_test_client):
        """Verify different serializers create different cache keys for same function arguments."""
        std_count = 0
        auto_count = 0

        @cache(ttl=60, serializer="std", namespace="serializer_std")
        def compute_std(x: int) -> dict:
            nonlocal std_count
            std_count += 1
            return {"result": x * 2, "count": std_count, "serializer": "std"}

        @cache(ttl=60, serializer="auto", namespace="serializer_auto")
        def compute_auto(x: int) -> dict:
            nonlocal auto_count
            auto_count += 1
            return {"result": x * 2, "count": auto_count, "serializer": "auto"}

        # Call both functions with same argument
        result_std = compute_std(5)
        result_auto = compute_auto(5)

        assert result_std["serializer"] == "std"
        assert result_auto["serializer"] == "auto"
        assert std_count == 1
        assert auto_count == 1

        # Verify different cache keys exist
        std_keys = redis_test_client.keys("t:default:ns:serializer_std*")
        auto_keys = redis_test_client.keys("t:default:ns:serializer_auto*")

        assert len(std_keys) > 0, "Expected StandardSerializer cache key"
        assert len(auto_keys) > 0, "Expected AutoSerializer cache key"

        # Verify raw keys have different serializer suffixes
        gen = CacheKeyGenerator()
        raw_key_std = gen.generate_key(
            compute_std, (5,), {}, namespace="serializer_std", integrity_checking=True, serializer_type="std"
        )
        raw_key_auto = gen.generate_key(
            compute_auto, (5,), {}, namespace="serializer_auto", integrity_checking=True, serializer_type="auto"
        )

        assert raw_key_std.endswith("1s") or ":1s" in raw_key_std, f"Expected ':1s' for StandardSerializer, got: {raw_key_std}"
        assert raw_key_auto.endswith("1a") or ":1a" in raw_key_auto, f"Expected ':1a' for AutoSerializer, got: {raw_key_auto}"

    def test_real_redis_integration_with_cache_hits_and_misses(self, redis_test_client):
        """Verify actual cache hits and misses with real Redis backend."""
        call_count = 0

        @cache(ttl=60, namespace="hit_miss_test")
        def expensive_computation(user_id: int, multiplier: int = 10) -> dict:
            nonlocal call_count
            call_count += 1
            time.sleep(0.01)  # Simulate expensive work
            return {
                "user_id": user_id,
                "result": user_id * multiplier,
                "call_count": call_count,
                "timestamp": time.time(),
            }

        # First call - cache miss
        result1 = expensive_computation(123, multiplier=5)
        assert result1["user_id"] == 123
        assert result1["result"] == 615  # 123 * 5
        assert result1["call_count"] == 1
        assert call_count == 1
        timestamp1 = result1["timestamp"]

        # Second call with same args - cache hit
        time.sleep(0.05)  # Small delay to show timestamp doesn't change
        result2 = expensive_computation(123, multiplier=5)
        assert result2["user_id"] == 123
        assert result2["result"] == 615
        assert result2["call_count"] == 1  # Still 1, not incremented
        assert result2["timestamp"] == timestamp1  # Same timestamp = cached
        assert call_count == 1  # Function not called again

        # Third call with different args - cache miss
        result3 = expensive_computation(456, multiplier=5)
        assert result3["user_id"] == 456
        assert result3["result"] == 2280  # 456 * 5
        assert result3["call_count"] == 2  # New call
        assert call_count == 2

        # Fourth call with first args again - cache hit
        result4 = expensive_computation(123, multiplier=5)
        assert result4 == result2  # Same cached result
        assert call_count == 2  # Still 2

        # Verify cache keys in Redis
        keys = redis_test_client.keys("t:default:ns:hit_miss_test*")
        assert len(keys) == 2, f"Expected 2 cache keys (for 2 unique arg sets), got {len(keys)}"

    def test_default_serializer_with_different_integrity_settings(self, redis_test_client):
        """Verify default serializer (StandardSerializer) works with both integrity settings and creates different cache keys."""
        count_with = 0
        count_without = 0

        @cache(ttl=60, namespace="with_ic", integrity_checking=True)
        def cw(x: int) -> int:  # Short function name to avoid key truncation
            nonlocal count_with
            count_with += 1
            return x * 3

        @cache(ttl=60, namespace="without_ic", integrity_checking=False)
        def co(x: int) -> int:  # Short function name to avoid key truncation
            nonlocal count_without
            count_without += 1
            return x * 3

        # Execute both functions
        result1 = cw(10)
        result2 = co(10)

        assert result1 == 30
        assert result2 == 30
        assert count_with == 1
        assert count_without == 1

        # Second calls - should be cached
        result3 = cw(10)
        result4 = co(10)

        assert result3 == 30
        assert result4 == 30
        assert count_with == 1  # Not incremented - cached
        assert count_without == 1  # Not incremented - cached

        # Verify different cache keys exist in Redis
        with_ic_keys = redis_test_client.keys("t:default:ns:with_ic*")
        without_ic_keys = redis_test_client.keys("t:default:ns:without_ic*")

        assert len(with_ic_keys) > 0, "Expected cache key with integrity checking to exist"
        assert len(without_ic_keys) > 0, "Expected cache key without integrity checking to exist"

    def test_standard_serializer_with_complex_data_types(self, redis_test_client):
        """Verify StandardSerializer handles complex nested data structures."""
        call_count = 0

        @cache(ttl=60, namespace="complex_data")
        def get_complex_data(user_id: int) -> dict:
            nonlocal call_count
            call_count += 1
            return {
                "user_id": user_id,
                "profile": {
                    "name": "Alice",
                    "age": 30,
                    "settings": {"theme": "dark", "notifications": True},
                },
                "scores": [85, 92, 78, 90],
                "tags": ["vip", "verified"],
                "metadata": {
                    "created_at": "2025-11-17T10:00:00Z",
                    "updated_at": "2025-11-17T12:00:00Z",
                },
            }

        # First call - cache miss
        result1 = get_complex_data(123)
        assert result1["user_id"] == 123
        assert result1["profile"]["name"] == "Alice"
        assert result1["scores"] == [85, 92, 78, 90]
        assert call_count == 1

        # Second call - cache hit with full data preservation
        result2 = get_complex_data(123)
        assert result2 == result1
        assert result2["profile"]["settings"]["theme"] == "dark"
        assert result2["tags"] == ["vip", "verified"]
        assert call_count == 1  # Function not called again

        # Verify cache key exists
        keys = redis_test_client.keys("t:default:ns:complex_data*")
        assert len(keys) > 0, "Expected cache key to exist"
