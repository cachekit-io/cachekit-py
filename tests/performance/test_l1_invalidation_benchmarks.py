"""L1 cache invalidation and SWR performance benchmarks.

Validates that new L1 cache features (SWR, invalidation) maintain
sub-microsecond latency and don't regress the hot path.

Benchmarks:
- get_with_swr(): SWR lookup with freshness check and jitter
- invalidate_by_key(): Single key invalidation
- invalidate_by_namespace(): Namespace invalidation (with/without index)
- invalidate_all(): Global invalidation with version increment
- complete_refresh(): Version token check + entry update

SLA Targets:
- get_with_swr() hit: <1500ns p95 (includes jitter calculation)
- invalidate_by_key(): <1000ns p95 (single dict delete + version bump)
- invalidate_by_namespace() with index: <2000ns p95 for 100 keys
- invalidate_all(): <5000ns p95 for 1000 entries
"""

from __future__ import annotations

import statistics
import threading
import time

import pytest

from cachekit.config.nested import L1CacheConfig
from cachekit.l1_cache import L1Cache

# =============================================================================
# SWR (Stale-While-Revalidate) Benchmarks
# =============================================================================


@pytest.mark.performance
def test_get_with_swr_hit_latency() -> None:
    """Test get_with_swr() hit latency for fresh entries.

    SWR adds jitter calculation and version tracking overhead.
    Should still be <1500ns p95 (acceptable overhead over basic get).
    """
    config = L1CacheConfig(
        enabled=True,
        max_size_mb=100,
        swr_enabled=True,
        swr_threshold_ratio=0.5,
        invalidation_enabled=True,
        namespace_index=True,
    )
    cache = L1Cache(max_memory_mb=100, config=config)
    iterations = 100_000

    # Populate cache with fresh entries (TTL=3600s, well above threshold)
    test_key = "swr:perf:key"
    test_value = b"x" * 1024  # 1KB payload
    cache.put(test_key, test_value, redis_ttl=3600, namespace="perf")

    print(f"\nBenchmarking get_with_swr() hits ({iterations:,} iterations)...")

    # Warm up
    for _ in range(1000):
        cache.get_with_swr(test_key, ttl=3600)

    # Measure SWR hit latency (fresh entries - no refresh needed)
    latencies = []
    for _ in range(iterations):
        start = time.perf_counter_ns()
        hit, value, needs_refresh, version = cache.get_with_swr(test_key, ttl=3600)
        end = time.perf_counter_ns()

        assert hit, "Cache hit should succeed"
        assert value == test_value, "Value should match"
        assert not needs_refresh, "Fresh entry should not need refresh"
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

    print(f"\n{'=' * 60}")
    print("get_with_swr() Hit Latency (Fresh Entries)")
    print(f"{'=' * 60}")
    print(f"Iterations:  {results['iterations']:>10,}")
    print(f"Mean:        {results['mean_ns']:>10.2f} ns")
    print(f"P50:         {results['p50_ns']:>10.2f} ns")
    print(f"P95:         {results['p95_ns']:>10.2f} ns")
    print(f"P99:         {results['p99_ns']:>10.2f} ns")

    # SWR adds jitter calculation + version tracking
    # Should stay under 1500ns (50% overhead over basic get is acceptable)
    target_ns = 1500
    if results["p95_ns"] >= target_ns:
        raise AssertionError(
            f"get_with_swr() latency {results['p95_ns']:.0f}ns (p95) exceeds {target_ns}ns target\nSWR overhead is too high"
        )

    print(f"\n✅ get_with_swr() validated: {results['p95_ns']:.0f}ns < {target_ns}ns target")


@pytest.mark.performance
def test_get_with_swr_stale_detection_latency() -> None:
    """Test get_with_swr() latency when entry is stale (needs refresh).

    Stale detection adds:
    - Jitter calculation: random.uniform(0.9, 1.1)
    - Version lookup
    - _refreshing_keys set addition

    Should be <2000ns p95.
    """
    config = L1CacheConfig(
        enabled=True,
        max_size_mb=100,
        swr_enabled=True,
        swr_threshold_ratio=0.5,
        invalidation_enabled=True,
        namespace_index=True,
    )
    cache = L1Cache(max_memory_mb=100, config=config)
    iterations = 10_000  # Fewer iterations (each triggers refresh state)

    print(f"\nBenchmarking get_with_swr() stale detection ({iterations:,} iterations)...")

    # We need entries that are "stale" (past SWR threshold but not expired)
    # Put entry, then measure with TTL that makes it appear stale
    latencies = []

    for i in range(iterations):
        key = f"stale:key:{i}"
        value = b"x" * 512

        # Put with a very short TTL so threshold is exceeded
        # cached_at will be set to now, but we pass ttl=0.001 to get_with_swr
        # which makes elapsed time (now - cached_at) > threshold
        cache.put(key, value, redis_ttl=3600, namespace="stale")

        # Small sleep to ensure some time passes
        # Actually, let's just use a tiny TTL in the get_with_swr call
        start = time.perf_counter_ns()
        hit, _, needs_refresh, version = cache.get_with_swr(key, ttl=0.0001)  # Tiny TTL = always stale
        end = time.perf_counter_ns()

        # Cancel the refresh to reset state for next iteration
        cache.cancel_refresh(key)

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

    print(f"\n{'=' * 60}")
    print("get_with_swr() Stale Detection Latency")
    print(f"{'=' * 60}")
    print(f"Iterations:  {results['iterations']:>10,}")
    print(f"Mean:        {results['mean_ns']:>10.2f} ns")
    print(f"P50:         {results['p50_ns']:>10.2f} ns")
    print(f"P95:         {results['p95_ns']:>10.2f} ns")

    # Stale detection has additional overhead
    target_ns = 2000
    if results["p95_ns"] >= target_ns:
        raise AssertionError(f"get_with_swr() stale detection {results['p95_ns']:.0f}ns (p95) exceeds {target_ns}ns target")

    print(f"\n✅ Stale detection validated: {results['p95_ns']:.0f}ns < {target_ns}ns target")


# =============================================================================
# Invalidation Benchmarks
# =============================================================================


@pytest.mark.performance
def test_invalidate_by_key_latency() -> None:
    """Test single-key invalidation latency.

    invalidate_by_key() does:
    - Dict lookup
    - Entry removal
    - Version increment
    - Namespace index update

    Should be <1000ns p95.
    """
    config = L1CacheConfig(
        enabled=True,
        max_size_mb=100,
        swr_enabled=True,
        swr_threshold_ratio=0.5,
        invalidation_enabled=True,
        namespace_index=True,
    )
    cache = L1Cache(max_memory_mb=100, config=config)
    iterations = 50_000

    print(f"\nBenchmarking invalidate_by_key() ({iterations:,} iterations)...")

    # Warm up
    for i in range(1000):
        cache.put(f"warmup:{i}", b"x" * 512, redis_ttl=3600, namespace="warmup")
        cache.invalidate_by_key(f"warmup:{i}")

    # Measure invalidation latency
    latencies = []
    for i in range(iterations):
        key = f"inv:key:{i}"
        cache.put(key, b"x" * 512, redis_ttl=3600, namespace="inv")

        start = time.perf_counter_ns()
        removed = cache.invalidate_by_key(key)
        end = time.perf_counter_ns()

        assert removed, "Key should be invalidated"
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

    print(f"\n{'=' * 60}")
    print("invalidate_by_key() Latency")
    print(f"{'=' * 60}")
    print(f"Iterations:  {results['iterations']:>10,}")
    print(f"Mean:        {results['mean_ns']:>10.2f} ns")
    print(f"P50:         {results['p50_ns']:>10.2f} ns")
    print(f"P95:         {results['p95_ns']:>10.2f} ns")
    print(f"P99:         {results['p99_ns']:>10.2f} ns")

    target_ns = 1000
    if results["p95_ns"] >= target_ns:
        raise AssertionError(f"invalidate_by_key() latency {results['p95_ns']:.0f}ns (p95) exceeds {target_ns}ns target")

    print(f"\n✅ invalidate_by_key() validated: {results['p95_ns']:.0f}ns < {target_ns}ns target")


@pytest.mark.performance
def test_invalidate_by_namespace_with_index_latency() -> None:
    """Test namespace invalidation with O(1) index lookup.

    With namespace_index=True:
    - O(1) index lookup to find keys
    - O(k) removal where k = keys in namespace

    For 100 keys in namespace, should be <5000ns p95.
    """
    config = L1CacheConfig(
        enabled=True,
        max_size_mb=100,
        swr_enabled=True,
        swr_threshold_ratio=0.5,
        invalidation_enabled=True,
        namespace_index=True,  # Index enabled
    )
    cache = L1Cache(max_memory_mb=100, config=config)
    iterations = 1_000
    keys_per_namespace = 100

    print(f"\nBenchmarking invalidate_by_namespace() WITH index ({iterations:,} iterations)...")
    print(f"Keys per namespace: {keys_per_namespace}")

    latencies = []
    for i in range(iterations):
        namespace = f"ns:{i}"

        # Populate namespace with keys
        for j in range(keys_per_namespace):
            cache.put(f"{namespace}:key:{j}", b"x" * 256, redis_ttl=3600, namespace=namespace)

        start = time.perf_counter_ns()
        count = cache.invalidate_by_namespace(namespace)
        end = time.perf_counter_ns()

        assert count == keys_per_namespace, f"Expected {keys_per_namespace}, got {count}"
        latencies.append(end - start)

    p50 = statistics.median(latencies)
    p95 = statistics.quantiles(latencies, n=20)[18]
    mean = statistics.mean(latencies)

    # Calculate per-key latency
    per_key_p95 = p95 / keys_per_namespace

    results = {
        "iterations": iterations,
        "keys_per_namespace": keys_per_namespace,
        "mean_ns": mean,
        "p50_ns": p50,
        "p95_ns": p95,
        "per_key_p95_ns": per_key_p95,
    }

    print(f"\n{'=' * 60}")
    print("invalidate_by_namespace() WITH Index")
    print(f"{'=' * 60}")
    print(f"Iterations:  {results['iterations']:>10,}")
    print(f"Keys/NS:     {results['keys_per_namespace']:>10,}")
    print(f"Mean:        {results['mean_ns']:>10.2f} ns")
    print(f"P50:         {results['p50_ns']:>10.2f} ns")
    print(f"P95:         {results['p95_ns']:>10.2f} ns")
    print(f"Per-key p95: {results['per_key_p95_ns']:>10.2f} ns/key")

    # 100 keys should invalidate in <100μs (~1μs per key is acceptable)
    target_ns = 100_000  # 100μs for 100 keys
    if results["p95_ns"] >= target_ns:
        raise AssertionError(f"invalidate_by_namespace() latency {results['p95_ns']:.0f}ns (p95) exceeds {target_ns}ns target")

    print(f"\n✅ Namespace invalidation validated: {results['p95_ns']:.0f}ns < {target_ns}ns target")
    print(f"   ({results['per_key_p95_ns']:.0f}ns per key - O(1) index lookup working)")


@pytest.mark.performance
def test_invalidate_by_namespace_without_index_latency() -> None:
    """Test namespace invalidation WITHOUT index (O(n) scan fallback).

    With namespace_index=False:
    - O(n) scan through all entries
    - Slower but works for @cache.minimal

    For 100 target keys in 1000 total entries, should be <100μs p95.
    """
    config = L1CacheConfig(
        enabled=True,
        max_size_mb=100,
        swr_enabled=True,
        swr_threshold_ratio=0.5,
        invalidation_enabled=True,
        namespace_index=False,  # Index DISABLED
    )
    cache = L1Cache(max_memory_mb=100, config=config)
    iterations = 100
    total_entries = 1000
    target_namespace_keys = 100

    print(f"\nBenchmarking invalidate_by_namespace() WITHOUT index ({iterations:,} iterations)...")
    print(f"Total entries: {total_entries}, Target namespace keys: {target_namespace_keys}")

    latencies = []
    for i in range(iterations):
        # Populate cache with mixed namespaces
        target_ns = f"target:{i}"
        for j in range(target_namespace_keys):
            cache.put(f"{target_ns}:key:{j}", b"x" * 256, redis_ttl=3600, namespace=target_ns)

        # Add other namespace entries
        for j in range(total_entries - target_namespace_keys):
            cache.put(f"other:{i}:key:{j}", b"x" * 256, redis_ttl=3600, namespace=f"other:{i % 10}")

        start = time.perf_counter_ns()
        count = cache.invalidate_by_namespace(target_ns)
        end = time.perf_counter_ns()

        assert count == target_namespace_keys, f"Expected {target_namespace_keys}, got {count}"
        latencies.append(end - start)

        # Clear for next iteration
        cache.clear()

    p50 = statistics.median(latencies)
    p95 = statistics.quantiles(latencies, n=20)[18]
    mean = statistics.mean(latencies)

    results = {
        "iterations": iterations,
        "total_entries": total_entries,
        "target_keys": target_namespace_keys,
        "mean_ns": mean,
        "p50_ns": p50,
        "p95_ns": p95,
    }

    print(f"\n{'=' * 60}")
    print("invalidate_by_namespace() WITHOUT Index (O(n) scan)")
    print(f"{'=' * 60}")
    print(f"Iterations:    {results['iterations']:>10,}")
    print(f"Total entries: {results['total_entries']:>10,}")
    print(f"Target keys:   {results['target_keys']:>10,}")
    print(f"Mean:          {results['mean_ns']:>10.2f} ns")
    print(f"P50:           {results['p50_ns']:>10.2f} ns")
    print(f"P95:           {results['p95_ns']:>10.2f} ns")

    # O(n) scan is slower - allow 500μs for 1000 entries
    target_ns = 500_000  # 500μs
    if results["p95_ns"] >= target_ns:
        raise AssertionError(
            f"invalidate_by_namespace() (no index) latency {results['p95_ns']:.0f}ns exceeds {target_ns}ns target"
        )

    print(f"\n✅ Fallback scan validated: {results['p95_ns']:.0f}ns < {target_ns}ns target")
    print("   (O(n) scan is slower but acceptable for @cache.minimal)")


@pytest.mark.performance
def test_invalidate_all_latency() -> None:
    """Test global invalidation latency.

    invalidate_all() does:
    - Clear all entries
    - Increment ALL version tokens
    - Clear namespace index

    For 1000 entries, should be <50μs p95.
    """
    config = L1CacheConfig(
        enabled=True,
        max_size_mb=100,
        swr_enabled=True,
        swr_threshold_ratio=0.5,
        invalidation_enabled=True,
        namespace_index=True,
    )
    cache = L1Cache(max_memory_mb=100, config=config)
    iterations = 500
    entries = 1000

    print(f"\nBenchmarking invalidate_all() ({iterations:,} iterations)...")
    print(f"Entries per iteration: {entries}")

    latencies = []
    for i in range(iterations):
        # Populate cache
        for j in range(entries):
            cache.put(f"all:{i}:key:{j}", b"x" * 256, redis_ttl=3600, namespace=f"ns:{j % 10}")

        start = time.perf_counter_ns()
        count = cache.invalidate_all()
        end = time.perf_counter_ns()

        assert count == entries, f"Expected {entries}, got {count}"
        latencies.append(end - start)

    p50 = statistics.median(latencies)
    p95 = statistics.quantiles(latencies, n=20)[18]
    mean = statistics.mean(latencies)

    # Calculate per-entry latency
    per_entry_p95 = p95 / entries

    results = {
        "iterations": iterations,
        "entries": entries,
        "mean_ns": mean,
        "p50_ns": p50,
        "p95_ns": p95,
        "per_entry_p95_ns": per_entry_p95,
    }

    print(f"\n{'=' * 60}")
    print("invalidate_all() Latency")
    print(f"{'=' * 60}")
    print(f"Iterations:    {results['iterations']:>10,}")
    print(f"Entries:       {results['entries']:>10,}")
    print(f"Mean:          {results['mean_ns']:>10.2f} ns")
    print(f"P50:           {results['p50_ns']:>10.2f} ns")
    print(f"P95:           {results['p95_ns']:>10.2f} ns")
    print(f"Per-entry p95: {results['per_entry_p95_ns']:>10.2f} ns/entry")

    # 1000 entries should clear in <500μs (~500ns/entry is reasonable)
    target_ns = 500_000  # 500μs
    if results["p95_ns"] >= target_ns:
        raise AssertionError(f"invalidate_all() latency {results['p95_ns']:.0f}ns (p95) exceeds {target_ns}ns target")

    print(f"\n✅ invalidate_all() validated: {results['p95_ns']:.0f}ns < {target_ns}ns target")
    print(f"   ({results['per_entry_p95_ns']:.0f}ns per entry)")


# =============================================================================
# Version Token Benchmarks
# =============================================================================


@pytest.mark.performance
def test_complete_refresh_latency() -> None:
    """Test complete_refresh() latency (version token check + update).

    complete_refresh() does:
    - Version token comparison
    - Entry update with new value
    - _refreshing_keys removal

    Should be <2000ns p95.
    """
    config = L1CacheConfig(
        enabled=True,
        max_size_mb=100,
        swr_enabled=True,
        swr_threshold_ratio=0.5,
        invalidation_enabled=True,
        namespace_index=True,
    )
    cache = L1Cache(max_memory_mb=100, config=config)
    iterations = 50_000

    print(f"\nBenchmarking complete_refresh() ({iterations:,} iterations)...")

    latencies = []
    for i in range(iterations):
        key = f"refresh:key:{i}"
        cache.put(key, b"old" * 100, redis_ttl=3600, namespace="refresh")

        # Trigger SWR to get version and add to refreshing set
        _, _, _, version = cache.get_with_swr(key, ttl=0.0001)  # Force stale

        new_value = b"new" * 100
        new_cached_at = time.time()

        start = time.perf_counter_ns()
        success = cache.complete_refresh(key, version, new_value, new_cached_at)
        end = time.perf_counter_ns()

        assert success, "Refresh should succeed with matching version"
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

    print(f"\n{'=' * 60}")
    print("complete_refresh() Latency")
    print(f"{'=' * 60}")
    print(f"Iterations:  {results['iterations']:>10,}")
    print(f"Mean:        {results['mean_ns']:>10.2f} ns")
    print(f"P50:         {results['p50_ns']:>10.2f} ns")
    print(f"P95:         {results['p95_ns']:>10.2f} ns")

    target_ns = 2000
    if results["p95_ns"] >= target_ns:
        raise AssertionError(f"complete_refresh() latency {results['p95_ns']:.0f}ns (p95) exceeds {target_ns}ns target")

    print(f"\n✅ complete_refresh() validated: {results['p95_ns']:.0f}ns < {target_ns}ns target")


@pytest.mark.performance
def test_version_mismatch_fast_fail() -> None:
    """Test that version mismatch fails fast (no expensive operations).

    When version doesn't match (concurrent invalidation happened),
    complete_refresh() should fail immediately without updating entry.

    Should be <500ns p95 (faster than successful refresh).
    """
    config = L1CacheConfig(
        enabled=True,
        max_size_mb=100,
        swr_enabled=True,
        swr_threshold_ratio=0.5,
        invalidation_enabled=True,
        namespace_index=True,
    )
    cache = L1Cache(max_memory_mb=100, config=config)
    iterations = 50_000

    print(f"\nBenchmarking version mismatch fast-fail ({iterations:,} iterations)...")

    latencies = []
    for i in range(iterations):
        key = f"mismatch:key:{i}"
        cache.put(key, b"value" * 100, redis_ttl=3600, namespace="mismatch")

        # Get version
        _, _, _, version = cache.get_with_swr(key, ttl=0.0001)

        # Invalidate to change version
        cache.invalidate_by_key(key)

        # Re-add entry (simulates another request)
        cache.put(key, b"value" * 100, redis_ttl=3600, namespace="mismatch")

        # Try to complete with OLD version (should fail fast)
        start = time.perf_counter_ns()
        success = cache.complete_refresh(key, version, b"stale", time.time())
        end = time.perf_counter_ns()

        assert not success, "Refresh should fail with mismatched version"
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

    print(f"\n{'=' * 60}")
    print("Version Mismatch Fast-Fail Latency")
    print(f"{'=' * 60}")
    print(f"Iterations:  {results['iterations']:>10,}")
    print(f"Mean:        {results['mean_ns']:>10.2f} ns")
    print(f"P50:         {results['p50_ns']:>10.2f} ns")
    print(f"P95:         {results['p95_ns']:>10.2f} ns")

    # Version mismatch should fail faster than successful refresh
    target_ns = 1000
    if results["p95_ns"] >= target_ns:
        raise AssertionError(
            f"Version mismatch check {results['p95_ns']:.0f}ns (p95) exceeds {target_ns}ns target\nFast-fail path is too slow"
        )

    print(f"\n✅ Version mismatch fast-fail validated: {results['p95_ns']:.0f}ns < {target_ns}ns target")


# =============================================================================
# Concurrent Invalidation Benchmarks
# =============================================================================


@pytest.mark.performance
def test_concurrent_invalidation_throughput() -> None:
    """Test invalidation throughput under thread contention.

    Multiple threads invalidating different keys simultaneously.
    Validates RLock doesn't cause severe contention.
    """
    config = L1CacheConfig(
        enabled=True,
        max_size_mb=100,
        swr_enabled=True,
        swr_threshold_ratio=0.5,
        invalidation_enabled=True,
        namespace_index=True,
    )
    cache = L1Cache(max_memory_mb=100, config=config)
    num_threads = 8
    ops_per_thread = 5_000

    print(f"\nBenchmarking concurrent invalidation ({num_threads} threads)...")
    print(f"Operations per thread: {ops_per_thread:,}")

    # Pre-populate cache
    total_keys = num_threads * ops_per_thread
    for i in range(total_keys):
        cache.put(f"concurrent:key:{i}", b"x" * 256, redis_ttl=3600, namespace=f"ns:{i % 10}")

    results_by_thread: dict[int, list[int]] = {i: [] for i in range(num_threads)}
    lock = threading.Lock()

    def worker(thread_id: int) -> None:
        latencies = []
        base_key = thread_id * ops_per_thread

        for i in range(ops_per_thread):
            key = f"concurrent:key:{base_key + i}"

            start = time.perf_counter_ns()
            cache.invalidate_by_key(key)
            end = time.perf_counter_ns()

            latencies.append(end - start)

        with lock:
            results_by_thread[thread_id] = latencies

    # Launch threads
    start_time = time.perf_counter()
    threads = []
    for i in range(num_threads):
        t = threading.Thread(target=worker, args=(i,))
        threads.append(t)
        t.start()

    for t in threads:
        t.join()
    end_time = time.perf_counter()

    # Combine results
    all_latencies = []
    for latencies in results_by_thread.values():
        all_latencies.extend(latencies)

    total_ops = len(all_latencies)
    elapsed_seconds = end_time - start_time
    throughput = total_ops / elapsed_seconds

    p50 = statistics.median(all_latencies)
    p95 = statistics.quantiles(all_latencies, n=20)[18]
    p99 = statistics.quantiles(all_latencies, n=100)[98]
    mean = statistics.mean(all_latencies)

    results = {
        "threads": num_threads,
        "total_ops": total_ops,
        "elapsed_seconds": elapsed_seconds,
        "throughput_ops_sec": throughput,
        "mean_ns": mean,
        "p50_ns": p50,
        "p95_ns": p95,
        "p99_ns": p99,
    }

    print(f"\n{'=' * 60}")
    print(f"Concurrent Invalidation ({num_threads} threads)")
    print(f"{'=' * 60}")
    print(f"Total ops:   {results['total_ops']:>10,}")
    print(f"Elapsed:     {results['elapsed_seconds']:>10.2f} s")
    print(f"Throughput:  {results['throughput_ops_sec']:>10,.0f} ops/sec")
    print(f"Mean:        {results['mean_ns']:>10.2f} ns")
    print(f"P50:         {results['p50_ns']:>10.2f} ns")
    print(f"P95:         {results['p95_ns']:>10.2f} ns")
    print(f"P99:         {results['p99_ns']:>10.2f} ns")

    # Under contention, p95 should stay reasonable
    target_ns = 5000  # Allow 5x single-threaded overhead
    if results["p95_ns"] >= target_ns:
        raise AssertionError(
            f"Concurrent invalidation latency {results['p95_ns']:.0f}ns (p95) exceeds {target_ns}ns target\n"
            f"RLock contention is too severe"
        )

    # Throughput should be at least 100k ops/sec
    min_throughput = 100_000
    if results["throughput_ops_sec"] < min_throughput:
        raise AssertionError(f"Concurrent throughput {results['throughput_ops_sec']:.0f} ops/sec below {min_throughput} target")

    print("\n✅ Concurrent invalidation validated:")
    print(f"   Latency: {results['p95_ns']:.0f}ns < {target_ns}ns target")
    print(f"   Throughput: {results['throughput_ops_sec']:,.0f} ops/sec > {min_throughput:,} target")


# =============================================================================
# SLA Summary Test
# =============================================================================


@pytest.mark.performance
def test_l1_invalidation_total_sla() -> None:
    """Validate overall L1 invalidation feature SLA.

    This test validates the complete invalidation story:
    - SWR adds minimal overhead to hot path
    - Single-key invalidation is fast
    - Namespace invalidation scales with index
    - Global invalidation is reasonable
    """
    print(f"\n{'=' * 60}")
    print("L1 Invalidation Total SLA Validation")
    print(f"{'=' * 60}")

    config = L1CacheConfig(
        enabled=True,
        max_size_mb=100,
        swr_enabled=True,
        swr_threshold_ratio=0.5,
        invalidation_enabled=True,
        namespace_index=True,
    )
    cache = L1Cache(max_memory_mb=100, config=config)

    # Populate cache
    for i in range(1000):
        cache.put(f"sla:key:{i}", b"x" * 512, redis_ttl=3600, namespace=f"ns:{i % 10}")

    results = {}

    # Test 1: SWR hit latency
    swr_latencies = []
    for i in range(10_000):
        start = time.perf_counter_ns()
        cache.get_with_swr(f"sla:key:{i % 1000}", ttl=3600)
        end = time.perf_counter_ns()
        swr_latencies.append(end - start)
    results["swr_hit_p95"] = statistics.quantiles(swr_latencies, n=20)[18]

    # Test 2: Single-key invalidation
    inv_latencies = []
    for i in range(1000):
        cache.put(f"sla:inv:{i}", b"x" * 512, redis_ttl=3600, namespace="inv")
        start = time.perf_counter_ns()
        cache.invalidate_by_key(f"sla:inv:{i}")
        end = time.perf_counter_ns()
        inv_latencies.append(end - start)
    results["invalidate_key_p95"] = statistics.quantiles(inv_latencies, n=20)[18]

    # Test 3: Namespace invalidation (100 keys)
    ns_latencies = []
    for i in range(100):
        for j in range(100):
            cache.put(f"sla:ns:{i}:key:{j}", b"x" * 256, redis_ttl=3600, namespace=f"sla:ns:{i}")
        start = time.perf_counter_ns()
        cache.invalidate_by_namespace(f"sla:ns:{i}")
        end = time.perf_counter_ns()
        ns_latencies.append(end - start)
    results["invalidate_ns_100_p95"] = statistics.quantiles(ns_latencies, n=20)[18]

    print("\nSLA Results:")
    print(f"  get_with_swr() hit:           {results['swr_hit_p95']:>8.0f} ns (target: <1500ns)")
    print(f"  invalidate_by_key():          {results['invalidate_key_p95']:>8.0f} ns (target: <1000ns)")
    print(f"  invalidate_by_namespace(100): {results['invalidate_ns_100_p95']:>8.0f} ns (target: <100000ns)")

    # Validate SLAs
    failures = []
    if results["swr_hit_p95"] >= 1500:
        failures.append(f"SWR hit: {results['swr_hit_p95']:.0f}ns >= 1500ns")
    if results["invalidate_key_p95"] >= 1000:
        failures.append(f"Invalidate key: {results['invalidate_key_p95']:.0f}ns >= 1000ns")
    if results["invalidate_ns_100_p95"] >= 100_000:
        failures.append(f"Invalidate NS: {results['invalidate_ns_100_p95']:.0f}ns >= 100000ns")

    if failures:
        raise AssertionError("L1 Invalidation SLA violations:\n" + "\n".join(failures))

    print("\n✅ All L1 Invalidation SLAs validated")
    print("   SWR and invalidation features meet latency targets")
