"""Round trips of each data shape through AutoSerializer, the type-preserving cache serializer (serializer="auto")."""

from datetime import datetime, timezone

import pytest

from cachekit.serializers.auto_serializer import AutoSerializer

# Requires the [data] extra — absent e.g. in the free-threaded CI lane.
np = pytest.importorskip("numpy")
pd = pytest.importorskip("pandas")


def _roundtrip(value):
    serializer = AutoSerializer()
    data, metadata = serializer.serialize(value)
    return serializer.deserialize(data, metadata)


class TestAutoSerializerPatterns:
    """Each value shape must read back with its type and contents intact."""

    @pytest.mark.parametrize("value", [None, True, False, 42, -42, 3.14, -3.14, "hello", ""])
    def test_primitive_roundtrip(self, value):
        result = _roundtrip(value)
        assert result == value
        assert type(result) is type(value)

    def test_binary_roundtrip(self):
        data = b"binary \x00 data \xff"
        result = _roundtrip(data)
        assert result == data
        assert type(result) is bytes

    @pytest.mark.parametrize(
        "arr",
        [
            np.array([1, 2, 3, 4, 5]),
            np.array([1.0, 2.0, 3.0]),
            np.array([[1, 2], [3, 4]]),
            np.array([True, False, True]),
            np.zeros((3, 4, 5)),
        ],
    )
    def test_numpy_roundtrip(self, arr):
        result = _roundtrip(arr)
        np.testing.assert_array_equal(result, arr)
        assert result.dtype == arr.dtype
        assert result.shape == arr.shape

    @pytest.mark.parametrize(
        "response",
        [
            {"status": "ok"},
            {"data": [1, 2, 3], "count": 3},
            [{"id": 1, "name": "a"}, {"id": 2, "name": "b"}],
            {"nested": {"deep": {"value": 42}}},
        ],
    )
    def test_api_response_roundtrip(self, response):
        assert _roundtrip(response) == response

    def test_dataframe_roundtrip(self):
        df = pd.DataFrame(
            {
                "int_col": [1, 2, 3],
                "float_col": [1.1, 2.2, 3.3],
                "str_col": ["a", "b", "c"],
                "bool_col": [True, False, True],
            }
        )
        pd.testing.assert_frame_equal(_roundtrip(df), df)

    def test_complex_roundtrip(self):
        obj = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        result = _roundtrip(obj)
        assert result == obj
        assert result.tzinfo == timezone.utc

        complex_data = {
            "datetime": datetime.now(),
            "nested": {"list": [1, 2, datetime.now()]},
            "tuple": (1, "two", 3.0),
            "set": {1, 2, 3},
        }
        result = _roundtrip(complex_data)
        assert result == complex_data
        assert type(result["tuple"]) is tuple
        assert type(result["set"]) is set
        assert type(result["nested"]["list"][2]) is datetime

    def test_mixed_types(self):
        data = {
            "primitives": [1, 2.0, "three", True, None],
            "numpy": np.array([1, 2, 3]).tolist(),
            "nested": {"a": 1, "b": [2, 3], "c": {"d": 4}},
        }
        assert _roundtrip(data) == data
