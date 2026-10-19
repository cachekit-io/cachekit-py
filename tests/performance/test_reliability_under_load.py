"""Reliability framework performance under load.

Tests that exercise the production reliability features:
- A Redis outage (every call degrades to an uncached call)
- Backpressure (hitting max_concurrent limits)

CRITICAL: These tests validate that reliability features work under production stress.
They measure performance degradation when things go wrong, not just happy path.

The assertions are about behaviour: the breaker's state, which calls reached the backend, how
many backend operations ran at once, and that every caller got its value.
"""

from __future__ import annotations

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
# Test 1: Redis Outage
# =============================================================================


@pytest.mark.performance
@pytest.mark.integration
def test_redis_outage_degrades_to_uncached_calls(perf_redis_backend, record_property) -> None:
    """A Redis outage turns every call into an uncached call: no error reaches the caller.

    Backend read and write failures do not count toward the circuit breaker (docs/error-codes.md,
    "Circuit breaker open"), so the breaker stays closed and every call keeps trying Redis.
    """
    from cachekit.config.nested import CircuitBreakerConfig, L1CacheConfig

    backend = perf_redis_backend
    config = DecoratorConfig(
        backend=backend,
        l1=L1CacheConfig(enabled=False),  # every call goes to L2
        circuit_breaker=CircuitBreakerConfig(enabled=True),
    )

    @cache(config=config)
    def get_data(item_id: int) -> dict[str, Any]:
        return {"id": item_id, "data": "value"}

    latencies_normal = []
    for i in range(10):
        start = time.perf_counter_ns()
        assert get_data(i)["id"] == i
        latencies_normal.append(time.perf_counter_ns() - start)

    def failing(*args, **kwargs):
        raise BackendError("Simulated Redis failure", error_type=BackendErrorType.TRANSIENT)

    latencies_outage = []
    with patch.object(backend, "get", side_effect=failing) as l2_get, patch.object(backend, "set", side_effect=failing):
        for i in range(10):
            start = time.perf_counter_ns()
            assert get_data(100 + i)["id"] == 100 + i, "an outage must degrade to an uncached call, not an error"
            latencies_outage.append(time.perf_counter_ns() - start)
    assert l2_get.call_count == 10, "every call during the outage should still try Redis"
    assert get_data.get_health_status()["circuit_breaker"]["state"] == "closed"

    p95_normal = statistics.quantiles(latencies_normal, n=20)[18]
    p95_outage = statistics.quantiles(latencies_outage, n=20)[18]
    print(f"\n  P95 latency (normal): {p95_normal / 1000:.2f} μs, (outage): {p95_outage / 1000:.2f} μs")
    record_property("p95_normal_us", round(p95_normal / 1000, 2))
    record_property("p95_outage_us", round(p95_outage / 1000, 2))


# =============================================================================
# Test 2: Backpressure Under Load
# =============================================================================


class _SlowBackend:
    """In-process L2 whose operations take ``delay`` seconds and count how many run at once."""

    def __init__(self, delay: float) -> None:
        self.delay = delay
        self.store: dict[str, bytes] = {}
        self.in_flight = 0
        self.max_in_flight = 0
        self._lock = threading.Lock()

    def _op(self, fn, *args):
        with self._lock:
            self.in_flight += 1
            self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            time.sleep(self.delay)
            return fn(*args)
        finally:
            with self._lock:
                self.in_flight -= 1

    def get(self, key: str) -> bytes | None:
        return self._op(self.store.get, key)

    def set(self, key: str, value: bytes, ttl: int | None = None) -> None:
        self._op(self.store.__setitem__, key, value)

    def delete(self, key: str) -> bool:
        return self.store.pop(key, None) is not None

    def exists(self, key: str) -> bool:
        return key in self.store

    def health_check(self) -> tuple[bool, dict[str, str]]:
        return True, {"backend_type": "slow"}


@pytest.mark.performance
def test_backpressure_limit(record_property) -> None:
    """Backpressure caps concurrent backend operations at max_concurrent_requests.

    It gates backend I/O, not the cached function, so it needs a backend: 20 threads drive a
    backend whose every operation takes 50ms. A caller that cannot get a permit in time
    degrades to an uncached call, so every caller must still get its value.
    """
    from cachekit.config.nested import BackpressureConfig

    backend = _SlowBackend(delay=0.05)
    max_concurrent = 5
    config = DecoratorConfig(
        backend=backend,
        backpressure=BackpressureConfig(enabled=True, max_concurrent_requests=max_concurrent),
        encryption=False,
    )

    @cache(config=config)
    def get_data(item_id: int) -> dict[str, Any]:
        return {"id": item_id, "result": "data"}

    num_threads = 20  # More threads than limit
    iterations_per_thread = 5
    latencies: list[int] = []
    wrong: list[int] = []
    lock = threading.Lock()

    def worker(thread_id: int) -> None:
        for i in range(iterations_per_thread):
            item_id = thread_id * 100 + i
            start = time.perf_counter_ns()
            try:
                ok = get_data(item_id) == {"id": item_id, "result": "data"}
            except Exception:
                ok = False  # counted: an assert in a thread is only printed, never fails the test
            elapsed = time.perf_counter_ns() - start
            with lock:
                latencies.append(elapsed)
                if not ok:
                    wrong.append(item_id)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(num_threads)]
    start_time = time.perf_counter()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    elapsed_time = time.perf_counter() - start_time

    load_control = get_data.get_health_status()["components"]["load_control"]
    p95 = statistics.quantiles(latencies, n=20)[18]

    print("\n" + "=" * 80)
    print("BACKPRESSURE - CONCURRENT LIMIT")
    print("=" * 80)
    print(f"  Requests:            {len(latencies)} ({num_threads} threads)")
    print(f"  Max backend in flight: {backend.max_in_flight} (limit {max_concurrent})")
    print(f"  Rejected permits:    {load_control['rejected_count']}")
    print(f"  Total time:          {elapsed_time:.2f}s")
    print(f"  Median:              {statistics.median(latencies) / 1_000_000:.2f} ms")
    print(f"  P95:                 {p95 / 1_000_000:.2f} ms")
    record_property("p95_ms", round(p95 / 1_000_000, 2))
    record_property("rejected_permits", load_control["rejected_count"])

    assert wrong == [], f"{len(wrong)} callers got an error or a wrong value"
    assert len(latencies) == num_threads * iterations_per_thread
    # 20 callers on 50ms operations always fill the 5 slots; the cap must hold at that point.
    assert backend.max_in_flight == max_concurrent, f"{backend.max_in_flight} backend operations ran at once"


# =============================================================================
# Test 3: Full Stack Under Load (All Features Enabled)
# =============================================================================


@pytest.mark.performance
@pytest.mark.integration
def test_full_stack_under_load(perf_redis_backend, record_property) -> None:
    """Measure full stack performance with ALL reliability features enabled.

    Tests:
    - Circuit breaker monitoring overhead
    - Backpressure semaphore overhead
    - Stats collection overhead
    - L1 + L2 coordination

    This is the MOST REALISTIC test - all features enabled like production.
    """
    from cachekit.config.nested import (
        BackpressureConfig,
        CircuitBreakerConfig,
        L1CacheConfig,
        MonitoringConfig,
    )

    # Production config - ALL features enabled
    backend = perf_redis_backend
    config = DecoratorConfig(
        backend=backend,
        l1=L1CacheConfig(enabled=True),
        circuit_breaker=CircuitBreakerConfig(enabled=True),
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
    print("  ✓ Backpressure")
    print("  ✓ Stats collection")

    # Phase 1: Prime L1 cache
    print("\nPhase 1: Priming L1 cache")
    for i in range(100):
        get_data(i)
    assert call_count == 100

    # Phase 2: Measure L1 hits (hot path)
    print("\nPhase 2: Measuring L1 hits (hot path)")
    latencies_l1 = []
    with patch.object(backend, "get", wraps=backend.get) as l2_get:
        for _ in range(1000):
            start = time.perf_counter_ns()
            result = get_data(42)  # Same key = L1 hit
            end = time.perf_counter_ns()
            latencies_l1.append(end - start)
    assert result["id"] == 42
    assert (call_count, l2_get.call_count) == (100, 0), "an L1 hit ran the function or read Redis"

    p95_l1 = statistics.quantiles(latencies_l1, n=20)[18]
    print(f"  P95 latency (L1 hit): {p95_l1 / 1000:.2f} μs")

    # Phase 3: Measure L1 miss → L2 hit
    print("\nPhase 3: Measuring L1 miss → L2 hit")
    from cachekit.l1_cache import get_l1_cache

    # Clear L1 cache
    l1 = get_l1_cache()
    l1.clear()

    latencies_l2 = []
    with patch.object(backend, "get", wraps=backend.get) as l2_get:
        for i in range(100):  # Keys already in Redis from Phase 1
            start = time.perf_counter_ns()
            result = get_data(i)  # L1 miss → L2 hit → populate L1
            end = time.perf_counter_ns()
            latencies_l2.append(end - start)
            assert result["id"] == i
    assert (call_count, l2_get.call_count) == (100, 100), "an L1 miss did not come from Redis"

    p95_l2 = statistics.quantiles(latencies_l2, n=20)[18]
    print(f"  P95 latency (L1 miss → L2 hit): {p95_l2 / 1000:.2f} μs")

    # Phase 4: Concurrent load test
    print("\nPhase 4: Concurrent load test (10 threads)")
    num_threads = 10
    iterations_per_thread = 100

    latencies_concurrent: list[int] = []
    wrong: list[int] = []
    lock = threading.Lock()

    def worker(thread_id: int) -> None:
        for i in range(iterations_per_thread):
            item_id = thread_id * 1000 + i
            start = time.perf_counter_ns()
            result = get_data(item_id)
            end = time.perf_counter_ns()

            with lock:
                latencies_concurrent.append(end - start)
                if result["id"] != item_id:
                    wrong.append(item_id)  # an assert in a thread is only printed, never fails the test

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

    record_property("p95_l1_hit_us", round(p95_l1 / 1000, 2))
    record_property("p95_l2_hit_us", round(p95_l2 / 1000, 2))
    record_property("p95_concurrent_us", round(p95_concurrent / 1000, 2))

    # Thread 0's keys 0-99 are cached from phases 1-3; the other 9 threads' 900 keys are new.
    assert wrong == []
    assert len(latencies_concurrent) == num_threads * iterations_per_thread
    assert call_count == 100 + (num_threads - 1) * iterations_per_thread


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s", "-m", "performance"])
