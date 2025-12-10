"""Tests for cache key generation functionality."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from cachekit.key_generator import CacheKeyGenerator


class TestCacheKeyGenerator:
    """Test cache key generation algorithms."""

    @pytest.fixture
    def key_generator(self):
        """Create a basic key generator instance."""
        return CacheKeyGenerator()

    def test_key_generator_can_be_imported(self):
        """Test that CacheKeyGenerator can be imported."""
        from cachekit.key_generator import CacheKeyGenerator

        assert CacheKeyGenerator is not None

    def test_key_generation_includes_function_name_and_args(self, key_generator):
        """Test key generation includes function name and arguments."""

        def test_func(a, b):
            return a + b

        key = key_generator.generate_key(test_func, (1, 2), {})
        assert isinstance(key, str)
        assert len(key) > 0
        assert "test_func" in key

    def test_key_includes_function_module_and_name(self, key_generator):
        """Test that key includes function module and name."""

        def test_func():
            pass

        key = key_generator.generate_key(test_func, (), {})

        # Should include module and function name
        assert test_func.__module__ in key or "test_func" in key
        assert "test_func" in key

    def test_different_functions_generate_different_keys(self, key_generator):
        """Test that different functions generate different keys."""

        def func_a():
            pass

        def func_b():
            pass

        key_a = key_generator.generate_key(func_a, (), {})
        key_b = key_generator.generate_key(func_b, (), {})

        assert key_a != key_b

    def test_same_args_generate_same_key(self, key_generator):
        """Test that same arguments generate the same key."""

        def test_func(a, b):
            return a + b

        key1 = key_generator.generate_key(test_func, (1, 2), {})
        key2 = key_generator.generate_key(test_func, (1, 2), {})

        assert key1 == key2

    def test_different_args_generate_different_keys(self, key_generator):
        """Test that different arguments generate different keys."""

        def test_func(a, b):
            return a + b

        key1 = key_generator.generate_key(test_func, (1, 2), {})
        key2 = key_generator.generate_key(test_func, (3, 4), {})

        assert key1 != key2

    def test_kwargs_affect_key_generation(self, key_generator):
        """Test that keyword arguments affect key generation."""

        def test_func(a, b=None):
            return a

        key1 = key_generator.generate_key(test_func, (1,), {})
        key2 = key_generator.generate_key(test_func, (1,), {"b": 2})

        assert key1 != key2

    def test_arg_order_consistency(self, key_generator):
        """Test that argument order is handled consistently."""

        def test_func(a, b, c):
            return a + b + c

        # Same args in same order should generate same key
        key1 = key_generator.generate_key(test_func, (1, 2, 3), {})
        key2 = key_generator.generate_key(test_func, (1, 2, 3), {})
        assert key1 == key2

        # Different order should generate different key
        key3 = key_generator.generate_key(test_func, (3, 2, 1), {})
        assert key1 != key3

    def test_kwargs_order_independence(self, key_generator):
        """Test that kwargs order doesn't affect key generation."""

        def test_func(**kwargs):
            return kwargs

        key1 = key_generator.generate_key(test_func, (), {"a": 1, "b": 2})
        key2 = key_generator.generate_key(test_func, (), {"b": 2, "a": 1})

        # Should be the same regardless of dict order
        assert key1 == key2

    def test_complex_data_types(self, key_generator):
        """Test key generation with complex data types."""

        def test_func(data):
            return data

        # Lists
        key1 = key_generator.generate_key(test_func, ([1, 2, 3],), {})
        key2 = key_generator.generate_key(test_func, ([1, 2, 3],), {})
        assert key1 == key2

        # Dicts
        key3 = key_generator.generate_key(test_func, ({"a": 1, "b": 2},), {})
        key4 = key_generator.generate_key(test_func, ({"a": 1, "b": 2},), {})
        assert key3 == key4

        # Different values should generate different keys
        key5 = key_generator.generate_key(test_func, ([1, 2, 4],), {})
        assert key1 != key5

    def test_nested_data_structures(self, key_generator):
        """Test key generation with nested data structures."""

        def test_func(data):
            return data

        nested_data = {
            "users": [
                {"id": 1, "name": "Alice", "tags": ["admin", "user"]},
                {"id": 2, "name": "Bob", "tags": ["user"]},
            ],
            "metadata": {"version": "1.0", "created": "2023-01-01"},
        }

        key1 = key_generator.generate_key(test_func, (nested_data,), {})
        key2 = key_generator.generate_key(test_func, (nested_data,), {})

        assert key1 == key2
        assert isinstance(key1, str)
        assert len(key1) > 0

    def test_unhashable_types_handling(self, key_generator):
        """Test that unhashable types are handled properly."""

        def test_func(data):
            return data

        # Lists and dicts are unhashable but should still work
        key = key_generator.generate_key(test_func, ([1, 2, {"a": 1}],), {})
        assert isinstance(key, str)
        assert len(key) > 0

    def test_special_values(self, key_generator):
        """Test key generation with special values."""

        def test_func(value):
            return value

        # None
        key1 = key_generator.generate_key(test_func, (None,), {})

        # Empty containers
        key2 = key_generator.generate_key(test_func, ([],), {})
        key3 = key_generator.generate_key(test_func, ({},), {})
        key4 = key_generator.generate_key(test_func, ("",), {})

        # All should be different
        keys = [key1, key2, key3, key4]
        assert len(set(keys)) == len(keys)

    def test_namespace_support(self, key_generator):
        """Test that namespace is properly included in keys."""

        def test_func():
            return "test"

        key_no_namespace = key_generator.generate_key(test_func, (), {})
        key_with_namespace = key_generator.generate_key(test_func, (), {}, namespace="myapp")

        assert key_no_namespace != key_with_namespace
        assert "myapp" in key_with_namespace

    def test_key_length_and_format(self, key_generator):
        """Test that generated keys have consistent length and format."""

        def test_func(a, b, c):
            return a + b + c

        key = key_generator.generate_key(test_func, (1, 2, 3), {})

        # Should be a valid cache key (no spaces, reasonable length)
        assert " " not in key
        assert "\n" not in key
        assert len(key) <= 250  # Redis key length limit
        assert len(key) >= 10  # Should be substantial

    def test_collision_resistance(self, key_generator):
        """Test that key generation is collision resistant."""

        def test_func(data):
            return data

        # Generate many keys with similar but different data
        keys = set()
        for i in range(100):
            key = key_generator.generate_key(test_func, (f"data_{i}",), {})
            keys.add(key)

        # Should have no collisions
        assert len(keys) == 100

    def test_deterministic_generation(self, key_generator):
        """Test that key generation is deterministic across instances."""

        def test_func(a, b):
            return a + b

        # Create multiple generators
        gen1 = CacheKeyGenerator()
        gen2 = CacheKeyGenerator()

        key1 = gen1.generate_key(test_func, (1, 2), {"c": 3})
        key2 = gen2.generate_key(test_func, (1, 2), {"c": 3})

        assert key1 == key2

    def test_custom_objects_raise_type_error(self, key_generator):
        """Test that custom objects raise TypeError (fail fast)."""

        @dataclass
        class User:
            id: int
            name: str

        def test_func(user):
            return user.name

        user = User(1, "Alice")

        # Should raise TypeError for unsupported type
        with pytest.raises(TypeError, match="Unsupported type for cache key generation"):
            key_generator.generate_key(test_func, (user,), {})

    def test_performance_with_large_objects(self, key_generator):
        """Test that key generation performs well with large objects."""

        def test_func(data):
            return len(data)

        # Large list
        large_data = list(range(10000))

        import time

        start = time.time()
        key = key_generator.generate_key(test_func, (large_data,), {})
        end = time.time()

        # Should complete quickly (under 100ms)
        assert (end - start) < 0.1
        assert isinstance(key, str)
        assert len(key) > 0
