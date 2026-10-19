"""End-to-end cache latency profiling - all layers and code paths.

Measures complete cache flow:
- L1 hit path (with decorator)
- L1 miss (function call + populate)
- L1 hit and pure L2 hit with a Redis backend
- Async decorator L1 hit

Each test asserts that the path it timed is the path it claims: hits never call the function,
an L1 hit never reads L2, and every L1-disabled call reads L2.
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import patch

import pytest

from cachekit.decorators import cache

from .stats_utils import report

# =============================================================================
# 1. L1 HIT THROUGH DECORATOR
# =============================================================================


@pytest.mark.performance
def test_decorator_l1_cache_hit_latency(record_property) -> None:
    """Measure @cache decorator + L1 hit latency.

    This is the hot path users care about - decorator overhead + L1 lookup.
    """
    call_count = 0

    @cache(backend=None)  # L1-only
    def cached_func(x: int) -> int:
        nonlocal call_count
        call_count += 1
        return x * 2

    # Prime the cache
    cached_func(42)

    # Warm up
    for _ in range(1000):
        cached_func(42)

    print("\n" + "=" * 80)
    print("1. L1 HIT WITH DECORATOR (hot path)")
    print("=" * 80)

    latencies = []
    for _ in range(100_000):
        start = time.perf_counter_ns()
        result = cached_func(42)  # Same arg = L1 hit
        latencies.append(time.perf_counter_ns() - start)

    report(record_property, "Decorator + L1 cache hit", latencies)
    assert result == 84
    assert call_count == 1, f"{call_count - 1} measured calls missed L1"


# =============================================================================
# 2. L1 MISS (with L1-only backend)
# =============================================================================


@pytest.mark.performance
def test_decorator_l1_miss_latency(record_property) -> None:
    """Measure @cache decorator + L1 miss path.

    L1 miss forces function invocation + cache population.
    """
    called_with: list[int] = []

    @cache(backend=None)  # L1-only
    def cached_func(x: int) -> int:
        called_with.append(x)
        return x * 2

    # Warm up
    for _ in range(100):
        cached_func(1)

    print("\n" + "=" * 80)
    print("2. L1 MISS (different arg each time)")
    print("=" * 80)

    args = range(100_000, 110_000)  # Fewer iterations - misses are slower
    latencies = []
    results = []
    for x in args:
        start = time.perf_counter_ns()
        results.append(cached_func(x))  # Different arg = L1 miss
        latencies.append(time.perf_counter_ns() - start)

    report(record_property, "Decorator + L1 cache miss (function call + populate)", latencies)
    assert results == [x * 2 for x in args]
    assert called_with == [1, *args], "every new argument must miss and run the function exactly once"


# =============================================================================
# 3. WITH REDIS BACKEND (L1 hit, L2 available but not accessed)
# =============================================================================


@pytest.mark.performance
def test_decorator_redis_backend_l1_hit(perf_redis_backend, record_property) -> None:
    """Measure @cache with Redis backend, L1 hit.

    Redis is available but L1 hit avoids going to L2.
    """
    backend = perf_redis_backend
    call_count = 0

    @cache(backend=backend)
    def cached_func(x: int) -> int:
        nonlocal call_count
        call_count += 1
        return x * 2

    # Prime L1 cache
    cached_func(42)

    # Warm up
    for _ in range(1000):
        cached_func(42)

    print("\n" + "=" * 80)
    print("3. REDIS BACKEND - L1 HIT (L2 not accessed)")
    print("=" * 80)

    latencies = []
    with patch.object(backend, "get", wraps=backend.get) as l2_get:
        for _ in range(100_000):
            start = time.perf_counter_ns()
            result = cached_func(42)  # L1 hit, L2 not accessed
            latencies.append(time.perf_counter_ns() - start)

    report(record_property, "Decorator + Redis backend + L1 cache hit", latencies)
    assert result == 84
    assert call_count == 1
    assert l2_get.call_count == 0, f"{l2_get.call_count} measured calls read L2"


# =============================================================================
# 4. WITH REDIS BACKEND (L1 miss → L2 hit)
# =============================================================================


@pytest.mark.performance
def test_decorator_redis_backend_l1_miss_l2_hit(perf_redis_backend, record_property) -> None:
    """Measure @cache with Redis backend, L1 disabled: every call is an L2 hit.

    The first call populates Redis; every measured call then reads it back.
    """
    from cachekit.config.nested import L1CacheConfig
    from cachekit.decorators import DecoratorConfig

    backend = perf_redis_backend
    config = DecoratorConfig(backend=backend, l1=L1CacheConfig(enabled=False))  # Disable L1 to force L2
    call_count = 0

    @cache(config=config)
    def cached_func(x: int) -> int:
        nonlocal call_count
        call_count += 1
        return x * 2

    # Prime Redis
    cached_func(42)

    # Warm up
    for _ in range(100):
        cached_func(42)

    print("\n" + "=" * 80)
    print("4. REDIS BACKEND - L1 DISABLED (pure L2 hits)")
    print("=" * 80)

    iterations = 10_000  # L2 is slower, fewer iterations
    latencies = []
    with patch.object(backend, "get", wraps=backend.get) as l2_get:
        for _ in range(iterations):
            start = time.perf_counter_ns()
            result = cached_func(42)  # L1 disabled, hits L2 every time
            latencies.append(time.perf_counter_ns() - start)

    report(record_property, "Decorator + Redis backend (L1 disabled, pure L2)", latencies)
    assert result == 84
    assert call_count == 1, f"{call_count - 1} calls missed L2"
    assert l2_get.call_count == iterations, f"{l2_get.call_count} L2 reads for {iterations} calls"


# =============================================================================
# 5. ASYNC DECORATOR
# =============================================================================


@pytest.mark.performance
@pytest.mark.asyncio
async def test_async_decorator_cache_hit(record_property) -> None:
    """Measure @cache decorator on async function, L1 hit."""
    call_count = 0

    @cache(backend=None)
    async def async_cached_func(x: int) -> int:
        nonlocal call_count
        call_count += 1
        await asyncio.sleep(0)  # Yield control
        return x * 2

    # Prime cache
    await async_cached_func(42)

    # Warm up
    for _ in range(100):
        await async_cached_func(42)

    print("\n" + "=" * 80)
    print("5. ASYNC DECORATOR - L1 HIT")
    print("=" * 80)

    latencies = []
    for _ in range(10_000):  # Async has more overhead, fewer iterations
        start = time.perf_counter_ns()
        result = await async_cached_func(42)
        latencies.append(time.perf_counter_ns() - start)

    report(record_property, "Async decorator + L1 cache hit", latencies)
    assert result == 84
    assert call_count == 1, f"{call_count - 1} measured calls missed L1"


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
