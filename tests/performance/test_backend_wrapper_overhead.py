"""Per-request backend wrapper overhead.

Times the production per-request wrapper, ``PerRequestRedisBackend``: building one for a
tenant, and scoping a key to the calling context's tenant (a ContextVar read plus the
tenant's URL-encoding). No Redis call is made, so this is the wrapper's own cost, which the
per-request pattern pays on top of the network round trip.

Each test asserts the key the timed call produced, so it cannot time a wrapper that scopes
keys wrongly.
"""

from __future__ import annotations

import time
from collections.abc import Callable

import pytest

from cachekit.backends.redis.provider import PerRequestRedisBackend, tenant_context

from .stats_utils import report

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


@pytest.mark.performance
def test_wrapper_creation_overhead(record_property) -> None:
    """Building a tenant-bound wrapper: validation plus the tenant's URL-encoding."""
    latencies, backend = _measure(lambda: PerRequestRedisBackend(_CLIENT, "org:123"))
    report(record_property, "PerRequestRedisBackend creation", latencies)

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
    report(record_property, "Context-scoped key (ContextVar read + URL-encoding)", latencies)

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
    report(record_property, "Total per-request overhead (create + scope one key)", latencies)

    assert key == "t:org%3A123:user:456"
