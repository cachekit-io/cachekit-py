"""Unit tests for AutoSerializer new type support (UUID, set, frozenset, tuple).

Tests:
- UUID serialization roundtrip
- set/frozenset serialization roundtrip
- Nested complex objects with new types
- Error detection for unsupported types (Pydantic, ORM, custom classes)
- Security: _safe_hasattr prevents code execution
- Exception contract: forged payloads and object-hook diagnostics fail closed as SerializationError
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta
from uuid import UUID

import msgpack
import pytest
from hypothesis import given
from hypothesis import strategies as st

from cachekit._rust_serializer import ByteStorage
from cachekit.serializers.auto_serializer import _MAX_NON_STRING_SET_ELEMENTS, AutoSerializer
from cachekit.serializers.base import SerializationError
from tests.utils.hypothesis_settings import slow_first_draw

# Well-formed msgpack documents whose ndarray marker makes numpy raise something that is neither
# a decode-bound rejection nor a ValueError, pinning the serializer's exception contract:
# OverflowError (dict dtype with itemsize past C long), SyntaxError (numpy's comma-string dtype
# parser runs ast.literal_eval on the forged shape prefix "(1,f8"), and the M8[0ns] dtype that
# numpy accepts and pandas then dies on with SIGFPE — refused before any array is built.
FORGED_NDARRAYS = {
    "itemsize-past-c-long": msgpack.packb(
        {"__ndarray__": True, "dtype": {"names": ["a"], "formats": ["f8"], "itemsize": 2**63}, "shape": [1], "data": b"x" * 8}
    ),
    "dtype-shape-prefix-unparseable": msgpack.packb({"__ndarray__": True, "dtype": "(1,f8", "shape": [1], "data": b"x" * 8}),
    "datetime-zero-unit-multiplier": msgpack.packb({"__ndarray__": True, "dtype": "M8[0ns]", "shape": [1], "data": b"x" * 8}),
}


@pytest.mark.parametrize("payload", FORGED_NDARRAYS.values(), ids=list(FORGED_NDARRAYS))
@pytest.mark.parametrize(
    "serializer, wrap",
    [
        (AutoSerializer(enable_integrity_checking=False), lambda b: b),
        (AutoSerializer(), lambda b: bytes(ByteStorage("msgpack").store(b, "msgpack"))),
    ],
    ids=["plain", "verified-envelope"],
)
def test_forged_payload_failure_is_a_serialization_error(
    serializer: AutoSerializer, wrap: Callable[[bytes], bytes], payload: bytes
) -> None:
    with pytest.raises(SerializationError):
        serializer.deserialize(wrap(payload))


def test_hook_diagnostic_propagates_unwrapped_from_the_verified_envelope() -> None:
    """The object hook's SerializationError sits outside PAYLOAD_DECODE_ERRORS, so it leaves the
    verified-envelope decode unwrapped — that catch must never widen back to ``Exception``."""
    entry = bytes(ByteStorage("msgpack").store(msgpack.packb({"__uuid__": True}), "msgpack"))
    with pytest.raises(SerializationError, match=r"^Invalid UUID format: missing 'value' field") as excinfo:
        AutoSerializer().deserialize(entry)
    assert excinfo.value.__cause__ is None


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


def _uuid_marker(i: int) -> dict:
    return {"__uuid__": True, "value": str(UUID(int=i))}


def _set_doc(values: list, frozen: bool) -> bytes:
    return msgpack.packb({"__set__": True, "value": values, "frozen": frozen}, use_bin_type=True)


# UUID.__hash__ is hash(self.int), and an int hashes mod 2**61 - 1: these all hash alike.
_SAME_HASH = [12345 + k * (2**61 - 1) for k in range(40)]


class _Str(str):
    pass


class TestAutoSerializerSetHashFloodBound:
    """A stored set holds at most _MAX_NON_STRING_SET_ELEMENTS elements that are neither str nor bytes, and no three
    of them share a hash; either refusal comes before set() builds the set, so a forged one never pays for a quadratic
    build. The writer refuses a set the reader would refuse by count."""

    @pytest.fixture
    def counts(self, monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
        counts = {"hash": 0, "eq": 0}
        uuid_hash, uuid_eq = UUID.__hash__, UUID.__eq__

        def counting_hash(self: UUID) -> int:
            counts["hash"] += 1
            return uuid_hash(self)

        def counting_eq(self: UUID, other: object) -> bool:
            counts["eq"] += 1
            return uuid_eq(self, other)

        monkeypatch.setattr(UUID, "__hash__", counting_hash)
        monkeypatch.setattr(UUID, "__eq__", counting_eq)
        return counts

    @pytest.mark.parametrize("frozen", [False, True])
    @pytest.mark.parametrize(
        "element",
        [_uuid_marker, lambda i: {"__tuple__": True, "value": [_uuid_marker(i), 0]}],
        ids=["uuid", "tuple"],
    )
    def test_a_same_hash_set_is_refused_before_any_comparison(
        self, counts: dict[str, int], frozen: bool, element: Callable[[int], dict]
    ) -> None:
        data = _set_doc([element(i) for i in _SAME_HASH], frozen)
        with pytest.raises(SerializationError, match="three elements share one hash: a hash-flood bound"):
            AutoSerializer(enable_integrity_checking=False).deserialize(data)
        # Hashed once each, by the sort; set() would have compared each to every earlier one.
        assert counts == {"hash": len(_SAME_HASH), "eq": 0}

    def test_two_elements_may_share_a_hash_but_not_three(self) -> None:
        serializer = AutoSerializer()
        value = {(-1,), (-2,), UUID(int=_SAME_HASH[0]), UUID(int=_SAME_HASH[1]), *range(100)}
        assert hash((-1,)) == hash((-2,)) == hash((-1 - (2**61 - 1),))
        assert serializer.deserialize(*serializer.serialize(value)) == value
        for third in (UUID(int=_SAME_HASH[2]), (-1 - (2**61 - 1),)):
            with pytest.raises(SerializationError, match="three elements share one hash"):
                serializer.deserialize(*serializer.serialize(value | {third}))

    def test_ints_and_floats_on_one_hash_are_not_refused(self) -> None:
        """They compare in C, and few share a hash within msgpack's range: every int there with hash 0, and 2.0**k
        for the k whose float hash is 1."""
        p = 2**61 - 1
        value = {*(k * p for k in range(-4, 8)), *(2.0 ** (61 * k) for k in range(-16, 17))}
        assert {hash(x) for x in value} == {0, 1}
        value.add(UUID(int=0))  # so the hash check runs, over the UUID alone
        serializer = AutoSerializer()
        assert serializer.deserialize(*serializer.serialize(value)) == value

    @pytest.mark.parametrize("frozen", [False, True])
    def test_a_set_over_the_bound_is_refused_before_any_hash(self, counts: dict[str, int], frozen: bool) -> None:
        data = _set_doc([_uuid_marker(i) for i in range(_MAX_NON_STRING_SET_ELEMENTS + 1)], frozen)
        with pytest.raises(SerializationError, match="more than 1024 elements that are neither str nor bytes"):
            AutoSerializer(enable_integrity_checking=False).deserialize(data)
        assert counts == {"hash": 0, "eq": 0}

    def test_the_bound_counts_only_elements_that_are_neither_str_nor_bytes(self) -> None:
        serializer = AutoSerializer(enable_integrity_checking=False)
        strings = [*map(str, range(2000)), *(b"%d" % i for i in range(2000))]
        assert serializer.deserialize(_set_doc([*strings, *range(1024)], False)) == {*strings, *range(1024)}
        with pytest.raises(SerializationError, match="more than 1024 elements"):
            serializer.deserialize(_set_doc([*strings, *range(1025)], False))

    def test_a_set_at_the_bound_hashes_each_element_twice(self, counts: dict[str, int]) -> None:
        value = frozenset(UUID(int=i) for i in range(_MAX_NON_STRING_SET_ELEMENTS))
        serializer = AutoSerializer()
        data, metadata = serializer.serialize(value)
        counts.update(hash=0, eq=0)
        result = serializer.deserialize(data, metadata)
        # Once by the sort, once by frozenset(); no element shares a hash, so nothing is compared.
        assert counts == {"hash": 2 * len(value), "eq": 0}
        assert result == value

    @pytest.mark.parametrize(
        "value",
        [
            set(range(_MAX_NON_STRING_SET_ELEMENTS)),
            frozenset(range(-(2**63), -(2**63) + _MAX_NON_STRING_SET_ELEMENTS)),
            {i / 3 for i in range(_MAX_NON_STRING_SET_ELEMENTS)},
            {UUID(int=i * 2**64) for i in range(_MAX_NON_STRING_SET_ELEMENTS)},
            {(i, str(i)) for i in range(_MAX_NON_STRING_SET_ELEMENTS)},
            {frozenset({i, -i - 10}) for i in range(_MAX_NON_STRING_SET_ELEMENTS)},
            {datetime(2026, 1, 1) + timedelta(seconds=i) for i in range(_MAX_NON_STRING_SET_ELEMENTS)},
            {*range(_MAX_NON_STRING_SET_ELEMENTS - 3), True, None, 1.5},
            {*range(_MAX_NON_STRING_SET_ELEMENTS), *map(str, range(5000)), *(b"%d" % i for i in range(5000))},
            {*range(_MAX_NON_STRING_SET_ELEMENTS), *(_Str(i) for i in range(5000))},
        ],
        ids=["ints", "int64 floor", "floats", "uuids", "tuples", "frozensets", "datetimes", "mixed", "strs", "str subclass"],
    )
    @pytest.mark.parametrize("kind", [set, frozenset])
    def test_every_set_shape_at_the_bound_round_trips(self, value: set, kind: type) -> None:
        serializer = AutoSerializer()
        result = serializer.deserialize(*serializer.serialize({"v": kind(value)}))["v"]
        assert type(result) is kind
        assert result == value

    @pytest.mark.parametrize("kind", [set, frozenset])
    def test_the_writer_refuses_a_set_its_reader_would_refuse(self, kind: type) -> None:
        with pytest.raises(TypeError, match=rf"at most 1024 .* in one {kind.__name__}; this one holds 1025\."):
            AutoSerializer().serialize({"v": [kind(range(_MAX_NON_STRING_SET_ELEMENTS + 1))]})


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
        # Truncated msgpack can misparse into the numpy-envelope branch, whose
        # error classification differs without the [data] extra installed
        # (RuntimeError from the numpy guard instead of SerializationError).
        pytest.importorskip("numpy")

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

    @slow_first_draw
    @given(st.uuids())
    def test_uuid_roundtrip_property(self, test_uuid: UUID):
        """Property: UUID roundtrips through serialization unchanged."""
        serializer = AutoSerializer()
        data = {"id": test_uuid}

        serialized, metadata = serializer.serialize(data)
        deserialized = serializer.deserialize(serialized, metadata)

        assert deserialized["id"] == test_uuid
        assert isinstance(deserialized["id"], UUID)

    @slow_first_draw
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

    @slow_first_draw
    @given(st.lists(st.integers(min_value=-(2**63), max_value=2**63 - 1), min_size=0, max_size=100).map(frozenset))
    def test_frozenset_roundtrip_property(self, test_frozenset: frozenset):
        """Property: Frozensets roundtrip through serialization unchanged."""
        serializer = AutoSerializer()
        data = {"items": test_frozenset}

        serialized, metadata = serializer.serialize(data)
        deserialized = serializer.deserialize(serialized, metadata)

        assert deserialized["items"] == test_frozenset
        assert isinstance(deserialized["items"], frozenset)

    @slow_first_draw
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

    @slow_first_draw
    @given(st.lists(st.uuids(), min_size=1, max_size=100))
    def test_uuid_list_roundtrip_property(self, uuid_list: list[UUID]):
        """Property: Lists of UUIDs roundtrip correctly."""
        serializer = AutoSerializer()

        serialized, metadata = serializer.serialize(uuid_list)
        deserialized = serializer.deserialize(serialized, metadata)

        assert deserialized == uuid_list
        assert all(isinstance(u, UUID) for u in deserialized)

    @slow_first_draw
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

    @slow_first_draw
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

    @slow_first_draw
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

    @slow_first_draw
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

    @slow_first_draw
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

    @slow_first_draw
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


class TestAutoSerializerTuple:
    """Test tuple preservation through AutoSerializer roundtrip."""

    def test_simple_tuple_roundtrip(self):
        serializer = AutoSerializer()
        data = (1, 2, 3)
        serialized, metadata = serializer.serialize(data)
        result = serializer.deserialize(serialized, metadata)
        assert isinstance(result, tuple)
        assert result == (1, 2, 3)

    def test_nested_tuple_roundtrip(self):
        serializer = AutoSerializer()
        data = (1, (2, 3), (4, (5, 6)))
        serialized, metadata = serializer.serialize(data)
        result = serializer.deserialize(serialized, metadata)
        assert isinstance(result, tuple)
        assert isinstance(result[1], tuple)
        assert isinstance(result[2][1], tuple)
        assert result == (1, (2, 3), (4, (5, 6)))

    def test_empty_tuple_roundtrip(self):
        serializer = AutoSerializer()
        data = ()
        serialized, metadata = serializer.serialize(data)
        result = serializer.deserialize(serialized, metadata)
        assert isinstance(result, tuple)
        assert result == ()

    def test_tuple_in_dict(self):
        serializer = AutoSerializer()
        data = {"key": (1, 2), "other": "value"}
        serialized, metadata = serializer.serialize(data)
        result = serializer.deserialize(serialized, metadata)
        assert isinstance(result["key"], tuple)
        assert result["key"] == (1, 2)

    def test_tuple_in_list(self):
        serializer = AutoSerializer()
        data = [(1, 2), (3, 4)]
        serialized, metadata = serializer.serialize(data)
        result = serializer.deserialize(serialized, metadata)
        assert isinstance(result[0], tuple)
        assert isinstance(result[1], tuple)

    def test_tuple_with_set_and_datetime(self):
        """Tuple containing other special types that use _auto_default."""
        from datetime import datetime

        serializer = AutoSerializer()
        data = (1, {2, 3}, datetime(2025, 1, 1))
        serialized, metadata = serializer.serialize(data)
        result = serializer.deserialize(serialized, metadata)
        assert isinstance(result, tuple)
        assert isinstance(result[1], set)
        assert isinstance(result[2], datetime)

    def test_list_preserved_as_list_not_tuple(self):
        """Lists must stay as lists — only tuples get markers."""
        serializer = AutoSerializer()
        data = [1, 2, 3]
        serialized, metadata = serializer.serialize(data)
        result = serializer.deserialize(serialized, metadata)
        assert isinstance(result, list)

    def test_single_element_tuple_roundtrip(self):
        serializer = AutoSerializer()
        data = (1,)
        serialized, metadata = serializer.serialize(data)
        result = serializer.deserialize(serialized, metadata)
        assert isinstance(result, tuple)
        assert len(result) == 1
        assert result == (1,)

    def test_malformed_tuple_marker_missing_value(self):
        """Malformed __tuple__ marker raises SerializationError."""
        import msgpack

        serializer = AutoSerializer()
        bad_data = msgpack.packb({"__tuple__": True})
        with pytest.raises(SerializationError, match="missing 'value' field"):
            serializer.deserialize(bad_data)

    def test_malformed_tuple_marker_wrong_value_type(self):
        """__tuple__ marker with non-list value raises SerializationError."""
        import msgpack

        serializer = AutoSerializer()
        bad_data = msgpack.packb({"__tuple__": True, "value": "not a list"})
        with pytest.raises(SerializationError, match="expected list"):
            serializer.deserialize(bad_data)


class TestPythonicSerializerAlias:
    """Test 'pythonic' alias for AutoSerializer."""

    def test_pythonic_returns_auto_serializer(self):
        from cachekit.serializers import get_serializer

        s = get_serializer("pythonic")
        assert isinstance(s, AutoSerializer)


class TestColumnarFallbackExtensionDtypes:
    """The no-pyarrow columnar DataFrame fallback (_serialize_dataframe) must handle
    pandas nullable/extension dtypes without crashing (#160). Round-trip is lossy on
    this fallback (NA -> null), so we assert no-crash + value/null preservation, not
    exact dtype fidelity (that is the Arrow path's job)."""

    def test_nullable_dtypes_and_objects_roundtrip(self):
        pd = pytest.importorskip("pandas")
        ser = AutoSerializer(enable_integrity_checking=False)
        ser._arrow_serializer = None  # force the msgpack-columnar DataFrame fallback path
        df = pd.DataFrame(
            {
                "ints": pd.array([1, 2, None, 4], dtype="Int64"),  # capital -> missed numeric branch before
                "floats": pd.array([1.5, 2.5, None, 4.5], dtype="Float64"),
                "objs": ["x", None, "z", "w"],
                "plain": [10, 20, 30, 40],  # plain numpy int64 -> fast numeric path
            }
        )

        data, metadata = ser.serialize(df)  # previously raised: msgpack can't pack pd.NA
        out = ser.deserialize(data, metadata)

        assert list(out.columns) == ["ints", "floats", "objs", "plain"]
        assert out.shape == (4, 4)
        # Non-NA values preserved; NA positions are null after the lossy fallback.
        assert out["ints"].iloc[0] == 1 and out["ints"].iloc[3] == 4
        assert pd.isna(out["ints"].iloc[2])
        assert out["objs"].iloc[0] == "x" and out["objs"].iloc[2] == "z"
        assert pd.isna(out["objs"].iloc[1])
        assert out["plain"].tolist() == [10, 20, 30, 40]

    def test_pyarrow_backed_dtype_does_not_crash(self):
        pd = pytest.importorskip("pandas")
        pytest.importorskip("pyarrow")
        ser = AutoSerializer(enable_integrity_checking=False)
        ser._arrow_serializer = None  # force the msgpack-columnar DataFrame fallback path
        # "int64[pyarrow]".startswith("int") was True -> hit the numeric branch ->
        # .values.tobytes() AttributeError on the Arrow extension array, before the fix.
        df = pd.DataFrame({"x": pd.array([1, 2, 3], dtype="int64[pyarrow]")})

        data, metadata = ser.serialize(df)
        out = ser.deserialize(data, metadata)

        assert out["x"].tolist() == [1, 2, 3]

    def test_nullable_series_roundtrip(self):
        pd = pytest.importorskip("pandas")
        ser = AutoSerializer(enable_integrity_checking=False)
        s = pd.Series(pd.array([1, 2, None, 4], dtype="Int64"), name="n")

        data, metadata = ser.serialize(s)  # previously raised on the pd.NA sentinel
        out = ser.deserialize(data, metadata)

        assert out.name == "n"
        assert out.iloc[0] == 1 and out.iloc[3] == 4
        assert pd.isna(out.iloc[2])

    def test_pyarrow_backed_series_does_not_crash(self):
        pd = pytest.importorskip("pandas")
        pytest.importorskip("pyarrow")
        ser = AutoSerializer(enable_integrity_checking=False)
        s = pd.Series(pd.array([1, 2, 3], dtype="int64[pyarrow]"), name="x")

        data, metadata = ser.serialize(s)
        out = ser.deserialize(data, metadata)

        assert out.tolist() == [1, 2, 3]
