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

import functools
import logging

import msgpack
import pytest

from cachekit._rust_serializer import ByteStorage
from cachekit.cache_handler import handle_decrypt_failure
from cachekit.serializers import AutoSerializer
from cachekit.serializers.base import ERROR_ECHO_MAX, SerializationError, bounded_error

# Requires the [data] extra — absent e.g. in the free-threaded CI lane until
# numpy/pandas ship free-threaded wheels (LAB-511).
np = pytest.importorskip("numpy")
pd = pytest.importorskip("pandas")


def _no_arrow(**kwargs: bool) -> AutoSerializer:
    """An AutoSerializer forced onto the columnar msgpack DataFrame path (pyarrow absent)."""
    s = AutoSerializer(**kwargs)
    s._arrow_serializer = None
    return s


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

    def test_bounded_error_clips_and_single_lines(self) -> None:
        clipped = bounded_error(SerializationError("x" * (1024 * 1024) + "\nsecond line"))
        assert len(clipped) < ERROR_ECHO_MAX + 64
        assert "truncated, " in clipped and "\n" not in clipped

    def test_forged_dtype_produces_an_unbounded_message(self) -> None:
        # Guards the premise: without the bound the echoed text really is huge (the 4 KB dtype is
        # in there in full), so the assertions below are proving the bound does real work.
        assert len(str(_oversized_forged_error())) > 4096

    def test_handle_decrypt_failure_warning_line_is_bounded(self, caplog) -> None:
        # _handle_l2_read_error and the wrapper L1 SerializationError guard both route the poisoned
        # error here; this is the WARNING line emitted on every poisoned read.
        err = _oversized_forged_error()
        with caplog.at_level(logging.WARNING):
            handle_decrypt_failure(err, tier="l2", cache_key="ns:app:key", fail_closed=False)
        lines = [r.getMessage() for r in caplog.records if "decrypt/integrity failure" in r.getMessage()]
        assert lines and all(len(line) < 2048 for line in lines)
