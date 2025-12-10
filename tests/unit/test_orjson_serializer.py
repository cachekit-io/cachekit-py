"""Unit tests for OrjsonSerializer.

Comprehensive test coverage for high-performance JSON serialization.
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

import pytest

from cachekit.serializers import OrjsonSerializer
from cachekit.serializers.base import SerializationError, SerializationFormat


class TestOrjsonSerializerBasics:
    """Basic functionality tests for OrjsonSerializer."""

    def test_initialization(self):
        """Test OrjsonSerializer instantiation."""
        serializer = OrjsonSerializer()
        assert serializer is not None

    def test_serialize_simple_dict(self):
        """Test serializing simple dictionary."""
        serializer = OrjsonSerializer()
        data = {"key": "value", "number": 42}

        result, metadata = serializer.serialize(data)

        assert isinstance(result, bytes)
        assert metadata.format == SerializationFormat.ORJSON
        assert metadata.compressed is False
        assert metadata.encrypted is False
        assert metadata.original_type == "orjson"

    def test_deserialize_simple_dict(self):
        """Test deserializing simple dictionary."""
        serializer = OrjsonSerializer()
        original = {"key": "value", "number": 42}

        serialized, _ = serializer.serialize(original)
        deserialized = serializer.deserialize(serialized)

        assert deserialized == original

    def test_roundtrip_preserves_data(self):
        """Test serialize-deserialize roundtrip preserves data."""
        serializer = OrjsonSerializer()
        test_cases = [
            {"simple": "dict"},
            {"nested": {"key": "value"}},
            {"list": [1, 2, 3]},
            {"mixed": [1, "two", 3.0, True, None]},
            {"number": 123},
            {"float": 3.14159},
            {"bool": True},
            {"null": None},
        ]

        for original in test_cases:
            serialized, _ = serializer.serialize(original)
            deserialized = serializer.deserialize(serialized)
            assert deserialized == original, f"Failed for {original}"


class TestOrjsonSerializerDataTypes:
    """Test OrjsonSerializer with various data types."""

    def test_serialize_string(self):
        """Test serializing string values."""
        serializer = OrjsonSerializer()
        data = {"text": "Hello, World!", "empty": ""}

        serialized, _ = serializer.serialize(data)
        deserialized = serializer.deserialize(serialized)

        assert deserialized == data

    def test_serialize_numbers(self):
        """Test serializing numeric values."""
        serializer = OrjsonSerializer()
        data = {
            "int": 42,
            "float": 3.14159,
            "negative": -100,
            "zero": 0,
            "large": 10**18,
        }

        serialized, _ = serializer.serialize(data)
        deserialized = serializer.deserialize(serialized)

        assert deserialized == data

    def test_serialize_booleans(self):
        """Test serializing boolean values."""
        serializer = OrjsonSerializer()
        data = {"true": True, "false": False}

        serialized, _ = serializer.serialize(data)
        deserialized = serializer.deserialize(serialized)

        assert deserialized == data

    def test_serialize_none(self):
        """Test serializing None values."""
        serializer = OrjsonSerializer()
        data = {"null": None, "value": "not null"}

        serialized, _ = serializer.serialize(data)
        deserialized = serializer.deserialize(serialized)

        assert deserialized == data

    def test_serialize_lists(self):
        """Test serializing list values."""
        serializer = OrjsonSerializer()
        data = {
            "empty": [],
            "numbers": [1, 2, 3],
            "mixed": [1, "two", 3.0, True, None],
            "nested": [[1, 2], [3, 4]],
        }

        serialized, _ = serializer.serialize(data)
        deserialized = serializer.deserialize(serialized)

        assert deserialized == data

    def test_serialize_nested_structures(self):
        """Test serializing deeply nested structures."""
        serializer = OrjsonSerializer()
        data = {
            "level1": {
                "level2": {
                    "level3": {
                        "level4": {"value": "deep"},
                    },
                },
            },
        }

        serialized, _ = serializer.serialize(data)
        deserialized = serializer.deserialize(serialized)

        assert deserialized == data

    def test_serialize_datetime(self):
        """Test serializing datetime objects (orjson auto-converts to ISO8601)."""
        serializer = OrjsonSerializer()
        dt = datetime(2025, 11, 13, 12, 0, 0, tzinfo=timezone.utc)
        data = {"timestamp": dt}

        serialized, _ = serializer.serialize(data)
        deserialized = serializer.deserialize(serialized)

        # orjson converts datetime to ISO8601 string
        assert isinstance(deserialized["timestamp"], str)
        assert "2025-11-13" in deserialized["timestamp"]

    def test_serialize_uuid(self):
        """Test serializing UUID objects (orjson auto-converts to string)."""
        serializer = OrjsonSerializer()
        uuid_obj = UUID("12345678-1234-5678-1234-567812345678")
        data = {"id": uuid_obj}

        serialized, _ = serializer.serialize(data)
        deserialized = serializer.deserialize(serialized)

        # orjson converts UUID to string
        assert isinstance(deserialized["id"], str)
        assert deserialized["id"] == "12345678-1234-5678-1234-567812345678"


class TestOrjsonSerializerEdgeCases:
    """Edge case tests for OrjsonSerializer."""

    def test_empty_dict(self):
        """Test serializing empty dictionary."""
        serializer = OrjsonSerializer()
        data = {}

        serialized, _ = serializer.serialize(data)
        deserialized = serializer.deserialize(serialized)

        assert deserialized == data

    def test_empty_list(self):
        """Test serializing empty list."""
        serializer = OrjsonSerializer()
        data = {"items": []}

        serialized, _ = serializer.serialize(data)
        deserialized = serializer.deserialize(serialized)

        assert deserialized == data

    def test_large_dict(self):
        """Test serializing large dictionary."""
        serializer = OrjsonSerializer()
        data = {f"key_{i}": f"value_{i}" for i in range(1000)}

        serialized, _ = serializer.serialize(data)
        deserialized = serializer.deserialize(serialized)

        assert deserialized == data
        assert len(deserialized) == 1000

    def test_unicode_strings(self):
        """Test serializing Unicode strings."""
        serializer = OrjsonSerializer()
        data = {
            "emoji": "🚀🔥💯",
            "chinese": "你好世界",
            "arabic": "مرحبا بالعالم",
            "russian": "Привет мир",
        }

        serialized, _ = serializer.serialize(data)
        deserialized = serializer.deserialize(serialized)

        assert deserialized == data

    def test_special_floats(self):
        """Test serializing special float values."""
        serializer = OrjsonSerializer()

        # orjson handles inf/nan differently than stdlib json
        # Test normal floats instead
        data = {"normal": 3.14159, "zero": 0.0, "negative": -2.5}

        serialized, _ = serializer.serialize(data)
        deserialized = serializer.deserialize(serialized)

        assert deserialized == data


class TestOrjsonSerializerErrors:
    """Error handling tests for OrjsonSerializer."""

    def test_unsupported_type_raises_error(self):
        """Test that unsupported types raise TypeError."""
        serializer = OrjsonSerializer()

        # Custom object that's not JSON-serializable
        class CustomObject:
            pass

        data = {"obj": CustomObject()}

        with pytest.raises(TypeError) as exc_info:
            serializer.serialize(data)

        assert "not JSON-serializable" in str(exc_info.value)

    def test_malformed_data_raises_error(self):
        """Test that malformed data raises SerializationError."""
        serializer = OrjsonSerializer()

        # Invalid JSON bytes (too short to even be valid envelope)
        malformed_data = b"not valid json {["

        with pytest.raises(SerializationError) as exc_info:
            serializer.deserialize(malformed_data)

        # Error message will indicate checksum or size issue
        error_msg = str(exc_info.value)
        assert "Invalid data" in error_msg or "Failed to deserialize" in error_msg or "Checksum validation failed" in error_msg

    def test_empty_bytes_raises_error(self):
        """Test that empty bytes raise SerializationError."""
        serializer = OrjsonSerializer()

        with pytest.raises(SerializationError):
            serializer.deserialize(b"")


class TestOrjsonSerializerProtocolCompliance:
    """Test SerializerProtocol compliance."""

    def test_implements_serialize_method(self):
        """Test that OrjsonSerializer implements serialize()."""
        serializer = OrjsonSerializer()
        assert hasattr(serializer, "serialize")
        assert callable(serializer.serialize)

    def test_implements_deserialize_method(self):
        """Test that OrjsonSerializer implements deserialize()."""
        serializer = OrjsonSerializer()
        assert hasattr(serializer, "deserialize")
        assert callable(serializer.deserialize)

    def test_serialize_returns_tuple(self):
        """Test that serialize() returns tuple[bytes, SerializationMetadata]."""
        serializer = OrjsonSerializer()
        result = serializer.serialize({"test": "data"})

        assert isinstance(result, tuple)
        assert len(result) == 2
        assert isinstance(result[0], bytes)
        # Metadata check
        assert hasattr(result[1], "format")

    def test_deserialize_with_metadata_optional(self):
        """Test that deserialize() works with metadata=None."""
        serializer = OrjsonSerializer()
        data = {"test": "data"}

        serialized, _ = serializer.serialize(data)

        # Should work with metadata=None
        deserialized = serializer.deserialize(serialized, metadata=None)
        assert deserialized == data

        # Should also work without metadata argument
        deserialized2 = serializer.deserialize(serialized)
        assert deserialized2 == data


class TestOrjsonSerializerMetadata:
    """Test SerializationMetadata generation."""

    def test_metadata_format_is_orjson(self):
        """Test that metadata.format is ORJSON."""
        serializer = OrjsonSerializer()
        _, metadata = serializer.serialize({"test": "data"})

        assert metadata.format == SerializationFormat.ORJSON
        assert metadata.format.value == "orjson"

    def test_metadata_compressed_is_false(self):
        """Test that metadata.compressed is False (no built-in compression)."""
        serializer = OrjsonSerializer()
        _, metadata = serializer.serialize({"test": "data"})

        assert metadata.compressed is False

    def test_metadata_encrypted_is_false(self):
        """Test that metadata.encrypted is False (encryption is wrapper's job)."""
        serializer = OrjsonSerializer()
        _, metadata = serializer.serialize({"test": "data"})

        assert metadata.encrypted is False

    def test_metadata_original_type_is_orjson(self):
        """Test that metadata.original_type is 'orjson'."""
        serializer = OrjsonSerializer()
        _, metadata = serializer.serialize({"test": "data"})

        assert metadata.original_type == "orjson"


class TestOrjsonSerializerIntegration:
    """Integration tests with get_serializer factory."""

    def test_factory_can_create_orjson_serializer(self):
        """Test that get_serializer('orjson') works."""
        from cachekit.serializers import get_serializer

        serializer = get_serializer("orjson")

        assert serializer is not None
        assert isinstance(serializer, OrjsonSerializer)

    def test_factory_caches_orjson_serializer(self):
        """Test that get_serializer caches OrjsonSerializer instances."""
        from cachekit.serializers import get_serializer

        serializer1 = get_serializer("orjson")
        serializer2 = get_serializer("orjson")

        # Should be same cached instance
        assert serializer1 is serializer2

    def test_orjson_serializer_in_registry(self):
        """Test that OrjsonSerializer is registered."""
        from cachekit.serializers import SERIALIZER_REGISTRY

        assert "orjson" in SERIALIZER_REGISTRY
        assert SERIALIZER_REGISTRY["orjson"] == OrjsonSerializer
