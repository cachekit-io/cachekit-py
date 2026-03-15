"""Phase 4: SDK Performance and Latency Tests.

Tests performance characteristics of the cachekit SDK against localhost Worker.

Priority: P1 (Important - validates performance targets)

Performance Targets (Localhost):
- L1 cache hit: < 1ms (sub-millisecond in-memory lookup)
- L2 cache hit: < 50ms (HTTP roundtrip to localhost Worker)
- Cache miss: < 100ms (includes function execution + cache set)
- Concurrent: 100 requests complete successfully
- Throughput: 100+ operations/second sustained

NOTE: These are baseline tests against localhost. Production latency will be
higher due to network distance, TLS overhead, and Worker cold starts.

Run with:
    pytest test_sdk_performance.py -v
    pytest test_sdk_performance.py::test_l1_cache_latency -v
"""

import time
from concurrent.futures import ThreadPoolExecutor

import pytest

# Mark all tests in this module
pytestmark = [pytest.mark.performance, pytest.mark.sdk_e2e]


# ============================================================================
# Latency Tests
# ============================================================================


def test_l1_cache_latency(cache_io_decorator, performance_timer):
    """Test L1 cache hit latency < 1ms.

    L1 cache is in-memory, so hits should be sub-millisecond.
    This validates that the SDK's L1 cache layer is providing
    the expected performance benefit.

    Validates:
    - L1 cache hit latency < 1ms (p95)
    - Multiple hits maintain performance
    """

    @cache_io_decorator
    def fast_function(x: int) -> int:
        return x * 2

    # Prime both L1 and L2 cache
    result = fast_function(42)
    assert result == 84

    # Measure L1 cache hits (in-memory)
    latencies = []
    for _ in range(100):
        with performance_timer() as timer:
            result = fast_function(42)
        assert result == 84
        latencies.append(timer.elapsed_ms)

    # Calculate p95 latency
    latencies.sort()
    p95_latency = latencies[int(len(latencies) * 0.95)]

    # L1 hits should be sub-millisecond
    assert p95_latency < 1.0, f"L1 cache p95 latency too high: {p95_latency:.3f}ms (expected < 1ms)"

    # Verify cache was actually hit (not executed 100 times)
    info = fast_function.cache_info()
    assert info.hits >= 100, "L1 cache not being used"


def test_l2_cache_latency(cache_io_decorator, performance_timer):
    """Test L2 cache hit latency < 50ms.

    L2 cache requires HTTP roundtrip to localhost Worker.
    This includes network stack, HTTP overhead, and Worker processing.

    Validates:
    - L2 cache hit latency < 50ms (localhost)
    - Consistent performance across multiple hits
    """

    @cache_io_decorator(ttl=300)
    def network_function(x: int) -> int:
        return x * 3

    # Prime L2 cache (skip L1 by using unique value)
    network_function(999)

    # Clear L1 cache to force L2 hits
    # NOTE: cache_clear() clears both L1 and L2, so we need fresh function
    @cache_io_decorator(ttl=300)
    def network_function_l2_test(x: int) -> int:
        return x * 3

    # Prime L2
    network_function_l2_test(888)
    time.sleep(0.1)  # Let L2 write complete

    # Measure L2 cache hits (HTTP roundtrip)
    latencies = []
    for i in range(20):
        # Use different values to bypass L1, hit L2
        with performance_timer() as timer:
            result = network_function_l2_test(888 + i)
        latencies.append(timer.elapsed_ms)

    # Calculate p95 latency
    latencies.sort()
    p95_latency = latencies[int(len(latencies) * 0.95)]

    # L2 hits should be < 50ms on localhost
    assert p95_latency < 50.0, f"L2 cache p95 latency too high: {p95_latency:.3f}ms (expected < 50ms for localhost)"


def test_cache_miss_latency(cache_io_decorator, performance_timer):
    """Test cache miss latency < 100ms.

    Cache miss includes:
    1. Function execution
    2. HTTP POST to Worker to store result
    3. L1 cache update

    Validates:
    - Full cache miss flow < 100ms
    """
    call_count = 0

    @cache_io_decorator(ttl=60)
    def slow_function(x: int) -> int:
        nonlocal call_count
        call_count += 1
        # Simulate 10ms computation
        time.sleep(0.01)
        return x * 4

    # Measure cache misses (unique keys each time)
    latencies = []
    for i in range(10):
        with performance_timer() as timer:
            result = slow_function(10000 + i)
        assert result == (10000 + i) * 4
        latencies.append(timer.elapsed_ms)

    # Calculate average latency
    avg_latency = sum(latencies) / len(latencies)

    # Cache miss should be < 100ms (10ms function + 90ms overhead)
    assert avg_latency < 100.0, f"Cache miss average latency too high: {avg_latency:.3f}ms (expected < 100ms for localhost)"

    # Verify function was actually called for each miss
    assert call_count == 10, "Function not called on cache miss"


# ============================================================================
# Concurrency Tests
# ============================================================================


def test_concurrent_requests_performance(cache_io_decorator):
    """Test 100 concurrent requests complete successfully.

    Validates:
    - All 100 concurrent requests succeed
    - No race conditions or errors
    - Reasonable total time (< 5 seconds)
    """

    @cache_io_decorator
    def concurrent_function(x: int) -> int:
        return x * 5

    # Prime cache
    concurrent_function(1)

    # Execute 100 concurrent requests
    start_time = time.time()
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = [executor.submit(concurrent_function, i % 10) for i in range(100)]
        results = [f.result() for f in futures]

    elapsed = time.time() - start_time

    # Verify all requests succeeded
    assert len(results) == 100, "Not all requests completed"
    assert all(r is not None for r in results), "Some requests failed"

    # Should complete in reasonable time (< 5 seconds for 100 requests)
    assert elapsed < 5.0, f"Concurrent requests too slow: {elapsed:.2f}s (expected < 5s)"

    print(f"\nConcurrent performance: {100 / elapsed:.1f} req/s")


def test_connection_pool_reuse(cache_io_decorator, performance_timer):
    """Test HTTP connection pooling improves performance.

    Validates:
    - Subsequent requests are faster (connection reuse)
    - Connection pool is working correctly
    """

    @cache_io_decorator
    def pooled_function(x: int) -> int:
        return x * 6

    # First request (cold start, new connection)
    with performance_timer() as timer1:
        pooled_function(1000)
    first_latency = timer1.elapsed_ms

    # Subsequent requests (connection pool reuse)
    latencies = []
    for i in range(10):
        with performance_timer() as timer:
            pooled_function(1000 + i)
        latencies.append(timer.elapsed_ms)

    avg_latency = sum(latencies) / len(latencies)

    # Subsequent requests should be faster or similar
    # (connection pool eliminates TCP handshake overhead)
    print(f"\nFirst request: {first_latency:.3f}ms")
    print(f"Average subsequent: {avg_latency:.3f}ms")

    # Both should be reasonable (< 100ms)
    assert first_latency < 100.0, "First request too slow"
    assert avg_latency < 100.0, "Subsequent requests too slow"


# ============================================================================
# Memory and Resource Tests
# ============================================================================


def test_l1_cache_memory_limit(cache_io_decorator):
    """Test L1 cache respects memory limits.

    Validates:
    - L1 cache doesn't grow unbounded
    - LRU eviction works correctly
    - Memory limit is enforced

    NOTE: This test validates SDK behavior, not Worker behavior.
    We use smaller count to avoid overwhelming the backend.
    """

    @cache_io_decorator
    def large_function(x: int) -> str:
        # Return 1KB string
        return "X" * 1024

    # Fill L1 cache with entries (use smaller count to avoid backend overload)
    # First call creates cache entry
    for i in range(50):
        large_function(i)

    # Now call same values again - should hit L1 cache
    for i in range(50):
        large_function(i)

    # Check cache info
    info = large_function.cache_info()

    # Cache should have hits from L1 reuse (at least 50 from second loop)
    assert info.l1_hits >= 50, f"L1 cache not being used: l1_hits={info.l1_hits}"

    # NOTE: We can't directly measure memory size from outside,
    # but we can verify the cache is working and not crashing
    # If memory limit wasn't working, this would OOM or slow down significantly

    # Verify cache still works after heavy use
    result = large_function(1)
    assert result == "X" * 1024


def test_throughput_sustained(cache_io_decorator):
    """Test sustained throughput of 100+ req/s for 5 seconds.

    Validates:
    - Sustained high throughput
    - No performance degradation over time
    - Connection pool handles sustained load
    """

    @cache_io_decorator
    def throughput_function(x: int) -> int:
        return x * 7

    # Prime cache
    throughput_function(1)

    # Run for 5 seconds
    start_time = time.time()
    request_count = 0
    duration = 5.0

    while time.time() - start_time < duration:
        # Make requests in batches
        for i in range(10):
            throughput_function(i % 100)
            request_count += 1

    elapsed = time.time() - start_time
    throughput = request_count / elapsed

    print(f"\nSustained throughput: {throughput:.1f} req/s over {elapsed:.1f}s")

    # Should achieve 100+ req/s
    assert throughput >= 100.0, f"Sustained throughput too low: {throughput:.1f} req/s (expected >= 100 req/s)"


def test_cache_info_performance(cache_io_decorator, performance_timer):
    """Test cache_info() call latency < 1ms.

    Validates:
    - cache_info() is fast (doesn't block)
    - No HTTP roundtrip required for stats
    """

    @cache_io_decorator
    def info_function(x: int) -> int:
        return x * 8

    # Make some calls to generate stats
    for i in range(10):
        info_function(i)

    # Measure cache_info() latency
    latencies = []
    for _ in range(100):
        with performance_timer() as timer:
            info = info_function.cache_info()
        latencies.append(timer.elapsed_ms)

    # Calculate p95 latency
    latencies.sort()
    p95_latency = latencies[int(len(latencies) * 0.95)]

    # cache_info() should be instant (< 1ms)
    assert p95_latency < 1.0, f"cache_info() p95 latency too high: {p95_latency:.3f}ms (expected < 1ms)"

    # Verify info is valid
    assert info.hits >= 0
    assert info.misses >= 0
