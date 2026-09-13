"""Integrity-checking contract for AutoSerializer's top-level NumPy path (issue #155).

Top-level ``np.ndarray`` values were serialized by ``_serialize_numpy`` and returned
*directly*, bypassing Rust ``ByteStorage`` — so unlike the DataFrame/Series/msgpack
branches they carried no xxHash3-64 checksum. A corrupted numpy entry was silently
reconstructed as wrong data, violating the "never silently return corrupted data"
contract for the entire numpy path.

The fix prepends an 8-byte xxHash3-64 checksum to the ``NUMPY_RAW`` payload — no
compression — when ``enable_integrity_checking`` is on (the default / ``@cache``),
mirroring ``ArrowSerializer``; raw ``NUMPY_RAW`` bytes when off (``@cache.minimal``,
consistent with msgpack-off).
"""

from __future__ import annotations

import pytest
import xxhash

from cachekit.serializers import AutoSerializer
from cachekit.serializers.base import SerializationError

# Requires the [data] extra — absent e.g. in the free-threaded CI lane until
# numpy ships free-threaded wheels usable here (LAB-511).
np = pytest.importorskip("numpy")


@pytest.mark.unit
class TestAutoSerializerNumpyIntegrity:
    """Default (integrity-on) numpy path must detect corruption and fail closed."""

    def test_numpy_roundtrip_with_integrity(self) -> None:
        """Clean serialize/deserialize round-trip returns an equal, writable array."""
        serializer = AutoSerializer()  # default: enable_integrity_checking=True
        original = np.arange(2000, dtype=np.float64).reshape(40, 50)

        data, metadata = serializer.serialize(original)
        result = serializer.deserialize(data, metadata)

        assert isinstance(result, np.ndarray)
        np.testing.assert_array_equal(result, original)
        assert result.dtype == original.dtype
        assert result.shape == original.shape

    def test_numpy_is_enveloped_when_integrity_on(self) -> None:
        """With integrity on, numpy carries an 8-byte xxHash3-64 checksum prefix (no compression).

        Format: [8-byte xxHash3-64][NUMPY_RAW...], mirroring ArrowSerializer. Proves the checksum
        the lone special-case used to skip is now present, without LZ4's cost.
        """
        serializer = AutoSerializer()
        original = np.arange(2000, dtype=np.float64)
        data, _ = serializer.serialize(original)

        assert not data.startswith(b"NUMPY_RAW"), "checksum prefix must precede the NUMPY_RAW payload"
        assert data[8:17] == b"NUMPY_RAW", "expected [8-byte xxHash3-64][NUMPY_RAW...] envelope"
        assert xxhash.xxh3_64_digest(data[8:]) == data[:8], "prefix must be the xxHash3-64 of the payload"
        # checksum-only: wire stays ~1x the raw bytes (no compression inflation)
        assert len(data) < original.nbytes + 64, "checksum-only envelope must not inflate the payload"

    def test_numpy_corruption_detected(self) -> None:
        """Corrupting a payload byte raises SerializationError instead of returning wrong data.

        This is the #155 reproducer: pre-fix this returns a silently-wrong array (no raise).
        """
        serializer = AutoSerializer()
        original = np.arange(2000, dtype=np.float64)
        data, metadata = serializer.serialize(original)

        # Flip a byte deep in the payload (past the 8-byte xxHash3-64 checksum prefix).
        corrupted = bytearray(data)
        corrupted[len(corrupted) // 2] ^= 0xFF
        corrupted = bytes(corrupted)

        with pytest.raises(SerializationError):
            serializer.deserialize(corrupted, metadata)

    def test_numpy_bit_flip_detected(self) -> None:
        """A single bit flip in the payload is caught by the xxHash3-64 checksum."""
        serializer = AutoSerializer()
        data, metadata = serializer.serialize(np.arange(2000, dtype=np.float64))

        corrupted = bytearray(data)
        corrupted[len(corrupted) // 2] ^= 1 << 3
        corrupted = bytes(corrupted)

        with pytest.raises(SerializationError):
            serializer.deserialize(corrupted, metadata)


@pytest.mark.unit
class TestAutoSerializerNumpyMinimalMode:
    """@cache.minimal (integrity off) deliberately opts out of integrity for all types."""

    def test_numpy_raw_when_integrity_off(self) -> None:
        """With integrity off, numpy stays raw NUMPY_RAW (no checksum), mirroring msgpack-off."""
        serializer = AutoSerializer(enable_integrity_checking=False)
        original = np.arange(2000, dtype=np.float64)

        data, metadata = serializer.serialize(original)

        assert data.startswith(b"NUMPY_RAW"), "minimal mode keeps the raw, unchecked numpy format"
        result = serializer.deserialize(data, metadata)
        np.testing.assert_array_equal(result, original)


@pytest.mark.unit
class TestAutoSerializerNumpyEdgeCases:
    """The checksum-only integrity path must preserve every array shape/dtype it handled before."""

    @pytest.mark.parametrize(
        "arr",
        [
            np.array(3.14, dtype=np.float64),  # 0-d scalar array
            np.array([], dtype=np.float64),  # empty
            np.arange(12, dtype=np.int8).reshape(3, 4),  # int8, 2-d
            np.arange(6, dtype=np.int64),  # int64
            np.asfortranarray(np.arange(20, dtype=np.float64).reshape(4, 5)),  # non-contiguous
            (np.arange(50, dtype=np.float64).reshape(10, 5))[:, ::2],  # strided view
        ],
    )
    def test_numpy_roundtrip_shapes_and_dtypes(self, arr: np.ndarray) -> None:
        serializer = AutoSerializer()
        data, metadata = serializer.serialize(arr)
        result = serializer.deserialize(data, metadata)
        # dtype, shape, and values must all survive (ravel for values: tobytes() returns C-order).
        assert result.dtype == arr.dtype
        assert result.shape == arr.shape
        np.testing.assert_array_equal(result.ravel(), np.ascontiguousarray(arr).ravel())

    def test_numpy_deserialize_without_metadata(self) -> None:
        """Checksummed numpy decodes without metadata via structural detection.

        The decorator read path may not carry metadata; the ``[8-byte checksum][NUMPY_RAW...]``
        structure alone must be enough to route to the numpy deserializer.
        """
        serializer = AutoSerializer()
        original = np.arange(2000, dtype=np.float64)
        data, _ = serializer.serialize(original)

        result = serializer.deserialize(data)  # no metadata
        np.testing.assert_array_equal(result, original)

    def test_numpy_corruption_detected_without_metadata(self) -> None:
        """Corruption fails closed even with no metadata.

        Structural detection ([8-byte checksum][NUMPY_RAW...]) routes to the numpy codec before the
        generic retrieve()/msgpack fallback, so the checksum is verified regardless of metadata.
        """
        serializer = AutoSerializer()
        data, _ = serializer.serialize(np.arange(2000, dtype=np.float64))

        corrupted = bytearray(data)
        corrupted[len(corrupted) // 2] ^= 0xFF
        with pytest.raises(SerializationError):
            serializer.deserialize(bytes(corrupted))


@pytest.mark.unit
class TestAutoSerializerNumpyUnderEncryption:
    """Blast-radius check: the numpy checksum envelope must still survive encryption.

    @cache.secure rejects AutoSerializer (cross_sdk_compatible=False), so this exercises the
    direct EncryptionWrapper(serializer=AutoSerializer()) composition used in tests/tooling.
    """

    def test_numpy_roundtrip_through_encryption(self) -> None:
        from cachekit.serializers.encryption_wrapper import EncryptionWrapper

        master_key = bytes(range(32))
        wrapper = EncryptionWrapper(serializer=AutoSerializer(), master_key=master_key)
        original = np.arange(2000, dtype=np.float64).reshape(40, 50)
        cache_key = "numpy:integrity:test"

        # AAD v0x03 binds the cache_key into the ciphertext, so it is required both ways.
        data, metadata = wrapper.serialize(original, cache_key=cache_key)
        result = wrapper.deserialize(data, metadata, cache_key)

        np.testing.assert_array_equal(result, original)


def _numpy_raw(dtype: bytes, shape: tuple[int, ...], payload: bytes) -> bytes:
    """A NUMPY_RAW entry laid out exactly as ``_serialize_numpy`` writes it, fields attacker-chosen."""
    shape_data = b"".join(dim.to_bytes(4, "little") for dim in shape)
    header = len(dtype).to_bytes(2, "little") + dtype + len(shape_data).to_bytes(2, "little") + shape_data
    return b"NUMPY_RAW" + header + payload


# Each passes the header checks and reaches _deserialize_numpy's own except clause — the dtype
# decode or numpy raising TypeError / ValueError / SyntaxError (measured on numpy 1.26-2.3).
FORGED_NUMPY_RAW = {
    "dtype-not-understood": _numpy_raw(b"not-a-dtype", (1,), b"\x00" * 8),
    "itemsize-past-c-long": _numpy_raw(b"V9223372036854775808", (1,), b"\x00" * 8),
    "dtype-not-utf8": _numpy_raw(b"\xff\xfe", (1,), b"\x00" * 8),
    "shape-does-not-fit": _numpy_raw(b"<f8", (7,), b"\x00" * 8),
    "shape-prefix-unparseable": _numpy_raw(b"(1,f8", (1,), b"\x00" * 8),  # numpy -> ast.literal_eval -> SyntaxError
}


@pytest.mark.unit
class TestAutoSerializerNumpyForgedEntries:
    """A NUMPY_RAW entry is routed to ``_deserialize_numpy`` structurally, with no outer
    ``PAYLOAD_DECODE_ERRORS`` normalisation, so its own except clause is the whole fail-closed
    contract for a forged entry (LAB-2503). The checksum is unkeyed and does not help: whoever
    can write the backend can also write a matching xxHash3-64.
    """

    @pytest.mark.parametrize("entry", FORGED_NUMPY_RAW.values(), ids=list(FORGED_NUMPY_RAW))
    @pytest.mark.parametrize("checksummed", [False, True], ids=["raw", "checksummed"])
    def test_forged_entry_fails_closed_as_serialization_error(self, entry: bytes, checksummed: bool) -> None:
        if checksummed:
            entry = xxhash.xxh3_64_digest(entry) + entry
        with pytest.raises(SerializationError, match="Failed to deserialize NumPy array"):
            AutoSerializer().deserialize(entry)

    def test_degenerate_datetime_dtype_is_refused_before_any_array_is_built(self) -> None:
        # M8[0ns] passes np.frombuffer and then kills the process with SIGFPE inside pandas.
        with pytest.raises(SerializationError, match="zero datetime unit multiplier"):
            AutoSerializer().deserialize(_numpy_raw(b"M8[0ns]", (1,), b"\x00" * 8))
