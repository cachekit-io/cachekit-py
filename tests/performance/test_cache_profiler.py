"""Detailed component-level profiling of L1 cache operations.

This profiler breaks down the 450-500ns hit latency into individual components
to identify the actual bottleneck. Uses manual instrumentation to avoid profiler overhead.
"""

from __future__ import annotations

import statistics
import threading
import time
from collections import OrderedDict

import pytest

from cachekit.l1_cache import CacheEntry, L1Cache


@pytest.mark.performance
def test_l1_cache_component_breakdown() -> None:
    """Profile individual components of cache.get() hit path.

    Breaks down the 450-500ns into:
    1. Lock acquisition
    2. Dict lookup
    3. TTL check
    4. LRU move
    5. Counter update
    """
    cache = L1Cache(max_memory_mb=100)
    iterations = 100_000

    # Populate cache
    test_key = "perf:test:key"
    test_value = b"x" * 1024
    cache.put(test_key, test_value, redis_ttl=3600)

    # Warm up
    for _ in range(1000):
        cache.get(test_key)

    print("\n" + "=" * 80)
    print("L1 CACHE COMPONENT-LEVEL PROFILING")
    print("=" * 80)

    # Component 1: Lock acquisition overhead
    print("\n1. LOCK ACQUISITION OVERHEAD")
    print("-" * 80)
    lock_latencies = []
    lock = threading.RLock()

    for _ in range(iterations):
        start = time.perf_counter_ns()
        with lock:
            pass
        end = time.perf_counter_ns()
        lock_latencies.append(end - start)

    lock_p50 = statistics.median(lock_latencies)
    lock_p95 = statistics.quantiles(lock_latencies, n=20)[18]
    lock_mean = statistics.mean(lock_latencies)
    lock_stdev = statistics.stdev(lock_latencies)

    print("Empty lock acquire/release:")
    print(f"  Mean:   {lock_mean:>8.1f} ns")
    print(f"  P50:    {lock_p50:>8.1f} ns")
    print(f"  P95:    {lock_p95:>8.1f} ns")
    print(f"  StdDev: {lock_stdev:>8.1f} ns")

    # Component 2: Dict lookup only (no lock)
    print("\n2. DICT LOOKUP OVERHEAD")
    print("-" * 80)
    entry = CacheEntry(value=test_value, expires_at=time.time() + 3600, size_bytes=1024)
    test_dict: dict[str, CacheEntry] = {"key": entry}

    dict_latencies = []
    for _ in range(iterations):
        start = time.perf_counter_ns()
        test_dict.get("key")
        end = time.perf_counter_ns()
        dict_latencies.append(end - start)

    dict_p50 = statistics.median(dict_latencies)
    dict_p95 = statistics.quantiles(dict_latencies, n=20)[18]
    dict_mean = statistics.mean(dict_latencies)
    dict_stdev = statistics.stdev(dict_latencies)

    print("Dict lookup (hit, single entry):")
    print(f"  Mean:   {dict_mean:>8.1f} ns")
    print(f"  P50:    {dict_p50:>8.1f} ns")
    print(f"  P95:    {dict_p95:>8.1f} ns")
    print(f"  StdDev: {dict_stdev:>8.1f} ns")

    # Component 3: TTL check (time.time() call)
    print("\n3. TTL CHECK OVERHEAD (time.time())")
    print("-" * 80)
    ttl_latencies = []

    for _ in range(iterations):
        start = time.perf_counter_ns()
        now = time.time()
        _ = now >= (time.time() + 3600)
        end = time.perf_counter_ns()
        ttl_latencies.append(end - start)

    ttl_p50 = statistics.median(ttl_latencies)
    ttl_p95 = statistics.quantiles(ttl_latencies, n=20)[18]
    ttl_mean = statistics.mean(ttl_latencies)
    ttl_stdev = statistics.stdev(ttl_latencies)

    print("time.time() call + comparison:")
    print(f"  Mean:   {ttl_mean:>8.1f} ns")
    print(f"  P50:    {ttl_p50:>8.1f} ns")
    print(f"  P95:    {ttl_p95:>8.1f} ns")
    print(f"  StdDev: {ttl_stdev:>8.1f} ns")

    # Component 4: OrderedDict.move_to_end()
    print("\n4. LRU MOVE OVERHEAD (OrderedDict.move_to_end)")
    print("-" * 80)
    move_latencies = []
    ordered_dict: OrderedDict[str, CacheEntry] = OrderedDict()
    for i in range(100):
        ordered_dict[f"key_{i}"] = entry

    # Move a middle key (more realistic than first/last)
    for _ in range(iterations):
        start = time.perf_counter_ns()
        ordered_dict.move_to_end("key_50")
        end = time.perf_counter_ns()
        move_latencies.append(end - start)

    move_p50 = statistics.median(move_latencies)
    move_p95 = statistics.quantiles(move_latencies, n=20)[18]
    move_mean = statistics.mean(move_latencies)
    move_stdev = statistics.stdev(move_latencies)

    print("OrderedDict.move_to_end (100 entries):")
    print(f"  Mean:   {move_mean:>8.1f} ns")
    print(f"  P50:    {move_p50:>8.1f} ns")
    print(f"  P95:    {move_p95:>8.1f} ns")
    print(f"  StdDev: {move_stdev:>8.1f} ns")

    # Component 5: Counter increment
    print("\n5. COUNTER INCREMENT OVERHEAD")
    print("-" * 80)
    counter_latencies = []
    counter = 0

    for _ in range(iterations):
        start = time.perf_counter_ns()
        counter += 1
        end = time.perf_counter_ns()
        counter_latencies.append(end - start)

    counter_p50 = statistics.median(counter_latencies)
    counter_p95 = statistics.quantiles(counter_latencies, n=20)[18]
    counter_mean = statistics.mean(counter_latencies)
    counter_stdev = statistics.stdev(counter_latencies)

    print("Counter increment:")
    print(f"  Mean:   {counter_mean:>8.1f} ns")
    print(f"  P50:    {counter_p50:>8.1f} ns")
    print(f"  P95:    {counter_p95:>8.1f} ns")
    print(f"  StdDev: {counter_stdev:>8.1f} ns")

    # Real cache.get() measurement
    print("\n6. ACTUAL cache.get() MEASUREMENT")
    print("-" * 80)
    get_latencies = []

    for _ in range(iterations):
        start = time.perf_counter_ns()
        found, value = cache.get(test_key)
        end = time.perf_counter_ns()
        get_latencies.append(end - start)

    get_p50 = statistics.median(get_latencies)
    get_p95 = statistics.quantiles(get_latencies, n=20)[18]
    get_mean = statistics.mean(get_latencies)
    get_stdev = statistics.stdev(get_latencies)

    print("cache.get() (complete operation):")
    print(f"  Mean:   {get_mean:>8.1f} ns")
    print(f"  P50:    {get_p50:>8.1f} ns")
    print(f"  P95:    {get_p95:>8.1f} ns")
    print(f"  StdDev: {get_stdev:>8.1f} ns")

    # Summary
    print("\n" + "=" * 80)
    print("BREAKDOWN SUMMARY")
    print("=" * 80)

    component_sum = lock_p95 + dict_p95 + ttl_p95 + move_p95 + counter_p95
    components = {
        "Lock acquisition": lock_p95,
        "Dict lookup": dict_p95,
        "TTL check (time.time)": ttl_p95,
        "LRU move": move_p95,
        "Counter increment": counter_p95,
        "Sum (theoretical)": component_sum,
        "Actual cache.get()": get_p95,
        "Overhead (measurement)": get_p95 - component_sum,
    }

    for name, value in components.items():
        percentage = (value / get_p95 * 100) if get_p95 > 0 else 0
        print(f"{name:<35} {value:>8.1f} ns ({percentage:>5.1f}%)")

    print("\n" + "=" * 80)
    print("INTERPRETATION")
    print("=" * 80)
    print(f"""
The actual cache.get() p95 ({get_p95:.0f}ns) is composed of:
- Lock acquisition: {lock_p95:.0f}ns ({lock_p95 / get_p95 * 100:.1f}%)
- Dict lookup: {dict_p95:.0f}ns ({dict_p95 / get_p95 * 100:.1f}%)
- TTL check: {ttl_p95:.0f}ns ({ttl_p95 / get_p95 * 100:.1f}%)
- LRU move: {move_p95:.0f}ns ({move_p95 / get_p95 * 100:.1f}%)
- Counter: {counter_p95:.0f}ns ({counter_p95 / get_p95 * 100:.1f}%)
- Overhead: {get_p95 - component_sum:.0f}ns ({(get_p95 - component_sum) / get_p95 * 100:.1f}%)

Key insights:
1. Lock contention is the primary cost (~{lock_p95 / get_p95 * 100:.0f}% of total)
2. Dict lookup is fast (~{dict_p95 / get_p95 * 100:.0f}% of total)
3. TTL checking (time.time()) adds ~{ttl_p95 / get_p95 * 100:.0f}% overhead
4. LRU move is relatively cheap (~{move_p95 / get_p95 * 100:.0f}% of total)
5. The "overhead" is likely Python interpreter overhead between operations
""")


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
            cache._cache[f"key_{i}"] = entry
            cache._current_memory_bytes += 1024

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

        p95 = statistics.quantiles(latencies, n=20)[18]
        mean = statistics.mean(latencies)
        results.append((size, mean, p95))
        print(f"Size: {size:>6} entries  |  Mean: {mean:>8.1f}ns  |  P95: {p95:>8.1f}ns")

    # Check for scaling impact
    if results:
        baseline_p95 = results[0][2]
        max_p95 = results[-1][2]
        slowdown = (max_p95 / baseline_p95 - 1) * 100

        print(f"\nScaling impact: {slowdown:+.1f}% slowdown from {sizes[0]} to {sizes[-1]} entries")
        if slowdown < 5:
            print("✅ Dict lookup is O(1) - size has minimal impact")
        elif slowdown < 20:
            print("⚠️  Dict lookup has minor degradation with size")
        else:
            print(f"❌ Dict lookup has significant degradation ({slowdown:.1f}%)")


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

    print("\n" + "=" * 80)
    print("LOCK CONTENTION IMPACT")
    print("=" * 80)

    for num_threads in thread_counts:
        latencies: list[int] = []
        lock = threading.Lock()

        def worker(result_lock=lock, result_list=latencies):
            local_latencies = []
            for _ in range(iterations_per_thread):
                start = time.perf_counter_ns()
                found, value = cache.get(test_key)
                end = time.perf_counter_ns()
                local_latencies.append(end - start)

            with result_lock:
                result_list.extend(local_latencies)

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
        p95 = statistics.quantiles(latencies, n=20)[18]
        mean = statistics.mean(latencies)
        p99 = statistics.quantiles(latencies, n=100)[98]

        print(f"Threads: {num_threads:>2}  |  Mean: {mean:>8.1f}ns  |  P95: {p95:>8.1f}ns  |  P99: {p99:>8.1f}ns")

    print("\nNote: Lock contention grows with thread count")


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
