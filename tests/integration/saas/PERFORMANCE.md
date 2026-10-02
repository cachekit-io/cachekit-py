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
  runs go to arms A and B in a random balanced order, printed with its seed: a fixed pattern such as
  ABBA would line up with any periodic latency and load one arm. Both arms call the same function on the same
  keys, so the difference between them is noise. That gives the A/A floor, the smallest change an
  A/B from this vantage can claim.

The test asserts what was timed, not how fast it was: `cache_info().l2_hits` must grow by exactly
the number of timed calls, and `misses` must not grow. Each hit is also labelled with the serving
tier from the `X-CacheKit-Store-Source` response header, and only store-served (`do`) hits enter the A/A
arms. The timed cycle starts past the warm-up keys, so no timed read falls in the edge's few-second
in-memory window of a warm-up read.

Every network number is a reported value, never an assert, and carries the label
`vantage=<colo>, client wall time, <env>`. The colo comes from `/cdn-cgi/trace` at run time. Only
the in-process paths (L1 hits, `cache_info()`) keep sub-millisecond asserts.

## Statistics

The numbers come from `tests/performance/stats_utils.py`. A run of 20 calls is the unit of
inference. The estimate is the mean of the run medians, and its band is a 95% t-interval at
df = runs - 1. `effect_size_significant` calls a change only when it exceeds 5% of the baseline, each
side's band, and Welch's 95% band on the difference. A p95 is printed only at n >= 400 per arm from at least 10 runs, and
a p99 at n >= 2,000, each with a bootstrap interval over whole runs (uncalibrated, so not called a CI). Below that the output says
`inconclusive at n`. With one run's budget, every p95 here is still inconclusive.

## First numbers (2026-10-03, vantage=MEL, client wall time, dev)

Three runs of the module, minutes apart. Every timed hit in all three was store-served (`do`).

| Run | L2 hit p50 (n=200) | Hit run median ± band | GET-miss + SET p50 (n=50) | A/A delta | A/A floor (95% bands) |
|-----|--------------------|-----------------------|---------------------------|-----------|-----------------------|
| 1 (ABBA order) | 43.7 ms | 43.7 ± 0.6 ms | 112.7 ms | +0.4 ms | 1.3 ms |
| 2 (shuffled) | 41.5 ms | 41.5 ± 0.4 ms | 115.5 ms | -0.1 ms | 0.8 ms |
| 3 (shuffled) | 44.6 ms | 62.8 ± 28.1 ms | 111.9 ms | +12.5 ms | 63.5 ms |

Every A/A read as no change. In run 3 a few runs of 20 calls were slow from start to end, so the run
medians spread and the floor widened to 63.5 ms. The band absorbed it; it did not turn into a false
change. Use the floor of the session you compare in, never a floor from another session.

An earlier session the same day read 100.5 ms for the miss arm, about 12 ms away from these. That gap
is larger than any one session's miss band, so compare arms only inside one session, interleaved.

These numbers hold for this vantage only. They depend on where the client enters Cloudflare, the
store's region and how many reads the edge serves.
