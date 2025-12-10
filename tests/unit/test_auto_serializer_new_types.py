"""Unit tests for AutoSerializer new type support (UUID, set, frozenset).

Tests:
- UUID serialization roundtrip
- set/frozenset serialization roundtrip
- Nested complex objects with new types
- Error detection for unsupported types (Pydantic, ORM, custom classes)
- Security: _safe_hasattr prevents code execution
"""

from __future__ import annotations

from uuid import UUID

import pytest
from hypothesis import given
from hypothesis import strategies as st

from cachekit.serializers.auto_serializer import AutoSerializer
from cachekit.serializers.base import SerializationError


class TestAutoSerializerUUID:
    """Test UUID serialization support."""

    def test_serialize_uuid_object(self):
        """Test serializing a UUID object."""
        serializer = AutoSerializer()
        test_uuid = UUID("12345678-1234-5678-1234-567812345678")
        data = {"id": test_uuid}

        serialized, metadata = serializer.serialize(data)
        deserialized = serializer.deserialize(serialized, metadata)

        assert isinstance(deserialized["id"], UUID)
        assert deserialized["id"] == test_uuid

    def test_uuid_roundtrip_consistency(self):
        """Test UUID roundtrip across multiple iterations."""
        serializer = AutoSerializer()
        test_uuid = UUID("ffffffff-ffff-ffff-ffff-ffffffffffff")

        for _ in range(3):
            serialized, metadata = serializer.serialize({"id": test_uuid})
            deserialized = serializer.deserialize(serialized, metadata)
            assert deserialized["id"] == test_uuid

    def test_multiple_uuids(self):
        """Test serializing multiple UUIDs."""
        serializer = AutoSerializer()
        data = {
            "user_id": UUID("12345678-1234-5678-1234-567812345678"),
            "session_id": UUID("87654321-4321-8765-4321-876543218765"),
            "request_id": UUID("abcdefab-cdef-abcd-efab-cdefabcdefab"),
        }

        serialized, metadata = serializer.serialize(data)
        deserialized = serializer.deserialize(serialized, metadata)

        assert all(isinstance(v, UUID) for v in deserialized.values())
        assert deserialized == data

    def test_uuid_in_list(self):
        """Test UUIDs inside lists."""
        serializer = AutoSerializer()
        uuids = [
            UUID("12345678-1234-5678-1234-567812345678"),
            UUID("87654321-4321-8765-4321-876543218765"),
        ]
        data = {"ids": uuids}

        serialized, metadata = serializer.serialize(data)
        deserialized = serializer.deserialize(serialized, metadata)

        assert all(isinstance(u, UUID) for u in deserialized["ids"])
        assert deserialized["ids"] == uuids


class TestAutoSerializerSet:
    """Test set serialization support."""

    def test_serialize_set(self):
        """Test serializing a set."""
        serializer = AutoSerializer()
        test_set = {1, 2, 3}
        data = {"items": test_set}

        serialized, metadata = serializer.serialize(data)
        deserialized = serializer.deserialize(serialized, metadata)

        assert isinstance(deserialized["items"], set)
        assert deserialized["items"] == test_set

    def test_empty_set(self):
        """Test serializing an empty set."""
        serializer = AutoSerializer()
        data = {"items": set()}

        serialized, metadata = serializer.serialize(data)
        deserialized = serializer.deserialize(serialized, metadata)

        assert isinstance(deserialized["items"], set)
        assert len(deserialized["items"]) == 0

    def test_set_with_strings(self):
        """Test set containing strings."""
        serializer = AutoSerializer()
        test_set = {"apple", "banana", "cherry"}
        data = {"fruits": test_set}

        serialized, metadata = serializer.serialize(data)
        deserialized = serializer.deserialize(serialized, metadata)

        assert deserialized["fruits"] == test_set

    def test_set_type_preserved(self):
        """Test that set type is preserved (not converted to list)."""
        serializer = AutoSerializer()
        test_set = {1, 2, 3}
        serialized, metadata = serializer.serialize(test_set)
        deserialized = serializer.deserialize(serialized, metadata)

        assert isinstance(deserialized, set)
        assert not isinstance(deserialized, frozenset)
        assert deserialized == test_set


class TestAutoSerializerFrozenset:
    """Test frozenset serialization support."""

    def test_serialize_frozenset(self):
        """Test serializing a frozenset."""
        serializer = AutoSerializer()
        test_frozenset = frozenset([1, 2, 3])
        data = {"items": test_frozenset}

        serialized, metadata = serializer.serialize(data)
        deserialized = serializer.deserialize(serialized, metadata)

        assert isinstance(deserialized["items"], frozenset)
        assert deserialized["items"] == test_frozenset

    def test_empty_frozenset(self):
        """Test serializing an empty frozenset."""
        serializer = AutoSerializer()
        data = {"items": frozenset()}

        serialized, metadata = serializer.serialize(data)
        deserialized = serializer.deserialize(serialized, metadata)

        assert isinstance(deserialized["items"], frozenset)
        assert len(deserialized["items"]) == 0

    def test_frozenset_type_preserved(self):
        """Test that frozenset type is preserved (not converted to set)."""
        serializer = AutoSerializer()
        test_frozenset = frozenset([4, 5, 6])
        serialized, metadata = serializer.serialize(test_frozenset)
        deserialized = serializer.deserialize(serialized, metadata)

        assert isinstance(deserialized, frozenset)
        assert not isinstance(deserialized, set)
        assert deserialized == test_frozenset


class TestAutoSerializerComplexNesting:
    """Test nested complex objects with new types."""

    def test_nested_mixed_types(self):
        """Test nested structures with UUID, set, frozenset, and datetime."""
        from datetime import datetime

        serializer = AutoSerializer()
        test_uuid = UUID("12345678-1234-5678-1234-567812345678")
        test_datetime = datetime(2025, 11, 14, 12, 0, 0)

        data = {
            "user": {
                "id": test_uuid,
                "tags": {"admin", "active"},
                "locked_settings": frozenset(["theme", "language"]),
                "created_at": test_datetime,
            },
            "ids": [
                UUID("87654321-4321-8765-4321-876543218765"),
                UUID("abcdefab-cdef-abcd-efab-cdefabcdefab"),
            ],
        }

        serialized, metadata = serializer.serialize(data)
        deserialized = serializer.deserialize(serialized, metadata)

        assert isinstance(deserialized["user"]["id"], UUID)
        assert isinstance(deserialized["user"]["tags"], set)
        assert isinstance(deserialized["user"]["locked_settings"], frozenset)
        assert isinstance(deserialized["user"]["created_at"], datetime)
        assert all(isinstance(u, UUID) for u in deserialized["ids"])


class TestAutoSerializerErrorDetection:
    """Test error detection for unsupported types."""

    def test_pydantic_model_error(self):
        """Test helpful error for Pydantic models."""
        try:
            from pydantic import BaseModel
        except ImportError:
            return  # Skip if pydantic not installed

        serializer = AutoSerializer()

        class User(BaseModel):
            name: str
            age: int

        user = User(name="Alice", age=30)

        try:
            serializer.serialize(user)
            pytest.fail("Should raise TypeError for Pydantic model")
        except TypeError as e:
            assert "model_dump()" in str(e)
            assert "Pydantic" in str(e)

    def test_custom_class_error(self):
        """Test helpful error for custom classes."""
        serializer = AutoSerializer()

        class CustomClass:
            def __init__(self, value):
                self.value = value

        obj = CustomClass(42)

        try:
            serializer.serialize(obj)
            pytest.fail("Should raise TypeError for custom class")
        except TypeError as e:
            assert "custom class" in str(e).lower()

    def test_safe_hasattr_prevents_code_execution(self):
        """Test that _safe_hasattr prevents execution of malicious code."""
        from cachekit.serializers.auto_serializer import _safe_hasattr

        class MaliciousClass:
            def __getattr__(self, name):
                # Simulate malicious code that should NOT execute
                raise RuntimeError("MALICIOUS CODE EXECUTED")

        obj = MaliciousClass()

        # Should return False without raising
        result = _safe_hasattr(obj, "any_attribute")
        assert result is False
        # No exception raised = secure behavior

    def test_safe_hasattr_normal_usage(self):
        """Test that _safe_hasattr works for normal objects."""
        from cachekit.serializers.auto_serializer import _safe_hasattr

        class NormalClass:
            def __init__(self):
                self.existing = "yes"

        obj = NormalClass()

        assert _safe_hasattr(obj, "existing") is True
        assert _safe_hasattr(obj, "nonexistent") is False


class TestAutoSerializerCorruptionDetection:
    """Test corruption detection and error handling for special types.

    These tests validate that malformed serialized data is properly detected
    and raises SerializationError (not KeyError or other exceptions).
    """

    def test_uuid_missing_value_field(self):
        """Test that corrupted UUID marker (missing 'value') raises SerializationError."""
        import msgpack

        serializer = AutoSerializer()
        # Simulate corrupted data: UUID marker without value field
        corrupted = msgpack.packb({"__uuid__": True})  # Missing "value"

        with pytest.raises(SerializationError, match="Invalid UUID format"):
            serializer.deserialize(corrupted)

    def test_uuid_invalid_string(self):
        """Test that invalid UUID strings raise SerializationError."""
        import msgpack

        serializer = AutoSerializer()
        corrupted = msgpack.packb({"__uuid__": True, "value": "not-a-uuid"})

        with pytest.raises(SerializationError, match="Invalid UUID format"):
            serializer.deserialize(corrupted)

    def test_datetime_missing_value_field(self):
        """Test that corrupted datetime marker (missing 'value') raises SerializationError."""
        import msgpack

        serializer = AutoSerializer()
        corrupted = msgpack.packb({"__datetime__": True})  # Missing "value"

        with pytest.raises(SerializationError, match="Invalid datetime format"):
            serializer.deserialize(corrupted)

    def test_date_missing_value_field(self):
        """Test that corrupted date marker (missing 'value') raises SerializationError."""
        import msgpack

        serializer = AutoSerializer()
        corrupted = msgpack.packb({"__date__": True})  # Missing "value"

        with pytest.raises(SerializationError, match="Invalid date format"):
            serializer.deserialize(corrupted)

    def test_time_missing_value_field(self):
        """Test that corrupted time marker (missing 'value') raises SerializationError."""
        import msgpack

        serializer = AutoSerializer()
        corrupted = msgpack.packb({"__time__": True})  # Missing "value"

        with pytest.raises(SerializationError, match="Invalid time format"):
            serializer.deserialize(corrupted)

    def test_set_non_list_value(self):
        """Test that set marker with non-list value raises SerializationError."""
        import msgpack

        serializer = AutoSerializer()
        corrupted = msgpack.packb({"__set__": True, "value": "not-a-list"})

        with pytest.raises(SerializationError, match="Invalid set format"):
            serializer.deserialize(corrupted)

    def test_set_missing_value_field(self):
        """Test that corrupted set marker (missing 'value') raises SerializationError."""
        import msgpack

        serializer = AutoSerializer()
        corrupted = msgpack.packb({"__set__": True})  # Missing "value"

        with pytest.raises(SerializationError, match="Invalid set format"):
            serializer.deserialize(corrupted)

    def test_frozenset_missing_value_field(self):
        """Test that corrupted frozenset marker (missing 'value') raises SerializationError."""
        import msgpack

        serializer = AutoSerializer()
        # Frozenset uses __set__ marker with frozen=True flag
        corrupted = msgpack.packb({"__set__": True, "frozen": True})  # Missing "value"

        with pytest.raises(SerializationError, match="Invalid set format"):
            serializer.deserialize(corrupted)

    def test_corruption_in_nested_structure(self):
        """Test that corruption in nested data is detected."""
        import msgpack

        serializer = AutoSerializer()
        # Create a valid structure with a corrupted nested UUID
        data = {
            "user": {
                "id": {"__uuid__": True},  # Missing "value"
            }
        }
        corrupted = msgpack.packb(data)

        with pytest.raises(SerializationError):
            serializer.deserialize(corrupted)

    def test_partial_truncation_detection(self):
        """Test that truncated data raises appropriate errors."""

        serializer = AutoSerializer()
        valid_uuid = UUID("12345678-1234-5678-1234-567812345678")
        data = {"id": valid_uuid}

        serialized, metadata = serializer.serialize(data)
        # Truncate the data
        truncated = serialized[: len(serialized) // 2]

        with pytest.raises(SerializationError):
            serializer.deserialize(truncated, metadata)


class TestAutoSerializerPropertyBased:
    """Property-based tests using Hypothesis for new type support.

    These tests verify serialization properties across randomly generated inputs:
    - UUID roundtrip property: serialize(x) → deserialize → equals x
    - Set roundtrip property: serialize(x) → deserialize → equals x
    - Frozenset roundtrip property: serialize(x) → deserialize → equals x
    - Type preservation: type(deserialize(serialize(x))) == type(x)
    """

    @given(st.uuids())
    def test_uuid_roundtrip_property(self, test_uuid: UUID):
        """Property: UUID roundtrips through serialization unchanged."""
        serializer = AutoSerializer()
        data = {"id": test_uuid}

        serialized, metadata = serializer.serialize(data)
        deserialized = serializer.deserialize(serialized, metadata)

        assert deserialized["id"] == test_uuid
        assert isinstance(deserialized["id"], UUID)

    @given(st.lists(st.integers(min_value=-(2**63), max_value=2**63 - 1), min_size=0, max_size=100).map(set))
    def test_set_roundtrip_property(self, test_set: set):
        """Property: Sets roundtrip through serialization unchanged."""
        serializer = AutoSerializer()
        data = {"items": test_set}

        serialized, metadata = serializer.serialize(data)
        deserialized = serializer.deserialize(serialized, metadata)

        assert deserialized["items"] == test_set
        assert isinstance(deserialized["items"], set)
        assert not isinstance(deserialized["items"], frozenset)

    @given(st.lists(st.integers(min_value=-(2**63), max_value=2**63 - 1), min_size=0, max_size=100).map(frozenset))
    def test_frozenset_roundtrip_property(self, test_frozenset: frozenset):
        """Property: Frozensets roundtrip through serialization unchanged."""
        serializer = AutoSerializer()
        data = {"items": test_frozenset}

        serialized, metadata = serializer.serialize(data)
        deserialized = serializer.deserialize(serialized, metadata)

        assert deserialized["items"] == test_frozenset
        assert isinstance(deserialized["items"], frozenset)

    @given(
        st.dictionaries(
            st.text(min_size=1),
            st.uuids(),
            min_size=1,
            max_size=10,
        )
    )
    def test_multiple_uuids_roundtrip_property(self, uuid_dict: dict[str, UUID]):
        """Property: Multiple UUIDs in a dict roundtrip correctly."""
        serializer = AutoSerializer()

        serialized, metadata = serializer.serialize(uuid_dict)
        deserialized = serializer.deserialize(serialized, metadata)

        assert deserialized == uuid_dict
        assert all(isinstance(v, UUID) for v in deserialized.values())

    @given(st.lists(st.uuids(), min_size=1, max_size=100))
    def test_uuid_list_roundtrip_property(self, uuid_list: list[UUID]):
        """Property: Lists of UUIDs roundtrip correctly."""
        serializer = AutoSerializer()

        serialized, metadata = serializer.serialize(uuid_list)
        deserialized = serializer.deserialize(serialized, metadata)

        assert deserialized == uuid_list
        assert all(isinstance(u, UUID) for u in deserialized)

    @given(
        st.lists(st.integers(min_value=-(2**31), max_value=2**31 - 1), min_size=0, max_size=50).map(set),
        st.lists(st.integers(min_value=-(2**31), max_value=2**31 - 1), min_size=0, max_size=50).map(set),
    )
    def test_nested_sets_roundtrip_property(self, set1: set, set2: set):
        """Property: Nested sets in dict structure roundtrip correctly."""
        serializer = AutoSerializer()
        data = {"group_a": set1, "group_b": set2}

        serialized, metadata = serializer.serialize(data)
        deserialized = serializer.deserialize(serialized, metadata)

        assert deserialized == data
        assert isinstance(deserialized["group_a"], set)
        assert isinstance(deserialized["group_b"], set)

    @given(
        st.lists(st.text(), min_size=0, max_size=50).map(frozenset),
        st.lists(st.integers(min_value=-(2**31), max_value=2**31 - 1), min_size=0, max_size=50).map(frozenset),
    )
    def test_nested_frozensets_roundtrip_property(self, fset1: frozenset, fset2: frozenset):
        """Property: Nested frozensets roundtrip correctly."""
        serializer = AutoSerializer()
        data = {"immutable_a": fset1, "immutable_b": fset2}

        serialized, metadata = serializer.serialize(data)
        deserialized = serializer.deserialize(serialized, metadata)

        assert deserialized == data
        assert isinstance(deserialized["immutable_a"], frozenset)
        assert isinstance(deserialized["immutable_b"], frozenset)

    @given(
        st.dictionaries(
            st.text(min_size=1, max_size=10),
            st.one_of(
                st.uuids(),
                st.lists(st.integers(min_value=-(2**31), max_value=2**31 - 1), max_size=20).map(set),
                st.lists(st.integers(min_value=-(2**31), max_value=2**31 - 1), max_size=20).map(frozenset),
            ),
            min_size=1,
            max_size=5,
        )
    )
    def test_mixed_new_types_roundtrip_property(self, mixed_data: dict):
        """Property: Mixed UUIDs, sets, and frozensets roundtrip correctly."""
        serializer = AutoSerializer()

        serialized, metadata = serializer.serialize(mixed_data)
        deserialized = serializer.deserialize(serialized, metadata)

        assert deserialized == mixed_data

    @given(st.integers(min_value=0, max_value=100))
    def test_set_size_property(self, size: int):
        """Property: Sets of any size roundtrip with correct cardinality."""
        serializer = AutoSerializer()
        test_set = set(range(size))

        serialized, metadata = serializer.serialize(test_set)
        deserialized = serializer.deserialize(serialized, metadata)

        assert len(deserialized) == size
        assert isinstance(deserialized, set)
        assert deserialized == test_set

    @given(st.integers(min_value=0, max_value=100))
    def test_frozenset_immutability_property(self, size: int):
        """Property: Frozensets remain hashable after roundtrip."""
        serializer = AutoSerializer()
        test_frozenset = frozenset(range(size))

        serialized, metadata = serializer.serialize(test_frozenset)
        deserialized = serializer.deserialize(serialized, metadata)

        # Key property: frozensets are hashable
        hash(deserialized)  # Should not raise
        assert deserialized == test_frozenset

    @given(st.lists(st.uuids(), min_size=1, max_size=10))
    def test_uuid_deterministic_serialization_property(self, uuid_list: list[UUID]):
        """Property: Serializing same data produces consistent bytes."""
        serializer = AutoSerializer()

        # Serialize the same data twice
        bytes1, _ = serializer.serialize(uuid_list)
        bytes2, _ = serializer.serialize(uuid_list)

        # Should produce identical bytes (deterministic)
        assert bytes1 == bytes2

        # And should deserialize to same values
        result1 = serializer.deserialize(bytes1)
        result2 = serializer.deserialize(bytes2)
        assert result1 == result2
