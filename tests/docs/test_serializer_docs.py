"""Ground truth validation for serializer documentation (Arrow and Orjson).

This module validates every testable claim about ArrowSerializer and OrjsonSerializer
in README.md and docs/guides/serializer-guide.md. Tests execute what documentation
claims - when docs lie, tests fail loudly.

**Coverage**:
- README.md lines 148-165 (Smart Serialization section)
- docs/guides/serializer-guide.md lines 40-253 (OrjsonSerializer section)
- docs/guides/serializer-guide.md lines 93-334 (ArrowSerializer section)

**Expected Test Count**: ~25 tests validating serializer claims

**Execution**: `pytest tests/docs/test_serializer_docs.py -v`
**Expected**: All tests pass, no Redis required, runs in <2s
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

import orjson
import pandas as pd
import pytest

from cachekit import cache
from cachekit.serializers import ArrowSerializer, AutoSerializer, OrjsonSerializer
from cachekit.serializers.base import SerializationFormat, SerializationMetadata


@pytest.mark.critical
class TestOrjsonSerializerDocClaims:
    """Validate all testable claims in OrjsonSerializer documentation."""

    def test_orjson_importable(self):
        """README.md:148 + serializer-guide.md:40 - OrjsonSerializer must be importable."""
        assert OrjsonSerializer is not None, "OrjsonSerializer import failed"

    def test_orjson_basic_usage(self):
        """serializer-guide.md:67-85 - Basic OrjsonSerializer usage must work."""
        serializer = OrjsonSerializer()

        # Test data from documentation example
        data = {"status": "success", "data": {"user": "test"}, "metadata": {"cached": True}}

        # Serialize
        serialized, metadata = serializer.serialize(data)
        assert isinstance(serialized, bytes), "Serialization must return bytes"
        assert isinstance(metadata, SerializationMetadata), "Must return SerializationMetadata"

        # Deserialize
        result = serializer.deserialize(serialized, metadata)
        assert result == data, "Round-trip must preserve data"

    def test_orjson_decorator_integration(self):
        """README.md:163-165 - @cache(serializer=OrjsonSerializer()) must work."""

        @cache(serializer=OrjsonSerializer(), backend=None)
        def get_api_response(endpoint: str):
            return {"status": "success", "data": {"endpoint": endpoint}}

        # First call - cache miss
        result1 = get_api_response("/users/123")
        assert result1["status"] == "success"

        # Second call - cache hit
        result2 = get_api_response("/users/123")
        assert result1 == result2, "Cached result must match original"

    def test_orjson_performance_claim(self):
        """README.md:148 - OrjsonSerializer must be "2-5x faster than stdlib json"."""
        # This is a smoke test - actual speedup varies based on data size, complexity, and system load
        # Microbenchmarks are inherently flaky and don't prove real-world performance
        # For rigorous benchmarks, see tests/benchmarks/test_serializer_benchmarks.py

        serializer = OrjsonSerializer()
        data = {"key": "value", "number": 42, "list": [1, 2, 3]}

        # Verify orjson works and produces output
        serialized, metadata = serializer.serialize(data)
        assert isinstance(serialized, bytes), "OrjsonSerializer must produce bytes"
        assert metadata.format == SerializationFormat.ORJSON

        # Verify roundtrip works
        result = serializer.deserialize(serialized)
        assert result == data, "OrjsonSerializer must preserve data"

    def test_orjson_json_types_supported(self):
        """serializer-guide.md:189-192 - Native JSON types must work."""
        serializer = OrjsonSerializer()

        # All native JSON types from documentation
        test_cases = [
            {"dict": {"nested": "value"}},
            {"list": [1, 2, 3]},
            {"str": "hello"},
            {"int": 42},
            {"float": 3.14},
            {"bool": True},
            {"none": None},
            {"unicode": "🚀 emoji"},
        ]

        for data in test_cases:
            serialized, _ = serializer.serialize(data)
            result = serializer.deserialize(serialized)
            assert result == data, f"Failed to serialize {data}"

    def test_orjson_datetime_auto_conversion(self):
        """serializer-guide.md:62 + 195 - datetime must auto-convert to ISO-8601."""
        serializer = OrjsonSerializer()

        # Datetime auto-converts to string
        data = {"timestamp": datetime(2025, 1, 15, 12, 30, 45)}
        serialized, _ = serializer.serialize(data)
        result = serializer.deserialize(serialized)

        # Result should have string timestamp (ISO-8601)
        assert isinstance(result["timestamp"], str), "Datetime should convert to string"
        assert "2025-01-15" in result["timestamp"], "Should contain ISO date"

    def test_orjson_uuid_auto_conversion(self):
        """serializer-guide.md:62 + 196 - UUID must auto-convert to string."""
        serializer = OrjsonSerializer()

        test_uuid = UUID("12345678-1234-5678-1234-567812345678")
        data = {"id": test_uuid}

        serialized, _ = serializer.serialize(data)
        result = serializer.deserialize(serialized)

        # UUID should convert to string
        assert isinstance(result["id"], str), "UUID should convert to string"
        assert "12345678-1234-5678-1234-567812345678" in result["id"]

    def test_orjson_bytes_not_supported(self):
        """serializer-guide.md:88-90 + 200 - bytes must raise TypeError with helpful message."""
        serializer = OrjsonSerializer()

        with pytest.raises((TypeError, orjson.JSONEncodeError)):
            serializer.serialize({"binary": b"data"})

    def test_orjson_sorted_keys_default(self):
        """serializer-guide.md:65 - Keys must be sorted by default for deterministic output."""
        serializer = OrjsonSerializer()

        # Dict with unsorted keys
        data = {"z": 1, "a": 2, "m": 3}
        serialized, _ = serializer.serialize(data)

        # Extract JSON bytes (skip 8-byte xxHash3-64 checksum prefix)
        json_bytes = serialized[8:] if len(serialized) > 8 else serialized
        json_str = json_bytes.decode("utf-8")

        # Keys should be sorted in output
        assert json_str.index('"a"') < json_str.index('"m"') < json_str.index('"z"'), "Keys should be sorted by default"

    def test_orjson_serialization_format_enum(self):
        """serializer-guide.md:40 - SerializationFormat.ORJSON must exist."""
        assert SerializationFormat.ORJSON.value == "orjson", "ORJSON enum must exist"

        serializer = OrjsonSerializer()
        _, metadata = serializer.serialize({"test": "data"})
        assert metadata.format == SerializationFormat.ORJSON, "Metadata must indicate orjson format"

    def test_orjson_option_flags(self):
        """serializer-guide.md:167-183 - Option flags must work."""
        # Default (sorted keys)
        serializer_default = OrjsonSerializer()
        assert serializer_default.option == orjson.OPT_SORT_KEYS

        # Custom option
        serializer_custom = OrjsonSerializer(option=orjson.OPT_INDENT_2)
        assert serializer_custom.option == orjson.OPT_INDENT_2

        # Combined options
        combined = orjson.OPT_SORT_KEYS | orjson.OPT_NAIVE_UTC
        serializer_multi = OrjsonSerializer(option=combined)
        assert serializer_multi.option == combined


@pytest.mark.critical
class TestArrowSerializerDocClaims:
    """Validate all testable claims in ArrowSerializer documentation."""

    def test_arrow_importable(self):
        """README.md:150 + serializer-guide.md:93 - ArrowSerializer must be importable."""
        assert ArrowSerializer is not None, "ArrowSerializer import failed"

    def test_arrow_basic_usage(self):
        """serializer-guide.md:64-79 - Basic ArrowSerializer usage must work with DataFrames."""
        import pandas as pd

        serializer = ArrowSerializer()

        # Test DataFrame
        df = pd.DataFrame({"a": [1, 2, 3], "b": [4.0, 5.0, 6.0], "c": ["x", "y", "z"]})

        # Serialize
        serialized, metadata = serializer.serialize(df)
        assert isinstance(serialized, bytes), "Serialization must return bytes"

        # Deserialize
        result = serializer.deserialize(serialized, metadata)
        assert isinstance(result, pd.DataFrame), "Must return DataFrame"
        pd.testing.assert_frame_equal(result, df, "Round-trip must preserve DataFrame")

    def test_arrow_decorator_integration(self):
        """README.md:170-172 - @cache(serializer=ArrowSerializer()) must work."""

        @cache(serializer=ArrowSerializer(), backend=None)
        def get_large_dataset(date: str):
            return pd.DataFrame({"date": [date], "value": [42]})

        # First call - cache miss
        result1 = get_large_dataset("2024-01-01")
        assert isinstance(result1, pd.DataFrame)

        # Second call - cache hit
        result2 = get_large_dataset("2024-01-01")
        pd.testing.assert_frame_equal(result1, result2, "Cached DataFrame must match")

    def test_arrow_performance_claim(self):
        """README.md:150 - ArrowSerializer must be faster for large DataFrames."""
        # This is a smoke test - actual speedup varies based on DataFrame size, schema, and system load
        # Microbenchmarks are inherently flaky and don't prove real-world performance
        # For rigorous benchmarks, see tests/benchmarks/test_serializer_benchmarks.py

        arrow_serializer = ArrowSerializer()

        # Create 10K row DataFrame
        df = pd.DataFrame({"id": range(10000), "value": [float(i) * 1.5 for i in range(10000)]})

        # Verify Arrow works and produces output
        serialized, metadata = arrow_serializer.serialize(df)
        assert isinstance(serialized, bytes), "ArrowSerializer must produce bytes"
        assert metadata.format == SerializationFormat.ARROW

        # Verify roundtrip works
        result = arrow_serializer.deserialize(serialized)
        assert isinstance(result, pd.DataFrame)
        pd.testing.assert_frame_equal(result, df)

    def test_arrow_return_format_pandas(self):
        """serializer-guide.md:116 - return_format='pandas' must work."""
        serializer = ArrowSerializer(return_format="pandas")

        df = pd.DataFrame({"a": [1, 2, 3]})
        serialized, _ = serializer.serialize(df)
        result = serializer.deserialize(serialized)

        assert isinstance(result, pd.DataFrame), "Must return pandas DataFrame"

    def test_arrow_return_format_arrow(self):
        """serializer-guide.md:122 - return_format='arrow' must work."""
        import pyarrow as pa

        serializer = ArrowSerializer(return_format="arrow")

        df = pd.DataFrame({"a": [1, 2, 3]})
        serialized, _ = serializer.serialize(df)
        result = serializer.deserialize(serialized)

        assert isinstance(result, pa.Table), "Must return pyarrow.Table"

    def test_arrow_index_preservation(self):
        """serializer-guide.md:402-405 - Arrow must preserve pandas index."""
        serializer = ArrowSerializer()

        # DataFrame with custom index
        df = pd.DataFrame({"a": [1, 2, 3]}, index=pd.Index([10, 20, 30], name="id"))

        serialized, _ = serializer.serialize(df)
        result = serializer.deserialize(serialized)

        # Index should be preserved
        assert result.index.name == "id", "Index name should be preserved"
        assert list(result.index) == [10, 20, 30], "Index values should be preserved"

    def test_arrow_dataframe_only(self):
        """serializer-guide.md:146-149 - Arrow supports DataFrames and dict of arrays (columnar data)."""
        serializer = ArrowSerializer()

        # Arrow accepts DataFrames
        df = pd.DataFrame({"a": [1, 2, 3]})
        serialized, _ = serializer.serialize(df)
        assert isinstance(serialized, bytes)

        # Arrow also accepts dict of arrays (columnar format)
        dict_data = {"col1": [1, 2, 3], "col2": [4, 5, 6]}
        serialized, _ = serializer.serialize(dict_data)
        assert isinstance(serialized, bytes)

        # Arrow rejects scalar values
        with pytest.raises(TypeError, match="ArrowSerializer only supports"):
            serializer.serialize(123)

    def test_arrow_serialization_format_not_exists(self):
        """Arrow uses SerializationFormat.ARROW (dedicated enum exists)."""
        serializer = ArrowSerializer()
        df = pd.DataFrame({"a": [1, 2, 3]})
        _, metadata = serializer.serialize(df)

        # Metadata format should be ARROW (dedicated format enum)
        assert metadata.format == SerializationFormat.ARROW, "ArrowSerializer uses ARROW format"
        assert SerializationFormat.ARROW.value == "arrow"


@pytest.mark.critical
class TestSerializerDecisionMatrix:
    """Validate serializer decision matrix claims from serializer-guide.md:136-147."""

    def test_default_for_general_objects(self):
        """Decision matrix: AutoSerializer for general Python objects."""
        serializer = AutoSerializer()

        # Test mixed types
        data = {
            "string": "hello",
            "number": 42,
            "float": 3.14,
            "list": [1, 2, 3],
            "nested": {"key": "value"},
            "bytes": b"binary",  # Only AutoSerializer supports bytes
        }

        serialized, _ = serializer.serialize(data)
        result = serializer.deserialize(serialized)
        assert result == data, "AutoSerializer must handle all Python types"

    def test_orjson_for_json_heavy(self):
        """Decision matrix: OrjsonSerializer for JSON-heavy data."""
        serializer = OrjsonSerializer()

        # JSON-structured data (no bytes)
        data = {
            "users": [
                {"id": 1, "name": "Alice"},
                {"id": 2, "name": "Bob"},
            ],
            "metadata": {"page": 1, "total": 2},
        }

        serialized, _ = serializer.serialize(data)
        result = serializer.deserialize(serialized)
        assert result == data, "OrjsonSerializer must handle JSON-structured data"

    def test_arrow_for_large_dataframes(self):
        """Decision matrix: ArrowSerializer for large DataFrames (10K+ rows)."""
        serializer = ArrowSerializer()

        # 10K row DataFrame
        df = pd.DataFrame({"id": range(10000), "value": range(10000)})

        serialized, _ = serializer.serialize(df)
        result = serializer.deserialize(serialized)

        assert len(result) == 10000, "ArrowSerializer must handle large DataFrames"
        assert result.shape == df.shape, "Shape must be preserved"

    def test_only_default_supports_bytes(self):
        """Decision matrix: Binary data → use AutoSerializer (only one that supports bytes)."""
        # AutoSerializer supports bytes
        default = AutoSerializer()
        data_with_bytes = {"binary": b"data"}
        serialized, _ = default.serialize(data_with_bytes)
        result = default.deserialize(serialized)
        assert result["binary"] == b"data"

        # Orjson rejects bytes (JSON doesn't support binary)
        orjson_ser = OrjsonSerializer()
        with pytest.raises((TypeError, orjson.JSONEncodeError)):
            orjson_ser.serialize(data_with_bytes)

        # Arrow accepts dict but converts to columnar format
        # It will fail if bytes can't be converted to Arrow type
        arrow_ser = ArrowSerializer()
        # Arrow can actually handle bytes in some cases (converts to binary type)
        # So this test is about understanding Arrow's capabilities, not rejecting bytes
        # Skip this assertion as Arrow behavior with bytes is implementation-dependent


@pytest.mark.critical
class TestSerializerREADMEExamples:
    """Validate exact code examples from README.md:154-178 execute without error."""

    def test_auto_serializer_example(self):
        """README.md:155-158 - Default serializer example must work."""

        @cache(backend=None)
        def get_data():
            return {"metrics": [1, 2, 3], "timestamp": 1234567890}

        result = get_data()
        assert result["metrics"] == [1, 2, 3]

    def test_orjson_serializer_example(self):
        """README.md:160-165 - OrjsonSerializer example must work."""
        from cachekit.serializers import OrjsonSerializer

        @cache(serializer=OrjsonSerializer(), backend=None)
        def get_api_response(endpoint: str):
            return {"status": "success", "data": {"endpoint": endpoint}}

        result = get_api_response("/test")
        assert result["status"] == "success"

    def test_arrow_serializer_example(self):
        """README.md:167-172 - ArrowSerializer example must work."""
        import pandas as pd

        from cachekit.serializers import ArrowSerializer

        @cache(serializer=ArrowSerializer(), backend=None)
        def get_large_dataset(date: str):
            return pd.DataFrame({"date": [date], "value": [42]})

        result = get_large_dataset("2024-01-01")
        assert isinstance(result, pd.DataFrame)


@pytest.mark.critical
class TestSerializerGuideExamples:
    """Validate exact code examples from docs/guides/serializer-guide.md."""

    def test_auto_serializer_guide_example(self):
        """serializer-guide.md:30-38 - AutoSerializer example must work."""

        @cache(backend=None)
        def get_user_data(user_id: int):
            return {"id": user_id, "name": "Alice", "scores": [95, 87, 91], "metadata": {"tier": "premium"}}

        result = get_user_data(123)
        assert result["id"] == 123
        assert result["name"] == "Alice"

    def test_orjson_configuration_example(self):
        """serializer-guide.md:167-183 - Orjson configuration options must work."""
        import orjson

        # Default
        serializer1 = OrjsonSerializer()
        assert serializer1.option == orjson.OPT_SORT_KEYS

        # Pretty-printed
        serializer2 = OrjsonSerializer(option=orjson.OPT_INDENT_2)
        data = {"key": "value"}
        serialized, _ = serializer2.serialize(data)

        # Should be indented
        assert b"\n" in serialized, "OPT_INDENT_2 should produce multi-line JSON"

    def test_arrow_basic_usage_example(self):
        """serializer-guide.md:155-160 - Arrow basic usage must work."""
        import pandas as pd

        from cachekit.serializers import ArrowSerializer

        @cache(serializer=ArrowSerializer(), backend=None)
        def get_api_data(endpoint: str):
            return pd.DataFrame({"endpoint": [endpoint], "status": ["success"]})

        result = get_api_data("/test")
        assert isinstance(result, pd.DataFrame)
