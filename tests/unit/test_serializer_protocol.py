"""Unit tests for SerializerProtocol compliance.

Tests protocol validation, isinstance() checks, and contract enforcement.
"""

from __future__ import annotations

from typing import Any

from cachekit.serializers.arrow_serializer import ArrowSerializer
from cachekit.serializers.auto_serializer import AutoSerializer
from cachekit.serializers.base import SerializationFormat, SerializationMetadata, SerializerProtocol


class TestSerializerProtocolCompliance:
    """Test that serializers implement SerializerProtocol correctly."""

    def test_auto_serializer_implements_protocol(self):
        """AutoSerializer must implement SerializerProtocol."""
        serializer = AutoSerializer()
        assert isinstance(serializer, SerializerProtocol)

    def test_arrow_serializer_implements_protocol(self):
        """ArrowSerializer must implement SerializerProtocol."""
        serializer = ArrowSerializer()
        assert isinstance(serializer, SerializerProtocol)

    def test_custom_serializer_implements_protocol(self):
        """Custom class with serialize/deserialize methods implements protocol."""

        class CustomSerializer:
            def serialize(self, obj: Any) -> tuple[bytes, SerializationMetadata]:
                return b"custom", SerializationMetadata(serialization_format=SerializationFormat.MSGPACK)

            def deserialize(self, data: bytes, metadata: Any = None) -> Any:
                return "custom"

        serializer = CustomSerializer()
        assert isinstance(serializer, SerializerProtocol)

    def test_incomplete_serializer_does_not_implement_protocol(self):
        """Class without deserialize method does NOT implement protocol."""

        class IncompleteSerializer:
            def serialize(self, obj: Any) -> tuple[bytes, SerializationMetadata]:
                return b"data", SerializationMetadata(serialization_format=SerializationFormat.MSGPACK)

        serializer = IncompleteSerializer()
        assert not isinstance(serializer, SerializerProtocol)

    def test_protocol_requires_correct_signatures(self):
        """Protocol validation checks method signatures match."""

        class WrongSignatureSerializer:
            def serialize(self, obj: Any) -> bytes:  # Wrong return type
                return b"data"

            def deserialize(self, data: bytes) -> Any:
                return "data"

        serializer = WrongSignatureSerializer()
        # NOTE: Protocol checking is structural (duck typing), so this will pass isinstance()
        # but will fail at runtime when the decorator calls serialize() expecting tuple return.
        # This test documents the behavior - we rely on type checkers to catch signature mismatches.
        assert isinstance(serializer, SerializerProtocol)


class TestSerializerProtocolContract:
    """Test that serializers follow the SerializerProtocol contract."""

    def test_auto_serializer_serialize_returns_tuple(self):
        """serialize() must return (bytes, SerializationMetadata)."""
        serializer = AutoSerializer()
        result = serializer.serialize({"key": "value"})

        assert isinstance(result, tuple)
        assert len(result) == 2
        assert isinstance(result[0], bytes)
        assert isinstance(result[1], SerializationMetadata)

    def test_auto_serializer_deserialize_accepts_bytes(self):
        """deserialize() must accept bytes and return original object."""
        serializer = AutoSerializer()
        obj = {"key": "value", "number": 42}
        data, metadata = serializer.serialize(obj)

        result = serializer.deserialize(data, metadata)
        assert result == obj

    def test_serialization_metadata_has_required_fields(self):
        """SerializationMetadata must have format, compressed, encrypted fields."""
        serializer = AutoSerializer()
        _, metadata = serializer.serialize({"test": "data"})

        assert hasattr(metadata, "format")
        assert hasattr(metadata, "compressed")
        assert hasattr(metadata, "encrypted")
        assert isinstance(metadata.format, SerializationFormat)
        assert isinstance(metadata.compressed, bool)
        assert isinstance(metadata.encrypted, bool)

    def test_round_trip_preserves_data(self):
        """Serialize + deserialize must be lossless."""
        serializer = AutoSerializer()
        test_data = {
            "string": "hello",
            "number": 123,
            "float": 45.67,
            "list": [1, 2, 3],
            "nested": {"inner": "value"},
        }

        data, metadata = serializer.serialize(test_data)
        result = serializer.deserialize(data, metadata)

        assert result == test_data

    def test_serialize_with_none_metadata_optional(self):
        """deserialize() must work without metadata parameter (optional)."""
        serializer = AutoSerializer()
        obj = {"test": "data"}
        data, _ = serializer.serialize(obj)

        # Calling deserialize without metadata should work (metadata is optional)
        result = serializer.deserialize(data)
        assert result == obj


class TestSerializationMetadata:
    """Test SerializationMetadata dataclass behavior."""

    def test_metadata_initialization(self):
        """SerializationMetadata can be initialized with required fields."""
        metadata = SerializationMetadata(serialization_format=SerializationFormat.MSGPACK, compressed=True, encrypted=False)

        assert metadata.format == SerializationFormat.MSGPACK
        assert metadata.compressed is True
        assert metadata.encrypted is False

    def test_metadata_to_dict_conversion(self):
        """SerializationMetadata.to_dict() produces valid dictionary."""
        metadata = SerializationMetadata(
            serialization_format=SerializationFormat.MSGPACK,
            compressed=True,
            encrypted=False,
            original_type="dict",
        )

        result = metadata.to_dict()
        assert isinstance(result, dict)
        assert result["format"] == "msgpack"
        assert result["compressed"] is True
        # encrypted field only included if encrypted=True
        assert "encrypted" not in result
        assert result["original_type"] == "dict"

    def test_metadata_from_dict_roundtrip(self):
        """SerializationMetadata.from_dict() reconstructs metadata."""
        original = SerializationMetadata(
            serialization_format=SerializationFormat.MSGPACK,
            compressed=True,
            encrypted=False,
            original_type="list",
        )

        data = original.to_dict()
        reconstructed = SerializationMetadata.from_dict(data)

        assert reconstructed.format == original.format
        assert reconstructed.compressed == original.compressed
        assert reconstructed.encrypted == original.encrypted
        assert reconstructed.original_type == original.original_type

    def test_metadata_encryption_fields(self):
        """SerializationMetadata supports encryption-related fields."""
        metadata = SerializationMetadata(
            serialization_format=SerializationFormat.MSGPACK,
            encrypted=True,
            tenant_id="tenant-123",
            encryption_algorithm="AES-256-GCM",
            key_fingerprint="abc123",
        )

        assert metadata.encrypted is True
        assert metadata.tenant_id == "tenant-123"
        assert metadata.encryption_algorithm == "AES-256-GCM"
        assert metadata.key_fingerprint == "abc123"

        # to_dict() includes encryption fields
        data = metadata.to_dict()
        assert data["encrypted"] is True
        assert data["tenant_id"] == "tenant-123"
        assert data["encryption_algorithm"] == "AES-256-GCM"
        assert data["key_fingerprint"] == "abc123"
