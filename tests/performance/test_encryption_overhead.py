"""Encryption overhead benchmarks for zero-knowledge caching.

Measures the cost of client-side AES-256-GCM encryption across different serializers:
- StandardSerializer (MessagePack)
- OrjsonSerializer (JSON)
- ArrowSerializer (DataFrames)

Critical for zero-knowledge architecture where client-side encryption is mandatory.

Each test asserts what it timed: the round trip returns the data, and the encrypted payload does
not carry the plaintext.
"""

from __future__ import annotations

import secrets
import time
from typing import Any

import pytest

from cachekit.serializers import EncryptionWrapper, StandardSerializer

from .stats_utils import percentile


def benchmark_serializer(
    serializer, data: Any, iterations: int = 10_000, cache_key: str = "bench:encryption"
) -> dict[str, float]:
    """Benchmark serializer roundtrip performance.

    Args:
        serializer: Serializer to benchmark
        data: Data to serialize
        iterations: Number of iterations
        cache_key: Cache key for encryption AAD binding (only used for EncryptionWrapper)

    Returns:
        Dict with mean and p95 latencies in nanoseconds, plus the last round trip's
        serialized payload and deserialized result for the caller to check
    """
    latencies = []

    # Only pass cache_key for EncryptionWrapper (plain serializers don't accept it)
    is_encryption_wrapper = isinstance(serializer, EncryptionWrapper)

    # Warmup
    for _ in range(100):
        if is_encryption_wrapper:
            serialized, metadata = serializer.serialize(data, cache_key=cache_key)
            serializer.deserialize(serialized, metadata, cache_key=cache_key)
        else:
            serialized, metadata = serializer.serialize(data)
            serializer.deserialize(serialized, metadata)

    # Benchmark
    for _ in range(iterations):
        start = time.perf_counter_ns()
        if is_encryption_wrapper:
            serialized, metadata = serializer.serialize(data, cache_key=cache_key)
            result = serializer.deserialize(serialized, metadata, cache_key=cache_key)
        else:
            serialized, metadata = serializer.serialize(data)
            result = serializer.deserialize(serialized, metadata)
        end = time.perf_counter_ns()
        latencies.append(end - start)

    mean = sum(latencies) / len(latencies)
    p95 = percentile(latencies, 95)

    return {"mean": mean, "p95": p95, "payload": serialized, "result": result}


@pytest.fixture
def master_key() -> str:
    """A throwaway 256-bit master key (hex): the tests measure the cipher, not a deployment's key."""
    return secrets.token_hex(32)


def test_encryption_overhead_json(master_key, record_property):
    """Measure encryption overhead for JSON data (OrjsonSerializer).

    Use case: API responses, webhooks, session data with PII.
    """
    pytest.importorskip("orjson")  # the [json] extra
    from cachekit.serializers import OrjsonSerializer

    # API response with sensitive data
    api_data = {
        "user": {
            "id": 12345,
            "email": "user@example.com",
            "phone": "+1-555-0100",
            "ssn": "123-45-6789",
        },
        "session": {
            "token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...",
            "api_key": "sk_live_abcdef123456",
        },
        "metadata": {
            "timestamp": 1234567890,
            "ip_address": "192.168.1.1",
        },
    }

    # Plain JSON
    plain_json = OrjsonSerializer()
    result_plain = benchmark_serializer(plain_json, api_data, iterations=10_000)

    # Encrypted JSON
    encrypted_json = EncryptionWrapper(
        serializer=OrjsonSerializer(),
        master_key=bytes.fromhex(master_key),
        tenant_id="api-tenant",
    )
    result_encrypted = benchmark_serializer(encrypted_json, api_data, iterations=10_000)

    # Calculate overhead
    overhead_ns = result_encrypted["p95"] - result_plain["p95"]
    overhead_pct = (overhead_ns / result_plain["p95"]) * 100

    print("\n" + "=" * 80)
    print("ENCRYPTION OVERHEAD: JSON (OrjsonSerializer)")
    print("=" * 80)
    print(f"\nData: API response with PII ({len(str(api_data))} chars)")
    print("\nPlain JSON:")
    print(f"  Mean:   {result_plain['mean']:>10.0f} ns ({result_plain['mean'] / 1000:>6.2f} μs)")
    print(f"  P95:    {result_plain['p95']:>10.0f} ns ({result_plain['p95'] / 1000:>6.2f} μs)")
    print("\nEncrypted JSON (AES-256-GCM):")
    print(f"  Mean:   {result_encrypted['mean']:>10.0f} ns ({result_encrypted['mean'] / 1000:>6.2f} μs)")
    print(f"  P95:    {result_encrypted['p95']:>10.0f} ns ({result_encrypted['p95'] / 1000:>6.2f} μs)")
    print("\nOverhead:")
    print(f"  Absolute: {overhead_ns:>10.0f} ns ({overhead_ns / 1000:>6.2f} μs)")
    print(f"  Relative: {overhead_pct:>10.1f}%")
    print("=" * 80)

    record_property("overhead_p95_ns", overhead_ns)
    record_property("overhead_pct", round(overhead_pct, 1))

    assert result_plain["result"] == result_encrypted["result"] == api_data
    assert b"123-45-6789" in result_plain["payload"]
    assert b"123-45-6789" not in result_encrypted["payload"], "the encrypted payload carries the plaintext"


def test_encryption_overhead_msgpack(master_key, record_property):
    """Measure encryption overhead for MessagePack data (StandardSerializer).

    Use case: General Python objects, structured data with PII.
    """
    # Structured data with sensitive fields
    user_data = {
        "profile": {
            "ssn": "123-45-6789",
            "dob": "1990-01-01",
            "medical_record": "MRN-987654",
        },
        "financial": {
            "credit_card": "4532-1234-5678-9010",
            "bank_account": "123456789",
            "balance": 50000.00,
        },
        "metadata": {
            "created_at": 1234567890,
            "updated_at": 1234567900,
        },
    }

    # Plain MessagePack
    plain_msgpack = StandardSerializer()
    result_plain = benchmark_serializer(plain_msgpack, user_data, iterations=10_000)

    # Encrypted MessagePack
    encrypted_msgpack = EncryptionWrapper(
        serializer=StandardSerializer(),
        master_key=bytes.fromhex(master_key),
        tenant_id="user-tenant",
    )
    result_encrypted = benchmark_serializer(encrypted_msgpack, user_data, iterations=10_000)

    # Calculate overhead
    overhead_ns = result_encrypted["p95"] - result_plain["p95"]
    overhead_pct = (overhead_ns / result_plain["p95"]) * 100

    print("\n" + "=" * 80)
    print("ENCRYPTION OVERHEAD: MessagePack (StandardSerializer)")
    print("=" * 80)
    print(f"\nData: User profile with PII ({len(str(user_data))} chars)")
    print("\nPlain MessagePack:")
    print(f"  Mean:   {result_plain['mean']:>10.0f} ns ({result_plain['mean'] / 1000:>6.2f} μs)")
    print(f"  P95:    {result_plain['p95']:>10.0f} ns ({result_plain['p95'] / 1000:>6.2f} μs)")
    print("\nEncrypted MessagePack (AES-256-GCM):")
    print(f"  Mean:   {result_encrypted['mean']:>10.0f} ns ({result_encrypted['mean'] / 1000:>6.2f} μs)")
    print(f"  P95:    {result_encrypted['p95']:>10.0f} ns ({result_encrypted['p95'] / 1000:>6.2f} μs)")
    print("\nOverhead:")
    print(f"  Absolute: {overhead_ns:>10.0f} ns ({overhead_ns / 1000:>6.2f} μs)")
    print(f"  Relative: {overhead_pct:>10.1f}%")
    print("=" * 80)

    record_property("overhead_p95_ns", overhead_ns)
    record_property("overhead_pct", round(overhead_pct, 1))

    assert result_plain["result"] == result_encrypted["result"] == user_data
    assert b"4532-1234-5678-9010" in result_plain["payload"]
    assert b"4532-1234-5678-9010" not in result_encrypted["payload"], "the encrypted payload carries the plaintext"


def test_encryption_overhead_dataframes(master_key, record_property):
    """Measure encryption overhead for DataFrames (ArrowSerializer).

    Use case: ML features, analytics data, patient records.
    """
    pd = pytest.importorskip("pandas")  # the [data] extra
    pytest.importorskip("pyarrow")
    from cachekit.serializers import ArrowSerializer

    # Sensitive medical/ML data
    df = pd.DataFrame(
        {
            "patient_id": range(1000),
            "diagnosis": ["diabetes", "hypertension", "healthy"] * 333 + ["diabetes"],
            "risk_score": [0.8, 0.6, 0.1] * 333 + [0.8],
            "lab_result": [120.5, 140.2, 90.3] * 333 + [120.5],
            "medication": ["metformin", "lisinopril", "none"] * 333 + ["metformin"],
        }
    )

    # Plain Arrow
    plain_arrow = ArrowSerializer()
    result_plain = benchmark_serializer(plain_arrow, df, iterations=1_000)

    # Encrypted Arrow
    encrypted_arrow = EncryptionWrapper(
        serializer=ArrowSerializer(),
        master_key=bytes.fromhex(master_key),
        tenant_id="ml-tenant",
    )
    result_encrypted = benchmark_serializer(encrypted_arrow, df, iterations=1_000)

    # Calculate overhead
    overhead_ns = result_encrypted["p95"] - result_plain["p95"]
    overhead_pct = (overhead_ns / result_plain["p95"]) * 100

    print("\n" + "=" * 80)
    print("ENCRYPTION OVERHEAD: DataFrames (ArrowSerializer)")
    print("=" * 80)
    print(f"\nData: Patient records DataFrame ({df.shape[0]} rows, {df.shape[1]} cols)")
    print(f"Size: {df.memory_usage(deep=True).sum() / 1024:.1f} KB")
    print("\nPlain Arrow:")
    print(f"  Mean:   {result_plain['mean']:>10.0f} ns ({result_plain['mean'] / 1000:>6.2f} μs)")
    print(f"  P95:    {result_plain['p95']:>10.0f} ns ({result_plain['p95'] / 1000:>6.2f} μs)")
    print("\nEncrypted Arrow (AES-256-GCM):")
    print(f"  Mean:   {result_encrypted['mean']:>10.0f} ns ({result_encrypted['mean'] / 1000:>6.2f} μs)")
    print(f"  P95:    {result_encrypted['p95']:>10.0f} ns ({result_encrypted['p95'] / 1000:>6.2f} μs)")
    print("\nOverhead:")
    print(f"  Absolute: {overhead_ns:>10.0f} ns ({overhead_ns / 1000:>6.2f} μs)")
    print(f"  Relative: {overhead_pct:>10.1f}%")
    print("=" * 80)

    record_property("overhead_p95_ns", overhead_ns)
    record_property("overhead_pct", round(overhead_pct, 1))

    pd.testing.assert_frame_equal(result_plain["result"], df)
    pd.testing.assert_frame_equal(result_encrypted["result"], df)
    assert b"metformin" in result_plain["payload"]
    assert b"metformin" not in result_encrypted["payload"], "the encrypted payload carries the plaintext"
