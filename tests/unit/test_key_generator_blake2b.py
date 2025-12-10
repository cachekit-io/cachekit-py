"""Tests for Blake2b key generation."""

from __future__ import annotations

import time

import numpy as np
import pytest

from cachekit.key_generator import CacheKeyGenerator


class TestBlake2bKeyGeneration:
    """Test Blake2b key generation."""

    def test_blake2b_key_generation(self):
        """Test basic Blake2b key generation."""
        gen = CacheKeyGenerator()

        def test_func(a, b):
            return a + b

        key = gen.generate_key(test_func, (1, 2), {"c": 3})

        # Should generate a valid key
        assert isinstance(key, str)
        assert len(key) > 0
        assert "test_func" in key

        # Should be deterministic
        key2 = gen.generate_key(test_func, (1, 2), {"c": 3})
        assert key == key2

    def test_different_inputs_generate_different_keys(self):
        """Test that different inputs generate different keys."""
        gen = CacheKeyGenerator()

        def test_func(a, b):
            return a + b

        key1 = gen.generate_key(test_func, (1, 2), {"c": 3})
        key2 = gen.generate_key(test_func, (1, 3), {"c": 3})
        key3 = gen.generate_key(test_func, (1, 2), {"c": 4})

        # All keys should be different
        assert key1 != key2
        assert key1 != key3
        assert key2 != key3

    def test_blake2b_performance_simple_data(self):
        """Test Blake2b performance with simple data."""

        def test_func(a, b):
            return a + b

        args = (1, 2)
        kwargs = {"c": 3, "d": "hello"}
        iterations = 1000

        gen = CacheKeyGenerator()
        start = time.perf_counter()
        for _ in range(iterations):
            gen.generate_key(test_func, args, kwargs)
        blake2b_time = time.perf_counter() - start

        print(f"\nBlake2b performance ({iterations} iterations): {blake2b_time:.4f}s")
        # Should complete within reasonable time
        assert blake2b_time < 1.0

    def test_blake2b_performance_complex_data(self):
        """Test Blake2b performance with complex data."""

        def test_func(data):
            return len(data)

        # Complex nested data
        args = ([list(range(100))],)
        kwargs = {
            "metadata": {"version": "1.0", "tags": ["a", "b", "c"] * 10},
            "config": {str(i): i * 2 for i in range(50)},
        }
        iterations = 100

        gen = CacheKeyGenerator()
        start = time.perf_counter()
        for _ in range(iterations):
            gen.generate_key(test_func, args, kwargs)
        blake2b_time = time.perf_counter() - start

        print(f"\nComplex data performance ({iterations} iterations): {blake2b_time:.4f}s")
        # Should complete within reasonable time
        assert blake2b_time < 1.0

    def test_blake2b_with_numpy_arrays_raises_type_error(self):
        """Test that NumPy arrays raise TypeError (fail fast)."""

        def test_func(arr):
            return arr.sum()

        arr = np.arange(1000)
        gen = CacheKeyGenerator()

        # Should raise TypeError for unsupported type
        with pytest.raises(TypeError, match="Unsupported type for cache key generation"):
            gen.generate_key(test_func, (arr,), {})

    def test_blake2b_with_custom_objects(self):
        """Test Blake2b with basic objects that can be pickled."""

        def test_func(data):
            return data["name"]

        # Use basic dict instead of dataclass to avoid pickle issues
        user_data = {"id": 1, "name": "Alice", "tags": ["admin", "user"]}
        gen = CacheKeyGenerator()

        key = gen.generate_key(test_func, (user_data,), {})

        # Should generate valid key
        assert isinstance(key, str)
        assert len(key) > 0

    def test_initialization(self):
        """Test simple initialization without parameters."""
        gen = CacheKeyGenerator()
        # Should initialize without errors
        assert gen is not None

    def test_key_normalization(self):
        """Test that key normalization works correctly."""

        def test_func_with_very_long_name_that_exceeds_normal_limits():
            pass

        # Create very long args to trigger normalization
        long_args = ("x" * 300,)
        gen = CacheKeyGenerator()

        key = gen.generate_key(test_func_with_very_long_name_that_exceeds_normal_limits, long_args, {})

        # Should be within Redis limits
        assert len(key) <= 250
        # Should contain readable prefix
        assert "test_func" in key

    def test_blake2b_256_hash_length(self):
        """Test that Blake2b-256 produces 64 hex chars (32 bytes)."""
        gen = CacheKeyGenerator()

        def test_func(a, b):
            return a + b

        key = gen.generate_key(test_func, (1, 2), {})

        # Extract hash from key (format: ...args:<hash>:<metadata>)
        parts = key.split(":")
        args_index = parts.index("args")
        hash_part = parts[args_index + 1]

        # Blake2b-256 should produce 64 hex characters (32 bytes)
        assert len(hash_part) == 64, f"Expected 64 hex chars, got {len(hash_part)}"
        # Should be valid hex
        int(hash_part, 16)  # Will raise if not valid hex

    def test_compact_metadata_suffix(self):
        """Test compact metadata suffix format (:1s instead of :ic:1:ser:std)."""
        gen = CacheKeyGenerator()

        def test_func():
            return "test"

        # Test with StandardSerializer (default)
        key_std = gen.generate_key(test_func, (), {}, serializer_type="std")
        assert key_std.endswith(":1s"), f"Expected :1s suffix, got {key_std}"

        # Test with AutoSerializer
        key_auto = gen.generate_key(test_func, (), {}, serializer_type="auto")
        assert key_auto.endswith(":1a"), f"Expected :1a suffix, got {key_auto}"

        # Test with OrjsonSerializer
        key_orjson = gen.generate_key(test_func, (), {}, serializer_type="orjson")
        assert key_orjson.endswith(":1o"), f"Expected :1o suffix, got {key_orjson}"

        # Test with integrity_checking=False
        key_no_ic = gen.generate_key(test_func, (), {}, integrity_checking=False, serializer_type="std")
        assert key_no_ic.endswith(":0s"), f"Expected :0s suffix, got {key_no_ic}"

    def test_serializer_codes_mapping(self):
        """Test SERIALIZER_CODES constant is correct."""
        gen = CacheKeyGenerator()

        expected_codes = {
            "std": "s",
            "auto": "a",
            "orjson": "o",
            "arrow": "w",
        }

        assert gen.SERIALIZER_CODES == expected_codes

    def test_bytes_type_support(self):
        """Test that bytes type is supported."""
        gen = CacheKeyGenerator()

        def test_func(data):
            return len(data)

        # Test with bytes
        key1 = gen.generate_key(test_func, (b"binary_data",), {})
        key2 = gen.generate_key(test_func, (b"binary_data",), {})

        # Should be deterministic
        assert key1 == key2

        # Different bytes should produce different keys
        key3 = gen.generate_key(test_func, (b"other_data",), {})
        assert key1 != key3

    def test_unsupported_types_raise_type_error(self):
        """Test that various unsupported types raise TypeError."""
        gen = CacheKeyGenerator()

        def test_func(data):
            return data

        # Test datetime
        import datetime

        with pytest.raises(TypeError, match="Unsupported type"):
            gen.generate_key(test_func, (datetime.datetime.now(),), {})

        # Test set
        with pytest.raises(TypeError, match="Unsupported type"):
            gen.generate_key(test_func, ({1, 2, 3},), {})

        # Test custom class
        class Custom:
            pass

        with pytest.raises(TypeError, match="Unsupported type"):
            gen.generate_key(test_func, (Custom(),), {})

    def test_different_serializers_generate_different_keys(self):
        """Different serializers should produce different keys (metadata isolation)."""
        gen = CacheKeyGenerator()

        def test_func():
            return "test"

        key_std = gen.generate_key(test_func, (), {}, serializer_type="std")
        key_auto = gen.generate_key(test_func, (), {}, serializer_type="auto")
        key_orjson = gen.generate_key(test_func, (), {}, serializer_type="orjson")

        # All keys should be different due to metadata suffix
        assert key_std != key_auto
        assert key_std != key_orjson
        assert key_auto != key_orjson

        # Verify the keys only differ in the last character (serializer code)
        assert key_std[:-1] == key_auto[:-1]  # Same except last char
        assert key_std.endswith("s")
        assert key_auto.endswith("a")
        assert key_orjson.endswith("o")

    def test_kwargs_prefix_isolates_from_args(self):
        """Kwargs with K: prefix should differ from args without prefix."""
        gen = CacheKeyGenerator()

        def test_func(*args, **kwargs):
            return args, kwargs

        # Same values, different context
        key_args = gen.generate_key(test_func, ("key", "value"), {})
        key_kwargs = gen.generate_key(test_func, (), {"key": "key", "value": "value"})

        # K: prefix should differentiate
        assert key_args != key_kwargs

    def test_deeply_nested_collections(self):
        """Test 10-level deep nesting doesn't cause stack overflow."""
        gen = CacheKeyGenerator()

        def test_func(data):
            return data

        # Create 10 levels deep: [[[[[[[[[[42]]]]]]]]]]
        deeply_nested = 42
        for _ in range(10):
            deeply_nested = [deeply_nested]

        key = gen.generate_key(test_func, (deeply_nested,), {})
        assert isinstance(key, str)
        assert len(key) > 0

        # Verify determinism
        key2 = gen.generate_key(test_func, (deeply_nested,), {})
        assert key == key2

    def test_float_negative_zero_normalization(self):
        """Test that -0.0 normalizes to 0.0 (critical for cross-language compatibility)."""
        gen = CacheKeyGenerator()

        def test_func(val):
            return val

        key_zero = gen.generate_key(test_func, (0.0,), {})
        key_neg_zero = gen.generate_key(test_func, (-0.0,), {})

        # CRITICAL: Must produce identical keys
        assert key_zero == key_neg_zero, "Float -0.0 not normalized to 0.0!"

        # Verify it's not just a coincidence (different value should differ)
        key_one = gen.generate_key(test_func, (1.0,), {})
        assert key_zero != key_one
