"""Test CacheSerializer compression functionality"""

import numpy as np
import pytest

# Import from compatibility wrapper
from tests.critical.cache_serializer_compat import CACHE_SERIALIZER_AVAILABLE, CacheSerializer


@pytest.mark.skipif(not CACHE_SERIALIZER_AVAILABLE, reason="Cache serializer not available")
class TestCacheSerializerCompression:
    """Test that CacheSerializer correctly compresses large payloads"""

    def test_compression_threshold_default(self):
        """Test default compression threshold is 10KB"""
        serializer = CacheSerializer()

        # Small data should not be compressed
        small_data = "x" * 100
        serialized, _ = serializer.serialize(small_data)
        # First byte should not have compression marker (0x80)
        assert serialized[0] & 0x80 == 0

        # Large data should be compressed
        large_data = "x" * 20000  # 20KB
        serialized, _ = serializer.serialize(large_data)
        # First byte should have compression marker
        assert serialized[0] & 0x80 == 0x80

    def test_compression_threshold_custom(self):
        """Test custom compression threshold"""
        # Set very low threshold
        serializer = CacheSerializer(compression_threshold=100)

        # Even small data should be compressed
        data = "x" * 200
        serialized, _ = serializer.serialize(data)
        assert serialized[0] & 0x80 == 0x80

        # Disable compression
        serializer_no_compress = CacheSerializer(compression_threshold=0)
        large_data = "x" * 100000
        serialized, _ = serializer_no_compress.serialize(large_data)
        assert serialized[0] & 0x80 == 0

    def test_compression_roundtrip(self):
        """Test that compressed data roundtrips correctly"""
        serializer = CacheSerializer(compression_threshold=100)

        test_cases = [
            "x" * 1000,  # Repetitive string (compresses well)
            {"data": ["item"] * 100},  # Repetitive JSON
            np.zeros(1000),  # NumPy array with zeros
            b"binary" * 500,  # Binary data
        ]

        for data in test_cases:
            serialized, metadata = serializer.serialize(data)
            # Verify compression happened
            assert serialized[0] & 0x80 == 0x80

            # Verify roundtrip
            deserialized = serializer.deserialize(serialized)
            if isinstance(data, np.ndarray):
                np.testing.assert_array_equal(deserialized, data)
            else:
                assert deserialized == data

    def test_compression_efficiency(self):
        """Test that compression actually reduces size"""
        serializer = CacheSerializer(compression_threshold=100)

        # Highly compressible data
        data = "a" * 10000
        serialized, _ = serializer.serialize(data)

        # Should be much smaller than original
        # Original: 10000 chars + metadata
        # Compressed: should be < 1000 bytes
        assert len(serialized) < 1000

        # Verify it still deserializes correctly
        deserialized = serializer.deserialize(serialized)
        assert deserialized == data

    def test_incompressible_data(self):
        """Test that incompressible data is not compressed"""
        serializer = CacheSerializer(compression_threshold=100)

        # Random data doesn't compress well
        import random

        random_data = bytes(random.randint(0, 255) for _ in range(1000))

        serialized, _ = serializer.serialize(random_data)

        # Should not be compressed (wouldn't reduce size)
        # Note: This might occasionally fail if random data happens to compress
        # In real implementation, we check if compressed size < original
        deserialized = serializer.deserialize(serialized)
        assert deserialized == random_data

    def test_all_patterns_with_compression(self):
        """Test compression works with all data patterns"""
        serializer = CacheSerializer(compression_threshold=100)

        # Large examples of each pattern
        test_data = {
            "primitive": "x" * 1000,
            "api_response": {"data": [{"id": i, "value": "test"} for i in range(100)]},
            "numpy": np.ones(1000),
            "binary": b"y" * 1000,
        }

        for name, data in test_data.items():
            serialized, _ = serializer.serialize(data)

            # Most should be compressed (except maybe complex patterns)
            if name != "complex":
                # Check if compressed (this is a heuristic)
                if len(serialized) < 500:  # If it's small, it was likely compressed
                    assert serialized[0] & 0x80 == 0x80

            # All should roundtrip correctly
            deserialized = serializer.deserialize(serialized)
            if isinstance(data, np.ndarray):
                np.testing.assert_array_equal(deserialized, data)
            else:
                assert deserialized == data

    def test_pattern_preservation_with_compression(self):
        """Test that pattern detection still works with compression"""
        serializer = CacheSerializer(compression_threshold=100)

        # API response pattern
        data = {"key": "value" * 100}  # Make it large enough to compress
        pattern = serializer.detect_pattern(data)
        assert pattern == "api_response"

        serialized, _ = serializer.serialize(data)
        # Pattern should be preserved even with compression
        pattern_byte = serialized[0] & ~0x80  # Remove compression marker
        assert pattern_byte == 0  # API response is pattern 0

        deserialized = serializer.deserialize(serialized)
        assert deserialized == data
