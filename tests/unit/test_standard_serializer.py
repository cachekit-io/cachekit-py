"""Unit tests for StandardSerializer.

Comprehensive test coverage for language-agnostic MessagePack serialization.
Tests verify protocol compliance, roundtrip serialization, error handling,
and multi-language compatibility.
"""

from __future__ import annotations

import math
import random
import re
from datetime import date, datetime, time

import msgpack
import pytest

from cachekit.serializers import standard_serializer
from cachekit.serializers.base import SerializationError, SerializationFormat, SerializationMetadata, SerializerProtocol
from cachekit.serializers.standard_serializer import (
    StandardSerializer,
)

CROSS_CONFIG_MESSAGE = "Cache entry was written with integrity checking on but this reader has integrity checking disabled"

PROBE_BUDGET = 256 * 1024  # largest declared payload an integrity-off reader verifies (see deserialize Raises:)
LZ4_BOUND_FOR_BUDGET = 20 + PROBE_BUDGET * 110 // 100  # lz4_flex get_maximum_output_size(PROBE_BUDGET)


class _RetrieveSpy:
    """Counts retrieve() calls on the wrapped ByteStorage."""

    def __init__(self, inner) -> None:
        self.inner, self.calls = inner, 0

    def retrieve(self, data):
        self.calls += 1
        return self.inner.retrieve(data)


# Integrity-off values and the bytes main wrote for them. The first three are shaped like a ByteStorage
# envelope ``[bytes, [8 ints], int, format]`` and pass the two-byte pre-filter (``\x94\xc4``), so
# retrieve() is what clears them; the first two score a perfect 4/4 on
# AutoSerializer._looks_like_envelope, so their round-trip proves shape decides nothing here.
INTEGRITY_OFF_VALUES = [
    pytest.param(
        [b"x", [1] * 8, 0, "msgpack"],
        b"\x94\xc4\x01x\x98\x01\x01\x01\x01\x01\x01\x01\x01\x00\xa7msgpack",
        id="4of4-minimal",
    ),
    pytest.param(
        [b"\x89PNG", [255, 0, 0, 255, 0, 255, 0, 255], 4096, "series"],
        b"\x94\xc4\x04\x89PNG\x98\xcc\xff\x00\x00\xcc\xff\x00\xcc\xff\x00\xcc\xff\xcd\x10\x00\xa6series",
        id="4of4-png",
    ),
    pytest.param(
        [b"blob", [1, 2, 3, 4, 5, 6, 7, 8], 42, "label"],
        b"\x94\xc4\x04blob\x98\x01\x02\x03\x04\x05\x06\x07\x08*\xa5label",
        id="3of4",
    ),
    pytest.param([1, 2, 3, "msgpack"], b"\x94\x01\x02\x03\xa7msgpack", id="plain-4-list"),
    pytest.param({"admin": False, "user": "alice"}, b"\x82\xa5admin\xc2\xa4user\xa5alice", id="dict"),
]


def _envelope(payload_len: int, *, legacy: bool = False) -> bytes:
    """A healthy integrity-on envelope around ``payload_len`` random bytes, optionally re-encoded in
    the legacy layout older core versions wrote (payload as an int array instead of ``bin``)."""
    data, _ = StandardSerializer().serialize(random.Random(payload_len).randbytes(payload_len))
    if not legacy:
        return data
    compressed, checksum, original_size, fmt = msgpack.unpackb(data)
    return msgpack.packb([list(compressed), checksum, original_size, fmt], use_bin_type=True)


@pytest.mark.unit
class TestStandardSerializerProtocolCompliance:
    """Test StandardSerializer protocol implementation."""

    def test_implements_protocol(self) -> None:
        """Test that StandardSerializer implements SerializerProtocol.

        Validates structural subtyping compliance (PEP 544).
        """
        serializer = StandardSerializer()
        assert isinstance(serializer, SerializerProtocol)

    def test_serialize_returns_tuple(self) -> None:
        """Test that serialize() returns (bytes, SerializationMetadata) tuple."""
        serializer = StandardSerializer()
        result = serializer.serialize({"test": 123})

        assert isinstance(result, tuple)
        assert len(result) == 2
        assert isinstance(result[0], bytes)
        assert result[1] is not None

    def test_deserialize_accepts_bytes(self) -> None:
        """Test that deserialize() accepts bytes and returns object."""
        serializer = StandardSerializer()
        data, _ = serializer.serialize({"test": 123})

        result = serializer.deserialize(data)
        assert isinstance(result, dict)
        assert result == {"test": 123}

    def test_deserialize_optional_metadata_parameter(self) -> None:
        """Test that deserialize() works with and without metadata parameter."""
        serializer = StandardSerializer()
        data, metadata = serializer.serialize({"test": 123})

        # With metadata
        result_with = serializer.deserialize(data, metadata=metadata)
        assert result_with == {"test": 123}

        # Without metadata (metadata is optional)
        result_without = serializer.deserialize(data)
        assert result_without == {"test": 123}


@pytest.mark.unit
class TestStandardSerializerPrimitiveTypes:
    """Test StandardSerializer with primitive types."""

    def test_serialize_primitives(self) -> None:
        """Test roundtrip serialization of all primitive types.

        Validates support for None, bool, int, float, str, bytes.
        """
        serializer = StandardSerializer()
        primitives = {
            "none": None,
            "true": True,
            "false": False,
            "int": 42,
            "negative_int": -100,
            "zero": 0,
            "float": 3.14159,
            "string": "Hello, World!",
            "empty_string": "",
            "bytes": b"binary data",
            "empty_bytes": b"",
        }

        serialized, metadata = serializer.serialize(primitives)
        assert isinstance(serialized, bytes)
        assert metadata.format == SerializationFormat.MSGPACK

        deserialized = serializer.deserialize(serialized)
        assert deserialized == primitives

    def test_serialize_none(self) -> None:
        """Test serialization of None value."""
        serializer = StandardSerializer()
        serialized, _ = serializer.serialize(None)
        deserialized = serializer.deserialize(serialized)
        assert deserialized is None

    def test_serialize_bool_true(self) -> None:
        """Test serialization of True boolean."""
        serializer = StandardSerializer()
        serialized, _ = serializer.serialize(True)
        deserialized = serializer.deserialize(serialized)
        assert deserialized is True

    def test_serialize_bool_false(self) -> None:
        """Test serialization of False boolean."""
        serializer = StandardSerializer()
        serialized, _ = serializer.serialize(False)
        deserialized = serializer.deserialize(serialized)
        assert deserialized is False

    def test_serialize_int(self) -> None:
        """Test serialization of integers (positive, negative, zero)."""
        serializer = StandardSerializer()
        test_cases = [0, 1, -1, 42, -100, 2**31 - 1, -(2**31), 2**63 - 1]

        for value in test_cases:
            serialized, _ = serializer.serialize(value)
            deserialized = serializer.deserialize(serialized)
            assert deserialized == value

    def test_serialize_float(self) -> None:
        """Test serialization of floating point numbers."""
        serializer = StandardSerializer()
        test_cases = [0.0, 1.5, -3.14159, 1e10, 1e-10, 3.141592653589793]

        for value in test_cases:
            serialized, _ = serializer.serialize(value)
            deserialized = serializer.deserialize(serialized)
            assert deserialized == value

    def test_serialize_string(self) -> None:
        """Test serialization of strings."""
        serializer = StandardSerializer()
        test_cases = ["", "a", "hello", "Hello, World!", "line1\nline2"]

        for value in test_cases:
            serialized, _ = serializer.serialize(value)
            deserialized = serializer.deserialize(serialized)
            assert deserialized == value

    def test_serialize_bytes(self) -> None:
        """Test serialization of bytes."""
        serializer = StandardSerializer()
        test_cases = [b"", b"a", b"hello", b"\x00\x01\x02", b"\xff\xfe"]

        for value in test_cases:
            serialized, _ = serializer.serialize(value)
            deserialized = serializer.deserialize(serialized)
            assert deserialized == value


@pytest.mark.unit
class TestStandardSerializerCollectionTypes:
    """Test StandardSerializer with collection types."""

    def test_serialize_collections(self) -> None:
        """Test roundtrip serialization of collections (list, tuple, dict).

        Validates support for nested structures.
        """
        serializer = StandardSerializer()
        collections = {
            "list": [1, 2, 3],
            "tuple": (4, 5, 6),  # Tuples become lists in MessagePack
            "dict": {"nested": "value"},
            "list_of_dicts": [{"a": 1}, {"b": 2}],
            "dict_of_lists": {"items": [1, 2, 3]},
        }

        serialized, metadata = serializer.serialize(collections)
        assert isinstance(serialized, bytes)
        assert metadata.format == SerializationFormat.MSGPACK

        deserialized = serializer.deserialize(serialized)
        # Note: tuples become lists in MessagePack
        assert deserialized["list"] == [1, 2, 3]
        assert deserialized["tuple"] == [4, 5, 6]
        assert deserialized["dict"] == {"nested": "value"}
        assert deserialized["list_of_dicts"] == [{"a": 1}, {"b": 2}]
        assert deserialized["dict_of_lists"] == {"items": [1, 2, 3]}

    def test_serialize_empty_list(self) -> None:
        """Test serialization of empty list."""
        serializer = StandardSerializer()
        serialized, _ = serializer.serialize([])
        deserialized = serializer.deserialize(serialized)
        assert deserialized == []

    def test_serialize_empty_dict(self) -> None:
        """Test serialization of empty dict."""
        serializer = StandardSerializer()
        serialized, _ = serializer.serialize({})
        deserialized = serializer.deserialize(serialized)
        assert deserialized == {}

    def test_serialize_empty_tuple(self) -> None:
        """Test serialization of empty tuple (becomes empty list)."""
        serializer = StandardSerializer()
        serialized, _ = serializer.serialize(())
        deserialized = serializer.deserialize(serialized)
        assert deserialized == []

    def test_serialize_empty_collections(self) -> None:
        """Test serialization of all empty collection types."""
        serializer = StandardSerializer()
        empty_collections = {"list": [], "dict": {}, "tuple": (), "nested": {"list": [], "dict": {}}}

        serialized, _ = serializer.serialize(empty_collections)
        deserialized = serializer.deserialize(serialized)

        assert deserialized["list"] == []
        assert deserialized["dict"] == {}
        assert deserialized["tuple"] == []
        assert deserialized["nested"] == {"list": [], "dict": {}}

    def test_serialize_nested_structures(self) -> None:
        """Test serialization of deeply nested structures.

        Validates handling of dict/list nesting beyond typical usage.
        """
        serializer = StandardSerializer()
        deep_structure = {
            "level1": {
                "level2": {
                    "level3": {
                        "level4": {
                            "level5": {
                                "items": [1, 2, {"nested": "value"}],
                                "count": 42,
                            }
                        }
                    }
                }
            }
        }

        serialized, _ = serializer.serialize(deep_structure)
        deserialized = serializer.deserialize(serialized)
        assert deserialized == deep_structure

    def test_serialize_mixed_collections(self) -> None:
        """Test serialization of complex mixed structures."""
        serializer = StandardSerializer()
        mixed = {
            "strings": ["a", "b", "c"],
            "numbers": [1, 2.5, -3],
            "booleans": [True, False, True],
            "mixed_list": [1, "two", 3.0, True, None, b"bytes"],
            "nested_mixed": [
                {"key": "value", "number": 42},
                [1, 2, [3, 4, {"deep": True}]],
                "string",
            ],
        }

        serialized, _ = serializer.serialize(mixed)
        deserialized = serializer.deserialize(serialized)
        assert deserialized == mixed


@pytest.mark.unit
class TestStandardSerializerDatetimeTypes:
    """Test StandardSerializer with datetime types."""

    def test_serialize_datetime(self) -> None:
        """Test roundtrip serialization of datetime objects.

        Validates ISO-8601 encoding via MessagePack extension 0xC0.
        """
        serializer = StandardSerializer()
        dt = datetime(2024, 1, 15, 12, 30, 45, 123456)

        serialized, _ = serializer.serialize(dt)
        deserialized = serializer.deserialize(serialized)

        # datetime should be preserved exactly
        assert deserialized == dt
        assert isinstance(deserialized, datetime)

    def test_serialize_datetime_with_microseconds(self) -> None:
        """Test datetime preservation with microsecond precision."""
        serializer = StandardSerializer()
        dt = datetime(2024, 12, 31, 23, 59, 59, 999999)

        serialized, _ = serializer.serialize(dt)
        deserialized = serializer.deserialize(serialized)

        assert deserialized == dt

    def test_serialize_date(self) -> None:
        """Test roundtrip serialization of date objects."""
        serializer = StandardSerializer()
        d = date(2024, 1, 15)

        serialized, _ = serializer.serialize(d)
        deserialized = serializer.deserialize(serialized)

        assert deserialized == d
        assert isinstance(deserialized, date)

    def test_serialize_time(self) -> None:
        """Test roundtrip serialization of time objects."""
        serializer = StandardSerializer()
        t = time(12, 30, 45, 123456)

        serialized, _ = serializer.serialize(t)
        deserialized = serializer.deserialize(serialized)

        assert deserialized == t
        assert isinstance(deserialized, time)

    def test_serialize_datetime_edge_cases(self) -> None:
        """Test datetime serialization with edge case values."""
        serializer = StandardSerializer()
        test_cases = [
            datetime(1900, 1, 1, 0, 0, 0),  # Old date
            datetime(2099, 12, 31, 23, 59, 59),  # Far future
            datetime(2024, 2, 29, 12, 0, 0),  # Leap year
        ]

        for dt in test_cases:
            serialized, _ = serializer.serialize(dt)
            deserialized = serializer.deserialize(serialized)
            assert deserialized == dt

    def test_serialize_datetime_in_dict(self) -> None:
        """Test datetime serialization within dict structure."""
        serializer = StandardSerializer()
        data = {
            "created": datetime(2024, 1, 15, 12, 30, 0),
            "name": "test",
            "count": 42,
        }

        serialized, _ = serializer.serialize(data)
        deserialized = serializer.deserialize(serialized)

        assert deserialized == data
        assert isinstance(deserialized["created"], datetime)

    def test_serialize_datetime_in_list(self) -> None:
        """Test datetime serialization within list structure."""
        serializer = StandardSerializer()
        data = [
            "item1",
            datetime(2024, 1, 15, 12, 30, 0),
            42,
            date(2024, 1, 15),
            time(12, 30, 0),
        ]

        serialized, _ = serializer.serialize(data)
        deserialized = serializer.deserialize(serialized)

        assert deserialized == data
        assert isinstance(deserialized[1], datetime)
        assert isinstance(deserialized[3], date)
        assert isinstance(deserialized[4], time)


@pytest.mark.unit
class TestStandardSerializerSpecialFloats:
    """Test StandardSerializer with special float values."""

    def test_serialize_special_floats(self) -> None:
        """Test serialization of inf, -inf, and nan.

        MessagePack supports these special IEEE 754 values.
        """
        serializer = StandardSerializer()
        special_floats = {
            "positive_inf": float("inf"),
            "negative_inf": float("-inf"),
            "nan": float("nan"),
        }

        serialized, _ = serializer.serialize(special_floats)
        deserialized = serializer.deserialize(serialized)

        assert math.isinf(deserialized["positive_inf"]) and deserialized["positive_inf"] > 0
        assert math.isinf(deserialized["negative_inf"]) and deserialized["negative_inf"] < 0
        assert math.isnan(deserialized["nan"])

    def test_serialize_positive_infinity(self) -> None:
        """Test serialization of positive infinity."""
        serializer = StandardSerializer()
        value = float("inf")

        serialized, _ = serializer.serialize(value)
        deserialized = serializer.deserialize(serialized)

        assert math.isinf(deserialized) and deserialized > 0

    def test_serialize_negative_infinity(self) -> None:
        """Test serialization of negative infinity."""
        serializer = StandardSerializer()
        value = float("-inf")

        serialized, _ = serializer.serialize(value)
        deserialized = serializer.deserialize(serialized)

        assert math.isinf(deserialized) and deserialized < 0

    def test_serialize_nan(self) -> None:
        """Test serialization of NaN (not a number)."""
        serializer = StandardSerializer()
        value = float("nan")

        serialized, _ = serializer.serialize(value)
        deserialized = serializer.deserialize(serialized)

        assert math.isnan(deserialized)


@pytest.mark.unit
class TestStandardSerializerIntegrityChecking:
    """Test StandardSerializer integrity checking with ByteStorage."""

    def test_integrity_checking_enabled(self) -> None:
        """Test that ByteStorage is used when integrity checking is enabled.

        Verifies enable_integrity_checking=True wraps with ByteStorage (LZ4 + xxHash3-64).
        """
        serializer = StandardSerializer(enable_integrity_checking=True)
        data = {"test": "data" * 100}  # Larger data to see compression

        serialized, metadata = serializer.serialize(data)

        # Metadata should show compressed=True and format=MSGPACK
        assert metadata.format == SerializationFormat.MSGPACK
        assert metadata.compressed is True

        # Should deserialize correctly through ByteStorage
        deserialized = serializer.deserialize(serialized)
        assert deserialized == data

    def test_integrity_checking_disabled(self) -> None:
        """Test that plain MessagePack is used when integrity checking is disabled.

        Verifies enable_integrity_checking=False uses raw MessagePack without ByteStorage.
        """
        serializer = StandardSerializer(enable_integrity_checking=False)
        data = {"test": "data"}

        serialized, metadata = serializer.serialize(data)

        # Metadata should show compressed=False
        assert metadata.format == SerializationFormat.MSGPACK
        assert metadata.compressed is False

        # Should deserialize as plain MessagePack
        deserialized = serializer.deserialize(serialized)
        assert deserialized == data

    @pytest.mark.parametrize(
        "header",
        [
            pytest.param(lambda meta: meta, id="compressed-true"),
            pytest.param(
                lambda meta: SerializationMetadata.from_dict({k: v for k, v in meta.to_dict().items() if k != "compressed"}),
                id="compressed-absent",
            ),
            pytest.param(lambda meta: None, id="no-metadata"),
        ],
    )
    @pytest.mark.parametrize(
        "wrap",
        [
            pytest.param(bytes, id="bytes"),
            pytest.param(memoryview, id="memoryview"),
            # indexes to signed ints (0x94 reads as -108), so the layout test must see a normalised view
            pytest.param(lambda b: memoryview(b).cast("b"), id="signed-memoryview"),
            pytest.param(bytearray, id="bytearray"),
        ],
    )
    def test_enveloped_entry_read_with_integrity_off_fails_closed(self, header, wrap) -> None:
        """Writer on, reader off: the envelope is refused whatever the header says, rather than
        returning its positional fields ``[payload, checksum, size, format]`` as the value."""
        data, meta = StandardSerializer(enable_integrity_checking=True).serialize({"admin": False, "user": "alice"})
        assert meta.compressed is True

        off = StandardSerializer(enable_integrity_checking=False)
        with pytest.raises(SerializationError, match=f"^{re.escape(CROSS_CONFIG_MESSAGE)}$") as excinfo:
            off.deserialize(wrap(data), header(meta))
        assert type(excinfo.value) is SerializationError  # no subclass: shape decides nothing here

    @pytest.mark.parametrize(
        ("payload_len", "legacy", "markers"),
        [
            pytest.param(8, False, (0xC4,), id="bin8"),
            pytest.param(300, False, (0xC5,), id="bin16"),
            pytest.param(70_000, False, (0xC6,), id="bin32"),
            pytest.param(8, True, tuple(range(0x90, 0xA0)), id="legacy-fixarray"),
            pytest.param(300, True, (0xDC,), id="legacy-array16"),
            pytest.param(70_000, True, (0xDD,), id="legacy-array32"),
        ],
    )
    def test_every_envelope_layout_bytestorage_writes_is_refused_with_integrity_off(self, payload_len, legacy, markers) -> None:
        """The pre-filter admits every payload-slot marker ByteStorage has written, current (bin) and
        legacy (int array), at each msgpack length boundary."""
        data = _envelope(payload_len, legacy=legacy)
        assert data[0] == 0x94
        assert data[1] in markers

        with pytest.raises(SerializationError, match=f"^{re.escape(CROSS_CONFIG_MESSAGE)}$"):
            StandardSerializer(enable_integrity_checking=False).deserialize(data)

    @pytest.mark.parametrize(
        "payload",
        [
            pytest.param(b"\x00" * (PROBE_BUDGET - 5), id="compressible"),  # bin32 header is 5 bytes
            # incompressible: the longest payload slot a real envelope within the budget carries
            pytest.param(random.Random(0).randbytes(PROBE_BUDGET - 5), id="incompressible"),
        ],
    )
    def test_envelope_at_the_probe_budget_is_refused_without_metadata(self, payload) -> None:
        data, _ = StandardSerializer().serialize(payload)
        compressed, _, declared, _ = msgpack.unpackb(data)
        assert declared == PROBE_BUDGET
        assert len(compressed) <= LZ4_BOUND_FOR_BUDGET

        with pytest.raises(SerializationError, match=f"^{re.escape(CROSS_CONFIG_MESSAGE)}$"):
            StandardSerializer(enable_integrity_checking=False).deserialize(data)

    def test_envelope_over_the_probe_budget_is_verified_only_through_its_header(self) -> None:
        """retrieve() decompresses the whole declared payload before it checks the checksum, so a
        value declaring more than the budget is never probed. That keeps a caller-shaped value from
        costing a large decompress per read; the price is the documented residual: a healthy
        envelope over the budget read with no metadata comes back as its fields. Its own
        ``compressed=True`` header still refuses it at any size."""
        data, meta = StandardSerializer().serialize(b"\x00" * (PROBE_BUDGET - 4))
        fields = msgpack.unpackb(data)
        assert fields[2] == PROBE_BUDGET + 1

        off = StandardSerializer(enable_integrity_checking=False)
        assert off.deserialize(data) == fields
        with pytest.raises(SerializationError, match=f"^{re.escape(CROSS_CONFIG_MESSAGE)}$"):
            off.deserialize(data, meta)

    @pytest.mark.parametrize(("value", "golden"), INTEGRITY_OFF_VALUES)
    @pytest.mark.parametrize("with_metadata", [True, False])
    def test_envelope_shaped_value_round_trips_with_integrity_off(self, value, golden, with_metadata) -> None:
        """Only an envelope that VERIFIES is refused, so a legitimate value shaped exactly like
        one still round-trips — with its own metadata and without."""
        off = StandardSerializer(enable_integrity_checking=False)
        data, meta = off.serialize(value)
        assert off.deserialize(data, meta if with_metadata else None) == value

    @pytest.mark.parametrize(
        ("slot", "rot"),
        [
            pytest.param(1, lambda checksum: [checksum[0] ^ 1, *checksum[1:]], id="checksum"),
            pytest.param(2, lambda original_size: original_size + 1, id="original_size"),
        ],
    )
    def test_rotted_envelope_is_refused_only_through_its_header_with_integrity_off(self, slot, rot) -> None:
        """A retrieve() failure of any kind means "not verified", so without metadata the plain decode
        stands: the documented residual, as rot in the reader's own entries goes undetected. The
        writer's ``compressed=True`` header, which ``@cache`` always passes, still refuses it."""
        data, meta = StandardSerializer(enable_integrity_checking=True).serialize({"a": 1})
        fields = msgpack.unpackb(data)
        fields[slot] = rot(fields[slot])
        rotted = msgpack.packb(fields, use_bin_type=True)

        off = StandardSerializer(enable_integrity_checking=False)
        assert off.deserialize(rotted) == fields
        with pytest.raises(SerializationError, match=f"^{re.escape(CROSS_CONFIG_MESSAGE)}$"):
            off.deserialize(rotted, meta)

    def test_value_equal_to_a_valid_envelope_is_refused_with_integrity_off(self) -> None:
        """The documented residual on the other side: a value that IS a valid envelope's fields
        verifies, so it is refused on every plaintext read, with its own metadata and without."""
        envelope_fields = msgpack.unpackb(_envelope(8))
        off = StandardSerializer(enable_integrity_checking=False)
        data, meta = off.serialize(envelope_fields)

        for metadata in (meta, None):
            with pytest.raises(SerializationError, match=f"^{re.escape(CROSS_CONFIG_MESSAGE)}$"):
                off.deserialize(data, metadata)

    @pytest.mark.parametrize(
        ("payload_len", "legacy"),
        [
            pytest.param(8, False, id="bin8"),
            pytest.param(300, False, id="bin16"),
            pytest.param(70_000, False, id="bin32"),
            pytest.param(8, True, id="legacy-fixarray"),
            pytest.param(300, True, id="legacy-array16"),
            pytest.param(70_000, True, id="legacy-array32"),
        ],
    )
    def test_decrypted_read_probes_only_the_legacy_layout(self, payload_len, legacy) -> None:
        """``encrypted`` metadata means EncryptionWrapper decrypted these bytes under an AAD that binds
        ``compressed``. The only writer that sealed an envelope under ``compressed=False`` (AutoSerializer
        before 0.12.0) predates the bin layout, so a decrypted bin-layout value is never probed and comes
        back, while a decrypted legacy-layout envelope is still verified and refused."""
        data = _envelope(payload_len, legacy=legacy)
        off = StandardSerializer(enable_integrity_checking=False)
        _, meta = off.serialize(None)
        decrypted = SerializationMetadata.from_dict({**meta.to_dict(), "encrypted": True})
        spy = _RetrieveSpy(off._byte_storage)
        off._byte_storage = spy  # type: ignore[assignment]

        if legacy:
            with pytest.raises(SerializationError, match=f"^{re.escape(CROSS_CONFIG_MESSAGE)}$"):
                off.deserialize(data, decrypted)
        else:
            assert off.deserialize(data, decrypted) == msgpack.unpackb(data)
        assert spy.calls == int(legacy)

    def test_decrypted_read_skips_the_envelope_probe(self) -> None:
        """A decrypted bin-layout value equal to a valid envelope's fields comes back unprobed, while the
        same bytes on a plaintext read are refused. A ``compressed=True`` header still refuses on either
        read."""
        envelope_fields = msgpack.unpackb(_envelope(8))
        off = StandardSerializer(enable_integrity_checking=False)
        data, meta = off.serialize(envelope_fields)
        decrypted = SerializationMetadata.from_dict({**meta.to_dict(), "encrypted": True})
        spy = _RetrieveSpy(off._byte_storage)
        off._byte_storage = spy  # type: ignore[assignment]

        assert off.deserialize(data, decrypted) == envelope_fields
        assert spy.calls == 0
        with pytest.raises(SerializationError, match=f"^{re.escape(CROSS_CONFIG_MESSAGE)}$"):
            off.deserialize(data, meta)

        enveloped = SerializationMetadata.from_dict({**decrypted.to_dict(), "compressed": True})
        with pytest.raises(SerializationError, match=f"^{re.escape(CROSS_CONFIG_MESSAGE)}$"):
            off.deserialize(_envelope(8), enveloped)

    @pytest.mark.parametrize("healthy_at_decode", [True, False], ids=["rotted-after-decode", "repaired-after-decode"])
    def test_decode_and_probe_judge_one_snapshot_of_a_mutable_buffer(self, monkeypatch, healthy_at_decode) -> None:
        """A bytearray (or a view over an mmap) can change between the plain decode and the probe.
        Both must judge the bytes that decoded: a healthy envelope is refused even if the buffer rots
        afterwards, and a rotted one is returned as the fields it decoded to even if it is repaired."""
        data, _ = StandardSerializer().serialize({"a": 1})
        pos = len(msgpack.packb(msgpack.unpackb(data)[0]))  # last byte of the LZ4 payload, always a literal
        buf = bytearray(data)
        if not healthy_at_decode:
            buf[pos] ^= 1
        decoded_fields = msgpack.unpackb(bytes(buf))
        real_unpackb_bounded = standard_serializer.unpackb_bounded

        def decode_then_flip(*args, **kwargs):
            value = real_unpackb_bounded(*args, **kwargs)
            buf[pos] ^= 1  # the caller's buffer changes after the decode has read it
            return value

        monkeypatch.setattr(standard_serializer, "unpackb_bounded", decode_then_flip)
        off = StandardSerializer(enable_integrity_checking=False)
        if healthy_at_decode:
            with pytest.raises(SerializationError, match=f"^{re.escape(CROSS_CONFIG_MESSAGE)}$"):
                off.deserialize(buf)
        else:
            assert off.deserialize(buf) == decoded_fields

    @pytest.mark.parametrize(("extra", "probed"), [(0, True), (1, False)], ids=["at-lz4-bound", "over-lz4-bound"])
    def test_probe_skips_a_payload_slot_longer_than_any_envelope_within_budget(self, extra, probed) -> None:
        """No envelope declaring at most the budget can carry a payload longer than lz4_flex's
        worst-case output for the budget, so a longer slot 0 is never handed to retrieve(), which
        would copy all of it before failing. The value round-trips either way."""
        value = [b"\x00" * (LZ4_BOUND_FOR_BUDGET + extra), [1] * 8, 1000, "x"]
        off = StandardSerializer(enable_integrity_checking=False)
        data, _ = off.serialize(value)
        spy = _RetrieveSpy(off._byte_storage)
        off._byte_storage = spy  # type: ignore[assignment]

        assert off.deserialize(data) == value
        assert spy.calls == int(probed)

    @pytest.mark.parametrize(("value", "golden"), INTEGRITY_OFF_VALUES)
    def test_integrity_off_write_bytes_are_pinned(self, value, golden) -> None:
        """Integrity-off writes are plain MessagePack; the read-side gate must not move them."""
        data, meta = StandardSerializer(enable_integrity_checking=False).serialize(value)
        assert data == golden
        assert meta.compressed is False

    def test_metadata_format_msgpack(self) -> None:
        """Test that metadata always reports SerializationFormat.MSGPACK."""
        serializer = StandardSerializer()
        data = {"test": 123}

        _, metadata = serializer.serialize(data)
        assert metadata.format == SerializationFormat.MSGPACK
        assert metadata.original_type == "msgpack"
        assert metadata.encrypted is False


@pytest.mark.unit
class TestStandardSerializerUnsupportedTypes:
    """Test StandardSerializer error handling for unsupported types."""

    def test_numpy_error(self) -> None:
        """Test that NumPy arrays raise TypeError with helpful message.

        Validates NUMPY_ERROR_MESSAGE is raised.
        """
        serializer = StandardSerializer()

        # Create a fake NumPy array class (avoid hard dependency)
        class FakeNumpyArray:
            __module__ = "numpy"
            __name__ = "ndarray"

        fake_np_array = FakeNumpyArray()

        with pytest.raises(TypeError) as exc_info:
            serializer.serialize(fake_np_array)

        error_msg = str(exc_info.value)
        assert "NumPy" in error_msg or "does not support" in error_msg

    def test_pandas_dataframe_error(self) -> None:
        """Test that pandas DataFrames raise TypeError with helpful message.

        Validates PANDAS_ERROR_MESSAGE is raised.
        """
        serializer = StandardSerializer()

        # Create a fake pandas DataFrame class
        class FakePandasDataFrame:
            __module__ = "pandas.core.frame"
            __name__ = "DataFrame"

        fake_df = FakePandasDataFrame()

        with pytest.raises(TypeError) as exc_info:
            serializer.serialize(fake_df)

        error_msg = str(exc_info.value)
        assert "pandas" in error_msg.lower() or "does not support" in error_msg

    def test_pandas_series_error(self) -> None:
        """Test that pandas Series raise TypeError with helpful message."""
        serializer = StandardSerializer()

        # Create a fake pandas Series class
        class FakePandasSeries:
            __module__ = "pandas.core.series"
            __name__ = "Series"

        fake_series = FakePandasSeries()

        with pytest.raises(TypeError) as exc_info:
            serializer.serialize(fake_series)

        error_msg = str(exc_info.value)
        assert "pandas" in error_msg.lower() or "does not support" in error_msg

    def test_pydantic_error(self) -> None:
        """Test that Pydantic models raise TypeError with helpful message.

        Validates PYDANTIC_ERROR_MESSAGE is raised.
        """
        serializer = StandardSerializer()

        # Create a fake Pydantic model by creating a class named BaseModel in hierarchy
        # The check looks for base.__name__ == "BaseModel" in type(obj).__mro__
        class BaseModel:  # This class will appear in MRO with __name__ = "BaseModel"
            pass

        class FakePydanticModel(BaseModel):
            pass

        fake_model = FakePydanticModel()

        with pytest.raises(TypeError) as exc_info:
            serializer.serialize(fake_model)

        error_msg = str(exc_info.value)
        assert "Pydantic" in error_msg or "model_dump" in error_msg

    def test_orm_error(self) -> None:
        """Test that ORM models raise TypeError with helpful message.

        Validates ORM_ERROR_MESSAGE is raised for SQLAlchemy/Django models.
        """
        serializer = StandardSerializer()

        # Create a fake ORM model by subclassing a class with ORM base in hierarchy
        class FakeOrmBase:
            __name__ = "DeclarativeBase"

        class FakeSqlAlchemyModel(FakeOrmBase):
            pass

        fake_orm = FakeSqlAlchemyModel()

        with pytest.raises(TypeError) as exc_info:
            serializer.serialize(fake_orm)

        error_msg = str(exc_info.value)
        assert "ORM" in error_msg or "does not support" in error_msg

    def test_custom_class_error(self) -> None:
        """Test that custom classes raise TypeError with helpful message.

        Validates CUSTOM_CLASS_ERROR_MESSAGE is raised.
        """
        serializer = StandardSerializer()

        class CustomClass:
            def __init__(self) -> None:
                self.field = "value"

        custom_obj = CustomClass()

        with pytest.raises(TypeError) as exc_info:
            serializer.serialize(custom_obj)

        error_msg = str(exc_info.value)
        assert "custom class" in error_msg.lower() or "does not support" in error_msg


@pytest.mark.unit
class TestStandardSerializerLargeData:
    """Test StandardSerializer with large data."""

    def test_serialize_large_data(self) -> None:
        """Test serialization of 10KB+ data.

        Validates ByteStorage compression efficiency with larger payloads.
        """
        serializer = StandardSerializer(enable_integrity_checking=True)

        # Create ~10KB data
        large_data = {"items": [{"id": i, "name": f"Item {i}", "data": "x" * 100} for i in range(50)]}

        serialized, metadata = serializer.serialize(large_data)
        assert isinstance(serialized, bytes)
        assert len(serialized) > 0

        # Verify roundtrip
        deserialized = serializer.deserialize(serialized)
        assert deserialized == large_data

        # When compression is enabled, serialized should be smaller than uncompressed
        assert metadata.compressed is True

    def test_serialize_very_large_data(self) -> None:
        """Test serialization of 100KB+ data."""
        serializer = StandardSerializer(enable_integrity_checking=True)

        # Create ~100KB data
        large_data = {"values": list(range(10000)), "name": "large", "data": "y" * 10000}

        serialized, _ = serializer.serialize(large_data)
        deserialized = serializer.deserialize(serialized)

        assert deserialized == large_data


@pytest.mark.unit
class TestStandardSerializerUnicodeHandling:
    """Test StandardSerializer with Unicode and special characters."""

    def test_serialize_unicode(self) -> None:
        """Test serialization of Unicode strings including emoji.

        Validates cross-language string compatibility.
        """
        serializer = StandardSerializer()
        unicode_data = {
            "emoji": "🎉 🚀 💡",
            "chinese": "你好世界",
            "arabic": "مرحبا بالعالم",
            "mixed": "Hello 世界 🌍",
            "special": "©®™€¥",
        }

        serialized, _ = serializer.serialize(unicode_data)
        deserialized = serializer.deserialize(serialized)

        assert deserialized == unicode_data

    def test_serialize_emoji(self) -> None:
        """Test serialization of emoji strings."""
        serializer = StandardSerializer()
        emoji_data = {
            "smileys": "😀😃😄😁",
            "hearts": "❤️🧡💛",
            "animals": "🐶🐱🐭",
        }

        serialized, _ = serializer.serialize(emoji_data)
        deserialized = serializer.deserialize(serialized)

        assert deserialized == emoji_data

    def test_serialize_special_characters(self) -> None:
        """Test serialization of special Unicode characters."""
        serializer = StandardSerializer()
        special_data = {
            "accents": "àáâãäå",
            "symbols": "©®™§¶",
            "math": "±×÷≠≈∞",
            "arrows": "←→↑↓",
        }

        serialized, _ = serializer.serialize(special_data)
        deserialized = serializer.deserialize(serialized)

        assert deserialized == special_data

    def test_serialize_mixed_unicode(self) -> None:
        """Test serialization of mixed Unicode content."""
        serializer = StandardSerializer()
        mixed_unicode = {
            "content": [
                "Hello World",
                "你好",
                "🎉",
                "café",
                "naïve",
            ],
        }

        serialized, _ = serializer.serialize(mixed_unicode)
        deserialized = serializer.deserialize(serialized)

        assert deserialized == mixed_unicode


@pytest.mark.unit
class TestStandardSerializerInitialization:
    """Test StandardSerializer initialization and configuration."""

    def test_default_initialization(self) -> None:
        """Test StandardSerializer with default parameters."""
        serializer = StandardSerializer()
        assert serializer.enable_integrity_checking is True

    def test_initialization_with_integrity_checking_disabled(self) -> None:
        """Test StandardSerializer initialization with integrity checking disabled."""
        serializer = StandardSerializer(enable_integrity_checking=False)
        assert serializer.enable_integrity_checking is False

    def test_initialization_with_integrity_checking_enabled(self) -> None:
        """Test StandardSerializer initialization with integrity checking enabled."""
        serializer = StandardSerializer(enable_integrity_checking=True)
        assert serializer.enable_integrity_checking is True


@pytest.mark.unit
class TestStandardSerializerRoundtrip:
    """Test comprehensive roundtrip scenarios."""

    def test_roundtrip_complex_structure(self) -> None:
        """Test roundtrip with complex mixed structure."""
        serializer = StandardSerializer()
        complex_data = {
            "user": {
                "id": 123,
                "name": "Alice",
                "email": "alice@example.com",
                "created": datetime(2024, 1, 15, 12, 30, 0),
            },
            "items": [
                {"id": 1, "price": 10.50, "available": True},
                {"id": 2, "price": 20.75, "available": False},
                {"id": 3, "price": 15.25, "available": True},
            ],
            "metadata": {
                "total": 3,
                "page": 1,
                "timestamp": datetime.now(),
            },
            "binary_data": b"binary content here",
        }

        serialized, metadata = serializer.serialize(complex_data)
        assert metadata.format == SerializationFormat.MSGPACK

        deserialized = serializer.deserialize(serialized)

        # Verify all fields
        assert deserialized["user"]["id"] == 123
        assert deserialized["user"]["name"] == "Alice"
        assert isinstance(deserialized["user"]["created"], datetime)
        assert len(deserialized["items"]) == 3
        assert deserialized["items"][0]["price"] == 10.50
        assert deserialized["metadata"]["total"] == 3
        assert isinstance(deserialized["metadata"]["timestamp"], datetime)
        assert deserialized["binary_data"] == b"binary content here"

    def test_multiple_roundtrips(self) -> None:
        """Test multiple serialize-deserialize cycles (verify idempotency)."""
        serializer = StandardSerializer()
        original_data = {"test": "data", "number": 42, "list": [1, 2, 3]}

        # First cycle
        serialized1, _ = serializer.serialize(original_data)
        deserialized1 = serializer.deserialize(serialized1)
        assert deserialized1 == original_data

        # Second cycle (re-serialize the deserialized data)
        serialized2, _ = serializer.serialize(deserialized1)
        deserialized2 = serializer.deserialize(serialized2)
        assert deserialized2 == original_data

        # Third cycle
        serialized3, _ = serializer.serialize(deserialized2)
        deserialized3 = serializer.deserialize(serialized3)
        assert deserialized3 == original_data

        # All serialized forms should be identical
        assert serialized1 == serialized2 == serialized3


@pytest.mark.unit
class TestStandardSerializerErrorHandling:
    """Test StandardSerializer error handling."""

    def test_deserialize_invalid_data(self) -> None:
        """Test deserialization of corrupted/invalid data."""
        serializer = StandardSerializer()

        # Invalid MessagePack data
        with pytest.raises(SerializationError):
            serializer.deserialize(b"not valid msgpack data \xff\xfe")

    def test_serialize_set_not_supported(self) -> None:
        """Test that set type raises helpful error."""
        serializer = StandardSerializer()

        with pytest.raises(TypeError):
            serializer.serialize({1, 2, 3})

    def test_serialize_frozenset_not_supported(self) -> None:
        """Test that frozenset type raises helpful error."""
        serializer = StandardSerializer()

        with pytest.raises(TypeError):
            serializer.serialize(frozenset([1, 2, 3]))


@pytest.mark.unit
class TestStandardSerializerEdgeCases:
    """Test StandardSerializer edge cases."""

    def test_serialize_empty_bytes_in_dict(self) -> None:
        """Test empty bytes within dict."""
        serializer = StandardSerializer()
        data = {"content": b"", "name": "test"}

        serialized, _ = serializer.serialize(data)
        deserialized = serializer.deserialize(serialized)

        assert deserialized == data
        assert deserialized["content"] == b""

    def test_serialize_deep_nesting_limits(self) -> None:
        """Test serialization with extreme nesting depth."""
        serializer = StandardSerializer()

        # Create very deep structure
        deep = "value"
        for _ in range(50):
            deep = {"nested": deep}

        serialized, _ = serializer.serialize(deep)
        deserialized = serializer.deserialize(serialized)

        assert deserialized == deep

    def test_serialize_mixed_types_in_list(self) -> None:
        """Test lists containing all supported types."""
        serializer = StandardSerializer()
        mixed_list = [
            None,
            True,
            False,
            42,
            -100,
            3.14,
            "string",
            b"bytes",
            [1, 2, 3],
            {"key": "value"},
            datetime(2024, 1, 15, 12, 30, 0),
            date(2024, 1, 15),
            time(12, 30, 0),
        ]

        serialized, _ = serializer.serialize(mixed_list)
        deserialized = serializer.deserialize(serialized)

        assert len(deserialized) == len(mixed_list)
        assert deserialized[0] is None
        assert deserialized[1] is True
        assert deserialized[2] is False
        assert deserialized[10] == datetime(2024, 1, 15, 12, 30, 0)
        assert isinstance(deserialized[10], datetime)


@pytest.mark.unit
class TestStandardSerializerConvenienceFunctions:
    """Test module-level convenience functions."""

    def test_serialize_convenience_function(self) -> None:
        """Test the module-level serialize() function."""
        from cachekit.serializers.standard_serializer import serialize

        data = {"test": "value", "number": 42}
        serialized = serialize(data)

        assert isinstance(serialized, bytes)
        assert len(serialized) > 0

    def test_deserialize_convenience_function(self) -> None:
        """Test the module-level deserialize() function."""
        from cachekit.serializers.standard_serializer import deserialize, serialize

        original = {"test": "value", "number": 42}
        serialized = serialize(original)
        deserialized = deserialize(serialized)

        assert deserialized == original


@pytest.mark.unit
class TestStandardSerializerOrmVariations:
    """Test ORM error detection with different base class names."""

    def test_orm_base_model_name(self) -> None:
        """Test ORM detection with 'Base' class name."""
        serializer = StandardSerializer()

        class Base:
            pass

        class SqlAlchemyModel(Base):
            pass

        obj = SqlAlchemyModel()

        with pytest.raises(TypeError) as exc_info:
            serializer.serialize(obj)

        error_msg = str(exc_info.value)
        assert "ORM" in error_msg or "does not support" in error_msg

    def test_orm_model_model_name(self) -> None:
        """Test ORM detection with 'Model' class name."""
        serializer = StandardSerializer()

        class Model:
            pass

        class DjangoModel(Model):
            pass

        obj = DjangoModel()

        with pytest.raises(TypeError) as exc_info:
            serializer.serialize(obj)

        error_msg = str(exc_info.value)
        assert "ORM" in error_msg or "does not support" in error_msg


@pytest.mark.unit
class TestStandardSerializerDatetimeErrors:
    """Malformed markers: only a two-key map is read as a marker, so only a two-key map can fail."""

    @pytest.mark.parametrize("sentinel", ["__datetime__", "__date__", "__time__"])
    def test_deserialize_two_key_marker_missing_value_raises(self, sentinel: str) -> None:
        """A marker-sized map with its sentinel but no 'value' field is corrupt cached data."""
        import msgpack

        serializer = StandardSerializer(enable_integrity_checking=False)
        data = msgpack.packb({sentinel: True, "other": 1})

        with pytest.raises(SerializationError, match="missing 'value' field"):
            serializer.deserialize(data)

    @pytest.mark.parametrize(
        "value",
        [
            {"__datetime__": True},  # one key: raised before the length check, now a plain dict
            {"__date__": True, "value": "2026-01-01", "extra": 1},  # three keys: was a date, now a plain dict
            {"__time__": True, "value": None, "extra": 1},  # three keys: raised, now a plain dict
        ],
    )
    def test_deserialize_non_two_key_map_is_never_a_marker(self, value: dict) -> None:
        """No encoder writes a marker of another size, so such a map round-trips as the dict it is."""
        import msgpack

        serializer = StandardSerializer(enable_integrity_checking=False)

        assert serializer.deserialize(msgpack.packb(value)) == value

    def test_deserialize_two_key_records_are_untouched(self) -> None:
        """A two-key map without a truthy sentinel is user data, not a marker."""
        serializer = StandardSerializer()
        value = [{"a": i, "b": str(i)} for i in range(10)] + [{"__datetime__": False, "value": "x"}]

        data, meta = serializer.serialize(value)

        assert serializer.deserialize(data, meta) == value


@pytest.mark.unit
class TestStandardSerializerRegressionAndEdgeCases:
    """Test regression cases and additional edge cases."""

    def test_serialize_returns_metadata_with_compressed_flag(self) -> None:
        """Test that metadata correctly reflects compression status."""
        # With integrity checking (compression enabled)
        serializer_on = StandardSerializer(enable_integrity_checking=True)
        _, meta_on = serializer_on.serialize({"test": "data"})
        assert meta_on.compressed is True
        assert meta_on.format == SerializationFormat.MSGPACK

        # Without integrity checking (no compression)
        serializer_off = StandardSerializer(enable_integrity_checking=False)
        _, meta_off = serializer_off.serialize({"test": "data"})
        assert meta_off.compressed is False
        assert meta_off.format == SerializationFormat.MSGPACK

    def test_deserialize_with_none_metadata(self) -> None:
        """Test deserialize with None metadata explicitly passed."""
        serializer = StandardSerializer()
        data, _ = serializer.serialize({"key": "value"})

        # Explicitly pass None for metadata
        result = serializer.deserialize(data, metadata=None)
        assert result == {"key": "value"}

    def test_roundtrip_with_all_supported_types_together(self) -> None:
        """Test comprehensive roundtrip with all supported types combined."""
        serializer = StandardSerializer()
        comprehensive_data = {
            "primitives": {
                "none": None,
                "true": True,
                "false": False,
                "int": 42,
                "float": 3.14,
                "string": "test",
                "bytes": b"data",
            },
            "collections": {
                "list": [1, 2, 3],
                "tuple": (4, 5, 6),
                "dict": {"nested": "dict"},
                "mixed": [1, "two", {"three": 3}],
            },
            "datetime_types": {
                "datetime": datetime(2024, 1, 15, 12, 30, 45),
                "date": date(2024, 1, 15),
                "time": time(12, 30, 45),
            },
            "special_floats": {
                "inf": float("inf"),
                "neg_inf": float("-inf"),
                "nan": float("nan"),
            },
        }

        serialized, metadata = serializer.serialize(comprehensive_data)
        assert isinstance(serialized, bytes)
        assert metadata.format == SerializationFormat.MSGPACK

        deserialized = serializer.deserialize(serialized)

        # Verify all nested structures
        assert deserialized["primitives"]["none"] is None
        assert deserialized["primitives"]["true"] is True
        assert deserialized["primitives"]["int"] == 42
        assert deserialized["primitives"]["float"] == 3.14
        assert deserialized["primitives"]["string"] == "test"
        assert deserialized["primitives"]["bytes"] == b"data"
        assert deserialized["collections"]["list"] == [1, 2, 3]
        assert deserialized["datetime_types"]["datetime"] == datetime(2024, 1, 15, 12, 30, 45)
        assert isinstance(deserialized["datetime_types"]["date"], date)
        assert isinstance(deserialized["datetime_types"]["time"], time)
        assert math.isinf(deserialized["special_floats"]["inf"]) and deserialized["special_floats"]["inf"] > 0
        assert math.isinf(deserialized["special_floats"]["neg_inf"]) and deserialized["special_floats"]["neg_inf"] < 0
        assert math.isnan(deserialized["special_floats"]["nan"])
