"""Example: Statistically rigorous L1 cache hit performance test.

Demonstrates best practices for performance testing:
- Multiple independent runs (5 runs); the run, not the sample, is the unit of inference
- A run-level confidence interval (t over per-run medians), not just a p95 point estimate
- Percentiles over every raw sample: outliers are counted, never filtered
- JIT warmup with variance monitoring

The figures are informational: printed and recorded with ``record_property``, never asserted. A
nanosecond interval measured on one host says nothing about a loaded one, and a threshold
on it fails on a busy neighbour, not a regression. Each test asserts from the cache's own hit/miss
counters that every call it timed took the path it reports (hit or miss).
"""

from __future__ import annotations

import pytest

from cachekit.l1_cache import L1Cache

from .stats_utils import PerformanceResult, benchmark_with_gc_handling, coefficient_of_variation


def _record(record_property, prefix: str, result: PerformanceResult) -> None:
    """Attach the run-level estimate and the raw p95 to the test's report."""
    record_property(f"{prefix}_center_{result.unit}", round(result.center, 1))
    record_property(f"{prefix}_ci95_upper_{result.unit}", round(result.ci_95_upper, 1))
    record_property(f"{prefix}_p95_{result.unit}", round(result.p95, 1))


def _counts(cache: L1Cache) -> tuple[int, int]:
    stats = cache.get_stats()
    return stats["hits"], stats["misses"]


@pytest.mark.performance
def test_l1_cache_hit_statistically_rigorous(record_property) -> None:
    """L1 cache hit latency with statistical rigor.

    Runs benchmark 5 times independently with:
    - A run-level confidence interval to detect real changes
    - Every raw sample kept (outliers counted as a diagnostic)
    - JIT stabilization monitoring

    This is the "right way" to benchmark small operations.
    """
    cache = L1Cache(max_memory_mb=100)

    # Pre-populate cache
    for i in range(100):
        cache.put(f"hit:{i}", b"x" * 1024, redis_ttl=3600)

    # Benchmark with statistical rigor
    result = benchmark_with_gc_handling(
        name="L1 Cache Hit (Statistically Rigorous)",
        fn=lambda: cache.get("hit:42"),
        iterations_per_run=10_000,
        runs=5,  # 5 independent runs
        warmup_iterations=2000,
        unit="ns",
    )

    # Print detailed results
    print(f"\n{result}")
    print(
        f"\nInterpretation:\n"
        f"  • p95={result.p95:.0f}ns over every raw sample (no tail claim at 5 runs)\n"
        f"  • 95% CI=[{result.ci_95_lower:.0f}, {result.ci_95_upper:.0f}]ns: the run-level estimate (mean of per-run medians)\n"
        f"  • {result.outlier_count} outliers counted (kept in every percentile)\n"
        f"  • JIT warmed up in {result.jit_warmup_samples:,} iterations\n"
    )

    _record(record_property, "hit", result)

    hits, misses = _counts(cache)
    assert misses == 0 and hits >= result.samples, f"{misses} timed lookups missed"


@pytest.mark.performance
def test_l1_cache_miss_statistically_rigorous(record_property) -> None:
    """L1 cache miss latency with statistical rigor.

    Misses should be fast - validates dict lookup performance.
    """
    cache = L1Cache(max_memory_mb=100)

    # Lookup that will never exist
    counter = [0]

    def miss_fn() -> None:
        """Generate unique key for miss."""
        found, _ = cache.get(f"miss:{counter[0]}")
        counter[0] += 1
        assert not found

    result = benchmark_with_gc_handling(
        name="L1 Cache Miss (Statistically Rigorous)",
        fn=miss_fn,
        iterations_per_run=10_000,
        runs=5,
        warmup_iterations=1000,
        unit="ns",
    )

    print(f"\n{result}")

    _record(record_property, "miss", result)

    hits, misses = _counts(cache)
    assert hits == 0 and misses == counter[0] >= result.samples


@pytest.mark.performance
def test_l1_cache_comparison_with_effect_size(record_property) -> None:
    """Demonstrate effect size detection: compare hit vs miss performance.

    Shows how to detect whether an observed difference is meaningful
    or just measurement noise.
    """
    cache = L1Cache(max_memory_mb=100)

    # Populate for hits
    for i in range(100):
        cache.put(f"cmp:{i}", b"x" * 512, redis_ttl=3600)

    # Benchmark hits
    hit_result = benchmark_with_gc_handling(
        name="L1 Cache Hit (Comparison)",
        fn=lambda: cache.get("cmp:42"),
        iterations_per_run=5_000,
        runs=5,
        warmup_iterations=1000,
        unit="ns",
    )
    hits_after_hit_arm = _counts(cache)
    assert hits_after_hit_arm[1] == 0, "a timed hit missed"

    # Benchmark misses (same cache)
    counter = [0]

    def miss_fn() -> None:
        found, _ = cache.get(f"cmp:nomatch:{counter[0]}")
        counter[0] += 1

    miss_result = benchmark_with_gc_handling(
        name="L1 Cache Miss (Comparison)",
        fn=miss_fn,
        iterations_per_run=5_000,
        runs=5,
        warmup_iterations=1000,
        unit="ns",
    )

    # Print comparison
    print(f"\n{'=' * 70}")
    print("COMPARISON: Hit vs Miss Performance")
    print(f"{'=' * 70}")
    print(f"\n{hit_result}")
    print(f"\n{miss_result}")

    # Analyze difference
    hit_mean = hit_result.mean
    miss_mean = miss_result.mean
    diff_percent = ((hit_mean - miss_mean) / miss_mean) * 100

    print("\nDifference:")
    print(f"  Hit mean:   {hit_mean:>10.0f}ns")
    print(f"  Miss mean:  {miss_mean:>10.0f}ns")
    print(f"  Difference: {diff_percent:>+10.1f}%")

    # Check if difference is statistically significant
    from .stats_utils import effect_size_significant

    is_significant = effect_size_significant(miss_result, hit_result, threshold=0.05)

    if is_significant:
        print("\n✅ Difference IS significant (beyond 5% and beyond both run-level bands)")
    else:
        print("\n⚠️  Difference is NOT statistically significant (measurement noise)")

    _record(record_property, "hit", hit_result)
    _record(record_property, "miss", miss_result)
    record_property("difference_significant", is_significant)

    # The miss arm added misses only: the two arms timed the paths they are named for.
    assert _counts(cache) == (hits_after_hit_arm[0], counter[0])


@pytest.mark.performance
def test_confidence_intervals_matter(record_property) -> None:
    """Demonstrates where the uncertainty comes from: runs, not samples.

    Ten times the samples per run barely narrows the run-level interval, because what varies
    is the host from one run to the next. Only more runs narrow it.
    """
    cache = L1Cache(max_memory_mb=100)

    # Populate cache
    for i in range(100):
        cache.put(f"ci:{i}", b"x" * 1024, redis_ttl=3600)

    # Same run count, different samples per run
    result_small = benchmark_with_gc_handling(
        name="L1 Cache Hit (5 runs x 5k samples)",
        fn=lambda: cache.get("ci:42"),
        iterations_per_run=5_000,
        runs=5,
        warmup_iterations=500,
        unit="ns",
    )

    result_large = benchmark_with_gc_handling(
        name="L1 Cache Hit (5 runs x 50k samples)",
        fn=lambda: cache.get("ci:42"),
        iterations_per_run=50_000,
        runs=5,
        warmup_iterations=5000,
        unit="ns",
    )

    print(f"\n{'=' * 70}")
    print("CONFIDENCE INTERVAL COMPARISON: 5k vs 50k samples per run")
    print(f"{'=' * 70}")
    print(f"\n{result_small}")
    print(f"\nCI width: {result_small.ci_95_upper - result_small.ci_95_lower:.0f}ns")
    print(f"\n{result_large}")
    print(f"\nCI width: {result_large.ci_95_upper - result_large.ci_95_lower:.0f}ns")

    print(
        "\nKey insight:\n"
        "  The interval is a t-interval over 5 run medians, so its width tracks run-to-run spread.\n"
        "  Pooling 250k samples as if independent would report an interval a few ns wide, which\n"
        "  the next run would not reproduce. To narrow it, add runs, not samples."
    )

    _record(record_property, "small", result_small)
    _record(record_property, "large", result_large)

    # Same runs, ten times the samples per run: the comparison is only about samples per run.
    assert (result_small.runs, result_large.runs) == (5, 5)
    assert result_large.samples == 10 * result_small.samples
    assert _counts(cache)[1] == 0, "a timed lookup missed"


@pytest.mark.performance
def test_l1_cache_speedup_ratio_validation(record_property) -> None:
    """Report L1 hit and miss latency, and the hit/miss ratio.

    In L1-only mode (no L2 backend), hits and misses are both fast dict
    operations. Hits include LRU bookkeeping (move_to_end), so they may
    be slightly slower than misses — this is expected.

    Real speedup (10-20x) only manifests when L2 backend (Redis/SaaS)
    adds network latency to misses.
    """
    cache = L1Cache(max_memory_mb=100)

    # Populate cache for hits
    for i in range(100):
        cache.put(f"speedup:key:{i}", b"x" * 1024, redis_ttl=3600)

    # Measure hit performance
    hit_result = benchmark_with_gc_handling(
        name="L1 Cache Hits (Speedup Test)",
        fn=lambda: cache.get("speedup:key:42"),
        iterations_per_run=10_000,
        runs=3,
        warmup_iterations=1000,
        unit="ns",
    )
    hits_after_hit_arm = _counts(cache)
    assert hits_after_hit_arm[1] == 0, "a timed hit missed"

    # Measure miss performance (different keys, never cached)
    counter = [0]

    def miss_fn() -> None:
        found, _ = cache.get(f"speedup:miss:{counter[0]}")
        counter[0] += 1

    miss_result = benchmark_with_gc_handling(
        name="L1 Cache Misses (Speedup Test)",
        fn=miss_fn,
        iterations_per_run=10_000,
        runs=3,
        warmup_iterations=1000,
        unit="ns",
    )

    print("\nL1 Performance Analysis:")
    print(f"  Hit mean:   {hit_result.mean:.0f}ns")
    print(f"  Miss mean:  {miss_result.mean:.0f}ns")

    # Informational: a hit's LRU bookkeeping makes it a little slower than a miss, but the ratio
    # of two sub-microsecond means follows host load, not cachekit.
    overhead_ratio = hit_result.mean / miss_result.mean
    print(f"  Hit/Miss ratio: {overhead_ratio:.2f}x")
    _record(record_property, "hit", hit_result)
    _record(record_property, "miss", miss_result)
    record_property("hit_miss_ratio", round(overhead_ratio, 3))

    assert _counts(cache) == (hits_after_hit_arm[0], counter[0])


@pytest.mark.performance
def test_l1_cache_consistency_with_coefficient_of_variation(record_property) -> None:
    """Report how consistent L1 cache performance is from run to run. The CV is informational.

    The CV of run medians measures the host as much as cachekit: on a loaded host it crossed 0.20
    in at least 2 of 6 back-to-back runs (2026-10-03) with nothing changed, so it cannot gate.

    Coefficient of variation (CV) measures consistency:
    - CV < 0.05: Excellent (highly stable)
    - CV < 0.10: Very good (typical for L1 cached operations)
    - CV > 0.20: Poor (high variance indicates measurement issues)
    """
    cache = L1Cache(max_memory_mb=100)

    # Populate cache
    for i in range(100):
        cache.put(f"cv:key:{i}", b"x" * 1024, redis_ttl=3600)

    result = benchmark_with_gc_handling(
        name="L1 Cache Consistency Test",
        fn=lambda: cache.get("cv:key:42"),
        iterations_per_run=10_000,
        runs=5,
        warmup_iterations=1000,
        unit="ns",
    )

    # CV of the run medians: run-to-run consistency. A CV over raw samples is dominated by rare
    # multi-microsecond scheduler spikes (it read 0.98 even on the old trimmed samples).
    cv = coefficient_of_variation(result.run_medians)

    print("\nConsistency Analysis (Coefficient of Variation of run medians):")
    print(f"  Run medians: {', '.join(f'{m:.0f}' for m in result.run_medians)} ns")
    print(f"  CV:         {cv:.3f} ({cv * 100:.1f}%)")

    if cv < 0.05:
        consistency = "Excellent (highly stable)"
    elif cv < 0.10:
        consistency = "Very good (typical for L1 cached ops)"
    elif cv < 0.20:
        consistency = "Good"
    else:
        consistency = "Poor (high variance indicates issues)"

    print(f"  Rating:     {consistency}")

    print(f"ℹ️  Consistency (informational): CV={cv:.3f}")
    record_property("cv_run_medians", round(cv, 4))

    # The CV is over 5 run medians of hits; a miss among them would time the wrong path.
    assert len(result.run_medians) == 5
    hits, misses = _counts(cache)
    assert misses == 0 and hits >= result.samples, f"{misses} timed lookups missed"


@pytest.mark.performance
def test_l1_cache_with_multiple_payload_sizes(record_property) -> None:
    """Report L1 cache hit latency across payload sizes (informational)."""
    cache = L1Cache(max_memory_mb=100)

    payload_configs = {
        "tiny": b"x" * 50,  # 50 bytes
        "small": b"x" * 500,  # 500 bytes
        "medium": b"x" * 1024,  # 1KB
        "large": b"x" * 10240,  # 10KB
    }

    results = {}

    for size_name, payload in payload_configs.items():
        # Populate cache
        cache.put(f"payload:{size_name}:test", payload, redis_ttl=3600)

        # Measure hit latency
        result = benchmark_with_gc_handling(
            name=f"L1 Cache Hit ({size_name}: {len(payload)} bytes)",
            fn=lambda pk=f"payload:{size_name}:test": cache.get(pk),
            iterations_per_run=5_000,
            runs=3,
            warmup_iterations=500,
            unit="ns",
        )

        results[size_name] = result
        assert cache.get(f"payload:{size_name}:test") == (True, payload)

    # Print comparison
    print(f"\n{'=' * 70}")
    print("L1 Cache Performance by Payload Size")
    print(f"{'=' * 70}")
    for size_name, result in results.items():
        print(f"\n{size_name.upper()}:")
        print(f"  Mean:  {result.mean:>10.0f}ns")
        print(f"  P95:   {result.p95:>10.0f}ns")
        print(f"  P99:   {result.p99:>10.0f}ns")

    tiny_mean = results["tiny"].mean
    large_mean = results["large"].mean
    for size_name, result in results.items():
        _record(record_property, size_name, result)

    print(f"\n   Tiny (50B):  {tiny_mean:.0f}ns")
    print(f"   Large (10K): {large_mean:.0f}ns ({(large_mean / tiny_mean):.1f}x slower)")

    assert _counts(cache)[1] == 0, "a timed lookup missed"
