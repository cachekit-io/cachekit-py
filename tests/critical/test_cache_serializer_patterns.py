"""Test CacheSerializer pattern detection and serialization"""

import numpy as np
import pandas as pd
import pytest

# Import from compatibility wrapper
from tests.critical.cache_serializer_compat import CACHE_SERIALIZER_AVAILABLE, CacheSerializer


@pytest.mark.skipif(not CACHE_SERIALIZER_AVAILABLE, reason="Cache serializer not available")
class TestCacheSerializerPatterns:
    """Test that CacheSerializer correctly handles different data patterns"""

    def setup_method(self):
        """Set up test fixtures"""
        self.serializer = CacheSerializer()

    def test_primitive_detection(self):
        """Test primitive type detection"""
        assert self.serializer.detect_pattern(None) == "primitive"
        assert self.serializer.detect_pattern(True) == "primitive"
        assert self.serializer.detect_pattern(42) == "primitive"
        assert self.serializer.detect_pattern(3.14) == "primitive"
        assert self.serializer.detect_pattern("hello") == "primitive"

    def test_binary_detection(self):
        """Test binary data detection"""
        assert self.serializer.detect_pattern(b"binary data") == "binary"

    def test_numpy_detection(self):
        """Test NumPy array detection"""
        arr = np.array([1, 2, 3, 4, 5])
        assert self.serializer.detect_pattern(arr) == "numpy_array"

        # Test different dtypes
        assert self.serializer.detect_pattern(np.array([1.0, 2.0, 3.0])) == "numpy_array"
        assert self.serializer.detect_pattern(np.array([[1, 2], [3, 4]])) == "numpy_array"

    def test_api_response_detection(self):
        """Test API response (JSON-compatible) detection"""
        # Simple dict
        assert self.serializer.detect_pattern({"key": "value"}) == "api_response"

        # Nested dict
        assert (
            self.serializer.detect_pattern({"status": "ok", "data": {"id": 123, "name": "test"}, "count": 42}) == "api_response"
        )

        # List of dicts
        assert self.serializer.detect_pattern([{"id": 1, "name": "a"}, {"id": 2, "name": "b"}]) == "api_response"

    def test_dataframe_detection(self):
        """Test DataFrame detection"""
        df = pd.DataFrame({"a": [1, 2, 3], "b": [4, 5, 6]})
        assert self.serializer.detect_pattern(df) == "dataframe"

    def test_complex_detection(self):
        """Test complex object detection"""

        # Custom class
        class CustomObject:
            def __init__(self):
                self.value = 42

        obj = CustomObject()
        assert self.serializer.detect_pattern(obj) == "complex"

        # Dict with non-string keys
        assert self.serializer.detect_pattern({1: "value"}) == "complex"

        # Dict with non-JSON-serializable values
        assert self.serializer.detect_pattern({"key": CustomObject()}) == "complex"

    def test_primitive_roundtrip(self):
        """Test primitive serialization roundtrip"""
        values = [None, True, False, 42, -42, 3.14, -3.14, "hello", ""]

        for value in values:
            serialized, metadata = self.serializer.serialize(value)
            deserialized = self.serializer.deserialize(serialized)
            assert deserialized == value
            assert type(deserialized) is type(value)

    def test_binary_roundtrip(self):
        """Test binary data roundtrip"""
        data = b"binary \x00 data \xff"
        serialized, metadata = self.serializer.serialize(data)
        deserialized = self.serializer.deserialize(serialized)
        assert deserialized == data

    def test_numpy_roundtrip(self):
        """Test NumPy array roundtrip"""
        arrays = [
            np.array([1, 2, 3, 4, 5]),
            np.array([1.0, 2.0, 3.0]),
            np.array([[1, 2], [3, 4]]),
            np.array([True, False, True]),
            np.zeros((3, 4, 5)),
        ]

        for arr in arrays:
            serialized, metadata = self.serializer.serialize(arr)
            deserialized = self.serializer.deserialize(serialized)
            np.testing.assert_array_equal(deserialized, arr)
            assert deserialized.dtype == arr.dtype
            assert deserialized.shape == arr.shape

    def test_api_response_roundtrip(self):
        """Test API response roundtrip"""
        responses = [
            {"status": "ok"},
            {"data": [1, 2, 3], "count": 3},
            [{"id": 1, "name": "a"}, {"id": 2, "name": "b"}],
            {"nested": {"deep": {"value": 42}}},
        ]

        for response in responses:
            serialized, metadata = self.serializer.serialize(response)
            deserialized = self.serializer.deserialize(serialized)
            assert deserialized == response

    def test_dataframe_roundtrip(self):
        """Test DataFrame roundtrip"""
        df = pd.DataFrame(
            {
                "int_col": [1, 2, 3],
                "float_col": [1.1, 2.2, 3.3],
                "str_col": ["a", "b", "c"],
                "bool_col": [True, False, True],
            }
        )

        serialized, metadata = self.serializer.serialize(df)
        deserialized = self.serializer.deserialize(serialized)

        # DataFrames need special comparison
        pd.testing.assert_frame_equal(deserialized, df)

    def test_complex_roundtrip(self):
        """Test complex object roundtrip via pickle"""
        # Use a standard library class that's pickleable
        from datetime import datetime, timezone

        # Create a complex object with nested attributes
        obj = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)

        serialized, metadata = self.serializer.serialize(obj)
        deserialized = self.serializer.deserialize(serialized)

        assert deserialized == obj
        assert deserialized.year == 2024
        assert deserialized.tzinfo == timezone.utc

        # Also test with a more complex structure
        complex_data = {
            "datetime": datetime.now(),
            "nested": {"list": [1, 2, datetime.now()]},
            "tuple": (1, "two", 3.0),
            "set": {1, 2, 3},
        }

        serialized, metadata = self.serializer.serialize(complex_data)
        deserialized = self.serializer.deserialize(serialized)

        assert deserialized["datetime"] == complex_data["datetime"]
        assert deserialized["tuple"] == complex_data["tuple"]
        assert deserialized["set"] == complex_data["set"]

    def test_mixed_types(self):
        """Test mixed type handling"""
        data = {
            "primitives": [1, 2.0, "three", True, None],
            "numpy": np.array([1, 2, 3]).tolist(),  # Will be JSON serialized
            "nested": {"a": 1, "b": [2, 3], "c": {"d": 4}},
        }

        serialized, metadata = self.serializer.serialize(data)
        deserialized = self.serializer.deserialize(serialized)
        assert deserialized == data

    def test_performance_patterns(self):
        """Test that pattern detection is fast"""
        import time

        # Create test data
        primitive = "test string"
        api_response = {"key": "value", "count": 42}
        numpy_array = np.random.rand(100, 100)

        # Time pattern detection (should be < 1ms each)
        for data in [primitive, api_response, numpy_array]:
            start = time.time()
            for _ in range(1000):
                self.serializer.detect_pattern(data)
            elapsed = time.time() - start
            avg_time = elapsed / 1000
            assert avg_time < 0.001  # Less than 1ms per detection
