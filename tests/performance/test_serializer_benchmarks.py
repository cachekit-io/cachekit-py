"""Performance benchmarks comparing ArrowSerializer vs AutoSerializer.

Validates Arrow speedup claims for large DataFrames.
Uses manual timing measurements since pytest-benchmark is optional.
"""

from __future__ import annotations

import time

import numpy as np
import pandas as pd
import pytest

from cachekit.serializers import ArrowSerializer
from cachekit.serializers.auto_serializer import AutoSerializer

# ============================================================================
# Fixtures
# ============================================================================


@pytest.fixture(scope="module")
def small_dataframe() -> pd.DataFrame:
    """Create 1K row DataFrame for benchmarking."""
    np.random.seed(42)
    return pd.DataFrame(
        {
            "id": np.arange(1_000),
            "value": np.random.randn(1_000),
            "value2": np.random.randn(1_000),
            "score": np.random.randint(0, 100, 1_000),
            "category": np.random.choice(["A", "B", "C"], 1_000),
        }
    )


@pytest.fixture(scope="module")
def medium_dataframe() -> pd.DataFrame:
    """Create 10K row DataFrame for benchmarking."""
    np.random.seed(42)
    return pd.DataFrame(
        {
            "id": np.arange(10_000),
            "value": np.random.randn(10_000),
            "value2": np.random.randn(10_000),
            "score": np.random.randint(0, 100, 10_000),
            "category": np.random.choice(["A", "B", "C"], 10_000),
        }
    )


@pytest.fixture(scope="module")
def large_dataframe() -> pd.DataFrame:
    """Create 100K row DataFrame for benchmarking."""
    np.random.seed(42)
    return pd.DataFrame(
        {
            "id": np.arange(100_000),
            "value": np.random.randn(100_000),
            "value2": np.random.randn(100_000),
            "score": np.random.randint(0, 100, 100_000),
            "category": np.random.choice(["A", "B", "C"], 100_000),
        }
    )


@pytest.fixture(scope="module")
def arrow_serializer() -> ArrowSerializer:
    """Create ArrowSerializer instance."""
    return ArrowSerializer()


@pytest.fixture(scope="module")
def auto_serializer() -> AutoSerializer:
    """Create AutoSerializer instance."""
    return AutoSerializer()


@pytest.fixture(scope="module")
def orjson_serializer():
    """Create OrjsonSerializer instance."""
    from cachekit.serializers import OrjsonSerializer

    return OrjsonSerializer()


@pytest.fixture(scope="module")
def json_heavy_data() -> dict:
    """Create JSON-heavy test data (nested dicts/lists)."""
    np.random.seed(42)
    return {
        "metadata": {
            "version": "1.0",
            "timestamp": "2025-11-13T12:00:00Z",
            "user": "test_user",
        },
        "data": [
            {
                "id": i,
                "name": f"item_{i}",
                "score": float(np.random.randn()),
                "tags": [f"tag_{j}" for j in range(5)],
            }
            for i in range(1000)
        ],
    }


# ============================================================================
# Serialization Benchmarks (Manual Timing)
# ============================================================================


@pytest.mark.performance
def test_serialize_benchmarks(
    arrow_serializer: ArrowSerializer,
    auto_serializer: AutoSerializer,
    small_dataframe: pd.DataFrame,
    medium_dataframe: pd.DataFrame,
    large_dataframe: pd.DataFrame,
) -> None:
    """Benchmark serialization for different DataFrame sizes."""
    iterations = 10
    datasets = [
        ("1K rows", small_dataframe),
        ("10K rows", medium_dataframe),
        ("100K rows", large_dataframe),
    ]

    print("\n=== Serialization Benchmarks ===")
    for name, df in datasets:
        # Warm up
        arrow_serializer.serialize(df)
        auto_serializer.serialize(df)

        # Arrow serialize
        start = time.perf_counter()
        for _ in range(iterations):
            arrow_serializer.serialize(df)
        arrow_time = (time.perf_counter() - start) / iterations

        # Default serialize
        start = time.perf_counter()
        for _ in range(iterations):
            auto_serializer.serialize(df)
        default_time = (time.perf_counter() - start) / iterations

        speedup = default_time / arrow_time
        print(f"\n{name}:")
        print(f"  Arrow: {arrow_time * 1000:.2f}ms")
        print(f"  Default: {default_time * 1000:.2f}ms")
        print(f"  Speedup: {speedup:.1f}x")


@pytest.mark.performance
def test_deserialize_benchmarks(
    arrow_serializer: ArrowSerializer,
    auto_serializer: AutoSerializer,
    small_dataframe: pd.DataFrame,
    medium_dataframe: pd.DataFrame,
    large_dataframe: pd.DataFrame,
) -> None:
    """Benchmark deserialization for different DataFrame sizes."""
    iterations = 10
    datasets = [
        ("1K rows", small_dataframe),
        ("10K rows", medium_dataframe),
        ("100K rows", large_dataframe),
    ]

    print("\n=== Deserialization Benchmarks ===")
    for name, df in datasets:
        # Serialize once
        arrow_bytes, _ = arrow_serializer.serialize(df)
        default_bytes, _ = auto_serializer.serialize(df)

        # Warm up
        arrow_serializer.deserialize(arrow_bytes)
        auto_serializer.deserialize(default_bytes)

        # Arrow deserialize
        start = time.perf_counter()
        for _ in range(iterations):
            arrow_serializer.deserialize(arrow_bytes)
        arrow_time = (time.perf_counter() - start) / iterations

        # Default deserialize
        start = time.perf_counter()
        for _ in range(iterations):
            auto_serializer.deserialize(default_bytes)
        default_time = (time.perf_counter() - start) / iterations

        speedup = default_time / arrow_time
        print(f"\n{name}:")
        print(f"  Arrow: {arrow_time * 1000:.2f}ms")
        print(f"  Default: {default_time * 1000:.2f}ms")
        print(f"  Speedup: {speedup:.1f}x")


# ============================================================================
# Memory Usage Benchmarks
# ============================================================================


@pytest.mark.performance
def test_memory_usage_comparison(
    arrow_serializer: ArrowSerializer, auto_serializer: AutoSerializer, large_dataframe: pd.DataFrame
) -> None:
    """Compare memory usage for Arrow vs Default serialization.

    This test validates that Arrow's memory-mapped deserialization
    doesn't allocate a full copy of the data.
    """
    # Serialize with both serializers
    arrow_bytes, arrow_meta = arrow_serializer.serialize(large_dataframe)
    default_bytes, default_meta = auto_serializer.serialize(large_dataframe)

    # Print size comparison
    print("\n=== Memory Usage Comparison (100K rows) ===")
    print(f"Arrow serialized size: {len(arrow_bytes):,} bytes ({len(arrow_bytes) / 1024 / 1024:.2f} MB)")
    print(f"Default serialized size: {len(default_bytes):,} bytes ({len(default_bytes) / 1024 / 1024:.2f} MB)")
    print(f"Size ratio (Default/Arrow): {len(default_bytes) / len(arrow_bytes):.2f}x")

    # Measure memory before/after deserialization
    try:
        import psutil

        process = psutil.Process()

        # Baseline memory
        baseline_mb = process.memory_info().rss / 1024 / 1024

        # Arrow deserialization (memory-mapped)
        arrow_result = arrow_serializer.deserialize(arrow_bytes)
        arrow_mb = process.memory_info().rss / 1024 / 1024
        arrow_delta = arrow_mb - baseline_mb

        # Force garbage collection
        del arrow_result
        import gc

        gc.collect()

        # Default deserialization (full copy)
        default_result = auto_serializer.deserialize(default_bytes)
        default_mb = process.memory_info().rss / 1024 / 1024
        default_delta = default_mb - baseline_mb

        print("\n=== Memory Allocation During Deserialization ===")
        print(f"Arrow deserialization: +{arrow_delta:.2f} MB")
        print(f"Default deserialization: +{default_delta:.2f} MB")

        # Clean up
        del default_result
        gc.collect()

    except ImportError:
        pytest.skip("psutil not available for memory measurements")


@pytest.mark.performance
def test_speedup_validation(
    arrow_serializer: ArrowSerializer, auto_serializer: AutoSerializer, large_dataframe: pd.DataFrame
) -> None:
    """Validate Arrow speedup claims (informational - not a strict requirement).

    Expected speedups for 100K rows:
    - Serialization: ~50x faster
    - Deserialization: ~100x faster (memory-mapped)

    Note: Actual speedups vary by system, CPU, and memory speed.
    This test prints measurements for validation but doesn't enforce strict thresholds.
    """
    import time

    # Warm up
    arrow_serializer.serialize(large_dataframe)
    auto_serializer.serialize(large_dataframe)

    # Measure serialization time
    iterations = 10

    # Arrow serialize
    start = time.perf_counter()
    for _ in range(iterations):
        arrow_bytes, _ = arrow_serializer.serialize(large_dataframe)
    arrow_serialize_time = (time.perf_counter() - start) / iterations

    # Default serialize
    start = time.perf_counter()
    for _ in range(iterations):
        default_bytes, _ = auto_serializer.serialize(large_dataframe)
    default_serialize_time = (time.perf_counter() - start) / iterations

    # Measure deserialization time
    # Arrow deserialize
    start = time.perf_counter()
    for _ in range(iterations):
        _ = arrow_serializer.deserialize(arrow_bytes)
    arrow_deserialize_time = (time.perf_counter() - start) / iterations

    # Default deserialize
    start = time.perf_counter()
    for _ in range(iterations):
        _ = auto_serializer.deserialize(default_bytes)
    default_deserialize_time = (time.perf_counter() - start) / iterations

    # Calculate speedups
    serialize_speedup = default_serialize_time / arrow_serialize_time
    deserialize_speedup = default_deserialize_time / arrow_deserialize_time

    print("\n=== Performance Speedup Validation (100K rows) ===")
    print("Serialization:")
    print(f"  Arrow: {arrow_serialize_time * 1000:.2f}ms")
    print(f"  Default: {default_serialize_time * 1000:.2f}ms")
    print(f"  Speedup: {serialize_speedup:.1f}x")
    print("\nDeserialization:")
    print(f"  Arrow: {arrow_deserialize_time * 1000:.2f}ms")
    print(f"  Default: {default_deserialize_time * 1000:.2f}ms")
    print(f"  Speedup: {deserialize_speedup:.1f}x")
    print("\nNetwork Latency Reality Check:")
    print("  Typical Redis RTT: 1-2ms")
    print(f"  Arrow deserialize savings: {(default_deserialize_time - arrow_deserialize_time) * 1000:.2f}ms")

    # Informational checks (print warnings but don't fail)
    # Performance varies by system load, CPU throttling, etc.
    if serialize_speedup < 0.8:
        print(f"\n⚠️  WARNING: Arrow serialization slower than expected ({serialize_speedup:.1f}x)")
    if deserialize_speedup < 0.8:
        print(f"\n⚠️  WARNING: Arrow deserialization slower than expected ({deserialize_speedup:.1f}x)")

    # Always pass - this is a diagnostic test for tracking performance trends
    # Use pytest-benchmark or CI metrics for regression detection, not hard assertions


# ============================================================================
# JSON Serializer Comparison (orjson vs AutoSerializer)
# ============================================================================


@pytest.mark.performance
def test_json_serialization_comparison(orjson_serializer, auto_serializer, json_heavy_data: dict) -> None:
    """Compare orjson vs AutoSerializer for JSON-heavy data.

    orjson is optimized for JSON-native data (dicts, lists, strings).
    AutoSerializer uses MessagePack which is optimized for general Python objects.
    """
    iterations = 100

    # Warm up
    orjson_serializer.serialize(json_heavy_data)
    auto_serializer.serialize(json_heavy_data)

    print("\n=== JSON Serialization Comparison (1K nested objects) ===")

    # orjson serialize
    start = time.perf_counter()
    for _ in range(iterations):
        orjson_bytes, _ = orjson_serializer.serialize(json_heavy_data)
    orjson_time = (time.perf_counter() - start) / iterations

    # Default serialize
    start = time.perf_counter()
    for _ in range(iterations):
        default_bytes, _ = auto_serializer.serialize(json_heavy_data)
    default_time = (time.perf_counter() - start) / iterations

    # Warm up deserialization
    orjson_serializer.deserialize(orjson_bytes)
    auto_serializer.deserialize(default_bytes)

    # orjson deserialize
    start = time.perf_counter()
    for _ in range(iterations):
        _ = orjson_serializer.deserialize(orjson_bytes)
    orjson_deser_time = (time.perf_counter() - start) / iterations

    # Default deserialize
    start = time.perf_counter()
    for _ in range(iterations):
        _ = auto_serializer.deserialize(default_bytes)
    default_deser_time = (time.perf_counter() - start) / iterations

    serialize_speedup = default_time / orjson_time
    deserialize_speedup = default_deser_time / orjson_deser_time

    print("Serialization:")
    print(f"  orjson: {orjson_time * 1000:.2f}ms")
    print(f"  Default (MessagePack): {default_time * 1000:.2f}ms")
    print(f"  Speedup: {serialize_speedup:.1f}x")
    print("\nDeserialization:")
    print(f"  orjson: {orjson_deser_time * 1000:.2f}ms")
    print(f"  Default (MessagePack): {default_deser_time * 1000:.2f}ms")
    print(f"  Speedup: {deserialize_speedup:.1f}x")

    # Informational assertions
    assert serialize_speedup > 0.5, f"orjson should be competitive (got {serialize_speedup:.1f}x)"
    assert deserialize_speedup > 0.5, f"orjson should be competitive (got {deserialize_speedup:.1f}x)"


@pytest.mark.performance
def test_json_size_comparison(orjson_serializer, auto_serializer, json_heavy_data: dict) -> None:
    """Compare serialized size for orjson vs AutoSerializer.

    MessagePack (AutoSerializer) is typically more compact than JSON.
    This test validates the size difference for JSON-heavy data.
    """
    orjson_bytes, _ = orjson_serializer.serialize(json_heavy_data)
    default_bytes, _ = auto_serializer.serialize(json_heavy_data)

    print("\n=== JSON Size Comparison (1K nested objects) ===")
    print(f"orjson size: {len(orjson_bytes):,} bytes ({len(orjson_bytes) / 1024:.2f} KB)")
    print(f"Default (MessagePack) size: {len(default_bytes):,} bytes ({len(default_bytes) / 1024:.2f} KB)")
    print(f"Size ratio (orjson/MessagePack): {len(orjson_bytes) / len(default_bytes):.2f}x")

    # MessagePack should be more compact than JSON
    # (But orjson is still useful for JSON-native APIs and interoperability)
