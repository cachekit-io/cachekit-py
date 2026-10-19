"""Detailed component-level profiling of L1 cache operations.

This profiler breaks the hit latency down into individual components to find the
bottleneck. Uses manual instrumentation to avoid profiler overhead.

Each test asserts, from the cache's own hit/miss counters, that the calls it timed were hits.
"""

from __future__ import annotations

import statistics
import threading
import time
from collections import OrderedDict

import pytest

from cachekit.l1_cache import CacheEntry, L1Cache

from .stats_utils import percentile, report


def _timed(operation, iterations: int) -> list[int]:
    """Per-call latencies (ns) of ``operation`` over ``iterations`` calls."""
    latencies = []
    for _ in range(iterations):
        start = time.perf_counter_ns()
        operation()
        latencies.append(time.perf_counter_ns() - start)
    return latencies


@pytest.mark.performance
def test_l1_cache_component_breakdown(record_property) -> None:
    """Profile the pieces of the cache.get() hit path against the whole call.

    Components: lock acquisition, dict lookup, TTL check (time.time()), LRU move, counter update.
    """
    cache = L1Cache(max_memory_mb=100)
    iterations = 100_000

    test_key = "perf:test:key"
    test_value = b"x" * 1024
    cache.put(test_key, test_value, redis_ttl=3600)
    for _ in range(1000):  # warm up
        cache.get(test_key)

    lock = threading.RLock()
    entry = CacheEntry(value=test_value, expires_at=time.time() + 3600, size_bytes=1024)
    test_dict: dict[str, CacheEntry] = {"key": entry}
    ordered_dict: OrderedDict[str, CacheEntry] = OrderedDict((f"key_{i}", entry) for i in range(100))
    counter = [0]

    def lock_cycle():
        with lock:
            pass

    def ttl_check():
        return time.time() >= time.time() + 3600

    def count():
        counter[0] += 1

    components = {
        "lock": ("Lock acquire/release", lock_cycle),
        "dict": ("Dict lookup (hit, single entry)", lambda: test_dict.get("key")),
        "ttl": ("TTL check (time.time() + comparison)", ttl_check),
        "lru": ("OrderedDict.move_to_end (100 entries)", lambda: ordered_dict.move_to_end("key_50")),
        "counter": ("Counter increment", count),
    }
    p95s = {}
    for prefix, (label, operation) in components.items():
        latencies = _timed(operation, iterations)
        report(record_property, label, latencies, f"{prefix}_")
        p95s[label] = percentile(latencies, 95)

    stats_before = cache.get_stats()
    get_latencies = _timed(lambda: cache.get(test_key), iterations)
    stats_after = cache.get_stats()
    assert cache.get(test_key) == (True, test_value)
    assert stats_after["hits"] - stats_before["hits"] == iterations
    assert stats_after["misses"] == stats_before["misses"] == 0

    report(record_property, "cache.get() (complete operation)", get_latencies, "get_")
    get_p95 = percentile(get_latencies, 95)
    component_sum = sum(p95s.values())
    print(f"\nComponent p95s sum to {component_sum:.1f} ns of the {get_p95:.1f} ns cache.get() p95:")
    for label, value in p95s.items():
        print(f"  {label:<40} {value:>8.1f} ns ({value / get_p95 * 100:>5.1f}%)")


@pytest.mark.performance
def test_l1_cache_dict_size_impact() -> None:
    """Test if dict size impacts lookup latency.

    Does lookup performance degrade as the cache grows?
    """
    iterations = 50_000
    sizes = [1, 10, 100, 1000, 10000]

    print("\n" + "=" * 80)
    print("DICT SIZE IMPACT ON LOOKUP LATENCY")
    print("=" * 80)

    results = []
    for size in sizes:
        cache = L1Cache(max_memory_mb=500)
        entry = CacheEntry(value=b"x" * 1024, expires_at=time.time() + 3600, size_bytes=1024)

        # Populate cache with N entries
        for i in range(size):
            cache._state.cache[f"key_{i}"] = entry
            cache._state.memory_bytes += 1024

        # Warm up
        for _ in range(1000):
            cache.get(f"key_{size // 2}")

        # Measure latency
        latencies = []
        for _ in range(iterations):
            start = time.perf_counter_ns()
            found, value = cache.get(f"key_{size // 2}")
            end = time.perf_counter_ns()
            latencies.append(end - start)

        # The entries were planted in the cache's state directly; every timed get must have hit one.
        stats = cache.get_stats()
        assert (found, value) == (True, entry.value)
        assert (stats["hits"], stats["misses"]) == (1000 + iterations, 0), f"size {size}: {stats}"

        p95 = percentile(latencies, 95)
        mean = statistics.mean(latencies)
        results.append((size, mean, p95))
        print(f"Size: {size:>6} entries  |  Mean: {mean:>8.1f}ns  |  P95: {p95:>8.1f}ns")

    slowdown = (results[-1][2] / results[0][2] - 1) * 100
    print(f"\nScaling impact: {slowdown:+.1f}% p95 change from {sizes[0]} to {sizes[-1]} entries")


@pytest.mark.performance
def test_l1_cache_lock_contention() -> None:
    """Test lock contention under high concurrency.

    How much does lock contention impact p95 latency?
    """
    cache = L1Cache(max_memory_mb=100)
    test_key = "perf:test:key"
    test_value = b"x" * 1024
    cache.put(test_key, test_value, redis_ttl=3600)

    thread_counts = [1, 2, 4, 8]
    iterations_per_thread = 10_000
    wrong_values: list[int] = []

    print("\n" + "=" * 80)
    print("LOCK CONTENTION IMPACT")
    print("=" * 80)

    for num_threads in thread_counts:
        latencies: list[int] = []
        lock = threading.Lock()

        def worker(result_lock=lock, result_list=latencies):
            local_latencies = []
            wrong = 0
            for _ in range(iterations_per_thread):
                start = time.perf_counter_ns()
                found, value = cache.get(test_key)
                end = time.perf_counter_ns()
                local_latencies.append(end - start)
                # Counted, not asserted: an assert in a thread is only printed, never fails the test.
                wrong += value != test_value

            with result_lock:
                result_list.extend(local_latencies)
                wrong_values.append(wrong)

        # Launch threads
        threads = []
        for _ in range(num_threads):
            t = threading.Thread(target=worker)
            threads.append(t)
            t.start()

        # Wait for completion
        for t in threads:
            t.join()

        # Analyze
        p95 = percentile(latencies, 95)
        mean = statistics.mean(latencies)
        p99 = percentile(latencies, 99)

        print(f"Threads: {num_threads:>2}  |  Mean: {mean:>8.1f}ns  |  P95: {p95:>8.1f}ns  |  P99: {p99:>8.1f}ns")

    print("\nNote: Lock contention grows with thread count")

    # Contention must serve no wrong value and lose no counter update.
    assert wrong_values == [0] * sum(thread_counts)
    stats = cache.get_stats()
    assert (stats["hits"], stats["misses"]) == (sum(thread_counts) * iterations_per_thread, 0)


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
