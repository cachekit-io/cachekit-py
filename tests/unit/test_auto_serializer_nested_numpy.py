"""Unit tests for AutoSerializer nested NumPy array support (GitHub Issue #50).

Bug: serializer="auto" fails on numpy arrays nested in dicts/lists.

The serialize() method has a top-level isinstance(obj, np.ndarray) check that routes
to the efficient NUMPY_RAW binary path. But when the ndarray is nested inside a dict
or list, it falls through to _serialize_msgpack(), which uses msgpack.packb() with
_auto_default as the custom encoder. The _auto_default function has NO handler for
numpy arrays, so msgpack raises TypeError when it encounters an ndarray value.

These tests exercise:
- Dict containing a numpy array value (exact reproduction case)
- Dict containing a 2D numpy array
- List containing numpy arrays
- Deeply nested numpy arrays
- Mixed types (numpy + datetime + UUID + set) in same structure
- Roundtrip fidelity (serialize -> deserialize -> np.testing.assert_array_equal)
- Corruption detection for malformed __ndarray__ markers
"""

from __future__ import annotations

import pytest

from cachekit.serializers.auto_serializer import AutoSerializer
from cachekit.serializers.base import SerializationError

np = pytest.importorskip("numpy")


class TestNestedNumpyArrayInDict:
    """Bug reproduction: numpy arrays as dict values fail serialization."""

    def test_dict_with_1d_numpy_array(self):
        """Core bug: dict containing a 1D numpy array should serialize without error."""
        serializer = AutoSerializer()
        data = {"values": np.array([1, 2, 3, 4, 5])}

        # This should NOT raise TypeError
        serialized, metadata = serializer.serialize(data)
        deserialized = serializer.deserialize(serialized, metadata)

        assert "values" in deserialized
        np.testing.assert_array_equal(deserialized["values"], data["values"])

    def test_dict_with_2d_numpy_array(self):
        """Dict containing a 2D numpy array should roundtrip correctly."""
        serializer = AutoSerializer()
        matrix = np.array([[1, 2], [3, 4]])
        data = {"matrix": matrix}

        serialized, metadata = serializer.serialize(data)
        deserialized = serializer.deserialize(serialized, metadata)

        np.testing.assert_array_equal(deserialized["matrix"], matrix)

    def test_dict_with_float_numpy_array(self):
        """Dict containing a float numpy array preserves dtype."""
        serializer = AutoSerializer()
        arr = np.array([1.5, 2.7, 3.14], dtype=np.float64)
        data = {"measurements": arr}

        serialized, metadata = serializer.serialize(data)
        deserialized = serializer.deserialize(serialized, metadata)

        np.testing.assert_array_equal(deserialized["measurements"], arr)
        assert deserialized["measurements"].dtype == np.float64

    def test_dict_with_multiple_numpy_arrays(self):
        """Dict containing multiple numpy arrays as values."""
        serializer = AutoSerializer()
        data = {
            "x": np.array([1, 2, 3]),
            "y": np.array([4.0, 5.0, 6.0]),
            "z": np.array([[7, 8], [9, 10]]),
        }

        serialized, metadata = serializer.serialize(data)
        deserialized = serializer.deserialize(serialized, metadata)

        np.testing.assert_array_equal(deserialized["x"], data["x"])
        np.testing.assert_array_equal(deserialized["y"], data["y"])
        np.testing.assert_array_equal(deserialized["z"], data["z"])


class TestNestedNumpyArrayInList:
    """Numpy arrays nested inside lists."""

    def test_list_with_numpy_arrays(self):
        """List containing numpy arrays should serialize."""
        serializer = AutoSerializer()
        data = [np.array([1, 2, 3]), np.array([4, 5, 6])]

        serialized, metadata = serializer.serialize(data)
        deserialized = serializer.deserialize(serialized, metadata)

        assert len(deserialized) == 2
        np.testing.assert_array_equal(deserialized[0], data[0])
        np.testing.assert_array_equal(deserialized[1], data[1])

    def test_dict_with_list_of_numpy_arrays(self):
        """Dict containing a list of numpy arrays."""
        serializer = AutoSerializer()
        data = {"layers": [np.array([1, 2]), np.array([3, 4, 5])]}

        serialized, metadata = serializer.serialize(data)
        deserialized = serializer.deserialize(serialized, metadata)

        np.testing.assert_array_equal(deserialized["layers"][0], data["layers"][0])
        np.testing.assert_array_equal(deserialized["layers"][1], data["layers"][1])


class TestDeeplyNestedNumpyArray:
    """Numpy arrays deep in nested structures."""

    def test_nested_dict_with_numpy_array(self):
        """Deeply nested dict containing a numpy array."""
        serializer = AutoSerializer()
        data = {
            "model": {
                "layer1": {
                    "weights": np.array([0.1, 0.2, 0.3]),
                }
            }
        }

        serialized, metadata = serializer.serialize(data)
        deserialized = serializer.deserialize(serialized, metadata)

        np.testing.assert_array_equal(
            deserialized["model"]["layer1"]["weights"],
            data["model"]["layer1"]["weights"],
        )


class TestMixedTypesWithNestedNumpy:
    """Numpy arrays mixed with other custom types in same structure."""

    def test_numpy_with_datetime(self):
        """Dict containing both numpy array and datetime."""
        from datetime import datetime

        serializer = AutoSerializer()
        dt = datetime(2025, 11, 14, 12, 0, 0)
        data = {
            "embeddings": np.array([0.1, 0.2, 0.3]),
            "created_at": dt,
        }

        serialized, metadata = serializer.serialize(data)
        deserialized = serializer.deserialize(serialized, metadata)

        np.testing.assert_array_equal(deserialized["embeddings"], data["embeddings"])
        assert deserialized["created_at"] == dt

    def test_numpy_with_uuid(self):
        """Dict containing both numpy array and UUID."""
        from uuid import UUID

        serializer = AutoSerializer()
        uid = UUID("12345678-1234-5678-1234-567812345678")
        data = {
            "id": uid,
            "vector": np.array([1.0, 2.0, 3.0]),
        }

        serialized, metadata = serializer.serialize(data)
        deserialized = serializer.deserialize(serialized, metadata)

        assert deserialized["id"] == uid
        np.testing.assert_array_equal(deserialized["vector"], data["vector"])

    def test_numpy_with_set(self):
        """Dict containing both numpy array and set."""
        serializer = AutoSerializer()
        data = {
            "features": np.array([1, 2, 3]),
            "tags": {"ml", "production"},
        }

        serialized, metadata = serializer.serialize(data)
        deserialized = serializer.deserialize(serialized, metadata)

        np.testing.assert_array_equal(deserialized["features"], data["features"])
        assert deserialized["tags"] == {"ml", "production"}


class TestNestedNumpyEdgeCases:
    """Edge cases for nested numpy arrays."""

    def test_empty_numpy_array_in_dict(self):
        """Dict containing an empty numpy array."""
        serializer = AutoSerializer()
        data = {"empty": np.array([])}

        serialized, metadata = serializer.serialize(data)
        deserialized = serializer.deserialize(serialized, metadata)

        np.testing.assert_array_equal(deserialized["empty"], data["empty"])

    def test_scalar_numpy_in_dict(self):
        """Dict containing a 0-dimensional numpy array (scalar)."""
        serializer = AutoSerializer()
        data = {"scalar": np.array(42)}

        serialized, metadata = serializer.serialize(data)
        deserialized = serializer.deserialize(serialized, metadata)

        np.testing.assert_array_equal(deserialized["scalar"], data["scalar"])

    def test_3d_numpy_array_in_dict(self):
        """Dict containing a 3D numpy array."""
        serializer = AutoSerializer()
        arr = np.arange(24).reshape(2, 3, 4)
        data = {"tensor": arr}

        serialized, metadata = serializer.serialize(data)
        deserialized = serializer.deserialize(serialized, metadata)

        np.testing.assert_array_equal(deserialized["tensor"], arr)

    def test_numpy_array_with_string_and_int_siblings(self):
        """Dict containing numpy array alongside primitive types."""
        serializer = AutoSerializer()
        data = {
            "name": "model_v1",
            "version": 3,
            "weights": np.array([0.5, 0.3, 0.2]),
            "active": True,
        }

        serialized, metadata = serializer.serialize(data)
        deserialized = serializer.deserialize(serialized, metadata)

        assert deserialized["name"] == "model_v1"
        assert deserialized["version"] == 3
        assert deserialized["active"] is True
        np.testing.assert_array_equal(deserialized["weights"], data["weights"])


class TestNestedNumpyWithIntegrityChecking:
    """Test nested numpy with both integrity checking enabled and disabled."""

    def test_nested_numpy_without_integrity_checking(self):
        """Nested numpy should work even without ByteStorage envelope."""
        serializer = AutoSerializer(enable_integrity_checking=False)
        data = {"arr": np.array([1, 2, 3])}

        serialized, metadata = serializer.serialize(data)
        deserialized = serializer.deserialize(serialized, metadata)

        np.testing.assert_array_equal(deserialized["arr"], data["arr"])

    def test_nested_numpy_with_integrity_checking(self):
        """Nested numpy should work with ByteStorage envelope."""
        serializer = AutoSerializer(enable_integrity_checking=True)
        data = {"arr": np.array([1, 2, 3])}

        serialized, metadata = serializer.serialize(data)
        deserialized = serializer.deserialize(serialized, metadata)

        np.testing.assert_array_equal(deserialized["arr"], data["arr"])


class TestNestedNumpyCorruptionDetection:
    """Test corruption detection for the __ndarray__ marker."""

    def test_ndarray_missing_data_field(self):
        """Corrupted __ndarray__ marker missing 'data' should raise SerializationError."""
        import msgpack

        serializer = AutoSerializer(enable_integrity_checking=False)
        corrupted = msgpack.packb({"__ndarray__": True, "shape": [3], "dtype": "float64"})

        with pytest.raises(SerializationError, match="missing required fields"):
            serializer.deserialize(corrupted)

    def test_ndarray_missing_shape_field(self):
        """Corrupted __ndarray__ marker missing 'shape' should raise SerializationError."""
        import msgpack

        serializer = AutoSerializer(enable_integrity_checking=False)
        corrupted = msgpack.packb({"__ndarray__": True, "data": b"\x00" * 24, "dtype": "float64"})

        with pytest.raises(SerializationError, match="missing required fields"):
            serializer.deserialize(corrupted)

    def test_ndarray_missing_dtype_field(self):
        """Corrupted __ndarray__ marker missing 'dtype' should raise SerializationError."""
        import msgpack

        serializer = AutoSerializer(enable_integrity_checking=False)
        corrupted = msgpack.packb({"__ndarray__": True, "data": b"\x00" * 24, "shape": [3]})

        with pytest.raises(SerializationError, match="missing required fields"):
            serializer.deserialize(corrupted)
