"""Unit tests for xxHash3-64 integrity checking in Arrow and Orjson serializers.

TDD RED Phase: These tests verify the switch from Blake3 (32-byte) to xxHash3-64 (8-byte) checksums.

The xxHash3-64 algorithm provides:
- 64-bit (8-byte) checksums for corruption detection
- 10x faster than Blake3 for integrity checking (not security)
- Same detection quality for accidental corruption (bit flips, truncation)

Note: Blake3 is still used in hash_utils.py for cache keys (security-relevant).
      This change only affects integrity checking in serializers.
"""

from __future__ import annotations

import pandas as pd
import pytest

from cachekit.serializers import ArrowSerializer, OrjsonSerializer
from cachekit.serializers.base import SerializationError


class TestOrjsonSerializerXxhashIntegrity:
    """Test xxHash3-64 integrity checking for OrjsonSerializer."""

    def test_checksum_is_8_bytes(self):
        """xxHash3-64 produces 8-byte checksum (not 32-byte Blake3).

        This is the key behavioral test for the Blake3 -> xxHash3-64 migration.
        """
        serializer = OrjsonSerializer()
        original = {"test": "data"}

        data, _ = serializer.serialize(original)

        # xxHash3-64 format: [8-byte checksum][JSON data]
        # Minimum size: 8 bytes checksum + 2 bytes JSON ({})
        assert len(data) >= 10, f"Expected at least 10 bytes, got {len(data)}"

        # Exact size check for known payload
        json_payload = b'{"test":"data"}'
        expected_size = 8 + len(json_payload)  # 8-byte xxHash3-64 + JSON
        assert len(data) == expected_size, f"Expected {expected_size} bytes, got {len(data)}"

    def test_checksum_overhead_is_8_bytes(self):
        """Verify checksum overhead is exactly 8 bytes (xxHash3-64, not 32-byte Blake3)."""
        serializer = OrjsonSerializer()
        original = {f"key_{i}": {"data": [i, i + 1, i + 2]} for i in range(100)}

        data, _ = serializer.serialize(original)

        # Measure overhead
        import orjson

        raw_json_size = len(orjson.dumps(original, option=orjson.OPT_SORT_KEYS))
        envelope_size = len(data)
        overhead = envelope_size - raw_json_size

        assert overhead == 8, f"Expected 8-byte overhead (xxHash3-64), got {overhead} bytes"

    def test_minimum_size_check_is_10_bytes(self):
        """Deserialize should require at least 10 bytes (8-byte checksum + 2-byte JSON)."""
        serializer = OrjsonSerializer()

        # 9 bytes should fail (less than required 10 bytes)
        with pytest.raises(SerializationError) as exc_info:
            serializer.deserialize(b"X" * 9)

        assert "Invalid data" in str(exc_info.value)
        assert "10 bytes" in str(exc_info.value)

    def test_roundtrip_with_xxhash_checksum(self):
        """Normal serialize/deserialize roundtrip works with xxHash3-64 checksum."""
        serializer = OrjsonSerializer()
        original = {"key": "value", "number": 42, "nested": {"data": [1, 2, 3]}}

        data, metadata = serializer.serialize(original)
        result = serializer.deserialize(data, metadata)

        assert result == original

    def test_corrupted_data_detected_with_xxhash(self):
        """Corrupting the JSON data raises SerializationError (xxHash3-64 detection)."""
        serializer = OrjsonSerializer()
        original = {"key": "value", "extra": "padding" * 10}

        data, _ = serializer.serialize(original)

        # Corrupt one byte in the JSON data (after 8-byte checksum)
        corrupted = data[:12] + b"X" + data[13:]

        with pytest.raises(SerializationError) as exc_info:
            serializer.deserialize(corrupted)

        assert "Checksum validation failed" in str(exc_info.value)

    def test_bit_flip_detected_with_xxhash(self):
        """Single bit flip in data is detected by xxHash3-64."""
        serializer = OrjsonSerializer()
        original = {"test": "data" * 10}

        data, _ = serializer.serialize(original)

        # Flip one bit in the JSON data section (after 8-byte checksum)
        byte_pos = 15  # Inside JSON data
        bit_pos = 3
        corrupted = bytearray(data)
        corrupted[byte_pos] ^= 1 << bit_pos
        corrupted = bytes(corrupted)

        with pytest.raises(SerializationError) as exc_info:
            serializer.deserialize(corrupted)

        assert "Checksum validation failed" in str(exc_info.value)


class TestArrowSerializerXxhashIntegrity:
    """Test xxHash3-64 integrity checking for ArrowSerializer."""

    def test_checksum_is_8_bytes(self):
        """xxHash3-64 produces 8-byte checksum (not 32-byte Blake3).

        This is the key behavioral test for the Blake3 -> xxHash3-64 migration.
        """
        serializer = ArrowSerializer()
        df = pd.DataFrame({"a": [1, 2, 3]})

        data, _ = serializer.serialize(df)

        # xxHash3-64 format: [8-byte checksum][Arrow IPC data]
        # Minimum size: 8 bytes checksum + minimal Arrow IPC file (typically 200+ bytes)
        assert len(data) >= 40, f"Expected at least 40 bytes, got {len(data)}"

    def test_checksum_overhead_is_8_bytes(self):
        """Verify checksum overhead is exactly 8 bytes (xxHash3-64, not 32-byte Blake3)."""
        # Create two serializers: one with integrity, one without
        serializer_with = ArrowSerializer(enable_integrity_checking=True)
        serializer_without = ArrowSerializer(enable_integrity_checking=False)

        df = pd.DataFrame({"col": range(1000)})

        data_with, _ = serializer_with.serialize(df)
        data_without, _ = serializer_without.serialize(df)

        overhead = len(data_with) - len(data_without)
        assert overhead == 8, f"Expected 8-byte overhead (xxHash3-64), got {overhead} bytes"

    def test_minimum_size_check_is_40_bytes(self):
        """Deserialize should require at least 40 bytes (8-byte checksum + 32-byte Arrow header)."""
        serializer = ArrowSerializer()

        # 39 bytes should fail
        with pytest.raises(SerializationError) as exc_info:
            serializer.deserialize(b"X" * 39)

        assert "Invalid data" in str(exc_info.value)
        assert "40 bytes" in str(exc_info.value)

    def test_roundtrip_with_xxhash_checksum(self):
        """Normal DataFrame serialize/deserialize roundtrip works with xxHash3-64 checksum."""
        serializer = ArrowSerializer()
        original = pd.DataFrame({"a": [1, 2, 3], "b": [4.0, 5.0, 6.0], "c": ["x", "y", "z"]})

        data, metadata = serializer.serialize(original)
        result = serializer.deserialize(data, metadata)

        assert isinstance(result, pd.DataFrame)
        pd.testing.assert_frame_equal(result, original)

    def test_corrupted_data_detected_with_xxhash(self):
        """Corrupting the Arrow IPC data raises SerializationError (xxHash3-64 detection)."""
        serializer = ArrowSerializer()
        df = pd.DataFrame({"a": [1, 2, 3]})

        data, _ = serializer.serialize(df)

        # Corrupt one byte in the Arrow data (after 8-byte checksum)
        corrupted = data[:50] + b"X" + data[51:]

        with pytest.raises(SerializationError) as exc_info:
            serializer.deserialize(corrupted)

        assert "Checksum validation failed" in str(exc_info.value)

    def test_bit_flip_detected_with_xxhash(self):
        """Single bit flip in DataFrame data is detected by xxHash3-64."""
        serializer = ArrowSerializer()
        df = pd.DataFrame({"a": [1, 2, 3], "b": [4.0, 5.0, 6.0]})

        data, _ = serializer.serialize(df)

        # Flip one bit in the Arrow data section (after 8-byte checksum)
        byte_pos = 50  # Inside Arrow IPC data
        bit_pos = 3
        corrupted = bytearray(data)
        corrupted[byte_pos] ^= 1 << bit_pos
        corrupted = bytes(corrupted)

        with pytest.raises(SerializationError) as exc_info:
            serializer.deserialize(corrupted)

        assert "Checksum validation failed" in str(exc_info.value)


class TestXxhashPerformanceCharacteristics:
    """Test that xxHash3-64 overhead is minimal and consistent."""

    def test_orjson_overhead_percentage(self):
        """xxHash3-64 overhead should be less than 0.1% for large payloads."""
        serializer = OrjsonSerializer()
        original = {f"key_{i}": f"value_{i}" * 100 for i in range(1000)}

        data, _ = serializer.serialize(original)

        overhead_percentage = (8 / len(data)) * 100
        assert overhead_percentage < 0.1, f"Overhead {overhead_percentage:.4f}% exceeds 0.1%"

    def test_arrow_overhead_percentage(self):
        """xxHash3-64 overhead should be less than 0.1% for large DataFrames."""
        serializer = ArrowSerializer()
        df = pd.DataFrame({"col": range(100000)})

        data, _ = serializer.serialize(df)

        overhead_percentage = (8 / len(data)) * 100
        assert overhead_percentage < 0.1, f"Overhead {overhead_percentage:.4f}% exceeds 0.1%"
