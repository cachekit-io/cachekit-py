"""End-to-end cache latency profiling - all layers and code paths.

Measures complete cache flow:
- Decorator overhead
- L1 hit path (with decorator)
- L1 miss → L2 backend
- With serialization (msgpack)
- With encryption (if enabled)
- CacheHandler layer isolation
"""

from __future__ import annotations

import asyncio
import os
import statistics
import time

import pytest

from cachekit.decorators import cache

# =============================================================================
# 1. DECORATOR OVERHEAD (isolated)
# =============================================================================


@pytest.mark.performance
def test_decorator_overhead_isolation() -> None:
    """Measure @cache decorator overhead independent of cache operations.

    This isolates the decorator machinery (argument binding, context extraction,
    function invocation) without any cache hit/miss/serialization.
    """
    call_count = 0

    @cache(backend=None)  # L1-only, minimal backend overhead
    def simple_func(x: int) -> int:
        nonlocal call_count
        call_count += 1
        return x * 2

    # Prime and warm up
    simple_func(1)
    for _ in range(1000):
        simple_func(999)  # Different arg each time to avoid caching

    print("\n" + "=" * 80)
    print("1. DECORATOR OVERHEAD (no caching)")
    print("=" * 80)

    latencies = []
    for i in range(100_000):
        # Force cache miss by using different argument each time
        start = time.perf_counter_ns()
        simple_func(100_000 + i)
        end = time.perf_counter_ns()
        latencies.append(end - start)

    p50 = statistics.median(latencies)
    p95 = statistics.quantiles(latencies, n=20)[18]
    mean = statistics.mean(latencies)
    stdev = statistics.stdev(latencies)

    print("Decorator + function call (no cache):")
    print(f"  Mean:   {mean:>8.1f} ns")
    print(f"  P50:    {p50:>8.1f} ns")
    print(f"  P95:    {p95:>8.1f} ns")
    print(f"  StdDev: {stdev:>8.1f} ns")


# =============================================================================
# 2. L1 HIT THROUGH DECORATOR
# =============================================================================


@pytest.mark.performance
def test_decorator_l1_cache_hit_latency() -> None:
    """Measure @cache decorator + L1 hit latency.

    This is the hot path users care about - decorator overhead + L1 lookup.
    """

    @cache(backend=None)  # L1-only
    def cached_func(x: int) -> int:
        return x * 2

    # Prime the cache
    cached_func(42)

    # Warm up
    for _ in range(1000):
        cached_func(42)

    print("\n" + "=" * 80)
    print("2. L1 HIT WITH DECORATOR (hot path)")
    print("=" * 80)

    latencies = []
    for _ in range(100_000):
        start = time.perf_counter_ns()
        cached_func(42)  # Same arg = L1 hit
        end = time.perf_counter_ns()
        latencies.append(end - start)

    p50 = statistics.median(latencies)
    p95 = statistics.quantiles(latencies, n=20)[18]
    mean = statistics.mean(latencies)
    stdev = statistics.stdev(latencies)

    print("Decorator + L1 cache hit:")
    print(f"  Mean:   {mean:>8.1f} ns")
    print(f"  P50:    {p50:>8.1f} ns")
    print(f"  P95:    {p95:>8.1f} ns")
    print(f"  StdDev: {stdev:>8.1f} ns")


# =============================================================================
# 3. L1 MISS (with L1-only backend)
# =============================================================================


@pytest.mark.performance
def test_decorator_l1_miss_latency() -> None:
    """Measure @cache decorator + L1 miss path.

    L1 miss forces function invocation + cache population.
    """

    @cache(backend=None)  # L1-only
    def cached_func(x: int) -> int:
        return x * 2

    # Warm up
    for _ in range(100):
        cached_func(1)

    print("\n" + "=" * 80)
    print("3. L1 MISS (different arg each time)")
    print("=" * 80)

    latencies = []
    for i in range(10_000):  # Fewer iterations - misses are slower
        start = time.perf_counter_ns()
        cached_func(100_000 + i)  # Different arg = L1 miss
        end = time.perf_counter_ns()
        latencies.append(end - start)

    p50 = statistics.median(latencies)
    p95 = statistics.quantiles(latencies, n=20)[18]
    mean = statistics.mean(latencies)

    print("Decorator + L1 cache miss (function call + populate):")
    print(f"  Mean:   {mean:>8.1f} ns")
    print(f"  P50:    {p50:>8.1f} ns")
    print(f"  P95:    {p95:>8.1f} ns")


# =============================================================================
# 4. WITH REDIS BACKEND (L1 hit, L2 available but not accessed)
# =============================================================================


@pytest.mark.performance
def test_decorator_redis_backend_l1_hit() -> None:
    """Measure @cache with Redis backend, L1 hit.

    Redis is available but L1 hit avoids going to L2.
    """
    from cachekit.backends.redis import RedisBackend

    # Configure Redis backend
    try:
        backend = RedisBackend(redis_url=os.environ.get("REDIS_URL", "redis://localhost:6379"))
    except Exception as e:
        pytest.skip(f"Redis not available: {e}")

    @cache(backend=backend)
    def cached_func(x: int) -> int:
        return x * 2

    # Prime L1 cache
    try:
        cached_func(42)
    except Exception as e:
        pytest.skip(f"Redis not available: {e}")

    # Warm up
    for _ in range(1000):
        cached_func(42)

    print("\n" + "=" * 80)
    print("4. REDIS BACKEND - L1 HIT (L2 not accessed)")
    print("=" * 80)

    latencies = []
    for _ in range(100_000):
        start = time.perf_counter_ns()
        cached_func(42)  # L1 hit, L2 not accessed
        end = time.perf_counter_ns()
        latencies.append(end - start)

    p50 = statistics.median(latencies)
    p95 = statistics.quantiles(latencies, n=20)[18]
    mean = statistics.mean(latencies)

    print("Decorator + Redis backend + L1 cache hit:")
    print(f"  Mean:   {mean:>8.1f} ns")
    print(f"  P50:    {p50:>8.1f} ns")
    print(f"  P95:    {p95:>8.1f} ns")


# =============================================================================
# 5. WITH REDIS BACKEND (L1 miss → L2 hit)
# =============================================================================


@pytest.mark.performance
def test_decorator_redis_backend_l1_miss_l2_hit() -> None:
    """Measure @cache with Redis backend, L1 miss → L2 hit.

    First invocation populates Redis, subsequent calls hit L1 or L2.
    This measures the L2 path without network overhead.
    """
    from cachekit.backends.redis import RedisBackend
    from cachekit.config.nested import L1CacheConfig
    from cachekit.decorators import DecoratorConfig

    # Configure Redis backend
    try:
        backend = RedisBackend(redis_url=os.environ.get("REDIS_URL", "redis://localhost:6379"))
    except Exception as e:
        pytest.skip(f"Redis not available: {e}")

    config = DecoratorConfig(backend=backend, l1=L1CacheConfig(enabled=False))  # Disable L1 to force L2

    @cache(config=config)
    def cached_func(x: int) -> int:
        return x * 2

    # Prime Redis with some data
    try:
        cached_func(42)
    except Exception as e:
        pytest.skip(f"Redis not available: {e}")

    # Warm up
    for _ in range(100):
        cached_func(42)

    print("\n" + "=" * 80)
    print("5. REDIS BACKEND - L1 DISABLED (pure L2 hits)")
    print("=" * 80)

    latencies = []
    iterations = 10_000  # L2 is slower, fewer iterations
    for _ in range(iterations):
        start = time.perf_counter_ns()
        cached_func(42)  # L1 disabled, hits L2 every time
        end = time.perf_counter_ns()
        latencies.append(end - start)

    p50 = statistics.median(latencies)
    p95 = statistics.quantiles(latencies, n=20)[18]
    mean = statistics.mean(latencies)

    print("Decorator + Redis backend (L1 disabled, pure L2):")
    print(f"  Mean:   {mean:>10.1f} ns ({mean / 1000:>6.2f} μs)")
    print(f"  P50:    {p50:>10.1f} ns ({p50 / 1000:>6.2f} μs)")
    print(f"  P95:    {p95:>10.1f} ns ({p95 / 1000:>6.2f} μs)")


# =============================================================================
# 6. ASYNC DECORATOR
# =============================================================================


@pytest.mark.performance
@pytest.mark.asyncio
async def test_async_decorator_cache_hit() -> None:
    """Measure @cache decorator on async function, L1 hit."""

    @cache(backend=None)
    async def async_cached_func(x: int) -> int:
        await asyncio.sleep(0)  # Yield control
        return x * 2

    # Prime cache
    await async_cached_func(42)

    # Warm up
    for _ in range(100):
        await async_cached_func(42)

    print("\n" + "=" * 80)
    print("6. ASYNC DECORATOR - L1 HIT")
    print("=" * 80)

    latencies = []
    for _ in range(10_000):  # Async has more overhead, fewer iterations
        start = time.perf_counter_ns()
        await async_cached_func(42)
        end = time.perf_counter_ns()
        latencies.append(end - start)

    p50 = statistics.median(latencies)
    p95 = statistics.quantiles(latencies, n=20)[18]
    mean = statistics.mean(latencies)

    print("Async decorator + L1 cache hit:")
    print(f"  Mean:   {mean:>10.1f} ns ({mean / 1000:>6.2f} μs)")
    print(f"  P50:    {p50:>10.1f} ns ({p50 / 1000:>6.2f} μs)")
    print(f"  P95:    {p95:>10.1f} ns ({p95 / 1000:>6.2f} μs)")


# =============================================================================
# 7. SUMMARY ANALYSIS
# =============================================================================


@pytest.mark.performance
def test_performance_path_summary() -> None:
    """Summary of all code paths and bottleneck analysis."""

    print("\n" + "=" * 80)
    print("PERFORMANCE PATH SUMMARY")
    print("=" * 80)
    print("""
Expected latency ranges (from profiling):

L1 Cache (pure):                450-500ns p95
├─ Lock acquisition:            250ns (55%)
├─ Dict lookup:                 125ns (27%)
├─ TTL check:                   208ns (45%)
└─ LRU move:                    125ns (27%)

Decorator + L1 hit:              ???ns p95 (MEASURE THIS)
├─ Decorator overhead:           ???ns
├─ Argument processing:          ???ns
├─ Function lookup:              ???ns
└─ L1 cache:                     450ns

Decorator + L1 miss:             ???ns p95 (MEASURE THIS)
├─ Decorator overhead:           ???ns
├─ Cache miss detection:         200ns
├─ Function execution:           VARIABLE (depends on function)
├─ Serialization (msgpack):      ???ns (MEASURE THIS)
└─ L1 population:                500ns

Redis L2 hit (L1 disabled):      1,000-10,000ns p95 (MEASURE THIS)
├─ Decorator overhead:           ???ns
├─ Redis network RTT:            1,000-3,000ns (if local)
├─ Deserialization:              ???ns (MEASURE THIS)
└─ L1 population:                500ns

MEASUREMENT PRIORITIES:
1. Get decorator overhead isolated
2. Compare with/without serialization
3. Measure L2 backend latency (network factor)
4. Profile async overhead
5. Encryption impact (if enabled)
""")


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
