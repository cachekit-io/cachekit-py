# SDK Latency Against cachekit.io

`test_sdk_performance.py` times the Python SDK's `@cache.io` path against a live target. It defaults
to dev (`https://api.dev.cachekit.io`), and it skips unless `CACHEKIT_API_KEY` is set. Pass the key
through 1Password, never a file in the repo:

```bash
op run --env-file=<file with CACHEKIT_API_KEY=op://...> -- \
    uv run pytest tests/integration/saas/test_sdk_performance.py -v -s
```

The directory stays out of CI. One run of the module makes about 425 paced requests.

## What it measures

`test_l2_hit_latency_and_a_a_floor` runs with `l1_enabled=False`, so no call can be answered from
memory. It has two arms:

- **GET-miss + SET**: 50 never-seen keys, one call each (GET 404, run the function, SET).
- **L2 hit**: 10 discarded warm-up calls, then 10 runs of 20 calls cycling the 50 primed keys. The
  runs alternate between arms A and B in ABBA order. Both arms call the same function on the same
  keys, so the difference between them is noise. That gives the A/A floor, the smallest change an
  A/B from this vantage can claim.

The test asserts what was timed, not how fast it was: `cache_info().l2_hits` must grow by exactly
the number of timed calls, and `misses` must not grow. Each hit is also labelled with the serving
tier from the `X-CacheKit-Store-Source` response header.

Every network number is a reported value, never an assert, and carries the label
`vantage=<colo>, client wall time, <env>`. The colo comes from `/cdn-cgi/trace` at run time. Only
the in-process paths (L1 hits, `cache_info()`) keep sub-millisecond asserts.

## Statistics

The numbers come from `tests/performance/stats_utils.py`. A run of 20 calls is the unit of
inference. The estimate is the mean of the run medians, and its band is a 95% t-interval at
df = runs - 1. `effect_size_significant` calls a change only when it exceeds both 5% of the baseline
and the wider of the two bands. A p95 is printed only at n >= 400 per arm from at least 10 runs, and
a p99 at n >= 2,000, each with a bootstrap CI over whole runs. Below that the output says
`inconclusive at n`. With one run's budget, every p95 here is still inconclusive.

## First numbers (2026-10-03, vantage=MEL, client wall time, dev)

| Arm | n | p50 | Run median ± band |
|-----|---|-----|-------------------|
| L2 hit, served by the store (`do`) | 190 | 41.0 ms | |
| L2 hit, served by the edge's in-memory tier (`l0`) | 10 | 22.1 ms | |
| L2 hit, both tiers | 200 | 40.9 ms | 40.6 ± 1.0 ms |
| GET-miss + SET | 50 | 100.5 ms | 101.5 ± 2.8 ms |

The A/A had arm A at 40.1 ± 2.3 ms and arm B at 41.2 ± 0.6 ms: a delta of +1.1 ms, which reads as no
change. An L2-hit A/B from this vantage must move the run median by more than 2.3 ms before it counts.

These numbers hold for this vantage only. They depend on where the client enters Cloudflare, the
store's region and how many reads the edge serves.
