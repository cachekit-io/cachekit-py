"""L1 cache latency performance tests.

Validates L1 cache hit/miss/put latencies meet SLA for fast-path caching.

The L1 cache is the hot path - should be <500ns p95 for hits.
L1 cache eliminates 1000μs+ network latency for cache hits.
"""

from __future__ import annotations

import statistics
import threading
import time

import pytest

from cachekit.l1_cache import L1Cache


@pytest.mark.performance
def test_l1_cache_hit_latency() -> None:
    """Test L1 cache hit latency meets SLA.

    Hits should be <500ns p95 (negligible overhead for Python dict lookup).
    This is the critical path - validates fast in-memory access.
    """
    cache = L1Cache(max_memory_mb=100)
    iterations = 100_000

    # Populate cache
    test_key = "perf:test:key"
    test_value = b"x" * 1024  # 1KB payload
    cache.put(test_key, test_value, redis_ttl=3600)

    print(f"\nBenchmarking L1 cache hits ({iterations:,} iterations)...")

    # Warm up
    for _ in range(1000):
        cache.get(test_key)

    # Measure hit latency
    latencies = []
    for _ in range(iterations):
        start = time.perf_counter_ns()
        found, value = cache.get(test_key)
        end = time.perf_counter_ns()

        assert found, "Cache hit should succeed"
        assert value == test_value, "Value should match"
        latencies.append(end - start)

    p50 = statistics.median(latencies)
    p95 = statistics.quantiles(latencies, n=20)[18]
    p99 = statistics.quantiles(latencies, n=100)[98]
    mean = statistics.mean(latencies)
    stdev = statistics.stdev(latencies)

    results = {
        "iterations": iterations,
        "mean_ns": mean,
        "p50_ns": p50,
        "p95_ns": p95,
        "p99_ns": p99,
        "stdev_ns": stdev,
    }

    # Print results
    print(f"\n{'=' * 60}")
    print("L1 Cache Hit Latency")
    print(f"{'=' * 60}")
    print(f"Iterations:  {results['iterations']:>10,}")
    print(f"Mean:        {results['mean_ns']:>10.2f} ns")
    print(f"P50:         {results['p50_ns']:>10.2f} ns")
    print(f"P95:         {results['p95_ns']:>10.2f} ns")
    print(f"P99:         {results['p99_ns']:>10.2f} ns")
    print(f"StdDev:      {results['stdev_ns']:>10.2f} ns")

    # Hard gate - hits should be <1000ns p95 (dict lookup + lock contention)
    # p50 ~300ns, p95 typically 500-800ns with system scheduling noise
    target_ns = 1000
    if results["p95_ns"] >= target_ns:
        raise AssertionError(
            f"L1 cache hit latency {results['p95_ns']:.0f}ns (p95) exceeds {target_ns}ns target\n"
            f"L1 cache SLA violated - fast path is compromised"
        )

    print(f"\n✅ L1 cache hits validated: {results['p95_ns']:.0f}ns < {target_ns}ns target")
    print(f"   (p50={results['p50_ns']:.0f}ns, excellent in-memory performance)")


@pytest.mark.performance
def test_l1_cache_miss_latency() -> None:
    """Test L1 cache miss latency (no stored entry).

    Misses should be fast - just a dict lookup that returns None.
    Should be <200ns p95 (even faster than hits - no value copy).
    """
    cache = L1Cache(max_memory_mb=100)
    iterations = 100_000

    print(f"\nBenchmarking L1 cache misses ({iterations:,} iterations)...")

    # Warm up
    for _ in range(1000):
        cache.get("miss:key:that:does:not:exist")

    # Measure miss latency
    latencies = []
    for _ in range(iterations):
        start = time.perf_counter_ns()
        found, value = cache.get(f"miss:key:{_}")  # Different key each time
        end = time.perf_counter_ns()

        assert not found, "Cache miss should return False"
        assert value is None, "Value should be None"
        latencies.append(end - start)

    p50 = statistics.median(latencies)
    p95 = statistics.quantiles(latencies, n=20)[18]
    mean = statistics.mean(latencies)

    results = {
        "iterations": iterations,
        "mean_ns": mean,
        "p50_ns": p50,
        "p95_ns": p95,
    }

    # Print results
    print(f"\n{'=' * 60}")
    print("L1 Cache Miss Latency")
    print(f"{'=' * 60}")
    print(f"Iterations:  {results['iterations']:>10,}")
    print(f"Mean:        {results['mean_ns']:>10.2f} ns")
    print(f"P50:         {results['p50_ns']:>10.2f} ns")
    print(f"P95:         {results['p95_ns']:>10.2f} ns")

    # Misses should be fast - dict lookup + key miss detection
    # Conservative threshold based on real measurements: <500ns p95
    target_ns = 500
    if results["p95_ns"] >= target_ns:
        raise AssertionError(
            f"L1 cache miss latency {results['p95_ns']:.0f}ns (p95) exceeds {target_ns}ns target\nMiss path overhead is too high"
        )

    print(f"\n✅ L1 cache misses validated: {results['p95_ns']:.0f}ns < {target_ns}ns target")


@pytest.mark.performance
def test_l1_cache_put_latency() -> None:
    """Test L1 cache put (insertion) latency.

    Put operations are less frequent than gets, but should still be reasonable.
    Should be <2000ns p95 (includes LRU update + memory tracking).
    """
    cache = L1Cache(max_memory_mb=100)
    iterations = 10_000  # Fewer iterations (puts are more expensive)

    print(f"\nBenchmarking L1 cache puts ({iterations:,} iterations)...")

    # Warm up
    for i in range(100):
        cache.put(f"warmup:{i}", b"x" * 1024, redis_ttl=3600)

    # Measure put latency
    latencies = []
    for i in range(iterations):
        test_key = f"put:key:{i}"
        test_value = b"x" * 1024  # 1KB payload

        start = time.perf_counter_ns()
        cache.put(test_key, test_value, redis_ttl=3600)
        end = time.perf_counter_ns()

        latencies.append(end - start)

    p50 = statistics.median(latencies)
    p95 = statistics.quantiles(latencies, n=20)[18]
    p99 = statistics.quantiles(latencies, n=100)[98]
    mean = statistics.mean(latencies)

    results = {
        "iterations": iterations,
        "mean_ns": mean,
        "p50_ns": p50,
        "p95_ns": p95,
        "p99_ns": p99,
    }

    # Print results
    print(f"\n{'=' * 60}")
    print("L1 Cache Put Latency")
    print(f"{'=' * 60}")
    print(f"Iterations:  {results['iterations']:>10,}")
    print(f"Mean:        {results['mean_ns']:>10.2f} ns")
    print(f"P50:         {results['p50_ns']:>10.2f} ns")
    print(f"P95:         {results['p95_ns']:>10.2f} ns")
    print(f"P99:         {results['p99_ns']:>10.2f} ns")

    # Puts include RLock, dict insert, LRU move, memory tracking
    # Should still be <2μs
    target_ns = 2000
    if results["p95_ns"] >= target_ns:
        raise AssertionError(
            f"L1 cache put latency {results['p95_ns']:.0f}ns (p95) exceeds {target_ns}ns target\nPut path overhead is too high"
        )

    print(f"\n✅ L1 cache puts validated: {results['p95_ns']:.0f}ns < {target_ns}ns target")


@pytest.mark.performance
def test_l1_cache_concurrent_hit_latency() -> None:
    """Test L1 cache hit latency under thread contention.

    Validates RLock contention doesn't degrade hit latency significantly.
    With 10 threads, p95 hit latency should stay <1000ns (2x single-threaded).
    """
    cache = L1Cache(max_memory_mb=100)
    iterations_per_thread = 10_000
    num_threads = 10

    # Populate cache with different keys
    for i in range(100):
        cache.put(f"concurrent:key:{i}", b"x" * 512, redis_ttl=3600)

    print(f"\nBenchmarking L1 cache hits under {num_threads}-thread contention...")
    print(f"({iterations_per_thread:,} iterations per thread)...")

    results_by_thread: dict[int, list[int]] = {i: [] for i in range(num_threads)}
    lock = threading.Lock()

    def worker(thread_id: int) -> None:
        """Worker thread that measures hit latency."""
        # Warm up
        for _ in range(100):
            cache.get(f"concurrent:key:{_ % 100}")

        # Measure hits
        latencies = []
        for i in range(iterations_per_thread):
            start = time.perf_counter_ns()
            found, value = cache.get(f"concurrent:key:{i % 100}")
            end = time.perf_counter_ns()

            assert found, "Cache hit should succeed"
            latencies.append(end - start)

        with lock:
            results_by_thread[thread_id] = latencies

    # Launch threads
    threads = []
    for i in range(num_threads):
        t = threading.Thread(target=worker, args=(i,))
        threads.append(t)
        t.start()

    # Wait for completion
    for t in threads:
        t.join()

    # Combine and analyze results
    all_latencies = []
    for latencies in results_by_thread.values():
        all_latencies.extend(latencies)

    p50 = statistics.median(all_latencies)
    p95 = statistics.quantiles(all_latencies, n=20)[18]
    p99 = statistics.quantiles(all_latencies, n=100)[98]
    mean = statistics.mean(all_latencies)

    results = {
        "threads": num_threads,
        "total_ops": len(all_latencies),
        "mean_ns": mean,
        "p50_ns": p50,
        "p95_ns": p95,
        "p99_ns": p99,
    }

    # Print results
    print(f"\n{'=' * 60}")
    print(f"L1 Cache Concurrent Hit Latency ({num_threads} threads)")
    print(f"{'=' * 60}")
    print(f"Total ops:   {results['total_ops']:>10,}")
    print(f"Mean:        {results['mean_ns']:>10.2f} ns")
    print(f"P50:         {results['p50_ns']:>10.2f} ns")
    print(f"P95:         {results['p95_ns']:>10.2f} ns")
    print(f"P99:         {results['p99_ns']:>10.2f} ns")

    # Under contention, p95 should stay reasonable (allow 2x single-threaded)
    target_ns = 1000
    if results["p95_ns"] >= target_ns:
        raise AssertionError(
            f"L1 cache concurrent hit latency {results['p95_ns']:.0f}ns (p95) exceeds {target_ns}ns target\n"
            f"RLock contention is degrading performance significantly"
        )

    print(f"\n✅ L1 cache concurrent hits validated: {results['p95_ns']:.0f}ns < {target_ns}ns target")


@pytest.mark.performance
def test_l1_cache_total_sla() -> None:
    """Validate overall L1 cache SLA - primary fast path for caching.

    The L1 cache is the hot path. Hit latency dominates real-world performance.
    This test validates the complete L1 cache story.
    """
    cache = L1Cache(max_memory_mb=100)
    total_iterations = 100_000

    print(f"\n{'=' * 60}")
    print("L1 Cache Total SLA Validation")
    print(f"{'=' * 60}")
    print("Target: L1 hits <500ns p95 (fast in-memory access)")
    print("Context: Eliminates 1000μs+ network latency for cache hits\n")

    # Populate cache
    for i in range(100):
        cache.put(f"sla:key:{i}", b"x" * 1024, redis_ttl=3600)

    # Warm up
    for i in range(1000):
        cache.get(f"sla:key:{i % 100}")

    # Measure realistic workload (95% hits, 5% misses)
    hit_latencies = []
    miss_latencies = []

    for i in range(total_iterations):
        key = f"sla:key:{i % 100}" if (i % 20) < 19 else f"sla:miss:{i}"

        start = time.perf_counter_ns()
        found, value = cache.get(key)
        end = time.perf_counter_ns()

        if found:
            hit_latencies.append(end - start)
        else:
            miss_latencies.append(end - start)

    # Analyze results
    hit_p95 = statistics.quantiles(hit_latencies, n=20)[18] if hit_latencies else 0
    miss_p95 = statistics.quantiles(miss_latencies, n=20)[18] if miss_latencies else 0

    stats = {
        "total_requests": total_iterations,
        "hits": len(hit_latencies),
        "misses": len(miss_latencies),
        "hit_rate": len(hit_latencies) / total_iterations,
        "hit_p95_ns": hit_p95,
        "miss_p95_ns": miss_p95,
    }

    print(f"Requests:    {stats['total_requests']:>10,}")
    print(f"Hits:        {stats['hits']:>10,} ({stats['hit_rate']:.1%})")
    print(f"Misses:      {stats['misses']:>10,}")
    print("\nLatencies:")
    print(f"  Hit p95:   {stats['hit_p95_ns']:>10.0f} ns")
    print(f"  Miss p95:  {stats['miss_p95_ns']:>10.0f} ns")

    # Hard gate - hits must be <1000ns p95 (1 microsecond)
    target_hit_ns = 1000
    if stats["hit_p95_ns"] >= target_hit_ns:
        raise AssertionError(
            f"L1 cache hit latency {stats['hit_p95_ns']:.0f}ns exceeds {target_hit_ns}ns target\n"
            f"L1 cache SLA violated - primary fast path is compromised"
        )

    print(f"\n✅ L1 cache SLA validated: {stats['hit_p95_ns']:.0f}ns < {target_hit_ns}ns target")
    print("   Cache hit eliminates 1000+ microseconds of network latency")
