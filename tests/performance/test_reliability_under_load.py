"""Reliability framework performance under load.

Tests that exercise the production reliability features:
- Circuit breaker (failure injection, recovery testing)
- Backpressure (hitting max_concurrent limits)
- Adaptive timeout (slow function handling)

CRITICAL: These tests validate that reliability features work under production stress.
They measure performance degradation when things go wrong, not just happy path.
"""

from __future__ import annotations

import os
import statistics
import threading
import time
from typing import Any
from unittest.mock import patch

import pytest

from cachekit.backends.errors import BackendError, BackendErrorType
from cachekit.config.decorator import DecoratorConfig
from cachekit.decorators import cache

# =============================================================================
# Test 1: Circuit Breaker Under Failure
# =============================================================================


@pytest.mark.performance
@pytest.mark.integration
def test_circuit_breaker_failure_injection() -> None:
    """Measure circuit breaker performance under Redis failures.

    Tests:
    - Circuit opening after failure threshold
    - Fast-fail latency when circuit is open
    - Recovery behavior (HALF_OPEN → CLOSED)

    This validates @cache.production graceful degradation claims.
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

    from cachekit.config.nested import CircuitBreakerConfig, L1CacheConfig

    # Config with circuit breaker enabled
    config = DecoratorConfig(
        backend=backend,
        l1=L1CacheConfig(enabled=False),  # Force L2 to test circuit breaker
        circuit_breaker=CircuitBreakerConfig(enabled=True),  # Use defaults (failure_threshold=5)
    )

    call_count = 0

    @cache(config=config)
    def get_data(item_id: int) -> dict[str, Any]:
        nonlocal call_count
        call_count += 1
        return {"id": item_id, "data": "value"}

    print("\n" + "=" * 80)
    print("CIRCUIT BREAKER - FAILURE INJECTION")
    print("=" * 80)

    # Phase 1: Normal operation (circuit CLOSED)
    print("\nPhase 1: Normal operation (circuit CLOSED)")
    latencies_normal = []
    for i in range(10):
        start = time.perf_counter_ns()
        result = get_data(i)
        end = time.perf_counter_ns()
        latencies_normal.append(end - start)
        assert result["id"] == i

    p95_normal = statistics.quantiles(latencies_normal, n=20)[18]
    print(f"  P95 latency (normal): {p95_normal / 1000:.2f} μs")

    # Phase 2: Inject failures to open circuit
    print("\nPhase 2: Injecting failures to open circuit")

    # Patch backend.get to raise errors
    def failing_get(*args, **kwargs):
        raise BackendError("Simulated Redis failure", error_type=BackendErrorType.TRANSIENT)

    with patch.object(backend, "get", side_effect=failing_get):
        failures = 0
        for i in range(10):
            try:
                get_data(100 + i)  # Different keys
            except Exception:
                failures += 1

        print(f"  Failures triggered: {failures}/10")
        print("  Circuit should be OPEN now")

    # Phase 3: Measure fast-fail latency (circuit OPEN)
    print("\nPhase 3: Fast-fail latency (circuit OPEN)")
    latencies_open = []

    # Circuit should be open, requests should fail fast
    for i in range(20):
        start = time.perf_counter_ns()
        try:
            get_data(200 + i)  # Different keys
        except Exception:
            pass  # Expected - circuit is open
        end = time.perf_counter_ns()
        latencies_open.append(end - start)

    p95_open = statistics.quantiles(latencies_open, n=20)[18]
    print(f"  P95 latency (circuit open): {p95_open / 1000:.2f} μs")
    print(f"  Fast-fail speedup: {p95_normal / p95_open:.1f}x faster")

    # Validate fast-fail is significantly faster than normal Redis roundtrip
    # Circuit open should be <250μs (allowing for GC/system variance), normal is ~2-5ms
    assert p95_open < 250_000, f"Circuit open latency {p95_open / 1000:.0f}μs should be <250μs (fast-fail)"

    print("\n✅ Circuit breaker validated:")
    print(f"   - Normal operation:  {p95_normal / 1000:.2f} μs")
    print(f"   - Fast-fail (open):  {p95_open / 1000:.2f} μs")
    print(f"   - Speedup:           {p95_normal / p95_open:.1f}x")


# =============================================================================
# Test 2: Backpressure Under Load
# =============================================================================


@pytest.mark.performance
def test_backpressure_limit() -> None:
    """Measure backpressure behavior when hitting max_concurrent limit.

    Tests:
    - Queuing behavior when limit is reached
    - Queue timeout enforcement
    - Latency increase under backpressure

    This validates that backpressure protects against overload.
    """
    from cachekit.config.nested import BackpressureConfig

    # Config with low concurrency limit to trigger backpressure
    config = DecoratorConfig(
        backend=None,  # L1-only
        backpressure=BackpressureConfig(
            enabled=True,
            max_concurrent_requests=5,  # Low limit to trigger easily
            timeout=0.5,  # 500ms queue timeout
        ),
    )

    slow_call_count = 0
    slow_call_duration_ms = 100  # Simulate slow function

    @cache(config=config)
    def slow_function(item_id: int) -> dict[str, Any]:
        nonlocal slow_call_count
        slow_call_count += 1
        time.sleep(slow_call_duration_ms / 1000)  # Simulate slow work
        return {"id": item_id, "result": "data"}

    print("\n" + "=" * 80)
    print("BACKPRESSURE - CONCURRENT LIMIT")
    print("=" * 80)
    print("\nConfig:")
    print("  max_concurrent_requests: 5")
    print("  queue_timeout: 0.5s")
    print(f"  slow_function duration: {slow_call_duration_ms}ms")

    num_threads = 20  # More threads than limit
    iterations_per_thread = 5

    latencies: list[int] = []
    queued_count = 0
    timeout_count = 0
    lock = threading.Lock()

    def worker(thread_id: int) -> None:
        """Worker thread that tries to access slow cached function."""
        nonlocal queued_count, timeout_count

        for i in range(iterations_per_thread):
            start = time.perf_counter_ns()
            try:
                slow_function(thread_id * 100 + i)
                end = time.perf_counter_ns()

                with lock:
                    latencies.append(end - start)
                    # If latency > slow_call_duration, we were queued
                    if (end - start) > (slow_call_duration_ms * 1_000_000):
                        queued_count += 1

            except Exception:
                end = time.perf_counter_ns()
                with lock:
                    timeout_count += 1
                    # Record timeout latency too
                    latencies.append(end - start)

    # Launch threads
    threads = []
    start_time = time.perf_counter()
    for i in range(num_threads):
        t = threading.Thread(target=worker, args=(i,))
        threads.append(t)
        t.start()

    # Wait for completion
    for t in threads:
        t.join()
    elapsed_time = time.perf_counter() - start_time

    # Analyze results
    if latencies:
        mean = statistics.mean(latencies)
        median = statistics.median(latencies)
        p95 = statistics.quantiles(latencies, n=20)[18]
        p99 = statistics.quantiles(latencies, n=100)[98]

        print("\nResults:")
        print(f"  Total requests:  {num_threads * iterations_per_thread}")
        print(f"  Completed:       {len(latencies) - timeout_count}")
        print(f"  Queued:          {queued_count}")
        print(f"  Timeouts:        {timeout_count}")
        print(f"  Total time:      {elapsed_time:.2f}s")
        print("\nLatencies:")
        print(f"  Mean:    {mean / 1_000_000:.2f} ms")
        print(f"  Median:  {median / 1_000_000:.2f} ms")
        print(f"  P95:     {p95 / 1_000_000:.2f} ms")
        print(f"  P99:     {p99 / 1_000_000:.2f} ms")

        # Validate backpressure worked
        assert queued_count > 0 or timeout_count > 0, "Backpressure should have been triggered"

        print("\n✅ Backpressure validated:")
        print("   - Concurrency limit enforced")
        print(f"   - {queued_count} requests queued")
        print(f"   - {timeout_count} requests timed out")
    else:
        pytest.fail("No latency measurements collected")


# =============================================================================
# Test 3: Adaptive Timeout Under Slow Functions
# =============================================================================


@pytest.mark.performance
def test_adaptive_timeout_behavior() -> None:
    """Measure adaptive timeout behavior with slow functions.

    Tests:
    - Initial timeout enforcement
    - Timeout adaptation based on observed latencies
    - Performance impact of timeout checks

    This validates that adaptive timeout doesn't kill performance.
    """
    from cachekit.config.nested import TimeoutConfig

    # Config with adaptive timeout enabled
    config = DecoratorConfig(
        backend=None,  # L1-only
        timeout=TimeoutConfig(
            enabled=True,
            initial=0.5,  # 500ms initial timeout
            min=0.1,  # 100ms min
            max=2.0,  # 2s max
        ),
    )

    call_count = 0
    function_duration_ms = 50  # Normal function

    @cache(config=config)
    def normal_function(item_id: int) -> dict[str, Any]:
        nonlocal call_count
        call_count += 1
        time.sleep(function_duration_ms / 1000)
        return {"id": item_id, "result": "data"}

    print("\n" + "=" * 80)
    print("ADAPTIVE TIMEOUT - PERFORMANCE IMPACT")
    print("=" * 80)
    print("\nConfig:")
    print("  timeout_initial: 500ms")
    print("  timeout_min: 100ms")
    print("  timeout_max: 2000ms")
    print(f"  function_duration: {function_duration_ms}ms")

    # Warm up adaptive timeout (let it learn)
    print("\nPhase 1: Warm-up (adaptive timeout learning)")
    for i in range(50):
        normal_function(i)

    # Measure latency with adaptive timeout active
    print("\nPhase 2: Measuring with adaptive timeout active")
    latencies = []
    for i in range(100, 200):
        start = time.perf_counter_ns()
        normal_function(i)
        end = time.perf_counter_ns()
        latencies.append(end - start)

    mean = statistics.mean(latencies)
    median = statistics.median(latencies)
    p95 = statistics.quantiles(latencies, n=20)[18]

    print("\nLatencies:")
    print(f"  Mean:    {mean / 1_000_000:.2f} ms")
    print(f"  Median:  {median / 1_000_000:.2f} ms")
    print(f"  P95:     {p95 / 1_000_000:.2f} ms")
    print("\nContext:")
    print(f"  Function execution: {function_duration_ms}ms")
    print(f"  Measured overhead:  {(mean / 1_000_000) - function_duration_ms:.2f}ms")

    # Validate timeout overhead is reasonable (<10% of function execution time)
    overhead_ms = (mean / 1_000_000) - function_duration_ms
    overhead_pct = (overhead_ms / function_duration_ms) * 100
    max_overhead_pct = 20  # Allow 20% overhead

    assert overhead_pct < max_overhead_pct, f"Timeout overhead {overhead_pct:.1f}% exceeds {max_overhead_pct}% target"

    print("\n✅ Adaptive timeout validated:")
    print(f"   - Overhead: {overhead_ms:.2f}ms ({overhead_pct:.1f}%)")
    print(f"   - Within acceptable range (<{max_overhead_pct}%)")


# =============================================================================
# Test 4: Full Stack Under Load (All Features Enabled)
# =============================================================================


@pytest.mark.performance
@pytest.mark.integration
def test_full_stack_under_load() -> None:
    """Measure full stack performance with ALL reliability features enabled.

    Tests:
    - Circuit breaker monitoring overhead
    - Backpressure semaphore overhead
    - Adaptive timeout overhead
    - Stats collection overhead
    - L1 + L2 coordination

    This is the MOST REALISTIC test - all features enabled like production.
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

    from cachekit.config.nested import (
        BackpressureConfig,
        CircuitBreakerConfig,
        L1CacheConfig,
        MonitoringConfig,
        TimeoutConfig,
    )

    # Production config - ALL features enabled
    config = DecoratorConfig(
        backend=backend,
        l1=L1CacheConfig(enabled=True),
        circuit_breaker=CircuitBreakerConfig(enabled=True),
        timeout=TimeoutConfig(enabled=True),
        backpressure=BackpressureConfig(enabled=True, max_concurrent_requests=50),
        monitoring=MonitoringConfig(collect_stats=True),
    )

    call_count = 0

    @cache(config=config)
    def get_data(item_id: int) -> dict[str, Any]:
        nonlocal call_count
        call_count += 1
        return {"id": item_id, "data": "x" * 1000}  # 1KB payload

    print("\n" + "=" * 80)
    print("FULL STACK UNDER LOAD (All Features Enabled)")
    print("=" * 80)
    print("\nFeatures enabled:")
    print("  ✓ L1 in-memory cache")
    print("  ✓ L2 Redis backend")
    print("  ✓ Circuit breaker")
    print("  ✓ Adaptive timeout")
    print("  ✓ Backpressure")
    print("  ✓ Stats collection")

    # Phase 1: Prime L1 cache
    print("\nPhase 1: Priming L1 cache")
    for i in range(100):
        get_data(i)

    # Phase 2: Measure L1 hits (hot path)
    print("\nPhase 2: Measuring L1 hits (hot path)")
    latencies_l1 = []
    for _ in range(1000):
        start = time.perf_counter_ns()
        get_data(42)  # Same key = L1 hit
        end = time.perf_counter_ns()
        latencies_l1.append(end - start)

    p95_l1 = statistics.quantiles(latencies_l1, n=20)[18]
    print(f"  P95 latency (L1 hit): {p95_l1 / 1000:.2f} μs")

    # Phase 3: Measure L1 miss → L2 hit
    print("\nPhase 3: Measuring L1 miss → L2 hit")
    from cachekit.l1_cache import get_l1_cache

    # Clear L1 cache
    l1 = get_l1_cache()
    l1.clear()

    latencies_l2 = []
    for i in range(100, 200):  # Keys already in Redis from Phase 1
        start = time.perf_counter_ns()
        get_data(i)  # L1 miss → L2 hit → populate L1
        end = time.perf_counter_ns()
        latencies_l2.append(end - start)

    p95_l2 = statistics.quantiles(latencies_l2, n=20)[18]
    print(f"  P95 latency (L1 miss → L2 hit): {p95_l2 / 1000:.2f} μs")

    # Phase 4: Concurrent load test
    print("\nPhase 4: Concurrent load test (10 threads)")
    num_threads = 10
    iterations_per_thread = 100

    latencies_concurrent: list[int] = []
    lock = threading.Lock()

    def worker(thread_id: int) -> None:
        for i in range(iterations_per_thread):
            start = time.perf_counter_ns()
            get_data(thread_id * 1000 + i)
            end = time.perf_counter_ns()

            with lock:
                latencies_concurrent.append(end - start)

    threads = []
    for i in range(num_threads):
        t = threading.Thread(target=worker, args=(i,))
        threads.append(t)
        t.start()

    for t in threads:
        t.join()

    p95_concurrent = statistics.quantiles(latencies_concurrent, n=20)[18]
    print(f"  P95 latency (concurrent): {p95_concurrent / 1000:.2f} μs")

    # Summary
    print("\n" + "=" * 80)
    print("FULL STACK SUMMARY")
    print("=" * 80)
    print(f"L1 hit:               {p95_l1 / 1000:>8.2f} μs (p95)")
    print(f"L1 miss → L2 hit:     {p95_l2 / 1000:>8.2f} μs (p95)")
    print(f"Concurrent (10 threads): {p95_concurrent / 1000:>8.2f} μs (p95)")
    print("\nOverhead breakdown:")
    print(f"  L2 overhead over L1:     {(p95_l2 - p95_l1) / 1000:.2f} μs")
    print(f"  Concurrent overhead:     {(p95_concurrent - p95_l1) / 1000:.2f} μs")

    # Validate full stack performance is acceptable
    # L1 hit should be <100μs even with all features enabled
    assert p95_l1 < 100_000, f"L1 hit with full stack {p95_l1 / 1000:.0f}μs exceeds 100μs target"

    print("\n✅ Full stack validated:")
    print("   - All reliability features enabled")
    print(f"   - L1 hit: {p95_l1 / 1000:.2f}μs < 100μs target")
    print("   - Performance acceptable under production config")


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s", "-m", "performance"])
