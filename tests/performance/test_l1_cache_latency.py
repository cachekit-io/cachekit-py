"""L1 cache latency performance tests.

Measures L1 cache hit/miss/put latencies for the fast path. On a loaded host a nanosecond-scale
p95 moves by orders of magnitude, so each test asserts only that what it timed was what it
claims: hits returned the stored value, misses found nothing, the hit/miss counters add up.
"""

from __future__ import annotations

import threading
import time

import pytest

from cachekit.l1_cache import L1Cache

from .stats_utils import report


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

    report(record_property, "L1 cache hit", latencies)

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

    report(record_property, "L1 cache miss", latencies)

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

    report(record_property, "L1 cache put", latencies)

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

    report(record_property, f"L1 cache hit, {num_threads} threads", all_latencies)

    # Every thread finished, and contention lost no counter update and served no wrong value.
    assert wrong_values == [0] * num_threads
    stats = cache.get_stats()
    assert (stats["hits"], stats["misses"]) == (num_threads * (100 + iterations_per_thread), 0)


@pytest.mark.performance
def test_l1_cache_mixed_workload_hit_counts(record_property) -> None:
    """Time a 95% hit / 5% miss L1 workload; check every stored key hit and every other key missed."""
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

    print(f"Hits: {len(hit_latencies):,}, misses: {len(miss_latencies):,}")
    report(record_property, "Mixed workload, hits", hit_latencies, "hit_")
    report(record_property, "Mixed workload, misses", miss_latencies, "miss_")

    # Every key in the 19-of-20 slots is stored, every other key never was.
    assert (len(hit_latencies), len(miss_latencies)) == (total_iterations * 19 // 20, total_iterations // 20)
