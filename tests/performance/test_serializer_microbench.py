"""Serializer micro-benchmarks (pytest-benchmark, baseline-tracked).

Distribution-level latency for serialize / deserialize / roundtrip across JSON
(OrjsonSerializer) and DataFrame (ArrowSerializer) payloads, with the serializers'
built-in integrity checksums. Selected by ``--benchmark-only`` (``make benchmark``);
skipped in the normal suite via the ``--benchmark-skip`` default in pyproject.toml.
"""

from __future__ import annotations

from collections.abc import Iterator

import pandas as pd
import pytest

from cachekit.serializers import ArrowSerializer, OrjsonSerializer


@pytest.fixture(autouse=True)
def setup_di_for_redis_isolation() -> Iterator[None]:
    """Serializer benchmarks need no backend — override the root autouse Redis fixture.

    File-scoped (not a folder conftest): tests/performance/ also holds Redis-dependent
    tests, so the override must stay local to this file.
    """
    yield


class TestOrjsonSerializerBench:
    """Benchmark OrjsonSerializer with integrity checking."""

    @pytest.mark.benchmark
    def test_serialize_small_json(self, benchmark):
        """Small JSON (100 bytes) serialize time."""
        serializer = OrjsonSerializer()
        data = {"key": "value", "number": 42}

        result = benchmark(serializer.serialize, data)
        assert len(result[0]) > 0

    @pytest.mark.benchmark
    def test_serialize_medium_json(self, benchmark):
        """Medium JSON (10KB) serialize time."""
        serializer = OrjsonSerializer()
        data = {f"key_{i}": {"value": f"data_{i}", "count": i} for i in range(100)}

        result = benchmark(serializer.serialize, data)
        assert len(result[0]) > 0

    @pytest.mark.benchmark
    def test_serialize_large_json(self, benchmark):
        """Large JSON (100KB) serialize time."""
        serializer = OrjsonSerializer()
        data = {f"key_{i}": {"value": f"data_{i}", "count": i} for i in range(1000)}

        result = benchmark(serializer.serialize, data)
        assert len(result[0]) > 0

    @pytest.mark.benchmark
    def test_deserialize_small_json(self, benchmark):
        """Small JSON deserialize time (with checksum validation)."""
        serializer = OrjsonSerializer()
        data_obj = {"key": "value", "number": 42}
        serialized, metadata = serializer.serialize(data_obj)

        result = benchmark(serializer.deserialize, serialized, metadata)
        assert result == data_obj

    @pytest.mark.benchmark
    def test_deserialize_medium_json(self, benchmark):
        """Medium JSON deserialize time (with checksum validation)."""
        serializer = OrjsonSerializer()
        data_obj = {f"key_{i}": {"value": f"data_{i}", "count": i} for i in range(100)}
        serialized, metadata = serializer.serialize(data_obj)

        result = benchmark(serializer.deserialize, serialized, metadata)
        assert result == data_obj

    @pytest.mark.benchmark
    def test_deserialize_large_json(self, benchmark):
        """Large JSON deserialize time (with checksum validation)."""
        serializer = OrjsonSerializer()
        data_obj = {f"key_{i}": {"value": f"data_{i}", "count": i} for i in range(1000)}
        serialized, metadata = serializer.serialize(data_obj)

        result = benchmark(serializer.deserialize, serialized, metadata)
        assert result == data_obj

    @pytest.mark.benchmark
    def test_roundtrip_small_json(self, benchmark):
        """Full roundtrip (serialize + deserialize) for small JSON."""
        serializer = OrjsonSerializer()
        data = {"key": "value", "number": 42}

        def roundtrip():
            serialized, metadata = serializer.serialize(data)
            return serializer.deserialize(serialized, metadata)

        result = benchmark(roundtrip)
        assert result == data

    @pytest.mark.benchmark
    def test_roundtrip_large_json(self, benchmark):
        """Full roundtrip (serialize + deserialize) for large JSON."""
        serializer = OrjsonSerializer()
        data = {f"key_{i}": {"value": f"data_{i}", "count": i} for i in range(1000)}

        def roundtrip():
            serialized, metadata = serializer.serialize(data)
            return serializer.deserialize(serialized, metadata)

        result = benchmark(roundtrip)
        assert result == data


class TestArrowSerializerBench:
    """Benchmark ArrowSerializer with integrity checking."""

    @pytest.mark.benchmark
    def test_serialize_small_dataframe(self, benchmark):
        """Small DataFrame (10 rows) serialize time."""
        serializer = ArrowSerializer()
        df = pd.DataFrame({"a": range(10), "b": range(10, 20), "c": [f"row_{i}" for i in range(10)]})

        result = benchmark(serializer.serialize, df)
        assert len(result[0]) > 0

    @pytest.mark.benchmark
    def test_serialize_medium_dataframe(self, benchmark):
        """Medium DataFrame (1K rows) serialize time."""
        serializer = ArrowSerializer()
        df = pd.DataFrame(
            {
                "a": range(1000),
                "b": range(1000, 2000),
                "c": [f"row_{i}" for i in range(1000)],
                "d": [i * 1.5 for i in range(1000)],
            }
        )

        result = benchmark(serializer.serialize, df)
        assert len(result[0]) > 0

    @pytest.mark.benchmark
    def test_serialize_large_dataframe(self, benchmark):
        """Large DataFrame (100K rows) serialize time."""
        serializer = ArrowSerializer()
        df = pd.DataFrame(
            {
                "a": range(100000),
                "b": range(100000, 200000),
                "c": [f"row_{i}" for i in range(100000)],
            }
        )

        result = benchmark(serializer.serialize, df)
        assert len(result[0]) > 0

    @pytest.mark.benchmark
    def test_deserialize_small_dataframe(self, benchmark):
        """Small DataFrame deserialize time (with checksum validation)."""
        serializer = ArrowSerializer()
        df = pd.DataFrame({"a": range(10), "b": range(10, 20)})
        serialized, metadata = serializer.serialize(df)

        def deserialize():
            result = serializer.deserialize(serialized, metadata)
            return result

        result = benchmark(deserialize)
        assert len(result) == 10

    @pytest.mark.benchmark
    def test_deserialize_medium_dataframe(self, benchmark):
        """Medium DataFrame deserialize time (with checksum validation)."""
        serializer = ArrowSerializer()
        df = pd.DataFrame({"a": range(1000), "b": range(1000, 2000), "c": [f"row_{i}" for i in range(1000)]})
        serialized, metadata = serializer.serialize(df)

        def deserialize():
            result = serializer.deserialize(serialized, metadata)
            return result

        result = benchmark(deserialize)
        assert len(result) == 1000

    @pytest.mark.benchmark
    def test_deserialize_large_dataframe(self, benchmark):
        """Large DataFrame deserialize time (with checksum validation)."""
        serializer = ArrowSerializer()
        df = pd.DataFrame(
            {
                "a": range(100000),
                "b": range(100000, 200000),
                "c": [f"row_{i}" for i in range(100000)],
            }
        )
        serialized, metadata = serializer.serialize(df)

        def deserialize():
            result = serializer.deserialize(serialized, metadata)
            return result

        result = benchmark(deserialize)
        assert len(result) == 100000

    @pytest.mark.benchmark
    def test_roundtrip_small_dataframe(self, benchmark):
        """Full roundtrip (serialize + deserialize) for small DataFrame."""
        serializer = ArrowSerializer()
        df = pd.DataFrame({"a": range(10), "b": range(10, 20)})

        def roundtrip():
            serialized, metadata = serializer.serialize(df)
            return serializer.deserialize(serialized, metadata)

        result = benchmark(roundtrip)
        assert len(result) == 10

    @pytest.mark.benchmark
    def test_roundtrip_large_dataframe(self, benchmark):
        """Full roundtrip (serialize + deserialize) for large DataFrame."""
        serializer = ArrowSerializer()
        df = pd.DataFrame(
            {
                "a": range(10000),
                "b": range(10000, 20000),
                "c": [f"row_{i}" for i in range(10000)],
            }
        )

        def roundtrip():
            serialized, metadata = serializer.serialize(df)
            return serializer.deserialize(serialized, metadata)

        result = benchmark(roundtrip)
        assert len(result) == 10000
