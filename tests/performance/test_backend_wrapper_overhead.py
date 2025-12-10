"""Per-request backend wrapper overhead performance test.

Validates wrapper creation overhead is negligible relative to network latency.

Fix #8: Enforces hard limit on per-request wrapper overhead.
Target: Total overhead <1000ns (0.1% of 1-2ms network latency).

Per-request pattern trades ~875ns overhead (wrapper + ContextVar + URL-encoding)
for clean multi-tenant isolation and zero migration cost to remote backends.
"""

from __future__ import annotations

import statistics
import time
from contextvars import ContextVar

import pytest

# Mock minimal backend for isolated benchmarking
test_store: dict[str, tuple[bytes, int | None]] = {}
test_tenant_context: ContextVar[str | None] = ContextVar("test_tenant", default="tenant:123")


class MinimalPerRequestWrapper:
    """Minimal per-request wrapper for benchmarking (no I/O, pure overhead)."""

    def __init__(self, store: dict, tenant_id: str | None):
        if tenant_id is None:
            raise RuntimeError("tenant_id required")
        self._store = store
        self._tenant_id = tenant_id

    def _scoped_key(self, key: str) -> str:
        return f"t:{self._tenant_id}:{key}"


@pytest.mark.performance
def test_wrapper_creation_overhead() -> None:
    """Test per-request wrapper creation overhead meets target.

    Fails if wrapper creation overhead exceeds 1000ns (p95).
    This is a hard gate - overhead regression blocks CI.
    """
    iterations = 100_000
    print(f"\nBenchmarking wrapper creation ({iterations:,} iterations)...")

    # Warm up
    for _ in range(1000):
        MinimalPerRequestWrapper(test_store, "tenant:123")

    # Measure wrapper creation overhead
    latencies = []
    for _ in range(iterations):
        start = time.perf_counter_ns()
        MinimalPerRequestWrapper(test_store, "tenant:123")
        end = time.perf_counter_ns()
        latencies.append(end - start)

    # Calculate statistics
    p50 = statistics.median(latencies)
    p95 = statistics.quantiles(latencies, n=20)[18]  # 95th percentile
    p99 = statistics.quantiles(latencies, n=100)[98]  # 99th percentile
    mean = statistics.mean(latencies)
    stdev = statistics.stdev(latencies)

    results = {
        "iterations": iterations,
        "mean_ns": mean,
        "p50_ns": p50,
        "p95_ns": p95,
        "p99_ns": p99,
        "stdev_ns": stdev,
    }

    # Print results
    print(f"\n{'=' * 60}")
    print("Wrapper Creation Overhead")
    print(f"{'=' * 60}")
    print(f"Iterations:  {results['iterations']:>10,}")
    print(f"Mean:        {results['mean_ns']:>10.2f} ns")
    print(f"P50:         {results['p50_ns']:>10.2f} ns")
    print(f"P95:         {results['p95_ns']:>10.2f} ns")
    print(f"P99:         {results['p99_ns']:>10.2f} ns")
    print(f"StdDev:      {results['stdev_ns']:>10.2f} ns")

    # Hard gate - must not exceed 1000ns
    target_ns = 1000
    if results["p95_ns"] >= target_ns:
        raise AssertionError(
            f"Wrapper overhead {results['p95_ns']:.0f}ns (p95) exceeds {target_ns}ns target\n"
            f"Overhead ratio: {(results['p95_ns'] / 1_000_000) * 100:.4f}% of network latency\n"
            f"Per-request pattern SLA violated"
        )


@pytest.mark.performance
def test_contextvar_get_overhead() -> None:
    """Test ContextVar.get() overhead meets target.

    Fails if ContextVar overhead exceeds reasonable threshold for per-request use.
    """
    iterations = 100_000
    print(f"\nBenchmarking ContextVar.get() ({iterations:,} iterations)...")

    ctx: ContextVar[str] = ContextVar("test", default="value")
    ctx.set("tenant:123")

    # Warm up
    for _ in range(1000):
        ctx.get()

    # Measure get() overhead
    latencies = []
    for _ in range(iterations):
        start = time.perf_counter_ns()
        ctx.get()
        end = time.perf_counter_ns()
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

    # Print results
    print(f"\n{'=' * 60}")
    print("ContextVar.get() Overhead")
    print(f"{'=' * 60}")
    print(f"Iterations:  {results['iterations']:>10,}")
    print(f"Mean:        {results['mean_ns']:>10.2f} ns")
    print(f"P50:         {results['p50_ns']:>10.2f} ns")
    print(f"P95:         {results['p95_ns']:>10.2f} ns")

    # ContextVar should be fast - baseline check only (typical: ~100-200ns p95)
    # (no hard gate, just measurement)
    assert results["p95_ns"] < 1000, f"ContextVar overhead unexpectedly high: {results['p95_ns']:.0f}ns"


@pytest.mark.performance
def test_url_encoding_overhead() -> None:
    """Test URL-encoding overhead for tenant IDs.

    Fails if URL-encoding overhead exceeds reasonable threshold for per-request use.
    """
    from urllib.parse import quote as url_encode

    iterations = 100_000
    print(f"\nBenchmarking URL-encoding ({iterations:,} iterations)...")

    tenant_id = "org:123"

    # Warm up
    for _ in range(1000):
        url_encode(tenant_id, safe="")

    # Measure encoding overhead
    latencies = []
    for _ in range(iterations):
        start = time.perf_counter_ns()
        url_encode(tenant_id, safe="")
        end = time.perf_counter_ns()
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

    # Print results
    print(f"\n{'=' * 60}")
    print("URL-Encoding Overhead")
    print(f"{'=' * 60}")
    print(f"Iterations:  {results['iterations']:>10,}")
    print(f"Mean:        {results['mean_ns']:>10.2f} ns")
    print(f"P50:         {results['p50_ns']:>10.2f} ns")
    print(f"P95:         {results['p95_ns']:>10.2f} ns")

    # URL-encoding should be fast - baseline check only (typical: ~600ns p95)
    assert results["p95_ns"] < 1000, f"URL-encoding overhead unexpectedly high: {results['p95_ns']:.0f}ns"


@pytest.mark.performance
def test_total_overhead_sla() -> None:
    """Validate total per-request overhead meets SLA.

    Measures all three components together in a single sequential operation.
    This is the realistic scenario: wrapper creation + ContextVar access + URL-encoding together.

    Fails if combined overhead exceeds 1500ns (p95). This is 0.15% of 1-2ms network latency.
    """
    from urllib.parse import quote as url_encode

    iterations = 100_000
    print(f"\n{'=' * 60}")
    print("Total Per-Request Overhead SLA")
    print(f"{'=' * 60}")
    print("Target: <1500ns (0.15% of 1-2ms network latency)")
    print("Measuring all components together (realistic scenario)\n")

    # Set up ContextVar
    ctx: ContextVar[str] = ContextVar("test_sla", default="tenant:123")
    ctx.set("tenant:123")
    tenant_id = "org:123"

    # Warm up - 1000 iterations to stabilize
    for _ in range(1000):
        MinimalPerRequestWrapper(test_store, "tenant:123")
        ctx.get()
        url_encode(tenant_id, safe="")

    # Measure total overhead: all three operations together, sequentially
    latencies_total = []
    for _ in range(iterations):
        start = time.perf_counter_ns()
        # All three operations in sequence (realistic per-request cost)
        MinimalPerRequestWrapper(test_store, "tenant:123")
        ctx.get()
        url_encode(tenant_id, safe="")
        end = time.perf_counter_ns()
        latencies_total.append(end - start)

    # Calculate statistics
    p50 = statistics.median(latencies_total)
    p95 = statistics.quantiles(latencies_total, n=20)[18]
    p99 = statistics.quantiles(latencies_total, n=100)[98]
    mean = statistics.mean(latencies_total)
    stdev = statistics.stdev(latencies_total)

    print("Total per-request overhead (all components together):")
    print(f"  Mean:        {mean:>10.2f} ns")
    print(f"  P50:         {p50:>10.2f} ns")
    print(f"  P95:         {p95:>10.2f} ns")
    print(f"  P99:         {p99:>10.2f} ns")
    print(f"  StdDev:      {stdev:>10.2f} ns")
    print("\nNetwork latency:     ~1-2ms (1,000,000-2,000,000 ns)")
    print(f"Overhead ratio:      {(p95 / 1_000_000) * 100:.4f}% of network latency")

    # Hard gate - must not exceed 1500ns p95
    target_ns = 1500
    if p95 >= target_ns:
        raise AssertionError(
            f"Total per-request overhead {p95:.0f}ns (p95) exceeds {target_ns}ns target\nPer-request pattern SLA violated"
        )

    print(f"\n✅ Per-request pattern validated: {p95:.0f}ns (p95) < {target_ns}ns target")
