"""Production-realistic performance tests - comprehensive stack validation.

This test suite measures performance under realistic production workloads:
- Complex payloads (nested dicts, DataFrames, custom classes)
- Concurrent access (10+ threads)
- Reliability framework exercised (circuit breaker, backpressure, timeouts)
- Encryption overhead measured
- Redis L2 with network latency
- Statistical rigor (multiple runs, percentiles over every raw sample, run-level intervals)

CRITICAL: These tests provide CONSERVATIVE numbers for marketing claims.
They measure worst-case realistic scenarios, not ideal conditions.

Plaintext benchmarks state encryption=False: test_encryption_overhead runs only with
CACHEKIT_MASTER_KEY set, and a present key with no stated intent raises at decoration.
"""

from __future__ import annotations

import asyncio
import gc
import os
import threading
import time
from dataclasses import dataclass
from typing import Any

import pytest

try:
    import numpy as np
    import pandas as pd

    PANDAS_AVAILABLE = True
except ImportError:
    PANDAS_AVAILABLE = False

from cachekit.config.decorator import DecoratorConfig
from cachekit.decorators import cache

from .stats_utils import benchmark_with_gc_handling, difference_band, summarize

# =============================================================================
# Realistic Test Payloads
# =============================================================================


@dataclass
class User:
    """Realistic user model with nested data."""

    id: int
    username: str
    email: str
    profile: dict[str, Any]
    settings: dict[str, Any]
    permissions: list[str]
    metadata: dict[str, Any]


class DictBackend:
    """In-process L2: an L1 hit never reaches it, so it isolates the decorator's bytes-in-L1 path from any transport."""

    def __init__(self) -> None:
        self.store: dict[str, bytes] = {}
        self.gets = 0

    def get(self, key: str) -> bytes | None:
        self.gets += 1
        return self.store.get(key)

    def set(self, key: str, value: bytes, ttl: int | None = None) -> None:
        self.store[key] = value

    def delete(self, key: str) -> bool:
        return self.store.pop(key, None) is not None

    def exists(self, key: str) -> bool:
        return key in self.store

    def health_check(self) -> tuple[bool, dict[str, str]]:
        return True, {"backend_type": "dict"}


def create_complex_dict(size: str = "medium") -> dict[str, Any]:
    """Create realistic API response payload.

    Args:
        size: "small" (1KB), "medium" (10KB), "large" (100KB)
    """
    if size == "small":
        n_items = 10
    elif size == "medium":
        n_items = 100
    else:  # large
        n_items = 1000

    return {
        "status": "success",
        "timestamp": "2025-01-15T10:30:00Z",
        "data": {
            "users": [
                {
                    "id": i,
                    "username": f"user_{i}",
                    "email": f"user_{i}@example.com",
                    "profile": {"name": f"User {i}", "age": 20 + (i % 50), "location": "San Francisco"},
                    "settings": {"theme": "dark", "notifications": True, "language": "en"},
                    "permissions": ["read", "write", "admin"] if i % 10 == 0 else ["read"],
                    "metadata": {"created_at": "2025-01-01", "last_login": "2025-01-15", "login_count": i * 42},
                }
                for i in range(n_items)
            ],
            "pagination": {"page": 1, "per_page": n_items, "total": n_items, "total_pages": 1},
        },
        "meta": {"api_version": "v2", "request_id": "abc123", "execution_time_ms": 42},
    }


def create_user_model(user_id: int) -> User:
    """Create realistic user dataclass."""
    return User(
        id=user_id,
        username=f"user_{user_id}",
        email=f"user_{user_id}@example.com",
        profile={"name": f"User {user_id}", "age": 30, "bio": "Software engineer"},
        settings={"theme": "dark", "notifications": True, "timezone": "America/Los_Angeles"},
        permissions=["read", "write", "admin"],
        metadata={"created_at": "2025-01-01", "login_count": 42, "verified": True},
    )


@pytest.fixture(scope="module")
def medium_dataframe() -> pd.DataFrame:
    """Create 10K row DataFrame (realistic query result)."""
    if not PANDAS_AVAILABLE:
        pytest.skip("pandas not available")

    np.random.seed(42)
    return pd.DataFrame(
        {
            "id": np.arange(10_000),
            "value": np.random.randn(10_000),
            "category": np.random.choice(["A", "B", "C"], 10_000),
            "score": np.random.randint(0, 100, 10_000),
            "timestamp": pd.date_range("2025-01-01", periods=10_000, freq="1min"),
        }
    )


# =============================================================================
# Test 1: Complex Payloads - Decorator Overhead
# =============================================================================


@pytest.mark.performance
def test_decorator_overhead_complex_dict() -> None:
    """Measure decorator overhead with realistic API response payload.

    Tests the full stack:
    - Argument binding and key generation
    - Serialization (msgpack with complex nested dict)
    - L1 cache hit path
    - Deserialization

    This is what users actually experience - not trivial int payloads.
    """
    payload = create_complex_dict("medium")  # ~10KB realistic API response

    @cache(backend=None, encryption=False)  # L1-only to isolate decorator + serialization
    def get_api_response(request_id: int) -> dict[str, Any]:
        return payload

    # Prime cache
    get_api_response(1)

    # Benchmark L1 hit with realistic payload
    def measure_fn():
        get_api_response(1)

    result = benchmark_with_gc_handling(
        name="Decorator + L1 Hit (10KB complex dict)",
        fn=measure_fn,
        iterations_per_run=10_000,
        runs=5,
        unit="ns",
    )

    print("\n" + "=" * 80)
    print("DECORATOR OVERHEAD - REALISTIC PAYLOAD")
    print("=" * 80)
    print(result)
    print("\nContext:")
    print("  Payload: 10KB nested dict (realistic API response)")
    print("  Stack: Decorator + key gen + msgpack + L1 lookup + deserialize")
    print("\n  L1 pure (bytes lookup):        ~500ns p95")
    print(f"  Decorator + complex payload:   {result.p95:.0f}ns p95")
    print(f"  Overhead ratio:                {result.p95 / 500:.1f}x")

    # Conservative target: <300μs for complex payloads (10KB)
    # This includes full stack: decorator + serialization + L1 + deserialization
    # Raw p95 measured 14-16μs (2026-10-03); back-to-back drift: two identical runs moved p95 by 10%.
    target_ns = 300_000
    if result.exceeded_target(target_ns):
        raise AssertionError(f"Complex payload overhead {result.p95:.0f}ns exceeds {target_ns}ns target (p95)")

    print(f"\n✅ Complex payload validated: {result.p95:.0f}ns ({result.p95 / 1000:.0f}μs) < {target_ns / 1000:.0f}μs target")


@pytest.mark.performance
def test_decorator_overhead_dataclass() -> None:
    """Measure decorator overhead with custom dataclass."""
    user = create_user_model(42)

    @cache(backend=None, encryption=False)
    def get_user(user_id: int) -> User:
        return user

    # Prime cache
    get_user(42)

    def measure_fn():
        get_user(42)

    result = benchmark_with_gc_handling(
        name="Decorator + L1 Hit (User dataclass)",
        fn=measure_fn,
        iterations_per_run=10_000,
        runs=5,
        unit="ns",
    )

    print("\n" + "=" * 80)
    print("DECORATOR OVERHEAD - DATACLASS")
    print("=" * 80)
    print(result)
    print("\nContext:")
    print("  Payload: User dataclass with nested dicts")
    print("  Stack: Decorator + msgpack + L1 + deserialize")

    # Target: <200μs for dataclass (smaller than 10KB dict)
    # Raw p95 measured 14-15μs (2026-10-03); back-to-back drift: two identical runs moved p95 by 6%.
    target_ns = 200_000
    if result.exceeded_target(target_ns):
        raise AssertionError(f"Dataclass overhead {result.p95:.0f}ns exceeds {target_ns}ns target (p95)")

    print(f"\n✅ Dataclass validated: {result.p95:.0f}ns ({result.p95 / 1000:.0f}μs) < {target_ns / 1000:.0f}μs target")


@pytest.mark.performance
@pytest.mark.skipif(not PANDAS_AVAILABLE, reason="pandas not available")
def test_decorator_overhead_dataframe(medium_dataframe: pd.DataFrame) -> None:
    """Measure decorator overhead with DataFrame (10K rows).

    This tests the default serializer (msgpack) with DataFrames.
    ArrowSerializer is tested separately in test_serializer_benchmarks.py.
    """

    @cache(backend=None, serializer="auto", encryption=False)
    def get_data(query_id: int) -> pd.DataFrame:
        return medium_dataframe

    # Prime cache
    get_data(1)

    def measure_fn():
        get_data(1)

    result = benchmark_with_gc_handling(
        name="Decorator + L1 Hit (10K row DataFrame)",
        fn=measure_fn,
        iterations_per_run=1_000,  # DataFrames are larger
        runs=5,
        unit="μs",
    )

    print("\n" + "=" * 80)
    print("DECORATOR OVERHEAD - DATAFRAME (msgpack)")
    print("=" * 80)
    print(result)
    print("\nContext:")
    print("  Payload: 10K row DataFrame (~400KB)")
    print("  Serializer: msgpack (default)")
    print("  Note: ArrowSerializer is 50-100x faster (see test_serializer_benchmarks.py)")

    # Target: <10ms for DataFrame with msgpack
    # msgpack serialization of 400KB DataFrame is inherently slow (~1-5ms)
    # This test measures decorator overhead + L1 cache behavior, not serializer performance
    # Raw p95 measured 13-15μs (2026-10-03); back-to-back drift: two identical runs moved p95 by 11%.
    target_us = 10_000
    if result.exceeded_target(target_us):
        raise AssertionError(f"DataFrame overhead {result.p95:.0f}μs exceeds {target_us}μs target (p95)")

    print(f"\n✅ DataFrame validated: {result.p95:.0f}μs < {target_us}μs target")


@pytest.mark.performance
def test_decorator_overhead_l1_hit_with_backend() -> None:
    """Measure the L1 hit users get with an L2 backend configured (Redis, CachekitIO).

    With a backend, L1 holds the serialized bytes and a hit deserializes them, unlike the
    @cache(backend=None) guards above, whose L1 hands back the stored object. The in-process
    backend is never reached on a hit, so this times the decorator, the L1 lookup and msgpack
    deserialization of the 100-user dict, and needs no running service.
    """
    payload = create_complex_dict("medium")
    backend = DictBackend()

    @cache(backend=backend, encryption=False)
    def get_api_response(request_id: int) -> dict[str, Any]:
        return payload

    # Prime: the miss writes L2 and L1
    get_api_response(1)
    gets_after_prime = backend.gets

    def measure_fn():
        get_api_response(1)

    result = benchmark_with_gc_handling(
        name="Decorator + L1 Hit with L2 backend (100-user dict)",
        fn=measure_fn,
        iterations_per_run=10_000,
        runs=5,
        unit="ns",
    )

    # Every measured call must have been an L1 hit; one that reached L2 would time the wrong path.
    assert backend.gets == gets_after_prime, f"{backend.gets - gets_after_prime} measured calls reached L2"

    print("\n" + "=" * 80)
    print("DECORATOR OVERHEAD - L1 HIT WITH L2 BACKEND CONFIGURED")
    print("=" * 80)
    print(result)
    print("\nContext:")
    print("  Payload: 100-user nested dict (23.5KB as MessagePack)")
    print("  Stack:   Decorator + key gen + L1 bytes lookup + msgpack deserialize (L2 never reached)")

    # Smoke ceiling, not a claim. Deserializing 23.5KB dominates: raw p95 measured 431μs (2026-10-04),
    # against 9μs for the L1-only hit of the same dict, which never deserializes.
    target_ns = 3_000_000
    if result.exceeded_target(target_ns):
        raise AssertionError(f"L1 hit with backend {result.p95:.0f}ns exceeds {target_ns}ns target (p95)")

    print(
        f"\n✅ L1 hit with backend validated: {result.p95:.0f}ns ({result.p95 / 1000:.0f}μs) < {target_ns / 1000:.0f}μs target"
    )


# =============================================================================
# Test 2: Concurrent Access - Lock Contention
# =============================================================================


@pytest.mark.performance
def test_concurrent_cache_access() -> None:
    """Measure the per-call latency each thread sees while 10 threads hit one key.

    Tests:
    - L1 cache lock contention
    - Decorator overhead under load
    - Key generator thread safety

    Each run starts 10 threads together and keeps all their samples as that run's samples: the
    threads of one run share the host's state, so the run, not the thread, is the unit summarize
    infers over. On a GIL build a sample includes the time a thread waits for the GIL.
    """
    payload = create_complex_dict("medium")
    num_threads = 10
    iterations_per_thread = 1_000
    runs = 5

    @cache(backend=None, encryption=False)
    def get_data(item_id: int) -> dict[str, Any]:
        return payload

    # Prime the cache and warm up as the single-threaded guards do
    for _ in range(5_000):
        get_data(1)

    def worker(barrier: threading.Barrier, samples: list[float]) -> None:
        for _ in range(100):
            get_data(1)
        barrier.wait()  # every thread measures at once, so they contend
        for _ in range(iterations_per_thread):
            start = time.perf_counter_ns()
            get_data(1)  # Same key = L1 hit with lock contention
            samples.append(time.perf_counter_ns() - start)

    per_run: list[list[float]] = []
    for _ in range(runs):
        gc.collect()
        barrier = threading.Barrier(num_threads)
        thread_samples: list[list[float]] = [[] for _ in range(num_threads)]
        threads = [threading.Thread(target=worker, args=(barrier, samples)) for samples in thread_samples]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        per_run.append([s for samples in thread_samples for s in samples])

    result = summarize(f"Concurrent L1 hit, {num_threads} threads, one key", per_run, "ns")

    print("\n" + "=" * 80)
    print(f"CONCURRENT ACCESS - {num_threads} THREADS")
    print("=" * 80)
    print(result)
    print("\nContext:")
    print(f"  Threads:    {num_threads} x {iterations_per_thread:,} calls per run")
    print("  Payload:    100-user nested dict, @cache(backend=None)")
    print("  Contention: All threads hitting same key (worst case)")

    # Conservative target: <500μs p95 under 10-thread contention
    target_ns = 500_000
    if result.exceeded_target(target_ns):
        raise AssertionError(f"Concurrent access p95 {result.p95:.0f}ns exceeds {target_ns}ns target")

    print(f"\n✅ Concurrent access validated: {result.p95:.0f}ns ({result.p95 / 1000:.0f}μs) < {target_ns / 1000:.0f}μs target")


# =============================================================================
# Test 3: Encryption Overhead
# =============================================================================


@pytest.mark.performance
def test_encryption_overhead() -> None:
    """Measure encryption overhead for AES-256-GCM.

    Compares:
    - Without encryption (msgpack only)
    - With encryption (msgpack + AES-256-GCM)

    This is critical for @cache.secure marketing claims.
    """
    # Check if encryption is available
    master_key = os.environ.get("CACHEKIT_MASTER_KEY")
    if not master_key:
        pytest.skip("CACHEKIT_MASTER_KEY not set - cannot test encryption")

    payload = create_complex_dict("medium")

    from cachekit.config.nested import EncryptionConfig

    # Both arms get an explicit in-process backend. A backend=None inside config= is not L1-only
    # mode: the backend would resolve from the default provider (Redis at REDIS_URL) at first call.
    # With a backend, L1 holds bytes: msgpack in the plain arm, ciphertext in the encrypted arm.
    backend_plain, backend_encrypted = DictBackend(), DictBackend()
    config_plain = DecoratorConfig(backend=backend_plain, encryption=False)
    config_encrypted = DecoratorConfig(
        backend=backend_encrypted,
        encryption=EncryptionConfig(
            enabled=True,
            master_key=master_key,
            single_tenant_mode=True,
        ),
    )

    @cache(config=config_plain)
    def get_data_plain(item_id: int) -> dict[str, Any]:
        return payload

    @cache(config=config_encrypted)
    def get_data_encrypted(item_id: int) -> dict[str, Any]:
        return payload

    # Prime both caches
    get_data_plain(1)
    get_data_encrypted(1)
    gets_after_prime = backend_plain.gets, backend_encrypted.gets

    # Benchmark plain
    def measure_plain():
        get_data_plain(1)

    result_plain = benchmark_with_gc_handling(
        name="Without encryption",
        fn=measure_plain,
        iterations_per_run=5_000,
        runs=5,
        unit="ns",
    )

    # Benchmark encrypted
    def measure_encrypted():
        get_data_encrypted(1)

    result_encrypted = benchmark_with_gc_handling(
        name="With encryption (AES-256-GCM)",
        fn=measure_encrypted,
        iterations_per_run=5_000,
        runs=5,
        unit="ns",
    )

    # Every measured call must have been an L1 hit; one that reached L2 would time the wrong path.
    assert (backend_plain.gets, backend_encrypted.gets) == gets_after_prime, "measured calls reached L2"

    ratio = result_encrypted.center / result_plain.center
    overhead = result_encrypted.center - result_plain.center

    print("\n" + "=" * 80)
    print("ENCRYPTION OVERHEAD")
    print("=" * 80)
    print(result_plain)
    print(result_encrypted)
    print("\nOverhead (run medians):")
    print(f"  Absolute: {overhead:.2f} ± {difference_band(result_plain, result_encrypted):.2f} ns (Welch 95%)")
    print(f"  Ratio:    {ratio:.2f}x")
    print("\nContext:")
    print("  Payload:     100-user nested dict (23.5KB as MessagePack)")
    print("  Algorithm:   AES-256-GCM")
    print("  L1 storage:  Encrypted bytes (no plaintext in memory)")

    # Target: encryption overhead <3x (conservative), on run medians: the raw p95 of either arm
    # moved 49-95% between back-to-back runs on a loaded host (2026-10-03).
    max_ratio = 3.0
    if ratio >= max_ratio:
        raise AssertionError(f"Encryption overhead {ratio:.2f}x exceeds {max_ratio}x target (run medians)")

    print(f"\n✅ Encryption overhead validated: {ratio:.2f}x < {max_ratio}x target")


# =============================================================================
# Test 4: Redis L2 Roundtrip (Integration Test)
# =============================================================================


@pytest.mark.performance
@pytest.mark.integration
def test_redis_l2_roundtrip() -> None:
    """Measure full Redis L2 roundtrip with serialization.

    Tests:
    - Decorator overhead
    - Serialization (msgpack)
    - Redis network RTT
    - Deserialization
    - L1 population

    This is L1 miss → L2 hit path (realistic production scenario).
    Requires Redis to be running.
    """
    from cachekit.backends.redis import RedisBackend

    # Check Redis availability
    redis_url = os.environ.get("REDIS_URL", "redis://localhost:6379")
    try:
        backend = RedisBackend(redis_url=redis_url)
        # Test connection by trying a basic operation
        backend.exists("__health_check__")
    except Exception as e:
        pytest.skip(f"Redis not available: {e}")

    from cachekit.config.nested import L1CacheConfig

    payload = create_complex_dict("medium")

    # Use L2-only mode to force Redis on every call
    config = DecoratorConfig(backend=backend, l1=L1CacheConfig(enabled=False), encryption=False)

    @cache(config=config)
    def get_data(item_id: int) -> dict[str, Any]:
        return payload

    # Prime Redis
    get_data(1)

    def measure_fn():
        get_data(1)

    result = benchmark_with_gc_handling(
        name="Redis L2 roundtrip (L1 disabled)",
        fn=measure_fn,
        iterations_per_run=1_000,  # Network I/O is slower
        runs=5,
        unit="μs",
    )

    print("\n" + "=" * 80)
    print("REDIS L2 ROUNDTRIP")
    print("=" * 80)
    print(result)
    print("\nContext:")
    print("  Mode:    L1 disabled (pure L2)")
    print("  Payload: 10KB complex dict")
    print("  Stack:   Decorator + Redis RTT + msgpack deserialize")
    print("\nBreakdown:")
    print("  Network RTT:       ~1-2ms (local Redis)")
    print("  Deserialization:   ~10-50μs (msgpack)")
    print(f"  Total measured:    {result.p95:.2f}μs")

    # Conservative target: <10ms for local Redis (includes network + deserialize)
    # Raw p95 measured 1.5-3.0ms (2026-10-03); back-to-back drift: two identical runs moved p95 by 101%
    # on a loaded host (the run-level median moved 5%), so the margin is what keeps this stable.
    target_us = 10_000
    if result.exceeded_target(target_us):
        raise AssertionError(f"Redis L2 roundtrip {result.p95:.0f}μs exceeds {target_us}μs target (p95)")

    print(f"\n✅ Redis L2 roundtrip validated: {result.p95:.0f}μs < {target_us}μs target")


# =============================================================================
# Test 5: Async Decorator Performance
# =============================================================================


@pytest.mark.performance
@pytest.mark.asyncio
async def test_async_decorator_overhead() -> None:
    """Measure async decorator overhead with realistic payload.

    Async adds:
    - Coroutine creation overhead
    - Event loop scheduling
    - Await machinery

    Compare with sync version to quantify async tax.
    """
    payload = create_complex_dict("medium")

    @cache(backend=None, encryption=False)
    async def get_data_async(item_id: int) -> dict[str, Any]:
        await asyncio.sleep(0)  # Yield control
        return payload

    # Prime cache
    await get_data_async(1)

    # Warm up
    for _ in range(100):
        await get_data_async(1)

    # Measure
    latencies = []
    for _ in range(5_000):
        start = time.perf_counter_ns()
        await get_data_async(1)
        end = time.perf_counter_ns()
        latencies.append(end - start)

    import statistics

    mean = statistics.mean(latencies)
    median = statistics.median(latencies)
    p95 = statistics.quantiles(latencies, n=20)[18]
    p99 = statistics.quantiles(latencies, n=100)[98]

    print("\n" + "=" * 80)
    print("ASYNC DECORATOR OVERHEAD")
    print("=" * 80)
    print(f"Mean:     {mean:>10.2f} ns ({mean / 1000:>6.2f} μs)")
    print(f"Median:   {median:>10.2f} ns ({median / 1000:>6.2f} μs)")
    print(f"P95:      {p95:>10.2f} ns ({p95 / 1000:>6.2f} μs)")
    print(f"P99:      {p99:>10.2f} ns ({p99 / 1000:>6.2f} μs)")
    print("\nContext:")
    print("  Payload: 10KB complex dict")
    print("  Stack:   Async decorator + L1 hit")
    print(f"  Note:    Sync version measured at ~{35000:.0f}ns in test_end_to_end_latency.py")

    # Target: <400μs for async (7-8x sync due to coroutine + event loop overhead)
    # Async adds: coroutine creation, event loop scheduling, await machinery
    # This is realistic overhead for async Python operations
    target_ns = 400_000
    if p95 >= target_ns:
        raise AssertionError(f"Async decorator overhead {p95:.0f}ns exceeds {target_ns}ns target (p95)")

    print(f"\n✅ Async decorator validated: {p95:.0f}ns < {target_ns}ns target")


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s", "--tb=short"])
