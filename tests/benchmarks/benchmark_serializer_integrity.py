"""Detailed performance benchmarks for serializer integrity checking.

Measures:
- Serialize latency (with Blake3 checksum)
- Deserialize latency (with Blake3 validation)
- Blake3 hashing cost breakdown
- Overhead as percentage of total operation time
- Data size impact on overhead
"""

from __future__ import annotations

import time

import pandas as pd
import pytest

from cachekit.serializers import ArrowSerializer, OrjsonSerializer


class BenchmarkOrjsonIntegrity:
    """Benchmark OrjsonSerializer with Blake3 integrity checking."""

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


class BenchmarkArrowIntegrity:
    """Benchmark ArrowSerializer with Blake3 integrity checking."""

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


class Blake3OverheadAnalysis:
    """Analyze Blake3 hashing cost independent of serialization."""

    def test_blake3_cost_breakdown(self):
        """Measure Blake3 hashing cost for various data sizes."""
        import blake3

        print("\n=== Blake3 Hashing Cost Breakdown ===\n")

        test_sizes = [
            (100, "100 bytes (tiny)"),
            (1_000, "1 KB (small)"),
            (10_000, "10 KB (medium)"),
            (100_000, "100 KB (large)"),
            (1_000_000, "1 MB (very large)"),
        ]

        for size, label in test_sizes:
            data = b"x" * size
            iterations = 100

            start = time.perf_counter()
            for _ in range(iterations):
                blake3.blake3(data).digest()
            elapsed = time.perf_counter() - start

            avg_micros = (elapsed / iterations) * 1_000_000
            overhead_pct = (32 / size) * 100

            print(f"{label:25} | {avg_micros:8.2f}µs | {overhead_pct:6.2f}% size overhead")

    def test_orjson_serialize_breakdown(self):
        """Break down OrjsonSerializer serialize cost."""
        import orjson

        from cachekit.serializers import OrjsonSerializer

        print("\n=== OrjsonSerializer Serialize Breakdown ===\n")

        serializer = OrjsonSerializer()

        # Small data
        small_data = {"key": "value", "number": 42}
        iterations = 1000

        start = time.perf_counter()
        for _ in range(iterations):
            serializer.serialize(small_data)
        elapsed_with_checksum = time.perf_counter() - start

        # Measure orjson only (approximate)
        start = time.perf_counter()
        for _ in range(iterations):
            orjson.dumps(small_data)
        elapsed_orjson = time.perf_counter() - start

        checksum_cost = (elapsed_with_checksum - elapsed_orjson) / iterations * 1_000_000
        total_cost = elapsed_with_checksum / iterations * 1_000_000

        print("Small JSON (58 bytes):")
        print(f"  orjson only:        {(elapsed_orjson / iterations * 1_000_000):8.2f}µs")
        print(f"  Blake3 checksum:    {checksum_cost:8.2f}µs")
        print(f"  Total (with envelope): {total_cost:8.2f}µs")
        print(f"  Checksum overhead:  {(checksum_cost / total_cost * 100):6.2f}%")

        # Large data
        large_data = {f"key_{i}": {"value": f"data_{i}", "count": i} for i in range(1000)}
        iterations = 100

        start = time.perf_counter()
        for _ in range(iterations):
            serializer.serialize(large_data)
        elapsed_with_checksum = time.perf_counter() - start

        start = time.perf_counter()
        for _ in range(iterations):
            orjson.dumps(large_data)
        elapsed_orjson = time.perf_counter() - start

        checksum_cost = (elapsed_with_checksum - elapsed_orjson) / iterations * 1_000_000
        total_cost = elapsed_with_checksum / iterations * 1_000_000

        print("\nLarge JSON (~30 KB):")
        print(f"  orjson only:        {(elapsed_orjson / iterations * 1_000_000):8.2f}µs")
        print(f"  Blake3 checksum:    {checksum_cost:8.2f}µs")
        print(f"  Total (with envelope): {total_cost:8.2f}µs")
        print(f"  Checksum overhead:  {(checksum_cost / total_cost * 100):6.2f}%")

    def test_arrow_serialize_breakdown(self):
        """Break down ArrowSerializer serialize cost."""
        import pyarrow as pa

        from cachekit.serializers import ArrowSerializer

        print("\n=== ArrowSerializer Serialize Breakdown ===\n")

        serializer = ArrowSerializer()

        # Small DataFrame
        df_small = pd.DataFrame({"a": range(10), "b": range(10, 20)})
        iterations = 100

        start = time.perf_counter()
        for _ in range(iterations):
            serializer.serialize(df_small)
        elapsed_with_checksum = time.perf_counter() - start

        # Approximate Arrow IPC cost (simple bench)
        table = pa.Table.from_pandas(df_small)
        sink = pa.BufferOutputStream()
        start = time.perf_counter()
        for _ in range(iterations):
            writer = pa.ipc.new_stream(sink, table.schema)
            writer.write(table)
        elapsed_arrow = time.perf_counter() - start

        checksum_cost = (elapsed_with_checksum - elapsed_arrow) / iterations * 1_000_000
        total_cost = elapsed_with_checksum / iterations * 1_000_000

        print("Small DataFrame (10 rows, 2 cols):")
        print(f"  Arrow IPC only:     {(elapsed_arrow / iterations * 1_000_000):8.2f}µs")
        print(f"  Blake3 checksum:    {checksum_cost:8.2f}µs")
        print(f"  Total (with envelope): {total_cost:8.2f}µs")
        if total_cost > 0:
            print(f"  Checksum overhead:  {(checksum_cost / total_cost * 100):6.2f}%")

        # Large DataFrame
        df_large = pd.DataFrame(
            {
                "a": range(10000),
                "b": range(10000, 20000),
                "c": [f"row_{i}" for i in range(10000)],
            }
        )
        iterations = 10

        start = time.perf_counter()
        for _ in range(iterations):
            serializer.serialize(df_large)
        elapsed_with_checksum = time.perf_counter() - start

        print("\nLarge DataFrame (10K rows, 3 cols):")
        print(f"  Total (with checksum): {(elapsed_with_checksum / iterations * 1_000_000):8.2f}µs")
        print(f"  Per-row cost:          {((elapsed_with_checksum / iterations) / 10000 * 1_000_000):8.2f}µs")
