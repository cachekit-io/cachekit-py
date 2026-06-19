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
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from cachekit.serializers import AutoSerializer
from cachekit.serializers.base import SerializationError


def _no_arrow() -> AutoSerializer:
    """An AutoSerializer forced onto the columnar msgpack DataFrame path (pyarrow absent)."""
    s = AutoSerializer()
    s._arrow_serializer = None
    return s


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
