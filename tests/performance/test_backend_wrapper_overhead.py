"""Per-request backend wrapper overhead.

Times the production per-request wrapper, ``PerRequestRedisBackend``: building one for a
tenant, and scoping a key to the calling context's tenant (a ContextVar read plus the
tenant's URL-encoding). No Redis call is made, so this is the wrapper's own cost, which the
per-request pattern pays on top of a 1-2ms network round trip.

Figures are informational (printed and recorded with ``record_property``): a nanosecond
p95 moves by an order of magnitude on a loaded host. Each test asserts the key the timed
call produced, so it cannot time a wrapper that scopes keys wrongly.
"""

from __future__ import annotations

import statistics
import time
from collections.abc import Callable

import pytest

from cachekit.backends.redis.provider import PerRequestRedisBackend, tenant_context

# Never dereferenced: building a wrapper and scoping a key make no Redis call.
_CLIENT = object()


def _measure(fn: Callable[[], object], iterations: int = 100_000) -> tuple[list[int], object]:
    """Warm up, then time ``fn`` per call; returns the latencies and the last result."""
    for _ in range(1000):
        fn()
    latencies = []
    result = None
    for _ in range(iterations):
        start = time.perf_counter_ns()
        result = fn()
        latencies.append(time.perf_counter_ns() - start)
    return latencies, result


def _report(record_property, title: str, latencies: list[int]) -> None:
    p50 = statistics.median(latencies)
    p95 = statistics.quantiles(latencies, n=20)[18]
    p99 = statistics.quantiles(latencies, n=100)[98]
    print(f"\n{'=' * 60}")
    print(title)
    print(f"{'=' * 60}")
    print(f"Iterations:  {len(latencies):>10,}")
    print(f"Mean:        {statistics.mean(latencies):>10.2f} ns")
    print(f"P50:         {p50:>10.2f} ns")
    print(f"P95:         {p95:>10.2f} ns")
    print(f"P99:         {p99:>10.2f} ns")
    print(f"P95 as a share of a 1ms round trip: {p95 / 1_000_000 * 100:.4f}%")
    record_property("p50_ns", round(p50, 1))
    record_property("p95_ns", round(p95, 1))


@pytest.mark.performance
def test_wrapper_creation_overhead(record_property) -> None:
    """Building a tenant-bound wrapper: validation plus the tenant's URL-encoding."""
    latencies, backend = _measure(lambda: PerRequestRedisBackend(_CLIENT, "org:123"))
    _report(record_property, "PerRequestRedisBackend creation", latencies)

    assert backend.key_prefix == "t:org%3A123:"


@pytest.mark.performance
def test_context_scoped_key_overhead(record_property) -> None:
    """Scoping a key on the shared, context-following wrapper the provider hands out."""
    backend = PerRequestRedisBackend(_CLIENT, "default", follow_context=True)
    token = tenant_context.set("org:123")
    try:
        latencies, key = _measure(lambda: backend._scoped_key("user:456"))
    finally:
        tenant_context.reset(token)
    _report(record_property, "Context-scoped key (ContextVar read + URL-encoding)", latencies)

    assert key == "t:org%3A123:user:456"
    assert backend._scoped_key("user:456") == "t:default:user:456", "the reset context must fall back to the bound tenant"


@pytest.mark.performance
def test_total_overhead(record_property) -> None:
    """The whole per-request cost: build a wrapper for the request's tenant and scope one key."""
    token = tenant_context.set("org:123")
    try:
        latencies, key = _measure(lambda: PerRequestRedisBackend(_CLIENT, tenant_context.get())._scoped_key("user:456"))
    finally:
        tenant_context.reset(token)
    _report(record_property, "Total per-request overhead (create + scope one key)", latencies)

    assert key == "t:org%3A123:user:456"
