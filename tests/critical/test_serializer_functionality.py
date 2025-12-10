"""
CRITICAL PATH TEST: Rust Serializer Functionality

This test MUST pass for the Rust serializer to be usable.
Tests Rust serializer with real Redis.
"""

import pytest

from cachekit.decorators import cache

from ..utils.redis_test_helpers import RedisIsolationMixin

# Mark all tests in this module as critical
pytestmark = pytest.mark.critical


class TestRustSerializerFunctionality(RedisIsolationMixin):
    """Critical tests for Rust serializer functionality."""

    def test_rust_serializer_basic_types(self):
        """CRITICAL: Rust serializer must handle basic Python types."""
        call_count = 0

        @cache(ttl=300)
        def get_basic_data(data_id):
            nonlocal call_count
            call_count += 1
            return {
                "id": data_id,
                "string": "test",
                "integer": 42,
                "float": 3.14,
                "boolean": True,
                "null": None,
                "list": [1, 2, 3],
                "call_number": call_count,
            }

        # First call - cache miss
        result1 = get_basic_data(1)
        assert result1["string"] == "test"
        assert result1["integer"] == 42
        assert result1["boolean"] is True
        assert result1["call_number"] == 1
        assert call_count == 1

        # Second call - cache hit
        result2 = get_basic_data(1)
        assert result2 == result1
        assert result2["call_number"] == 1  # Should still be 1 (cached)
        assert call_count == 1  # Function not called again

    def test_rust_serializer_complex_types(self):
        """CRITICAL: Rust serializer must handle complex nested types."""
        call_count = 0

        @cache(ttl=300)
        def get_complex_data(data_id):
            nonlocal call_count
            call_count += 1
            return {
                "id": data_id,
                "nested": {"level1": {"level2": [1, 2, {"level3": "deep"}]}},
                "mixed_list": [1, "two", 3.0, None, True],
                "call_number": call_count,
            }

        # First call - cache miss
        result1 = get_complex_data(2)
        assert result1["nested"]["level1"]["level2"][2]["level3"] == "deep"
        assert result1["mixed_list"][4] is True
        assert result1["call_number"] == 1
        assert call_count == 1

        # Second call - cache hit
        result2 = get_complex_data(2)
        assert result2 == result1
        assert result2["call_number"] == 1  # Should still be 1 (cached)
        assert call_count == 1  # Function not called again

    @pytest.mark.skip(reason="Legacy test - MessagePack doesn't serialize Python sets natively")
    def test_rust_serializer_type_preservation(self):
        """CRITICAL: Rust serializer must preserve Python types correctly."""
        call_count = 0

        @cache(ttl=300)
        def get_typed_data(data_id):
            nonlocal call_count
            call_count += 1
            return {
                "id": data_id,
                "tuple": (1, 2, 3),  # Should remain tuple
                "set": {1, 2, 3},  # May become list (acceptable)
                "bool_true": True,  # Must remain bool
                "bool_false": False,  # Must remain bool
                "call_number": call_count,
            }

        # First call - cache miss
        result1 = get_typed_data(3)
        # Rust serializer preserves tuples correctly and converts sets to lists
        assert result1["tuple"] == (1, 2, 3)  # Correct tuple preservation
        assert sorted(result1["set"]) == [1, 2, 3]  # Acceptable conversion
        assert result1["bool_true"] is True
        assert result1["bool_false"] is False
        assert result1["call_number"] == 1
        assert call_count == 1

        # Second call - cache hit
        result2 = get_typed_data(3)
        assert result2 == result1
        assert result2["call_number"] == 1  # Should still be 1 (cached)
        assert call_count == 1  # Function not called again

    @pytest.mark.slow
    def test_rust_serializer_large_data(self):
        """CRITICAL: Rust serializer must handle large data structures."""
        call_count = 0

        @cache(ttl=300)
        def get_large_data(data_id):
            nonlocal call_count
            call_count += 1
            # Create a reasonably large data structure
            return {
                "id": data_id,
                "large_list": list(range(1000)),
                "large_dict": {str(i): i * 2 for i in range(100)},
                "call_number": call_count,
            }

        # First call - cache miss
        result1 = get_large_data(4)
        assert len(result1["large_list"]) == 1000
        assert result1["large_list"][999] == 999
        assert len(result1["large_dict"]) == 100
        assert result1["large_dict"]["99"] == 198
        assert result1["call_number"] == 1
        assert call_count == 1

        # Second call - cache should still work
        result2 = get_large_data(4)
        assert result2 == result1
        assert result2["call_number"] == 1  # Should still be 1 (cached)
        assert call_count == 1  # Function not called again

    def test_rust_serializer_data_integrity(self):
        """CRITICAL: Rust serializer must preserve data integrity."""
        test_data = {
            "string": "test",
            "integer": 42,
            "float": 3.14,
            "boolean": True,
            "null": None,
            "list": [1, 2, "three"],
            "nested": {"key": "value"},
        }

        @cache(ttl=300, namespace="integrity_rust")
        def get_test_data():
            return test_data.copy()

        result = get_test_data()

        # Verify basic structure
        assert result["string"] == "test"
        assert result["integer"] == 42
        assert result["float"] == 3.14
        assert result["boolean"] is True
        assert result["null"] is None
        assert len(result["list"]) == 3
        assert result["list"][2] == "three"
        assert result["nested"]["key"] == "value"

    def test_cache_invalidation_auto_serializer(self):
        """CRITICAL: Cache invalidation must work with default serializer."""
        call_count = 0

        @cache(ttl=300, namespace="invalidate_default", serializer="default")
        def get_counter_data():
            nonlocal call_count
            call_count += 1
            print(f"Function called - count now: {call_count}")
            try:
                result = {"count": call_count, "serializer": "default"}
                print(f"Function returning: {result}")
                return result
            except Exception as e:
                print(f"Function exception: {e}")
                raise

        # First call
        print("First call...")
        result1 = get_counter_data()
        assert result1["count"] == 1
        print(f"First result: {result1}")

        # Second call - should be cached
        print("Second call...")
        result2 = get_counter_data()
        print(f"Second result: {result2}, expected count: 1")
        assert result2["count"] == 1

        # Invalidate cache
        print("Invalidating cache...")
        get_counter_data.invalidate_cache()

        # Third call - should execute function again
        print("Third call...")
        result3 = get_counter_data()
        print(f"Third result: {result3}, expected count: 2")
        assert result3["count"] == 2

    def test_multiple_functions_isolated(self):
        """CRITICAL: Different cached functions should not interfere with each other."""

        @cache(ttl=300, namespace="isolated_func1")
        def get_func1_value():
            return {"source": "func1"}

        @cache(ttl=300, namespace="isolated_func2")
        def get_func2_value():
            return {"source": "func2"}

        @cache(ttl=300, namespace="isolated_func3")
        def get_func3_value():
            return {"source": "func3"}

        # All should work independently
        func1_result = get_func1_value()
        func2_result = get_func2_value()
        func3_result = get_func3_value()

        assert func1_result["source"] == "func1"
        assert func2_result["source"] == "func2"
        assert func3_result["source"] == "func3"

        # Each should maintain their own cache
        func1_result2 = get_func1_value()
        func2_result2 = get_func2_value()
        func3_result2 = get_func3_value()

        assert func1_result2 == func1_result
        assert func2_result2 == func2_result
        assert func3_result2 == func3_result
