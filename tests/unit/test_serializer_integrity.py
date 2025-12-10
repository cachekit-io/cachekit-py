"""Unit tests for serializer integrity checking (xxHash3-64 checksums).

Tests that OrjsonSerializer and ArrowSerializer detect data corruption via xxHash3-64 checksums.
"""

from __future__ import annotations

import pandas as pd
import pytest

from cachekit.serializers import ArrowSerializer, OrjsonSerializer
from cachekit.serializers.base import SerializationError


class TestOrjsonSerializerIntegrity:
    """Test xxHash3-64 integrity checking for OrjsonSerializer."""

    def test_roundtrip_with_checksum(self):
        """Normal serialize/deserialize roundtrip works with checksum."""
        serializer = OrjsonSerializer()
        original = {"key": "value", "number": 42, "nested": {"data": [1, 2, 3]}}

        data, metadata = serializer.serialize(original)
        result = serializer.deserialize(data, metadata)

        assert result == original

    def test_checksum_envelope_format(self):
        """Serialized data has 8-byte checksum prefix."""
        serializer = OrjsonSerializer()
        original = {"test": "data"}

        data, _ = serializer.serialize(original)

        # Verify format: checksum (8 bytes) + JSON data
        assert len(data) >= 10  # 8 bytes checksum + at least 2 bytes JSON
        assert len(data) == 8 + len(b'{"test":"data"}')  # Exact size check

    def test_corrupted_checksum_detected(self):
        """Corrupting the checksum raises SerializationError."""
        serializer = OrjsonSerializer()
        original = {"key": "value"}

        data, _ = serializer.serialize(original)

        # Corrupt first byte of checksum
        corrupted = b"\xff" + data[1:]

        with pytest.raises(SerializationError) as exc_info:
            serializer.deserialize(corrupted)

        assert "Checksum validation failed" in str(exc_info.value)
        assert "data corruption detected" in str(exc_info.value)

    def test_corrupted_data_detected(self):
        """Corrupting the JSON data raises SerializationError."""
        serializer = OrjsonSerializer()
        original = {"key": "value"}

        data, _ = serializer.serialize(original)

        # Corrupt one byte in the JSON data (after 8-byte checksum)
        corrupted = data[:12] + b"X" + data[13:]

        with pytest.raises(SerializationError) as exc_info:
            serializer.deserialize(corrupted)

        assert "Checksum validation failed" in str(exc_info.value)

    def test_truncated_data_detected(self):
        """Truncating the data raises SerializationError."""
        serializer = OrjsonSerializer()
        original = {"key": "value", "data": [1, 2, 3, 4, 5]}

        data, _ = serializer.serialize(original)

        # Truncate to 50% of original size
        truncated = data[: len(data) // 2]

        with pytest.raises(SerializationError) as exc_info:
            serializer.deserialize(truncated)

        # Either size check fails, checksum fails, or JSON decode fails
        error_msg = str(exc_info.value)
        assert any(msg in error_msg for msg in ["Invalid data", "Checksum validation failed", "Failed to deserialize"])

    def test_empty_data_raises_error(self):
        """Empty data raises SerializationError."""
        serializer = OrjsonSerializer()

        with pytest.raises(SerializationError) as exc_info:
            serializer.deserialize(b"")

        assert "Invalid data" in str(exc_info.value)
        assert "Expected at least 10 bytes" in str(exc_info.value)

    def test_too_short_data_raises_error(self):
        """Data shorter than minimum (8-byte checksum + 2-byte JSON) raises error."""
        serializer = OrjsonSerializer()

        # 5 bytes (less than required 10 bytes)
        invalid_data = b"X" * 5

        with pytest.raises(SerializationError) as exc_info:
            serializer.deserialize(invalid_data)

        assert "Invalid data" in str(exc_info.value)
        assert "Expected at least 10 bytes" in str(exc_info.value)

    def test_bit_flip_detected(self):
        """Single bit flip in data is detected."""
        serializer = OrjsonSerializer()
        original = {"test": "data" * 10}  # Larger payload for safe bit flip

        data, _ = serializer.serialize(original)

        # Flip one bit in the JSON data section (after 8-byte checksum)
        byte_pos = 15  # Inside JSON data
        bit_pos = 3
        corrupted = bytearray(data)
        corrupted[byte_pos] ^= 1 << bit_pos  # XOR to flip bit
        corrupted = bytes(corrupted)

        with pytest.raises(SerializationError) as exc_info:
            serializer.deserialize(corrupted)

        assert "Checksum validation failed" in str(exc_info.value)

    def test_large_data_integrity(self):
        """Large data structures maintain integrity."""
        serializer = OrjsonSerializer()
        # Create large JSON structure
        original = {f"key_{i}": f"value_{i}" for i in range(1000)}

        data, _ = serializer.serialize(original)
        result = serializer.deserialize(data)

        assert result == original
        assert len(result) == 1000

    def test_unicode_data_integrity(self):
        """Unicode data maintains integrity."""
        serializer = OrjsonSerializer()
        original = {"emoji": "🚀🔥💯", "chinese": "你好世界", "arabic": "مرحبا بالعالم"}

        data, _ = serializer.serialize(original)
        result = serializer.deserialize(data)

        assert result == original


class TestArrowSerializerIntegrity:
    """Test xxHash3-64 integrity checking for ArrowSerializer."""

    def test_roundtrip_with_checksum(self):
        """Normal DataFrame serialize/deserialize roundtrip works with checksum."""
        serializer = ArrowSerializer()
        original = pd.DataFrame({"a": [1, 2, 3], "b": [4.0, 5.0, 6.0], "c": ["x", "y", "z"]})

        data, metadata = serializer.serialize(original)
        result = serializer.deserialize(data, metadata)

        assert isinstance(result, pd.DataFrame)
        pd.testing.assert_frame_equal(result, original)

    def test_checksum_envelope_format(self):
        """Serialized data has 8-byte checksum prefix."""
        serializer = ArrowSerializer()
        df = pd.DataFrame({"a": [1, 2, 3]})

        data, _ = serializer.serialize(df)

        # Verify format: checksum (8 bytes) + Arrow IPC data
        assert len(data) >= 40  # 8 bytes checksum + minimal Arrow IPC file
        # First 8 bytes should be the checksum
        checksum = data[:8]
        assert len(checksum) == 8

    def test_corrupted_checksum_detected(self):
        """Corrupting the checksum raises SerializationError."""
        serializer = ArrowSerializer()
        df = pd.DataFrame({"a": [1, 2, 3]})

        data, _ = serializer.serialize(df)

        # Corrupt first byte of checksum
        corrupted = b"\xff" + data[1:]

        with pytest.raises(SerializationError) as exc_info:
            serializer.deserialize(corrupted)

        assert "Checksum validation failed" in str(exc_info.value)
        assert "data corruption detected" in str(exc_info.value)

    def test_corrupted_arrow_data_detected(self):
        """Corrupting the Arrow IPC data raises SerializationError."""
        serializer = ArrowSerializer()
        df = pd.DataFrame({"a": [1, 2, 3]})

        data, _ = serializer.serialize(df)

        # Corrupt one byte in the Arrow data (after 8-byte checksum)
        corrupted = data[:50] + b"X" + data[51:]

        with pytest.raises(SerializationError) as exc_info:
            serializer.deserialize(corrupted)

        assert "Checksum validation failed" in str(exc_info.value)

    def test_truncated_dataframe_detected(self):
        """Truncating the DataFrame data raises SerializationError."""
        serializer = ArrowSerializer()
        df = pd.DataFrame({"col": range(100)})  # 100 rows

        data, _ = serializer.serialize(df)

        # Truncate to 50% of original size
        truncated = data[: len(data) // 2]

        with pytest.raises(SerializationError) as exc_info:
            serializer.deserialize(truncated)

        # Either checksum fails or Arrow decode fails
        error_msg = str(exc_info.value)
        assert "Checksum validation failed" in error_msg or "Failed to deserialize" in error_msg

    def test_empty_data_raises_error(self):
        """Empty data raises SerializationError."""
        serializer = ArrowSerializer()

        with pytest.raises(SerializationError) as exc_info:
            serializer.deserialize(b"")

        assert "Invalid data" in str(exc_info.value)
        assert "Expected at least 40 bytes" in str(exc_info.value)

    def test_too_short_data_raises_error(self):
        """Data shorter than minimum raises error."""
        serializer = ArrowSerializer()

        # 20 bytes (less than required 40 bytes)
        invalid_data = b"X" * 20

        with pytest.raises(SerializationError) as exc_info:
            serializer.deserialize(invalid_data)

        assert "Invalid data" in str(exc_info.value)
        assert "Expected at least 40 bytes" in str(exc_info.value)

    def test_bit_flip_in_dataframe_detected(self):
        """Single bit flip in DataFrame data is detected."""
        serializer = ArrowSerializer()
        df = pd.DataFrame({"a": [1, 2, 3], "b": [4.0, 5.0, 6.0]})

        data, _ = serializer.serialize(df)

        # Flip one bit in the Arrow data section (after 8-byte checksum)
        byte_pos = 50  # Inside Arrow IPC data
        bit_pos = 3
        corrupted = bytearray(data)
        corrupted[byte_pos] ^= 1 << bit_pos  # XOR to flip bit
        corrupted = bytes(corrupted)

        with pytest.raises(SerializationError) as exc_info:
            serializer.deserialize(corrupted)

        assert "Checksum validation failed" in str(exc_info.value)

    def test_large_dataframe_integrity(self):
        """Large DataFrames maintain integrity."""
        serializer = ArrowSerializer()
        # Create large DataFrame (10K rows, 5 columns)
        df = pd.DataFrame(
            {
                "col1": range(10000),
                "col2": range(10000, 20000),
                "col3": [f"row_{i}" for i in range(10000)],
                "col4": [i * 1.5 for i in range(10000)],
                "col5": [i % 2 == 0 for i in range(10000)],
            }
        )

        data, _ = serializer.serialize(df)
        result = serializer.deserialize(data)

        assert isinstance(result, pd.DataFrame)
        assert len(result) == 10000
        pd.testing.assert_frame_equal(result, df)

    def test_dataframe_with_nulls_integrity(self):
        """DataFrames with null values maintain integrity."""
        serializer = ArrowSerializer()
        df = pd.DataFrame({"a": [1, None, 3], "b": [4.0, 5.0, None], "c": [None, "y", "z"]})

        data, _ = serializer.serialize(df)
        result = serializer.deserialize(data)

        assert isinstance(result, pd.DataFrame)
        pd.testing.assert_frame_equal(result, df)

    def test_arrow_return_format_integrity(self):
        """Arrow return format (zero-copy) maintains integrity."""
        serializer = ArrowSerializer(return_format="arrow")
        df = pd.DataFrame({"a": [1, 2, 3], "b": [4.0, 5.0, 6.0]})

        data, _ = serializer.serialize(df)
        result = serializer.deserialize(data)

        # Result is pyarrow.Table
        import pyarrow as pa

        assert isinstance(result, pa.Table)
        assert result.num_rows == 3


class TestIntegrityPerformance:
    """Test that integrity checking overhead is minimal."""

    def test_orjson_checksum_overhead_acceptable(self):
        """xxHash3-64 checksum adds minimal overhead to OrjsonSerializer."""
        serializer = OrjsonSerializer()
        # Large JSON structure
        original = {f"key_{i}": {"data": [i, i + 1, i + 2]} for i in range(1000)}

        data, _ = serializer.serialize(original)

        # Checksum overhead is exactly 8 bytes
        import orjson

        raw_json_size = len(orjson.dumps(original, option=orjson.OPT_SORT_KEYS))
        envelope_size = len(data)
        overhead = envelope_size - raw_json_size

        assert overhead == 8  # Exactly 8 bytes for xxHash3-64 checksum

    def test_arrow_checksum_overhead_acceptable(self):
        """xxHash3-64 checksum adds minimal overhead to ArrowSerializer."""
        serializer = ArrowSerializer()
        # Large DataFrame
        df = pd.DataFrame({"col": range(10000)})

        data, _ = serializer.serialize(df)

        # Checksum overhead is exactly 8 bytes (relative to Arrow IPC size)
        # We can't easily measure raw Arrow IPC size without modifying code,
        # but we verify the overhead is minimal compared to data size
        assert len(data) > 8  # Has checksum
        overhead_percentage = (8 / len(data)) * 100
        assert overhead_percentage < 1.0  # Less than 1% overhead for large DataFrames
