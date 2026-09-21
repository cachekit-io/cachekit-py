"""Mutation safety (#157) and DataFrame/Series corruption diagnostics (#156) for AutoSerializer.

#157: reconstruction via ``np.frombuffer`` returns read-only arrays that alias the source buffer
(on an L1 hit, the *cached* buffer). A caller mutating a cached value then crashes where an
uncached call would not, and risks corrupting the cache entry. Reads must be writable copies.

#156: the DataFrame/Series deserialize branches caught a checksum mismatch from ``retrieve()``
(surfaced as ``ValueError``) in a broad ``except`` and fell through to re-parsing the corrupt bytes
as raw msgpack — losing the corruption diagnostic. A checksum mismatch must surface as a clear
``SerializationError``.

DataFrames route through ArrowSerializer when pyarrow is installed, so the columnar msgpack path
(``_serialize_dataframe`` / the ``"dataframe"`` branch) is exercised by disabling the arrow
serializer. Series never use arrow, so they hit the columnar path unconditionally.

LAB-2503: ``TestDataFrameSeriesReadRoutes`` pins every DataFrame/Series read route (metadata x
integrity, and metadata-less via the envelope's format_id); it lives here because this file
already forces the columnar path.
"""

from __future__ import annotations

import copy
import functools
import logging
from collections.abc import Callable
from unittest import mock

import msgpack
import pytest
import xxhash

from cachekit._rust_serializer import ByteStorage
from cachekit.cache_handler import CacheOperationHandler, CacheSerializationHandler, handle_decrypt_failure
from cachekit.key_generator import CacheKeyGenerator
from cachekit.serializers import AutoSerializer
from cachekit.serializers.base import (
    ERROR_ECHO_MAX,
    EnvelopeShapeError,
    SerializationError,
    SerializationMetadata,
    bounded_error,
)

# Requires the [data] extra — absent e.g. in the free-threaded CI lane until
# numpy/pandas ship free-threaded wheels (LAB-511).
np = pytest.importorskip("numpy")
pd = pytest.importorskip("pandas")


def _no_arrow(**kwargs: bool) -> AutoSerializer:
    """An AutoSerializer forced onto the columnar msgpack DataFrame path (pyarrow absent)."""
    s = AutoSerializer(**kwargs)
    s._arrow_serializer = None
    return s


def _meta(base: SerializationMetadata, **overrides: object) -> SerializationMetadata:
    """A copy of a writer's metadata with fields overridden — a reader's view of the CK header."""
    clone = copy.copy(base)
    for key, value in overrides.items():
        setattr(clone, key, value)
    return clone


def _assert_equal(out: pd.DataFrame | pd.Series, expected: pd.DataFrame | pd.Series) -> None:
    if isinstance(expected, pd.DataFrame):
        pd.testing.assert_frame_equal(out, expected)
    else:
        pd.testing.assert_series_equal(out, expected)


FRAME = pd.DataFrame({"x": np.arange(5, dtype=np.float64), "n": np.arange(5, dtype=np.int64)})
SERIES = pd.Series(np.arange(8, dtype=np.float64), name="v")


@pytest.mark.unit
class TestDataFrameSeriesReadRoutes:
    """Every route a DataFrame/Series read can take must reconstruct the value: with metadata
    on both integrity settings, and — the decorator read path may carry none — from the
    verified envelope's own format_id (LAB-2503 moved that route under the fail-closed guard).
    """

    @pytest.mark.parametrize("value", [FRAME, SERIES], ids=["dataframe", "series"])
    @pytest.mark.parametrize("integrity", [True, False], ids=["integrity-on", "integrity-off"])
    def test_roundtrip_with_metadata(self, value: pd.DataFrame | pd.Series, integrity: bool) -> None:
        s = _no_arrow(enable_integrity_checking=integrity)
        data, meta = s.serialize(value)
        _assert_equal(s.deserialize(data, meta), value)

    @pytest.mark.parametrize("value", [FRAME, SERIES], ids=["dataframe", "series"])
    def test_roundtrip_without_metadata_via_envelope_format_id(self, value: pd.DataFrame | pd.Series) -> None:
        s = _no_arrow()
        data, _ = s.serialize(value)
        _assert_equal(s.deserialize(data), value)

    @pytest.mark.parametrize("value", [FRAME, SERIES], ids=["dataframe", "series"])
    def test_cross_config_written_off_read_on_fails_closed(self, value: pd.DataFrame | pd.Series) -> None:
        """LAB-2736 follow-up: an entry written with integrity off (no ByteStorage envelope,
        no checksum ever computed) must raise, not reconstruct, when read by a reader with
        integrity on — a same-shaped DataFrame/Series with silently wrong values is far more
        dangerous than a raw-dict/TypeError, so this path never falls through like the
        generic msgpack path does. Confirmed exploitable before this test existed: 6427/7208
        single-bit flips on such an entry decoded to a different-valued DataFrame with no
        error at all, and even the UNCORRUPTED entry decoded successfully despite never
        having been checksummed. Matches StandardSerializer's stricter contract."""
        writer = _no_arrow(enable_integrity_checking=False)
        reader = _no_arrow(enable_integrity_checking=True)
        data, meta = writer.serialize(value)

        with pytest.raises(SerializationError, match="envelope verification"):
            reader.deserialize(data, meta)

    @pytest.mark.parametrize("value", [FRAME, SERIES], ids=["dataframe", "series"])
    def test_cross_config_written_on_read_off_fails_closed(self, value: pd.DataFrame | pd.Series) -> None:
        """The other direction: an enveloped entry (metadata.compressed=True) read by a reader
        with integrity off must raise explicitly. Before the gate this only failed by luck —
        the envelope happens to msgpack-decode as a list, not the dict the columnar decoder
        expects — and surfaced as a misleading "forged columnar payload" error."""
        writer = _no_arrow(enable_integrity_checking=True)
        reader = _no_arrow(enable_integrity_checking=False)
        data, meta = writer.serialize(value)
        assert meta.compressed is True

        with pytest.raises(SerializationError, match="integrity checking disabled"):
            reader.deserialize(data, meta)

    @pytest.mark.parametrize("value", [FRAME, SERIES], ids=["dataframe", "series"])
    def test_corrupted_integrity_off_entry_read_on_fails_closed_not_silently_wrong(
        self, value: pd.DataFrame | pd.Series
    ) -> None:
        """The actual exploited shape: bit-flip an entry that was written with integrity
        off, then read it with integrity on. Every flip must raise SerializationError —
        none may silently return a DataFrame/Series with different values than what was
        written. A sample across the byte range (not exhaustive, for CI speed) is enough
        to pin the contract; the exhaustive proof (7208/7208 raised, 0 silent-wrong) was
        run by hand before this fix landed."""
        writer = _no_arrow(enable_integrity_checking=False)
        reader = _no_arrow(enable_integrity_checking=True)
        data, meta = writer.serialize(value)

        for byte_idx in range(0, len(data), max(1, len(data) // 40)):
            corrupted = bytearray(data)
            corrupted[byte_idx] ^= 0xFF
            try:
                out = reader.deserialize(bytes(corrupted), meta)
            except SerializationError:
                continue
            pytest.fail(f"byte {byte_idx} flip silently returned {out!r} instead of raising")


@pytest.mark.unit
class TestDeserializedArraysAreWritable:
    """#157: deserialized numeric arrays must be writable and must not alias the cached buffer."""

    def test_top_level_numpy_writable_and_unaliased(self) -> None:
        s = AutoSerializer()
        arr = np.arange(10, dtype=np.float64)
        data, meta = s.serialize(arr)

        out = s.deserialize(data, meta)
        assert out.flags.writeable, "deserialized numpy array must be writable"
        out[:] = -1.0  # must not raise on a read-only buffer

        # Mutating the returned array must not write back through the (possibly L1-cached) buffer.
        np.testing.assert_array_equal(s.deserialize(data, meta), arr)

    def test_nested_numpy_writable(self) -> None:
        s = AutoSerializer()
        data, meta = s.serialize({"a": np.arange(6, dtype=np.int64)})

        out = s.deserialize(data, meta)
        assert out["a"].flags.writeable, "nested numpy array must be writable"
        out["a"][0] = 42  # must not raise

    def test_series_numeric_values_writable(self) -> None:
        s = AutoSerializer()
        data, meta = s.serialize(pd.Series(np.arange(8, dtype=np.float64)))

        out = s.deserialize(data, meta)
        assert out.to_numpy(copy=False).flags.writeable, "deserialized Series values must be writable"

    def test_dataframe_numeric_column_writable(self) -> None:
        s = _no_arrow()
        data, meta = s.serialize(pd.DataFrame({"x": np.arange(5, dtype=np.float64)}))

        out = s.deserialize(data, meta)
        assert out["x"].to_numpy(copy=False).flags.writeable, "deserialized DataFrame column must be writable"


@pytest.mark.unit
class TestDataFrameSeriesCorruptionDiagnostic:
    """#156: a checksum mismatch on a DataFrame/Series read must fail closed with a clear error."""

    def test_series_corruption_raises_serialization_error(self) -> None:
        s = AutoSerializer()
        data, meta = s.serialize(pd.Series(np.arange(50, dtype=np.float64)))

        corrupted = bytearray(data)
        corrupted[len(corrupted) // 2] ^= 0xFF
        with pytest.raises(SerializationError):
            s.deserialize(bytes(corrupted), meta)

    def test_dataframe_corruption_raises_serialization_error(self) -> None:
        s = _no_arrow()
        data, meta = s.serialize(pd.DataFrame({"x": np.arange(50, dtype=np.float64)}))

        corrupted = bytearray(data)
        corrupted[len(corrupted) // 2] ^= 0xFF
        with pytest.raises(SerializationError):
            s.deserialize(bytes(corrupted), meta)


@pytest.mark.unit
class TestEnvelopeVerificationVsNotAnEnvelope:
    """LAB-2736: ``retrieve()`` raises a distinct type for a verified-but-corrupt envelope
    (checksum/decompression/size failure) vs. bytes that were never a ByteStorage envelope
    at all (e.g. written with integrity checking off). ``deserialize`` must fail closed on
    the former and keep falling through to the plain-msgpack path only on the latter.
    """

    def test_corrupted_payload_names_the_integrity_failure(self) -> None:
        s = AutoSerializer()
        data, meta = s.serialize({"nums": list(range(2000))})

        corrupted = bytearray(data)
        corrupted[len(corrupted) // 2] ^= 0xFF
        with pytest.raises(SerializationError) as exc_info:
            s.deserialize(bytes(corrupted), meta)

        message = str(exc_info.value)
        assert "envelope verification" in message
        assert "not a decodable MessagePack" not in message

    def test_plain_msgpack_written_with_integrity_off_still_falls_through(self) -> None:
        off = AutoSerializer(enable_integrity_checking=False)
        payload = {"a": 1, "b": [1, 2, 3]}
        data, _ = off.serialize(payload)

        on = AutoSerializer(enable_integrity_checking=True)
        assert on.deserialize(data) == payload

    def test_structurally_corrupt_envelope_on_generic_path_fails_closed(self) -> None:
        """LAB-2736 follow-up: metadata.compressed=True (the writer's own record that this
        entry should be a verified envelope) plus a structural parse failure — not just a
        checksum mismatch — must still raise, not fall through to unpackb_bounded on the
        still-enveloped bytes. Truncation forces DeserializationFailed (structural), the
        other branch of retrieve()'s failure taxonomy from the checksum-mismatch case
        ``test_corrupted_payload_names_the_integrity_failure`` already covers above."""
        s = AutoSerializer(enable_integrity_checking=True)
        data, meta = s.serialize({"a": 1, "b": [1, 2, 3]})
        assert meta.compressed is True

        truncated = data[: len(data) // 4]
        with pytest.raises(SerializationError, match="envelope verification"):
            s.deserialize(truncated, meta)

    def test_enveloped_entry_read_with_integrity_off_fails_closed(self) -> None:
        """Writer on, reader off, generic msgpack: retrieve() never runs, so nothing sets
        envelope_error and the compressed=True gate below it never fires. Before the gate
        the read fell through to unpackb on the ENVELOPE bytes and silently returned its
        positional fields — ``[payload, checksum, size, format]`` — as the cached value."""
        on = AutoSerializer(enable_integrity_checking=True)
        data, meta = on.serialize({"a": 1, "b": [1, 2, 3]})
        assert meta.compressed is True

        off = AutoSerializer(enable_integrity_checking=False)
        with pytest.raises(SerializationError, match="integrity checking disabled"):
            off.deserialize(data, meta)

    def test_absent_header_format_leaves_the_envelope_record_in_charge(self) -> None:
        """With ``metadata.original_type`` None (one flipped header byte), the former
        ``hasattr(...) else format_id`` never reached ``format_id`` and a checksum-verified
        Series envelope decoded as a dict. An absent header claim is not a disagreement, so
        the envelope's own record still decides and the Series comes back."""
        s = AutoSerializer()
        series = pd.Series([1.0, 2.0, 3.0], name="v")
        data, meta = s.serialize(series)
        meta.original_type = None

        out = s.deserialize(data, meta)
        assert isinstance(out, pd.Series)
        pd.testing.assert_series_equal(out, series)

    @pytest.mark.parametrize(
        ("stored", "lying_header"),
        [
            ({"a": 1}, "dataframe"),  # was type confusion: a dict came back as a DataFrame
            (pd.Series([1.0, 2.0], name="v"), "dataframe"),  # was a decode error on healthy data
            (pd.Series([1.0, 2.0], name="v"), "seriez"),  # header rot to a non-whitelisted value
        ],
    )
    def test_a_lying_header_format_fails_closed_rather_than_steering_the_decode(self, stored: object, lying_header: str) -> None:
        """A LYING header, not merely an absent one — absence is the weak case. The third case
        pins the symmetry: rot in the header must fail closed exactly like rot in the envelope,
        so the check cannot be narrowed to header values that happen to be decodable formats."""
        s = _no_arrow()  # columnar msgpack path, not Arrow IPC
        data, meta = s.serialize(stored)
        assert meta.original_type != lying_header
        meta.original_type = lying_header

        with pytest.raises(SerializationError, match="disagrees with header format"):
            s.deserialize(data, meta)

    @pytest.mark.parametrize(
        "claim",
        [["a"], {"a": 1}, 7, True, b"msgpack"],
        ids=["list", "dict", "int", "bool", "bytes"],
    )
    def test_a_header_format_that_is_not_a_string_is_refused_before_anything_reads_it(self, claim: object) -> None:
        """``original_type`` is not typed anywhere it is filled: the CK header is plaintext JSON
        and ``SerializationMetadata.from_dict`` hands ``data.get("original_type")`` straight
        through, so this slot arrives holding whatever decoded. Pinned on the NUMPY route, which
        reaches the claim before any envelope work. The message must name the TYPE and never the
        claim — the sites that echo one run ``repr`` before ``:.40`` truncates, so an oversized
        claim is rendered whole to emit 40 characters and a nested one raises ``RecursionError``,
        which is not a ``SerializationError`` and escapes every caller's ``except``."""
        s = AutoSerializer()
        data, meta = s.serialize(np.arange(4))
        assert meta.original_type == "numpy"
        meta.original_type = claim

        with pytest.raises(SerializationError, match="not a string") as caught:
            s.deserialize(data, meta)
        assert type(claim).__name__ in str(caught.value)
        assert repr(claim) not in str(caught.value), "the claim itself must never be rendered"

    def test_a_header_rotted_to_a_non_string_no_longer_returns_the_envelope_body_as_the_value(self) -> None:
        """Why the type check is not tidying-up. A non-str matches NO branch: it is not
        ``"numpy"``, not in ``(None, "arrow")``, not in ``("dataframe", "series")`` — so an
        integrity-off ``series`` entry whose header rotted to a dict skipped the columnar
        decode, skipped every gate (no envelope is built with integrity off), decoded the
        columnar body as plain msgpack, and returned it. A ``Series`` came back as a ``dict``,
        no error raised — the silent-wrong-type class this whole file exists to prevent. The
        equality chain could never have caught it: ``x in (tuple)`` compares and never hashes,
        so a non-str disagrees with every format without being read as wrong."""
        s = _no_arrow(enable_integrity_checking=False)
        data, meta = s.serialize(pd.Series([10.0, 20.0, 30.0], name="prices"))
        assert meta.original_type == "series"
        meta.original_type = {"rot": 1}

        with pytest.raises(SerializationError, match="not a string"):
            s.deserialize(data, meta)

    def test_an_unknown_envelope_format_fails_closed_rather_than_decoding_as_msgpack(self) -> None:
        """Rotted to an unknown value the envelope's format used to miss every branch and fall
        to ``unpackb_bounded``, handing back a dict for a checksum-verified Series."""
        s = _no_arrow()
        data, meta = s.serialize(pd.Series([1.0, 2.0], name="v"))
        envelope = msgpack.unpackb(data)  # [compressed_data, checksum, original_size, format]
        envelope[3] = "seriez"
        meta.original_type = None  # no header claim left to catch it

        with pytest.raises(SerializationError, match="unknown envelope format"):
            s.deserialize(msgpack.packb(envelope), meta)

    def test_a_flipped_envelope_format_cannot_silently_change_the_returned_type(self) -> None:
        """The mirror of the lying-header case. Series -> ``"msgpack"`` is a format this writer
        can emit, so the whitelist alone still returns a dict; the header's claim is what catches it."""
        s = _no_arrow()
        series = pd.Series([10.0, 20.0], name="v")
        data, meta = s.serialize(series)
        assert meta.original_type == "series"
        envelope = msgpack.unpackb(data)
        envelope[3] = "msgpack"

        with pytest.raises(SerializationError, match="disagrees with header format"):
            s.deserialize(msgpack.packb(envelope), meta)

    @pytest.mark.parametrize("integrity", [True, False])
    def test_an_arrow_entry_with_a_degraded_header_still_decodes_via_its_own_checksum(self, integrity: bool) -> None:
        """Arrow IPC never parses as a ByteStorage envelope, so an Arrow read carrying metadata
        always has ``envelope_error`` set — and its ``compressed`` flag means its own codec, not
        an envelope. Both fail-closed gates therefore used to swallow an Arrow entry that had
        merely lost ``original_type``, on the integrity-ON and integrity-OFF reader alike, even
        though ArrowSerializer verifies its own xxHash3-64. Structural detection runs first."""
        pytest.importorskip("pyarrow")
        frame = pd.DataFrame({"a": [1, 2, 3]})
        data, meta = AutoSerializer().serialize(frame)
        assert meta.original_type == "arrow", "this test needs the ArrowSerializer path"
        meta.original_type = None

        _assert_equal(AutoSerializer(enable_integrity_checking=integrity).deserialize(data, meta), frame)

    def test_a_lying_header_over_arrow_bytes_does_not_decode_as_arrow(self) -> None:
        """Recovering the degraded-header Arrow read must not become "structure beats the
        header": a header naming a DIFFERENT format contradicts the bytes, and decoding it as
        Arrow anyway would reinstate the single-rotted-field-decides-the-type class this whole
        contract exists to close. Only an absent (or agreeing) header licenses the recovery."""
        pytest.importorskip("pyarrow")
        data, meta = AutoSerializer().serialize(pd.DataFrame({"a": [1, 2, 3]}))
        assert meta.original_type == "arrow"
        meta.original_type = "msgpack"

        with pytest.raises(SerializationError, match="envelope verification"):
            AutoSerializer().deserialize(data, meta)

    @pytest.mark.parametrize("metadata_present", [True, False])
    def test_an_envelope_whose_bytes_contain_arrow_magic_is_not_hijacked(self, metadata_present: bool) -> None:
        """The structural Arrow route must authenticate, not sniff. LZ4 emits literals verbatim, so
        caching ``b"xxARROW1..."`` lands the magic at envelope offset 8 of a valid, checksum-intact
        entry, and a bare ``data[8:14] == b"ARROW1"`` test handed that healthy entry to the Arrow
        decoder, which called it corrupt — a false miss on every read, deterministic on recompute,
        so it never self-heals. No pyarrow needed: the collision is in the ByteStorage writer."""
        s = AutoSerializer()
        value = b"xxARROW1" + b"z" * 20
        data, meta = s.serialize(value)
        assert data[8:14] == b"ARROW1", "this test needs the magic collision it guards against"
        assert xxhash.xxh3_64_digest(data[8:]) != data[:8], "...and those bytes must not be a real Arrow checksum"
        meta.original_type = None

        assert s.deserialize(data, meta if metadata_present else None) == value

    @pytest.mark.parametrize("metadata_present", [True, False])
    def test_an_envelope_whose_bytes_contain_numpy_magic_is_not_hijacked(self, metadata_present: bool) -> None:
        """The NUMPY_RAW twin of the Arrow case above, and strictly worse before the fix: the numpy
        arm had no digest gate at all, so ``b"xxNUMPY_RAW..."`` — a checksum-intact envelope, header
        ``"msgpack"`` and correct — was handed to the numpy decoder on every read, which took the
        envelope's first 8 bytes for a digest, failed it, and raised. A permanent miss on a healthy
        key, with a backend write per call. Both magics now go through one ``_checksummed_prefix``."""
        s = AutoSerializer()
        value = b"xxNUMPY_RAW" + b"z" * 20
        data, meta = s.serialize(value)
        assert data[8:17] == b"NUMPY_RAW", "this test needs the magic collision it guards against"
        assert xxhash.xxh3_64_digest(data[8:]) != data[:8], "...and those bytes must not be a real NumPy checksum"
        assert meta.original_type == "msgpack"

        assert s.deserialize(data, meta if metadata_present else None) == value

    @pytest.mark.parametrize(
        "make_metadata",
        [
            lambda meta: None,
            lambda meta: _meta(meta, original_type="arrow"),
            lambda meta: _meta(meta, compressed=False),
            lambda meta: SerializationMetadata.from_dict({"format": "msgpack", "original_type": "msgpack"}),
        ],
        ids=["no-metadata", "header-arrow", "compressed-false", "compressed-key-absent"],
    )
    def test_integrity_off_reader_never_returns_a_healthy_envelope_as_the_value(
        self, make_metadata: Callable[[SerializationMetadata], SerializationMetadata | None]
    ) -> None:
        """An integrity-OFF reader builds no ByteStorage, so the whole retrieve() block is skipped
        and ``envelope_error`` stays None. Gating the shape check on ``envelope_error`` alone
        therefore never ran it for that reader, and a HEALTHY, uncorrupted envelope — no rot, no
        forgery — came back as its four fields, payload in plaintext at slot 0. Two entrances:
        no metadata at all, and a header claiming ``"arrow"`` (which a since-reverted exemption
        let skip the only raise on the metadata path). The last two are the dominant entrance:
        an integrity-off reader that DOES carry metadata, whose ``compressed`` is false or simply
        absent. The old parametrisation was ``[None, "arrow"]`` and both of those carry
        ``compressed=True``, so the cross-config gate fired first and this door was never driven —
        93% of the escapes went through the case the test did not have."""
        writer = AutoSerializer(enable_integrity_checking=True)
        reader = AutoSerializer(enable_integrity_checking=False)
        data, meta = writer.serialize({"field": "value"})

        with pytest.raises(SerializationError):
            reader.deserialize(data, make_metadata(meta))

    @pytest.mark.parametrize(
        "value",
        [
            [b"blob", list(range(1, 9)), 42, "label"],
            [b"\x89PNG", [255, 0, 0, 255, 0, 255, 0, 255], 4096, "series"],
            [b"payload", [10] * 8, 7, "not-a-format"],
            [b"x", [1] * 8, 0, "msgpack"],
            [b"\x89PNG", [255, 0, 0, 255, 0, 255, 0, 255], 4096, "rgb"],
            ["s", [1, 2, 3, 4, 5, 6, 7, 8], 5, "msgpack"],
        ],
        ids=[
            "3of4-label",
            "4of4-png-series",
            "3of4-unknown-format",
            "4of4-minimal-msgpack",
            "3of4-format-slot-irrelevant",
            "3of4-bytes-slot-irrelevant",
        ],
    )
    def test_a_value_shaped_exactly_like_an_envelope_is_deliberately_refused(self, value: list) -> None:
        """**This refusal is a chosen cost, not a bug — do not "fix" it without reading Raises:.**

        Two of these score a perfect 4/4 on ``_looks_like_envelope``; by shape they ARE envelopes.
        A parse does not rescue them either: a legitimate 4/4 list parses as a ``StorageEnvelope``
        and then fails the checksum, byte-indistinguishable from a rotted one. So the read either
        refuses these or returns rotted envelopes, and the trilemma table in ``deserialize``'s
        Raises: records that both alternatives were measured at all three corners.

        This corner was taken because the refusal is a deterministic miss that recomputes the right
        answer, while the alternative hands the caller a corrupted envelope's compressed payload as
        their object. An earlier round asserted the opposite here — ``== value`` — which pinned a
        94% rot-escape rate as intent. If this test starts failing, the rot leak is back.

        The last two rows are the two 3/4 doors: neither the bytes slot nor the format slot is
        required, so "envelope-shaped" is wider than an envelope-looking value. The type is
        asserted, not just the class: it carries its own telemetry reason, so this permanent
        benign refusal never counts as ``corruption``."""
        s = _no_arrow(enable_integrity_checking=False)
        data, meta = s.serialize(value)
        for metadata in (meta, None):
            with pytest.raises(EnvelopeShapeError, match="envelope verification"):
                s.deserialize(data, metadata)

    def test_single_byte_rot_sweep_on_an_integrity_off_reader_escapes_exactly_the_known_marker_set(self) -> None:
        """A full sweep — ALL 255 substitutions per byte — because a parametrised row list cannot
        show a rate, and an 8-value probe set cannot see the bytes that matter: an earlier version
        probed 8 values, found 0, and was named "no single-byte rot is ever returned" while 13
        escaped through bytes it never tried. The suite was green and the claim was false.

        This asserts the KNOWN escape set, not zero. The 13 are ``0xcb``/``0xcf``/``0xd3`` (the
        float64/uint64/int64 markers) at offsets 33-36, and ``0x2b`` at offset 2: a marker byte
        re-partitions every slot after it, two slots rot at once, the score drops to 2, and the
        ``>= 3`` guard passes it. That residual is documented in ``_looks_like_envelope`` and is
        not closable by threshold (``>= 2`` re-refuses LAB-4312's value). So this test pins the
        residual as RECORDED: the guard neutered fails it with hundreds of escapes; a threshold
        change fails it with zero; and the wire-format fix (LAB-4304) that legitimately empties
        the set must update this record on its way through.

        The sweep also pins the *refusals*, not just the escapes: every one of the ~12.4k mutants
        that does not escape must raise ``SerializationError``. A bare ``except Exception`` here
        would let an ``AttributeError`` out of a decode path and still read green, because a crash
        and a clean refusal are both "did not return a value" to a sweep that only counts escapes.
        Anything else is collected with its offset and byte so the whole set surfaces at once."""
        s = AutoSerializer(enable_integrity_checking=False)
        data, _ = AutoSerializer().serialize({"admin": False, "user": "alice", "n": 12345})
        assert len(data) == 49, "the known set below is specific to this exact payload"
        known = {(2, 0x2B)} | {(off, b) for off in (33, 34, 35, 36) for b in (0xCB, 0xCF, 0xD3)}

        returned: dict[tuple[int, int], object] = {}
        unwrapped: list[tuple[int, int, str]] = []
        for i in range(len(data)):
            for byte in range(256):
                if data[i] == byte:
                    continue
                mutant = bytearray(data)
                mutant[i] = byte
                try:
                    returned[(i, byte)] = s.deserialize(bytes(mutant), None)
                except SerializationError:
                    continue
                except Exception as exc:  # recorded, not swallowed: an unwrapped leak is the failure this arm exists to name
                    unwrapped.append((i, byte, repr(exc)))
        assert not unwrapped, f"corrupt input leaked a non-SerializationError to the caller: {unwrapped[:5]}"
        assert set(returned) == known, (
            f"escape set moved: +{sorted(set(returned) - known)[:5]} -{sorted(known - set(returned))[:5]}"
        )
        assert all(isinstance(v, list) for v in returned.values()), "every escape is a re-partitioned list"

    def test_forged_numpy_dtype_echo_is_bounded(self) -> None:
        """The numpy decode's own re-raise quotes the forged dtype verbatim; every other read-path
        wrap was bounded while this sibling echoed 65 KB. Bare route, no checksum, integrity-off
        reader — the shortest path to the raise."""
        raw = b"NUMPY_RAW" + (65_000).to_bytes(2, "little") + b"z" * 65_000 + (0).to_bytes(2, "little")

        with pytest.raises(SerializationError) as exc_info:
            AutoSerializer(enable_integrity_checking=False).deserialize(raw)
        assert len(str(exc_info.value)) < ERROR_ECHO_MAX + 200, f"numpy dtype echo not bounded: {len(str(exc_info.value))}"

    @pytest.mark.parametrize("reader_integrity", [True, False], ids=["reader-on", "reader-off"])
    @pytest.mark.parametrize("slot, bad", [(0, "notbytes"), (1, "AAAA"), (2, "x"), (3, 42), (3, "seriez"), (3, ["a"])])
    def test_metadata_absent_read_of_a_rotted_envelope_never_returns_the_envelope_itself(
        self, slot: int, bad: object, reader_integrity: bool
    ) -> None:
        """``deserialize(data)`` with no metadata on an envelope whose checksum / size / format slot
        rotted: ``retrieve()`` cannot parse it, and — because an envelope is itself valid msgpack —
        the plain-msgpack fall-through then returned ``[compressed_payload, checksum, size, fmt]``
        as the cached value. Silent wrong data on the public direct API, the exact shape the
        verified-envelope branch was written to close. Every slot, because a shape test keyed on
        the format alone waved ``42`` through and one keyed on the checksum waved ``"AAAA"``
        through: the envelope is recognised by whichever invariant slot survived."""
        data, _ = AutoSerializer().serialize({"field": "value"})
        envelope = list(msgpack.unpackb(data))
        envelope[slot] = bad
        reader = AutoSerializer(enable_integrity_checking=reader_integrity)

        with pytest.raises(SerializationError, match="envelope verification"):
            reader.deserialize(msgpack.packb(envelope))

    @pytest.mark.parametrize("claim", ["msgpack", "arrow"])  # measured: every non-numpy claim takes one branch
    @pytest.mark.parametrize("writer_integrity", [True, False], ids=["checksummed", "bare"])
    @pytest.mark.parametrize("reader_integrity", [True, False], ids=["reader-on", "reader-off"])
    def test_numpy_bytes_are_refused_when_the_header_names_another_format(
        self, claim: str, writer_integrity: bool, reader_integrity: bool
    ) -> None:
        """LAB-4312: the structural NumPy route returned an ndarray whatever the header said, so
        the format-agreement rule this file exists to establish never fired on it — ``"msgpack"``
        over NUMPY_RAW bytes still produced an array, on both readers and both prefix forms.

        Both readers and both prefix forms, because the route runs before either is consulted.
        Why it raises rather than skipping lives in ``deserialize``'s ``Raises:``, once."""
        writer = AutoSerializer(enable_integrity_checking=writer_integrity)
        data, meta = writer.serialize(np.arange(6, dtype=np.int32))
        assert meta.original_type == "numpy"
        assert (data[:9] == b"NUMPY_RAW") is not writer_integrity, "this test needs both prefix forms"
        meta.original_type = claim

        with pytest.raises(SerializationError, match="disagrees with header format"):
            AutoSerializer(enable_integrity_checking=reader_integrity).deserialize(data, meta)

    @pytest.mark.parametrize("claim", ["numpy", None], ids=["agrees", "absent"])
    @pytest.mark.parametrize("writer_integrity", [True, False], ids=["checksummed", "bare"])
    def test_numpy_bytes_still_decode_when_the_header_agrees_or_is_absent(
        self, claim: str | None, writer_integrity: bool
    ) -> None:
        """The other half of the gate above. ``None`` is the case it decides: a header that merely
        LOST ``original_type`` must still reach the numpy decode, exactly as the Arrow gate keeps
        a degraded-header Arrow entry readable, or the gate trades one false miss for another."""
        writer = AutoSerializer(enable_integrity_checking=writer_integrity)
        expected = np.arange(6, dtype=np.int32)
        data, meta = writer.serialize(expected)
        meta.original_type = claim

        np.testing.assert_array_equal(AutoSerializer().deserialize(data, meta), expected)

    @pytest.mark.parametrize(
        "value",
        [
            [1, 2, 3, "msgpack"],
            [1, 2, 3, "dataframe"],
            ["a", "b", "c", "series"],
            [None, None, None, "series"],
            [1, 2, 3, ["a"]],
            [1, 2, 3, {"k": 1}],
        ],
        ids=["ints-msgpack", "ints-dataframe", "strs-series", "nones-series", "unhashable-list", "unhashable-dict"],
    )
    def test_a_legitimate_four_element_list_is_not_mistaken_for_an_envelope(self, value: list) -> None:
        """LAB-4312: the shape test accepted the format slot ALONE as proof, so an integrity-off
        writer's plain-msgpack ``[1, 2, 3, "msgpack"]``, read back by an integrity-on serializer
        with no metadata, decoded fine and was then rejected as a corrupt envelope. Recompute is
        deterministic, so that miss never self-heals — the offset-8 magic collision's class,
        through the other door.

        The last two rows are its unfiled sibling: ``value[3] in _ENVELOPE_FORMATS`` on an
        unhashable slot does not evaluate False, it raises ``TypeError`` — straight out of
        ``deserialize``, past every ``except SerializationError`` a caller wrote. It only stayed
        hidden because ``checksum_ok or ...`` short-circuited whenever slot 1 survived."""
        data, _ = AutoSerializer(enable_integrity_checking=False).serialize(value)

        assert AutoSerializer(enable_integrity_checking=True).deserialize(data) == value

    @pytest.mark.parametrize("metadata_present", [True, False], ids=["with-metadata", "no-metadata"])
    @pytest.mark.parametrize("slot, field", [(0, "data"), (1, "checksum"), (2, "original_size"), (3, "format")])
    def test_the_echoed_envelope_failure_is_length_bounded(self, slot: int, field: str, metadata_present: bool) -> None:
        """Every envelope slot is attacker-written, and clipping the ones somebody thought to clip
        is not a bound: three ``!r:.40`` caps held ``format`` to 130 chars while 200 KB in the
        fixed-width ``checksum`` slot escaped at 200,162, because rmp_serde's own text quotes the
        slot it choked on and never passes through a cap. Parametrised over the whole envelope so
        a fourth slot cannot reopen it — and over both metadata states, because bounding one
        raise site (``_envelope_failure``) left the metadata-absent tail at 200,201: the bound is
        a property of the read path, so every re-raise that quotes an untrusted cause carries it."""
        s = _no_arrow()
        data, meta = s.serialize({"a": 1})
        meta.original_type = None
        envelope = list(msgpack.unpackb(data))
        envelope[slot] = "A" * 200_000

        with pytest.raises(SerializationError) as exc_info:
            s.deserialize(msgpack.packb(envelope) + b"\xc1", meta if metadata_present else None)
        assert len(str(exc_info.value)) < ERROR_ECHO_MAX + 200, f"{field} echo not bounded: {len(str(exc_info.value))} chars"

    def test_flipping_compressed_in_the_header_cannot_reopen_the_fall_through(self) -> None:
        """The fail-closed gate used to be ``... and metadata.compressed`` — a plaintext
        CK-header field. Flipping it False on a structurally corrupt integrity-on envelope
        let the generic path decode the envelope itself and return its four positional fields
        as the cached value. The gate must not consult it: an unauthenticated field may gate a
        raise, never a decode."""
        s = AutoSerializer()
        data, meta = s.serialize({"a": 1, "b": [1, 2, 3]})
        envelope = msgpack.unpackb(data)  # [compressed_data, checksum, original_size, format]
        envelope[1] = list(envelope[1])
        envelope[1][0] = "x"  # non-u8 checksum element: the envelope fails to PARSE, not to verify
        corrupted = msgpack.packb(envelope)
        meta.compressed = False

        with pytest.raises(SerializationError, match="envelope verification"):
            s.deserialize(corrupted, meta)


# A well-formed __ndarray__ marker: the object hook turns it into an ndarray wherever it sits, so a
# forged document can put an array where the writer only ever puts a list or a dict. M8[2s] is a
# dtype numpy accepts and pandas then asserts on (AssertionError, outside PAYLOAD_DECODE_ERRORS).
NDARRAY_M8_2S = {"__ndarray__": True, "dtype": "M8[2s]", "shape": [1], "data": b"\x00" * 8}
F8_COLUMN = {"type": "numeric", "data": b"\x00" * 8, "dtype": "<f8"}
# A marker msgpack decodes (the walk admits 1024 levels) but repr() cannot on 3.10/3.11 (RecursionError).
DEEP_LIST = functools.reduce(lambda acc, _: [acc], range(1000), [])


def _entry(kind: str, body: dict) -> bytes:
    """A checksummed ``dataframe`` / ``series`` entry carrying ``body``."""
    return bytes(ByteStorage("msgpack").store(msgpack.packb(body), kind))


def _columnar_entry(kind: str, column: dict) -> bytes:
    """A checksummed ``dataframe`` / ``series`` entry whose single column is ``column``."""
    body = (
        {"columns": ["x"], "index": None, "data": {"x": column}}
        if kind == "dataframe"
        else {"name": None, "index": None, **column}
    )
    return _entry(kind, body)


@pytest.mark.unit
class TestForgedColumnarPayloadIsRefused:
    """Forged DataFrame/Series documents are refused before pandas sees them (LAB-2503): a numeric
    column dtype the writer never emits (``M8[0ns]`` passes ``np.frombuffer`` and then kills the
    process with SIGFPE inside pandas — uncatchable), a column type marker other than the two the
    writer emits, and an ndarray smuggled via the ``__ndarray__`` hook into a field the writer only
    ever fills with a list or a dict.
    """

    @pytest.mark.parametrize("dtype", ["M8[0ns]", "m8[0ns]", "U4"])
    @pytest.mark.parametrize("kind", ["dataframe", "series"])
    def test_forged_column_dtype_is_a_serialization_error(self, kind: str, dtype: str) -> None:
        entry = _columnar_entry(kind, {**F8_COLUMN, "dtype": dtype})
        with pytest.raises(SerializationError, match="Forged columnar dtype"):
            AutoSerializer().deserialize(entry)

    @pytest.mark.parametrize("marker", ["forged", DEEP_LIST], ids=["unknown-string", "list-nested-1000-deep"])
    @pytest.mark.parametrize("kind", ["dataframe", "series"])
    def test_unknown_column_type_marker_is_refused(self, kind: str, marker: object) -> None:
        entry = _columnar_entry(kind, {"type": marker, "data": [1, 2]})
        with pytest.raises(SerializationError, match="Forged columnar payload: .* type is"):
            AutoSerializer().deserialize(entry)

    @pytest.mark.parametrize(
        "kind, body",
        [
            ("dataframe", {"columns": ["x"], "index": None, "data": {"x": NDARRAY_M8_2S}}),
            ("dataframe", {"columns": ["x"], "index": None, "data": {"x": {"type": "object", "data": NDARRAY_M8_2S}}}),
            ("dataframe", {"columns": NDARRAY_M8_2S, "index": None, "data": {"x": F8_COLUMN}}),
            ("dataframe", {"columns": ["x"], "index": NDARRAY_M8_2S, "data": {"x": F8_COLUMN}}),
            ("dataframe", {"columns": ["x"], "index": None, "data": NDARRAY_M8_2S}),
            ("series", {"name": None, "index": None, "type": "object", "data": NDARRAY_M8_2S}),
            ("series", {"name": None, "index": NDARRAY_M8_2S, **F8_COLUMN}),
        ],
        ids=[
            "df-column-is-ndarray",
            "df-object-data-is-ndarray",
            "df-columns-is-ndarray",
            "df-index-is-ndarray",
            "df-data-is-ndarray",
            "series-object-data-is-ndarray",
            "series-index-is-ndarray",
        ],
    )
    def test_ndarray_where_the_writer_emits_a_list_or_dict_is_refused(self, kind: str, body: dict) -> None:
        with pytest.raises(SerializationError, match="Forged columnar payload"):
            AutoSerializer().deserialize(_entry(kind, body))

    @pytest.mark.parametrize(
        "kind, body",
        [("dataframe", [1, 2]), ("series", 7)],
        ids=["dataframe-body-is-list", "series-body-is-int"],
    )
    def test_non_dict_document_is_refused(self, kind: str, body: object) -> None:
        # The writer always emits a dict body; a forged non-dict decodes cleanly under the msgpack
        # bound and now hits the _expect shape gate directly (the dead bytes-preamble that used to
        # sit ahead of it is gone), so the "document is <type>" guard is reachable in production.
        with pytest.raises(SerializationError, match="Forged columnar payload: document is"):
            AutoSerializer().deserialize(_entry(kind, body))  # type: ignore[arg-type]


def _numpy_raw(dtype: bytes, shape: bytes) -> bytes:
    return b"NUMPY_RAW" + len(dtype).to_bytes(2, "little") + dtype + len(shape).to_bytes(2, "little") + shape


class TestForgedNumpyMetadataIsRefused:
    """NUMPY_RAW dtype/shape metadata is untrusted: slicing past the end silently shortens and a
    partial 4-byte chunk used to parse as a dimension, so a forged 1-byte zero shape chunk built
    an EMPTY array instead of raising (CodeRabbit on cachekit-py#276)."""

    @pytest.mark.parametrize(
        ("payload", "why"),
        [
            (_numpy_raw(b"<f8", b"\x00"), "1-byte shape chunk parsed as dimension 0 -> empty array"),
            (_numpy_raw(b"<f8", b"\x01\x00\x00"), "3-byte shape chunk (not 4-aligned)"),
            (
                b"NUMPY_RAW" + (3).to_bytes(2, "little") + b"<f8" + (8).to_bytes(2, "little") + b"\x01\x00\x00\x00",
                "shape shorter than its length prefix",
            ),
            (b"NUMPY_RAW" + (9).to_bytes(2, "little") + b"<f8", "dtype shorter than its length prefix"),
        ],
    )
    def test_truncated_or_misaligned_metadata_is_a_serialization_error(self, payload: bytes, why: str) -> None:
        pytest.importorskip("numpy")
        with pytest.raises(SerializationError, match="truncated or misaligned"):
            AutoSerializer(enable_integrity_checking=False).deserialize(payload)

    def test_well_formed_numpy_still_round_trips(self) -> None:
        np = pytest.importorskip("numpy")
        arr = np.arange(6, dtype="<f8").reshape(2, 3)
        s = AutoSerializer(enable_integrity_checking=False)
        np.testing.assert_array_equal(s.deserialize(s.serialize(arr)[0]), arr)


def _oversized_forged_error() -> SerializationError:
    """The SerializationError raised by decoding a poisoned columnar entry that carries a 1 MiB
    column name and a 4 KB forged dtype — the real error object the read-path log sites echo."""
    big_name = "n" * (1024 * 1024)  # 1 MiB column name (already capped in the field echo, #276)
    big_dtype = "z" * 4096  # 4 KB forged dtype — numpy echoes the whole string, uncapped (LAB-3131)
    body = {
        "columns": [big_name],
        "index": None,
        "data": {big_name: {"type": "numeric", "data": b"", "dtype": big_dtype}},
    }
    with pytest.raises(SerializationError) as excinfo:
        AutoSerializer().deserialize(_entry("dataframe", body))
    return excinfo.value


@pytest.mark.unit
class TestForgedEntryErrorEchoIsBounded:
    """LAB-3131 AC1: a poisoned columnar entry of any size logs O(1)-bounded text at every wrap
    site. #276 capped the per-field marker/column echoes, but ``_dtype_from_untrusted`` still
    echoed the full forged dtype, so the SerializationError message — and every log line built
    from it — grew with the payload. The bound is applied once, at each read-path wrap site, via
    :func:`bounded_error`.
    """

    def test_bounded_error_clips_and_neutralizes_control_chars(self) -> None:
        # Over-length text is clipped to O(1) with the true length preserved for forensics.
        clipped = bounded_error(SerializationError("x" * (1024 * 1024)))
        assert len(clipped) <= ERROR_ECHO_MAX + 64
        assert "1048576 chars total" in clipped
        # Every line/terminal-control char is escaped, so the result is one terminal-safe line —
        # not just \n/\r (ANSI \x1b, vertical tab \x0b, Unicode line-sep U+2028 all handled).
        raw = "a\nb\rc\x1bd\x0be" + chr(0x2028) + "f"  # newline, CR, ANSI ESC, VT, U+2028 line-sep
        unsafe = bounded_error(SerializationError(raw))
        assert not any(ch in unsafe for ch in "\n\r\x1b\x0b" + chr(0x2028))
        assert "\\x1b" in unsafe

    def test_forged_dtype_error_is_bounded_at_the_columnar_wrap(self) -> None:
        # The columnar wrap is itself a re-raise site that interpolates an untrusted cause: a
        # forged 200 KB dtype inside a checksum-VALID envelope came out at 200,078 chars through
        # it while every other site was bounded. The bound belongs to the read path, not to one
        # function. (That the raw dtype text is huge is proven by bounded_error's own test above.)
        assert len(str(_oversized_forged_error())) < ERROR_ECHO_MAX + 200

    def test_forged_uuid_value_echo_is_bounded(self) -> None:
        # The object hook's UUID re-raise quoted the cached value verbatim: 200,036 chars from a
        # 200 KB field. Named by variable, not by the `{e}`/`{cause}` shape an earlier sweep
        # grepped for, which is how it survived four rounds of "the echo class is now complete".
        s = _no_arrow(enable_integrity_checking=False)
        with pytest.raises(SerializationError) as exc:
            s.deserialize(msgpack.packb({"__uuid__": True, "value": "A" * 200_000}))
        assert len(str(exc.value)) < ERROR_ECHO_MAX + 200, f"{len(str(exc.value))} chars"

    def test_forged_columnar_dtype_echo_is_bounded_at_the_raise(self) -> None:
        # Sibling of the same miss, in `_dtype_from_untrusted`. numpy's str() of a forged
        # structured dtype spec runs ~2x the spec's own size, so the echo grows without limit
        # with the entry: a 9 KB spec reached 19,373 chars before this bound.
        from cachekit.serializers.auto_serializer import _dtype_from_untrusted

        spec = [(f"f{i}", [(f"g{j}", "i4") for j in range(10)]) for i in range(10)]
        for numeric_only in (True, False):
            with pytest.raises(SerializationError) as exc:
                _dtype_from_untrusted(spec if numeric_only else "M8[0s]", numeric_only=numeric_only)
            assert len(str(exc.value)) < ERROR_ECHO_MAX + 200, f"{len(str(exc.value))} chars"

    def test_handle_decrypt_failure_warning_line_is_bounded(self, caplog) -> None:
        # _handle_l2_read_error and the wrapper L1 SerializationError guard both route the poisoned
        # error here; this is the WARNING line emitted on every poisoned read.
        err = _oversized_forged_error()
        with caplog.at_level(logging.WARNING):
            handle_decrypt_failure(err, tier="l2", cache_key="ns:app:key", fail_closed=False)
        lines = [r.getMessage() for r in caplog.records if "decrypt/integrity failure" in r.getMessage()]
        # Line = fixed template + one bounded_error() echo, so it is O(1) in ERROR_ECHO_MAX,
        # independent of the (multi-MB) forged payload.
        assert lines and all(len(line) < ERROR_ECHO_MAX + 256 for line in lines)

    def test_end_to_end_l2_read_of_forged_entry_logs_bounded_line(self, caplog) -> None:
        # The real read plumbing: get_cached_value -> _handle_l2_read_error -> handle_decrypt_failure.
        # Catches a regression if a future edit logs the poisoned error ahead of the bounded site.
        err = _oversized_forged_error()
        serialization = mock.MagicMock(spec=CacheSerializationHandler)
        serialization.deserialize_data.side_effect = err
        serialization.encryption_fail_closed = False  # real bool: a MagicMock is truthy -> fail-closed
        serialization.supports_mmap_read.return_value = False
        handler = CacheOperationHandler(serialization, CacheKeyGenerator())
        backend = mock.MagicMock()
        backend.get.return_value = b"poisoned-entry-bytes"
        handler.set_cache_handler(backend)

        with caplog.at_level(logging.WARNING):
            assert handler.get_cached_value("ns:app:key") is None  # fail-open miss
        lines = [r.getMessage() for r in caplog.records if "decrypt/integrity failure" in r.getMessage()]
        assert lines and all(len(line) < ERROR_ECHO_MAX + 256 for line in lines)
