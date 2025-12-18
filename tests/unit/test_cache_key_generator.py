"""Tests for cache key generation functionality."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from pathlib import Path, PurePosixPath, PureWindowsPath
from uuid import UUID

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
        with pytest.raises(TypeError, match="Unsupported type.*for cache key"):
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


class TestExtendedTypeNormalization:
    """Tests for Path, UUID, Decimal, Enum, datetime normalization.

    Per round-table review 2025-12-18: These are safe cross-language types.
    """

    @pytest.fixture
    def key_generator(self):
        """Create a basic key generator instance."""
        return CacheKeyGenerator()

    def test_path_uses_posix(self, key_generator):
        """Path converts to POSIX string format."""
        assert key_generator._normalize(Path("/data/cache/foo")) == "/data/cache/foo"
        # Windows paths also convert to POSIX (forward slashes)
        assert key_generator._normalize(PureWindowsPath("C:\\data\\cache\\foo")) == "C:/data/cache/foo"
        assert key_generator._normalize(PurePosixPath("/a/b/c")) == "/a/b/c"

    def test_path_in_key_generation(self, key_generator):
        """Path works in full key generation."""

        def func(path):
            return str(path)

        key1 = key_generator.generate_key(func, (Path("/data/cache/foo"),), {})
        key2 = key_generator.generate_key(func, (Path("/data/cache/foo"),), {})
        key3 = key_generator.generate_key(func, (Path("/data/cache/bar"),), {})

        assert key1 == key2  # Same path = same key
        assert key1 != key3  # Different path = different key

    def test_uuid_string_format(self, key_generator):
        """UUID normalizes to standard string format."""
        u = UUID("12345678-1234-5678-1234-567812345678")
        assert key_generator._normalize(u) == "12345678-1234-5678-1234-567812345678"

    def test_uuid_in_key_generation(self, key_generator):
        """UUID works in full key generation."""

        def func(user_id):
            return str(user_id)

        u1 = UUID("12345678-1234-5678-1234-567812345678")
        u2 = UUID("12345678-1234-5678-1234-567812345679")

        key1 = key_generator.generate_key(func, (u1,), {})
        key2 = key_generator.generate_key(func, (u1,), {})
        key3 = key_generator.generate_key(func, (u2,), {})

        assert key1 == key2
        assert key1 != key3

    def test_decimal_exact_string(self, key_generator):
        """Decimal preserves exact string representation."""
        # Critical for financial calculations - no floating point precision loss
        d = Decimal("3.14159265358979323846")
        assert key_generator._normalize(d) == "3.14159265358979323846"

        # Large decimal
        big = Decimal("12345678901234567890.123456789")
        assert key_generator._normalize(big) == "12345678901234567890.123456789"

    def test_decimal_in_key_generation(self, key_generator):
        """Decimal works in full key generation."""

        def func(price):
            return float(price)

        key1 = key_generator.generate_key(func, (Decimal("19.99"),), {})
        key2 = key_generator.generate_key(func, (Decimal("19.99"),), {})
        key3 = key_generator.generate_key(func, (Decimal("20.00"),), {})

        assert key1 == key2
        assert key1 != key3

    def test_enum_uses_value(self, key_generator):
        """Enum normalizes using .value."""

        class Color(Enum):
            RED = 1
            GREEN = 2
            BLUE = 3

        assert key_generator._normalize(Color.RED) == 1
        assert key_generator._normalize(Color.GREEN) == 2

    def test_enum_with_string_value(self, key_generator):
        """Enum with string value normalizes correctly."""

        class Status(Enum):
            PENDING = "pending"
            ACTIVE = "active"

        assert key_generator._normalize(Status.PENDING) == "pending"

    def test_enum_in_key_generation(self, key_generator):
        """Enum works in full key generation."""

        class Priority(Enum):
            LOW = 1
            HIGH = 2

        def func(priority):
            return priority.value

        key1 = key_generator.generate_key(func, (Priority.LOW,), {})
        key2 = key_generator.generate_key(func, (Priority.LOW,), {})
        key3 = key_generator.generate_key(func, (Priority.HIGH,), {})

        assert key1 == key2
        assert key1 != key3

    def test_datetime_utc_only(self, key_generator):
        """Datetime normalizes to ISO format (UTC required)."""
        dt_utc = datetime(2024, 1, 15, 12, 0, 0, tzinfo=timezone.utc)
        result = key_generator._normalize(dt_utc)

        assert "2024-01-15" in result
        assert isinstance(result, str)

    def test_datetime_naive_raises(self, key_generator):
        """Naive datetime raises TypeError (timezone ambiguity)."""
        dt_naive = datetime(2024, 1, 15, 12, 0, 0)

        with pytest.raises(TypeError, match="Naive datetime"):
            key_generator._normalize(dt_naive)

    def test_datetime_in_key_generation(self, key_generator):
        """Timezone-aware datetime works in full key generation."""

        def func(timestamp):
            return timestamp.isoformat()

        dt1 = datetime(2024, 1, 15, 12, 0, 0, tzinfo=timezone.utc)
        dt2 = datetime(2024, 1, 15, 13, 0, 0, tzinfo=timezone.utc)

        key1 = key_generator.generate_key(func, (dt1,), {})
        key2 = key_generator.generate_key(func, (dt1,), {})
        key3 = key_generator.generate_key(func, (dt2,), {})

        assert key1 == key2
        assert key1 != key3


class TestConstrainedArrayNormalization:
    """Tests for numpy array support with strict constraints.

    Per round-table review 2025-12-18:
    - 1D only (cross-language simplicity)
    - ≤100KB (memory safety)
    - 4 dtypes: i32, i64, f32, f64
    - 256-bit Blake2b hash
    - Little-endian byte order
    """

    @pytest.fixture
    def key_generator(self):
        """Create a basic key generator instance."""
        return CacheKeyGenerator()

    @pytest.fixture
    def np(self):
        """Import numpy (skip if not available)."""
        pytest.importorskip("numpy")
        import numpy as np

        return np

    def test_1d_float64_works(self, key_generator, np):
        """1D float64 array produces valid normalized list."""
        arr = np.array([1.0, 2.0, 3.0], dtype=np.float64)
        result = key_generator._normalize(arr)

        assert result[0] == "__array_v1__"  # Version prefix
        assert result[1] == [3]  # Shape as list
        assert result[2] == "f64"  # Dtype code
        assert len(result[3]) == 64  # 256-bit = 64 hex chars

    def test_1d_float32_works(self, key_generator, np):
        """1D float32 array produces valid normalized tuple."""
        arr = np.array([1.0, 2.0, 3.0], dtype=np.float32)
        result = key_generator._normalize(arr)

        assert result[2] == "f32"

    def test_1d_int64_works(self, key_generator, np):
        """1D int64 array produces valid normalized tuple."""
        arr = np.array([1, 2, 3], dtype=np.int64)
        result = key_generator._normalize(arr)

        assert result[2] == "i64"

    def test_1d_int32_works(self, key_generator, np):
        """1D int32 array produces valid normalized tuple."""
        arr = np.array([1, 2, 3], dtype=np.int32)
        result = key_generator._normalize(arr)

        assert result[2] == "i32"

    def test_same_content_same_hash(self, key_generator, np):
        """Identical array content produces identical hash."""
        arr1 = np.array([1.0, 2.0, 3.0], dtype=np.float64)
        arr2 = np.array([1.0, 2.0, 3.0], dtype=np.float64)

        result1 = key_generator._normalize(arr1)
        result2 = key_generator._normalize(arr2)

        assert result1[3] == result2[3]  # Same hash

    def test_different_content_different_hash(self, key_generator, np):
        """Different array content produces different hash."""
        arr1 = np.array([1.0, 2.0, 3.0], dtype=np.float64)
        arr2 = np.array([1.0, 2.0, 4.0], dtype=np.float64)

        result1 = key_generator._normalize(arr1)
        result2 = key_generator._normalize(arr2)

        assert result1[3] != result2[3]  # Different hash

    def test_2d_array_rejected(self, key_generator, np):
        """2D arrays are rejected (cross-language simplicity)."""
        arr = np.array([[1, 2], [3, 4]], dtype=np.int32)

        with pytest.raises(TypeError, match="Only 1D arrays"):
            key_generator._normalize(arr)

    def test_3d_array_rejected(self, key_generator, np):
        """3D arrays are rejected."""
        arr = np.zeros((2, 3, 4), dtype=np.float32)

        with pytest.raises(TypeError, match="Only 1D arrays"):
            key_generator._normalize(arr)

    def test_large_array_rejected(self, key_generator, np):
        """Arrays >100KB are rejected (memory safety)."""
        # 100,001 bytes = just over 100KB limit
        arr = np.zeros(100_001, dtype=np.int8)

        with pytest.raises(TypeError, match="Array too large"):
            key_generator._normalize(arr)

    def test_at_limit_array_accepted(self, key_generator, np):
        """Arrays exactly at 100KB are accepted."""
        # 100,000 bytes = exactly at limit (100KB)
        arr = np.zeros(100_000, dtype=np.int8)

        # Should convert to supported dtype
        with pytest.raises(TypeError, match="Unsupported array dtype"):
            # int8 isn't supported, but we test size limit is okay
            key_generator._normalize(arr)

        # With supported dtype at limit
        arr_f32 = np.zeros(25_000, dtype=np.float32)  # 25000 * 4 = 100KB
        result = key_generator._normalize(arr_f32)
        assert result[0] == "__array_v1__"

    def test_unsupported_dtype_rejected(self, key_generator, np):
        """Unsupported dtypes are rejected with helpful message."""
        dtypes_to_reject = [np.float16, np.int8, np.uint32, np.complex64]

        for dtype in dtypes_to_reject:
            arr = np.array([1, 2, 3], dtype=dtype)
            with pytest.raises(TypeError, match="Unsupported array dtype"):
                key_generator._normalize(arr)

    def test_aggregate_limit_enforced(self, key_generator, np):
        """Total array size across all args limited to 5MB (DoS prevention)."""
        # Create arrays that individually pass but aggregate exceeds 5MB
        # 60 arrays of ~100KB each = 6MB > 5MB limit
        arrays = [np.zeros(25_000, dtype=np.float32) for _ in range(60)]

        with pytest.raises(TypeError, match="Total array size exceeds"):
            key_generator._normalize(arrays)

    def test_cross_platform_determinism(self, key_generator, np):
        """Little-endian normalization produces consistent hashes."""
        # Create big-endian array
        arr_be = np.array([1.0, 2.0, 3.0], dtype=">f8")  # Big-endian float64
        result_be = key_generator._normalize(arr_be)

        # Create little-endian array
        arr_le = np.array([1.0, 2.0, 3.0], dtype="<f8")  # Little-endian float64
        result_le = key_generator._normalize(arr_le)

        # Should produce same hash (both normalized to little-endian)
        assert result_be[3] == result_le[3]

    def test_array_in_key_generation(self, key_generator, np):
        """Array works in full key generation."""

        def func(features):
            return features.mean()

        arr1 = np.array([1.0, 2.0, 3.0], dtype=np.float64)
        arr2 = np.array([1.0, 2.0, 4.0], dtype=np.float64)

        key1 = key_generator.generate_key(func, (arr1,), {})
        key2 = key_generator.generate_key(func, (arr1,), {})
        key3 = key_generator.generate_key(func, (arr2,), {})

        assert key1 == key2  # Same array = same key
        assert key1 != key3  # Different array = different key

    def test_mixed_args_with_array(self, key_generator, np):
        """Arrays work alongside other argument types."""

        def func(name, features, threshold):
            return features.mean() > threshold

        arr = np.array([1.0, 2.0, 3.0], dtype=np.float64)

        key1 = key_generator.generate_key(func, ("test", arr, 1.5), {})
        key2 = key_generator.generate_key(func, ("test", arr, 1.5), {})
        key3 = key_generator.generate_key(func, ("test", arr, 2.5), {})

        assert key1 == key2
        assert key1 != key3


class TestUnsupportedTypesWithGuidance:
    """Tests for helpful error messages on unsupported types.

    Per round-table review 2025-12-18: Fail fast with guidance.
    """

    @pytest.fixture
    def key_generator(self):
        """Create a basic key generator instance."""
        return CacheKeyGenerator()

    def test_set_rejected_with_guidance(self, key_generator):
        """set raises TypeError with sorting crash explanation."""
        with pytest.raises(TypeError, match="mixed-type sorting"):
            key_generator._normalize({1, 2, 3})

    def test_frozenset_rejected_with_guidance(self, key_generator):
        """frozenset raises TypeError with sorting crash explanation."""
        with pytest.raises(TypeError, match="mixed-type sorting"):
            key_generator._normalize(frozenset({1, 2, 3}))

    def test_pandas_dataframe_rejected_with_guidance(self, key_generator):
        """pandas DataFrame rejected with Parquet explanation."""
        pd = pytest.importorskip("pandas")

        df = pd.DataFrame({"a": [1, 2, 3]})
        with pytest.raises(TypeError, match="Parquet serialization is non-deterministic"):
            key_generator._normalize(df)

    def test_pandas_series_rejected(self, key_generator):
        """pandas Series rejected (same as DataFrame)."""
        pd = pytest.importorskip("pandas")

        s = pd.Series([1, 2, 3], name="test")
        with pytest.raises(TypeError, match="pandas"):
            key_generator._normalize(s)

    def test_custom_class_rejected_with_key_guidance(self, key_generator):
        """Custom class raises TypeError suggesting key= parameter."""

        class CustomObject:
            pass

        with pytest.raises(TypeError, match="key= parameter"):
            key_generator._normalize(CustomObject())

    def test_numpy_2d_array_guidance(self, key_generator):
        """2D numpy array gives specific constraint guidance."""
        np = pytest.importorskip("numpy")

        arr = np.array([[1, 2], [3, 4]], dtype=np.int32)
        with pytest.raises(TypeError, match="1D"):
            key_generator._normalize(arr)

    def test_numpy_large_array_guidance(self, key_generator):
        """Large numpy array gives size guidance."""
        np = pytest.importorskip("numpy")

        arr = np.zeros(200_000, dtype=np.float32)  # 800KB > 100KB limit
        with pytest.raises(TypeError, match="100,000"):
            key_generator._normalize(arr)
