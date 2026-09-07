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

import msgpack
import numpy as np
import pandas as pd
import pytest

from cachekit._rust_serializer import ByteStorage
from cachekit.serializers import AutoSerializer
from cachekit.serializers.base import SerializationError


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


@pytest.mark.unit
class TestForgedColumnarPayloadIsRefused:
    """Forged DataFrame/Series documents are refused before pandas sees them (LAB-2503): a numeric
    column dtype the writer never emits (``M8[0ns]`` passes ``np.frombuffer`` and then kills the
    process with SIGFPE inside pandas — uncatchable), and an ndarray smuggled via the ``__ndarray__``
    hook into a field the writer only ever fills with a list or a dict.
    """

    @pytest.mark.parametrize("dtype", ["M8[0ns]", "m8[0ns]", "U4"])
    @pytest.mark.parametrize("kind", ["dataframe", "series"])
    def test_forged_column_dtype_is_a_serialization_error(self, kind: str, dtype: str) -> None:
        column = {"type": "numeric", "data": b"\x00" * 8, "dtype": dtype}
        body = (
            {"columns": ["x"], "index": None, "data": {"x": column}}
            if kind == "dataframe"
            else {"name": None, "index": None, **column}
        )
        entry = bytes(ByteStorage("msgpack").store(msgpack.packb(body), kind))
        with pytest.raises(SerializationError, match="Forged columnar dtype"):
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
        entry = bytes(ByteStorage("msgpack").store(msgpack.packb(body), kind))
        with pytest.raises(SerializationError, match="Forged columnar payload"):
            AutoSerializer().deserialize(entry)
