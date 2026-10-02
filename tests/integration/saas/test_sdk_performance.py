"""Phase 4: SDK Performance and Latency Tests against a live cachekit.io target (dev by default).

In-process paths (L1 hits, cache_info) keep their sub-millisecond asserts. Anything that crosses
the network is a reported value, never an assert: a fixed millisecond threshold says more about
where the client sits than about the SDK. Every network result carries its label:
``vantage=<colo>, client wall time, <env>``, with the colo read from ``/cdn-cgi/trace`` at run time.

``test_l2_hit_latency_and_a_a_floor`` is the measurement this module exists for. It stays within
a few hundred paced requests, and the directory stays out of CI.

Run with:
    op run --env-file=<file with CACHEKIT_API_KEY=op://...> -- \\
        uv run pytest tests/integration/saas/test_sdk_performance.py -v -s
"""

import random
import statistics
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse

import pytest
import requests

from tests.performance.stats_utils import (
    PerformanceResult,
    balanced_order,
    effect_size_significant,
    format_tail,
    noise_floor,
    percentile,
    summarize,
)

# Mark all tests in this module
pytestmark = [pytest.mark.performance, pytest.mark.sdk_e2e]

KEYS = 50  # primed keys; each is revisited every KEYS calls, well past the edge's few-second L0
WARMUP_CALLS = 10
BLOCK = 20  # calls per run; runs alternate between the two A/A arms
RUNS_PER_ARM = 5  # in a random balanced order: 10 runs, 200 timed hits in total
PACE_S = 0.1  # sleep between calls: a few requests per second, far inside the dev limiter
# One fresh namespace per module run: keys a previous run left on the target would turn this
# run's first calls into L2 hits whose L1 copies expire with the old entry's remaining TTL.
NAMESPACE = f"perf_{uuid.uuid4().hex[:8]}"


@pytest.fixture(scope="module")
def vantage(sdk_config) -> str:
    """The label every network result carries: entry colo, what is timed, and the target."""
    trace = requests.get(f"{sdk_config['api_url']}/cdn-cgi/trace", timeout=5).text
    colo = dict(line.split("=", 1) for line in trace.splitlines() if "=" in line).get("colo", "unknown")
    host = urlparse(sdk_config["api_url"]).hostname
    env = "dev" if host == "api.dev.cachekit.io" else host
    return f"vantage={colo}, client wall time, {env}"


@pytest.fixture
def response_headers(cache_io_decorator):
    """Headers of every response the SDK's HTTP client receives, in order.

    The decorator's backend leases the per-thread client for this exact config, so leasing it
    here returns the same client; the hook only reads.
    """
    from cachekit.backends.cachekitio.client import lease_sync_http_client
    from cachekit.backends.cachekitio.config import CachekitIOBackendConfig

    lease = lease_sync_http_client(CachekitIOBackendConfig())
    seen: list = []

    def hook(response) -> None:
        seen.append(response.headers)

    lease.client.event_hooks["response"].append(hook)
    yield seen
    lease.client.event_hooks["response"].remove(hook)


def _timed_ms(fn, arg) -> float:
    start = time.perf_counter()
    fn(arg)
    return (time.perf_counter() - start) * 1000


def _timed_hit(fn, arg, response_headers: list) -> tuple[float, str]:
    """Time one call and return it with the serving tier of the response it caused."""
    seen = len(response_headers)
    latency = _timed_ms(fn, arg)
    if len(response_headers) == seen:
        return latency, "none"
    return latency, response_headers[-1].get("x-cachekit-store-source", "unlabelled")


def _report(result: PerformanceResult, label: str) -> None:
    print(f"\n{result.name} [{label}]")
    print(f"  n={result.samples} in {result.runs} runs; p50 {result.median:.1f} ms")
    print(f"  run median {result.center:.1f} ± {result.band:.1f} ms (95% t over run medians)")
    print(f"  {format_tail(95, result.p95, result.p95_ci, result.samples, result.runs, 'ms')}")


# ============================================================================
# Latency Tests
# ============================================================================


def test_l1_cache_latency(cache_io_decorator, performance_timer):
    """Test L1 cache hit latency < 1ms.

    L1 cache is in-memory, so hits should be sub-millisecond on any target.

    Validates:
    - L1 cache hit latency < 1ms (p95 over 100 hits)
    - Multiple hits maintain performance
    """

    @cache_io_decorator(namespace=NAMESPACE)
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

    p95_latency = percentile(latencies, 95)

    # L1 hits should be sub-millisecond
    assert p95_latency < 1.0, f"L1 cache p95 latency too high: {p95_latency:.3f}ms (expected < 1ms)"

    # Verify cache was actually hit (not executed 100 times)
    info = fast_function.cache_info()
    assert info.hits >= 100, "L1 cache not being used"


def test_l2_hit_latency_and_a_a_floor(cache_io_decorator, response_headers, sdk_config, vantage):
    """Time real L2 hits, time misses as their own arm, and set the A/A floor from the hits.

    L1 is off, so a repeated argument cannot be served from memory: after priming, every timed
    call is a GET the backend answers from its store. The counters prove it, not the timings.

    Arms:
    - GET-miss + SET: priming KEYS never-seen keys, one call each.
    - L2 hit: WARMUP_CALLS discarded, then 2 x RUNS_PER_ARM runs of BLOCK calls cycling the primed
      keys, in a random balanced order of arms A and B; both arms call the same function on the
      same keys, so any difference effect_size_significant calls is noise: the A/A floor.
    """
    namespace = f"perf_{uuid.uuid4().hex[:8]}"

    @cache_io_decorator(ttl=300, l1_enabled=False, namespace=namespace)
    def l2_function(x: int) -> int:
        return x * 3

    # Arm: GET-miss + SET (each call: GET 404, run the function, PUT)
    miss_runs: list[list[float]] = [[] for _ in range(5)]
    for i in range(KEYS):
        miss_runs[i * 5 // KEYS].append(_timed_ms(l2_function, i))
        time.sleep(PACE_S)
    primed = l2_function.cache_info()
    assert primed.misses == KEYS and primed.hits == 0, f"priming was not all misses: {primed}"

    for i in range(WARMUP_CALLS):
        l2_function(i % KEYS)
        time.sleep(PACE_S)
    before = l2_function.cache_info()

    # The timed cycle starts past the warm-up keys, so no timed read lands inside the edge's
    # few-second in-memory window of a warm-up read. Only store-served (`do`) hits enter the A/A
    # arms; any other tier is reported on its own line, so one tier never inflates a band.
    arms: dict[str, list[list[float]]] = {"A": [], "B": []}
    tiers: dict[str, list[float]] = {}
    call = WARMUP_CALLS
    seed = random.SystemRandom().randrange(2**32)
    order = balanced_order(RUNS_PER_ARM, random.Random(seed))
    print(f"\nRun order {order} (seed {seed})")
    for arm in order:
        run = []
        for _ in range(BLOCK):
            latency, tier = _timed_hit(l2_function, call % KEYS, response_headers)
            tiers.setdefault(tier, []).append(latency)
            if tier == "do":
                run.append(latency)
            call += 1
            time.sleep(PACE_S)
        arms[arm].append(run)

    after = l2_function.cache_info()
    timed = len(order) * BLOCK
    assert after.l2_hits - before.l2_hits == timed, f"not every timed call was an L2 hit: {before} -> {after}"
    assert after.misses == before.misses, f"a timed call missed: {before} -> {after}"
    assert after.l1_hits == 0, "L1 served a call with l1_enabled=False"

    miss = summarize("GET-miss + SET", miss_runs, unit="ms")
    hit_a = summarize("L2 hit, arm A", arms["A"], unit="ms")
    hit_b = summarize("L2 hit, arm B", arms["B"], unit="ms")
    hits = summarize("L2 hit, both arms", arms["A"] + arms["B"], unit="ms")
    for result in (miss, hits, hit_a, hit_b):
        _report(result, vantage)

    print(f"\nL2 hits by serving tier (x-cachekit-store-source) [{vantage}]")
    for tier, latencies in sorted(tiers.items()):
        print(f"  {tier:>10}: n={len(latencies):>3}  p50 {statistics.median(latencies):.1f} ms")

    changed = effect_size_significant(hit_a, hit_b)
    print(
        f"\nA/A (same function, same keys, shuffled runs, store-served hits): delta {hit_b.center - hit_a.center:+.1f} ms, "
        f"{'CHANGE' if changed else 'no change'} at the default 5% threshold.\n"
        f"Floor it sets: an L2-hit A/B from this vantage must move the run median by more than "
        f"{noise_floor(hit_a, hit_b, threshold=0.0):.1f} ms (the 95% bands), and by more than "
        f"{noise_floor(hit_a, hit_b):.1f} ms to be called at 5%."
    )


def test_connection_pool_reuse(cache_io_decorator, response_headers, vantage):
    """Report the first and the following L2 hits on one client; the pool keeps them on one connection.

    Reported, not asserted: a remote target's RTT dominates both numbers. One key read every
    ~0.15 s is mostly served from the edge's in-memory tier, so every number carries its tier.
    """

    @cache_io_decorator(ttl=300, l1_enabled=False, namespace=f"pool_{uuid.uuid4().hex[:8]}")
    def pooled_function(x: int) -> int:
        return x * 6

    pooled_function(1000)  # prime: GET miss + SET
    before = pooled_function.cache_info()

    hits = []
    for _ in range(11):
        hits.append(_timed_hit(pooled_function, 1000, response_headers))
        time.sleep(PACE_S)

    after = pooled_function.cache_info()
    assert after.l2_hits - before.l2_hits == 11 and after.misses == before.misses, f"{before} -> {after}"

    following: dict[str, list[float]] = {}
    for latency, tier in hits[1:]:
        following.setdefault(tier, []).append(latency)
    medians = ", ".join(f"{t} n={len(v)} p50 {statistics.median(v):.1f}ms" for t, v in sorted(following.items()))
    print(f"\nFirst L2 hit: {hits[0][0]:.1f}ms ({hits[0][1]}); the next 10: {medians} [{vantage}]")


# ============================================================================
# Concurrency Tests
# ============================================================================


def test_concurrent_requests_performance(cache_io_decorator):
    """Test 100 concurrent requests complete successfully.

    Validates:
    - All 100 concurrent requests succeed
    - No race conditions or errors
    - Reasonable total time (< 5 seconds; 10 keys, so most calls are L1 hits)
    """

    @cache_io_decorator(namespace=NAMESPACE)
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


# ============================================================================
# Memory and Resource Tests
# ============================================================================


def test_l1_cache_memory_limit(cache_io_decorator):
    """Test L1 cache serves repeat calls and survives use.

    Validates:
    - Repeat calls are L1 hits
    - The cache still works after use

    NOTE: This test validates SDK behavior, not Worker behavior. 20 entries keep it to 40 requests.
    """

    @cache_io_decorator(namespace=NAMESPACE)
    def large_function(x: int) -> str:
        # Return 1KB string
        return "X" * 1024

    # First call creates cache entry
    for i in range(20):
        large_function(i)

    # Now call same values again - should hit L1 cache
    for i in range(20):
        large_function(i)

    # Check cache info
    info = large_function.cache_info()

    # Cache should have hits from L1 reuse (at least 20 from second loop)
    assert info.l1_hits >= 20, f"L1 cache not being used: l1_hits={info.l1_hits}"

    # NOTE: We can't directly measure memory size from outside,
    # but we can verify the cache is working and not crashing

    # Verify cache still works after heavy use
    result = large_function(1)
    assert result == "X" * 1024


def test_throughput_sustained(cache_io_decorator):
    """Test sustained throughput of 100+ req/s for 5 seconds.

    10 keys, so after the first 10 calls (20 requests) every call is an L1 hit: this measures the
    SDK's hit path under load, not the backend.

    Validates:
    - Sustained high throughput
    - No performance degradation over time
    """

    @cache_io_decorator(namespace=NAMESPACE)
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
            throughput_function(i)
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

    @cache_io_decorator(namespace=NAMESPACE)
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

    p95_latency = percentile(latencies, 95)

    # cache_info() should be instant (< 1ms)
    assert p95_latency < 1.0, f"cache_info() p95 latency too high: {p95_latency:.3f}ms (expected < 1ms)"

    # Verify info is valid
    assert info.hits >= 0
    assert info.misses >= 0
