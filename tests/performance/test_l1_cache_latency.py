"""L1 cache latency performance tests.

Measures L1 cache hit/miss/put latencies for the fast path. The figures are informational:
printed and recorded with ``record_property``, never asserted. A 10-thread hit p95 measured
48,758ns on a loaded host against ~500ns on a quiet core, so a nanosecond budget cannot tell
a regression from a busy neighbour. Each test asserts that what it timed was what it claims:
hits returned the stored value, misses found nothing, the hit/miss counters add up.
"""

from __future__ import annotations

import statistics
import threading
import time

import pytest

from cachekit.l1_cache import L1Cache


@pytest.mark.performance
def test_l1_cache_hit_latency(record_property) -> None:
    """Measure L1 cache hit latency: the critical in-memory path."""
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

    record_property("p50_ns", round(p50, 1))
    record_property("p95_ns", round(p95, 1))

    stats = cache.get_stats()
    assert (stats["hits"], stats["misses"]) == (1000 + iterations, 0)


@pytest.mark.performance
def test_l1_cache_miss_latency(record_property) -> None:
    """Measure L1 cache miss latency (no stored entry): a dict lookup that finds nothing."""
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

    record_property("p50_ns", round(p50, 1))
    record_property("p95_ns", round(p95, 1))

    stats = cache.get_stats()
    assert (stats["hits"], stats["misses"]) == (0, 1000 + iterations)


@pytest.mark.performance
def test_l1_cache_put_latency(record_property) -> None:
    """Measure L1 cache put (insertion) latency: dict insert + LRU update + memory tracking."""
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

    record_property("p50_ns", round(p50, 1))
    record_property("p95_ns", round(p95, 1))

    # 10,100 x 1KB fits the 100MB budget, so every timed put must still be there.
    assert all(cache.get(f"put:key:{i}") == (True, b"x" * 1024) for i in range(iterations))


@pytest.mark.performance
def test_l1_cache_concurrent_hit_latency(record_property) -> None:
    """Measure L1 cache hit latency with 10 threads contending for its lock."""
    cache = L1Cache(max_memory_mb=100)
    iterations_per_thread = 10_000
    num_threads = 10

    # Populate cache with different keys
    for i in range(100):
        cache.put(f"concurrent:key:{i}", b"x" * 512, redis_ttl=3600)

    print(f"\nBenchmarking L1 cache hits under {num_threads}-thread contention...")
    print(f"({iterations_per_thread:,} iterations per thread)...")

    results_by_thread: dict[int, list[int]] = {i: [] for i in range(num_threads)}
    wrong_values: list[int] = []
    lock = threading.Lock()

    def worker(thread_id: int) -> None:
        """Worker thread that measures hit latency."""
        # Warm up
        for _ in range(100):
            cache.get(f"concurrent:key:{_ % 100}")

        # Measure hits
        latencies = []
        wrong = 0
        for i in range(iterations_per_thread):
            start = time.perf_counter_ns()
            found, value = cache.get(f"concurrent:key:{i % 100}")
            end = time.perf_counter_ns()

            # Counted, not asserted: an assert in a thread is only printed, never fails the test.
            wrong += value != b"x" * 512
            latencies.append(end - start)

        with lock:
            results_by_thread[thread_id] = latencies
            wrong_values.append(wrong)

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

    record_property("p50_ns", round(p50, 1))
    record_property("p95_ns", round(p95, 1))

    # Contention must lose no counter update and serve no wrong value.
    assert wrong_values == [0] * num_threads
    assert results["total_ops"] == num_threads * iterations_per_thread
    stats = cache.get_stats()
    assert (stats["hits"], stats["misses"]) == (num_threads * (100 + iterations_per_thread), 0)


@pytest.mark.performance
def test_l1_cache_total_sla(record_property) -> None:
    """Measure a realistic L1 workload: 95% hits, 5% misses, split by outcome."""
    cache = L1Cache(max_memory_mb=100)
    total_iterations = 100_000

    print(f"\n{'=' * 60}")
    print("L1 Cache Mixed Workload (95% hits)")
    print(f"{'=' * 60}")
    print("Context: an L1 hit replaces a network round trip to L2\n")

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
            assert value == b"x" * 1024
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

    record_property("hit_p95_ns", round(hit_p95, 1))
    record_property("miss_p95_ns", round(miss_p95, 1))

    # Every key in the 19-of-20 slots is stored, every other key never was.
    assert (stats["hits"], stats["misses"]) == (total_iterations * 19 // 20, total_iterations // 20)
