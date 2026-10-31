**[Home](README.md)** › **Architecture** › **Performance**

# Performance Guide

> **Sub-millisecond cache operations, measured and benchmarked**

---

## Key Numbers

> [!TIP]
> **Key numbers** (mean of five run medians; GitHub-hosted `ubuntu-latest` runner, AMD EPYC 7763, CPython 3.14.7, 2026-10-31; see [GitHub-Hosted Runner](#github-hosted-runner)):
> - **Decorator + L1 hit, `@cache(backend=None)`, 100-user nested dict (23.5KB as plain MessagePack)**: 0.311 ± 0.000μs (about 2,500 instructions per call on CPython 3.14, see [Instruction Budgets](#instruction-budgets))
> - **Same hit, 10K-row DataFrame**: 0.31 ± 0.00μs. An L1-only hit never serializes, so payload size barely matters
> - **Decorator + L1 hit with an L2 backend configured, same dict**: 284 ± 2μs. With a backend, L1 holds bytes and every hit deserializes them. This dict compresses, so L1 holds its envelope. Measured before hits stopped re-checking bytes they had already checked, which took about 20% off this hit; see [Why an L1-Only Hit Costs the Same for Any Payload](#why-an-l1-only-hit-costs-the-same-for-any-payload)
> - **Redis L2 hit**, L1 disabled, same dict, Redis in a container on the same runner: 539 ± 3μs
> - **Raw L1 byte-cache lookup** (`L1Cache.get`, no decorator): 354–362ns on a shared x86_64 Linux host (see [Measured Figures](#measured-figures))

## Measurement Methodology

Every figure on this page comes from a guard in `tests/performance/`:
- **Timer**: `time.perf_counter_ns()` around each call
- **Runs, not samples**: each guard takes 5 independent runs, with a forced garbage collection before each. The figure is the mean of the per-run medians with its 95% t band at df = 4 (`stats_utils.summarize`)
- **Every raw sample kept**: percentiles cover all samples; outliers are counted, never filtered
- **Warm-up**: each run warms up for at least 5,000 calls (2,000 for the raw L1 guard), stopping once the last 1,000 calls vary by less than 10%, and at most twice that minimum. The 10-thread guard warms up 5,000 calls once, then 100 per thread before each run
- **Pre-flight**: each session prints whether the host is throttled or loaded (`measurement_env.py`)
- **No tail claims at 5 runs**: the `P95` line of a guard's summary reads "inconclusive" unless it has at least 10 runs and 400 samples (the `P99` line needs 10 runs and 2,000 samples). Every guard below runs 5, so both lines read "inconclusive" and this page quotes no tail percentile. The guards' own lines after the summary ("Total measured", "Decorator + complex payload") still print the raw p95 of every sample, and record it with `record_property`; no guard asserts on it, and at 5 runs it does not support a tail claim
- **Behaviour, not budgets**: a guard asserts that its timed calls took the path it names (the cached function ran once, an L1 hit never reached L2, the value came back intact), never a time

## Measured Figures

Each cell is the mean of five run medians ± its 95% t band (the summary's `Run median:` line), from two back-to-back passes of the same guard, CPython 3.14.3, x86_64 Linux, 2026-10-03. The pre-flight reported no throttling or load, but the host is shared, so the figures are indicative: compare them with each other rather than with your machine. Sessions on 2026-10-03 and 2026-10-04 also timed the decorated hits and a Redis L2 hit; later changes made those paths faster, and the [GitHub-Hosted Runner](#github-hosted-runner) table has their current figures.

| Path | Guard (`tests/performance/`) | Pass 1 | Pass 2 |
|------|------------------------------|-------:|-------:|
| Raw L1 byte-cache lookup, `L1Cache.get` of 1KB, no decorator | `test_statistical_rigor.py::test_l1_cache_hit_statistically_rigorous` | 354 ± 68ns | 362 ± 41ns |

In this table the band is ±19% of its figure in pass 1 and ±11% in pass 2. That spread is why these suites inform rather than gate (see [Performance Regression Testing](#performance-regression-testing)).

Run it yourself:
```bash
uv run pytest tests/performance/test_statistical_rigor.py::test_l1_cache_hit_statistically_rigorous -v -s
```

### GitHub-Hosted Runner

The `Bench` workflow (`.github/workflows/bench.yml`) runs five guards from `tests/performance/` on a GitHub-hosted `ubuntu-latest` runner and writes their run medians into its job summary; run it from the Actions tab to refresh this table. The README quotes it. [Its run on 2026-10-31](https://github.com/cachekit-io/cachekit-py/actions/runs/41178786512) used an AMD EPYC 7763 (4 vCPUs), CPython 3.14.7 and Redis 7 in a container on the same runner. That is a different machine from the table above, so compare figures within one table.

| Path | Guard (`tests/performance/`) | Run median |
|------|------------------------------|-----------:|
| Decorator + L1 hit, `@cache(backend=None)`, 100-user nested dict (23.5KB as MessagePack) | `test_production_realism.py::test_decorator_overhead_complex_dict` | 0.311 ± 0.000μs |
| Decorator + L1 hit, `@cache(backend=None, serializer="auto")`, 10K-row DataFrame | `test_production_realism.py::test_decorator_overhead_dataframe` | 0.31 ± 0.00μs |
| Decorator + L1 hit with an L2 backend configured (in-process, never reached), same 100-user dict | `test_production_realism.py::test_decorator_overhead_l1_hit_with_backend` | 284 ± 2μs (before the walk skip, see [Key Numbers](#key-numbers)) |
| Same hit, plaintext and encrypted arms, interleaved | `test_production_realism.py::test_encryption_overhead` | 286 ± 1μs and 295 ± 1μs (before the walk skip) |
| Redis L2 hit, L1 disabled, same 100-user dict | `test_production_realism.py::test_redis_l2_roundtrip` | 539 ± 3μs |

The job summary prints each figure in the unit its guard reports: the dict's L1-only hit, the hit with a backend configured and the encryption arms in ns, the DataFrame hit and the Redis L2 hit in μs. This table is in μs, so divide the ns figures by 1,000 when you copy them in.

The dict's L1-only hit timed 311ns in every one of its five runs. The encryption guard's arms differ by 9.3 ± 1.0μs (Welch 95%): an encrypted hit adds a decrypt. Both arms were measured before the walk skip, which plaintext hits take and encrypted hits do not (see [Why an L1-Only Hit Costs the Same for Any Payload](#why-an-l1-only-hit-costs-the-same-for-any-payload)). That is the first hosted run whose band excludes zero, and [the 2026-10-23 run](https://github.com/cachekit-io/cachekit-py/actions/runs/39987019881) put the difference at 10 ± 21μs, so one run does not pin the cost down (see [Not Measured Here](#not-measured-here)). Under the object cache's lock, as it ran until the lock-free hit, that hit took 0.906 ± 0.007μs in the 2026-10-23 run and 0.874 ± 0.005μs in [a run on an EPYC 7763 the same day as the table's](https://github.com/cachekit-io/cachekit-py/actions/runs/41177503539). Hosted runners do not always get the same CPU model: [a run of an earlier commit on an AMD EPYC 9V45 runner](https://github.com/cachekit-io/cachekit-py/actions/runs/39964388582/attempts/2) took 44-59% as long on every row (0.529μs for the dict's L1-only hit, 242 ± 12μs for the Redis L2 hit).

The workflow runs these guards with this command, against a Redis it starts on the runner:

```bash
REDIS_URL=redis://localhost:6379 uv run pytest tests/performance/test_production_realism.py -s -q -p no:cacheprovider \
  -k "complex_dict or dataframe or with_backend or encryption_overhead or redis_l2"
```

Without `REDIS_URL`, the Redis guard uses the pytest-redis server the suite starts. The encryption guard generates a throwaway key, so it needs no `CACHEKIT_MASTER_KEY`.

## Why an L1-Only Hit Costs the Same for Any Payload

With `backend=None`, cachekit keeps the returned object itself in memory and hands that same object back on a hit (see [backend=None](backends/none.md)). Nothing is serialized or deserialized, so the 100-user dict and a 10K-row DataFrame cost about the same: 0.311μs and 0.31μs on the hosted EPYC 7763, for the wrapper's call and one lookup in the extension (see [Decorator + L1 Hit](#decorator--l1-hit-hot-path)).

With an L2 backend configured, L1 holds the serialized bytes instead, and a hit deserializes them, so that hit costs more as the payload grows. For the 100-user dict it took 284 ± 2μs on the same runner, measured before the walk skip described below, about 910 times an L1-only hit of the same dict. Nearly all of the difference is deserializing 23.5KB of MessagePack back into about 400 nested dicts. It is still about half of a Redis L2 hit there (539 ± 3μs), which deserializes the same bytes after the round trip.

For a plaintext cache with the default serializer and integrity checking on, an entry's first L1 hit decodes it as every read does, under the structure walk that bounds untrusted MessagePack, and then L1 holds the entry marked as walked: its verified MessagePack when that is no longer than the envelope, which is true of small values and incompressible ones, and otherwise the envelope itself. Later hits skip the walk, because the bytes it passed cannot change. They also skip the decoder's object hook when none of the strings `__datetime__`, `__date__` or `__time__` (or any text starting `__date`) appears anywhere in the MessagePack, because the hook would then hand back every map unchanged, and a hit on held MessagePack skips the envelope's LZ4 decompress and xxHash3-64 checksum as well. Bytes read from L2 are walked on every read. The 100-user dict compresses to a 3,171-byte envelope from 23.5KB of MessagePack, so L1 keeps its envelope, and its hits skip the walk and the hook: on CPython 3.14.3 the hit fell from 2,731,277 to 2,192,392 instructions (−19.7%, callgrind with the [Instruction Budgets](#instruction-budgets) method; no budget gates this large-value hit, and `l1_hit_backed` gates the small-value hit on held MessagePack), and an interleaved wall-clock run on a shared x86_64 Linux host put it 20.9% faster (median of 12 paired processes; the base against itself, −1.2%). The same dict with datetimes in it keeps the hook and fell 5.6%. The hosted figures above were measured before this change. Encrypted caches always keep the ciphertext envelope, and every hit on one still decrypts, walks and runs the hook.

### Decorator + L1 Hit (Hot Path)

An L1-only hit takes 0.311μs for the 100-user dict and 0.31μs for the DataFrame on the hosted EPYC 7763 ([GitHub-Hosted Runner](#github-hosted-runner)). The wrapper makes one call into the extension (`HitMemo.get`), which takes no lock and runs:

- **The memo key.** The call's arguments become a key: one `int` or `str` argument is its own key, and any other call's key holds its argument tuple. The memo takes a call only when it has at most 8 arguments, every one an `int`, `float`, `bool`, `None`, `str` or `bytes` matched by its exact type, and its strings, bytes and keyword names hold at most 256 characters between them. A `float`, `bool` or `bytes` puts the argument types in the key, so `1`, `1.0` and `True`, which compare equal, keep their own slots. Any other call (a list, a dict, a `datetime`, an `IntEnum`, longer strings) builds its cache key every time and looks it up under the object cache's lock: a hit with one list argument costs about 22 times the instructions of a memoised one. The cache keys themselves are unchanged.
- **One dict lookup** in the function's memo, for the entry the object cache stored for that key.
- **Its liveness and deadline.** The entry must still be stored, and its deadline not yet reached: its hard expiry, or with SWR the earliest point a refresh could be flagged (`ttl * swr_threshold_ratio * 0.9` after it was stored), past which the hit goes to the lock, which decides on the refresh.
- **The function's names.** The memo answers only while the function's `__module__` and `__qualname__` are the objects it was built with, as its cache keys embed them: renaming the function re-keys its calls.
- **The use time** for the cache's LRU order, and **the hit count** for `cache_info()`, both atomic.

A memoised L1-only hit runs nothing that reads the stats context backend calls use, so it does not set one, and it records no Prometheus metric. A miss takes more Python frames than it did before the memo, so a recursive `@cache(backend=None)` function reaches the recursion limit sooner: on CPython 3.14 at the default limit of 1,000, it recurses 197 levels deep instead of 247.

The object cache keeps the memo in step under its lock. A store, or a read under the lock, points the call's slot at its entry, and every removal (expiry, eviction, `invalidate_cache()`, `cache_clear()`, the reset in a forked child) ends the entry's hits and drops its slots before it returns. So a hit never serves a value once its removal has returned, and the memo keeps no removed value. It keeps a call's arguments only while their slot lasts, and at most 1,024 slots per function: a full memo empties before it takes a new key. Full, it holds about 110 to 140KiB when each call passes one `int`, and under 4.5MiB when every call fills its 256 characters with 4-byte ones, the most a call can make it keep (3.5 to 3.8MiB measured on CPython 3.12, 3.14 and free-threaded 3.14 and 3.15).

Only an L1-only function keeps a memo. A function with a backend builds its key on every call, as it did before the memo: other processes can invalidate its entries, and the memo would keep the arguments they erase; with L1 off, the process keeps nothing else from a call; and an encrypted function must keep no plaintext argument. Its key is also a small part of what its hit costs.

With ten threads calling one function on the same key at once, `test_concurrent_cache_access` times each call once its thread is running: execution time after scheduling, not request latency. Each sample starts inside its thread's loop, so on a GIL build, where only one thread runs Python at a time, the time a thread waits for its turn falls outside every sample. Its last figures predate the memo, and the hosted workflow does not run it. No guard measures how long a request waits under contention.

The deterministic figure is the `l1_hit` row of the [instruction budgets](#instruction-budgets): about **2,500 instructions per call** on CPython 3.14 and 2,600 on 3.12, against 10,900 and 11,600 when the hit took the object cache's lock, and about 60,000 before the memo and the fast path.

The raw L1 byte-cache lookup, without the decorator, takes 354–362ns. An L1-only hit does not use that cache.

### L1-Only Miss at a Full Budget

An L1-only miss stores its result in the object cache. Once the function's byte budget (`l1_max_size_mb`, 100MB by default) is full, each store first removes the entries that have expired, then evicts least-recently-used ones until the new value fits. Expired entries go first wherever they sit in LRU order, so an entry read just before it expired never pushes out a live one. A min-heap of deadlines finds them: when nothing has expired, the store looks at the earliest deadline and evicts, without scanning the cache for expired entries. The one exception is the heap rebuild described below. Before the heap, every such store scanned every entry while holding the lock the function's misses and its hits under the lock wait on, so with `ttl=None` (stored for a year, so nothing expires) each one cost time in proportion to the number of entries cached.

The least recently used entry is found without a scan too. A lock-free hit records the time of its use on the entry but cannot reorder the cache, so the cache of an L1-only function with a memo (see [Decorator + L1 Hit](#decorator--l1-hit-hot-path)) keeps one LRU record per entry: each store files its entry's record in clock order, and an eviction that reaches an entry used since its record was filed re-files the record at that use, in a min-heap, and moves on. Evicting an entry unused since its store costs O(1), and each re-filed record O(log n). A cache whose hits all take the lock, as `@cache.local()`'s do, keeps its LRU order in the cache itself, as before.

The `object_cache_put_full` row of the [instruction budgets](#instruction-budgets) measures a store into a full budget of 10,000 entries: about 160,000 instructions on CPython 3.14, most of it the size estimate of the stored value, against 5.64 million when every store scanned. A whole L1-only miss into a full 1MB budget, decorator and function included, costs about 238,800 instructions on CPython 3.14, 1.6% more than before the lock-free hit: each store also creates the record its hits read and points the memo at it, and each eviction ends them.

The heap is memory the budget does not count: about 64 bytes per entry on CPython 3.12 and 72 on 3.14, plus one stale record for each entry removed or refreshed since the heap was last rebuilt. Once the stale records outnumber the entries, the store that finds it rebuilds the heap from the cache: one pass over the entries, about once per as many stores as there are entries. With a memo, each entry also carries the record its hits read and its LRU record, about 170 to 180 bytes per entry the budget does not count (CPython 3.12 and 3.14), and the LRU records are rebuilt the same way, sorted back into clock order.

### Backed L1 Expiry Sweep

With a backend, L1 runs a background sweep every `l1_cleanup_interval_seconds` (30 by default) that removes expired entries from each namespace's byte cache. Every get and put in the namespace waits on that cache's lock, so the sweep never holds it for a scan: it pops a min-heap of deadlines, at most 128 due entries per hold, and gives up the GIL between holds so a waiting get goes first. When nothing has expired, the sweep reads the earliest deadline and stops. Removing 100,000 expired entries holds the lock for at most about 0.4ms at a time (`test_l1_expiry_sweep_lock_holds`, thread CPU time, CPython 3.12 and 3.14). A get on another thread waits at most 0.05ms. When the sweep was a single scan, it held the lock 23–35ms and the get waited 20–33ms.

The heap costs about 64 bytes per entry on CPython 3.12 and 72 on 3.14, which the budget does not count. A removed, evicted or overwritten entry leaves its old heap record behind, including an overwrite with the same deadline: each entry points at its own record, and only that record is live. Once stale records outnumber the entries, the sweep drops them in batches of 1,000 per lock hold, freeing each batch before it yields, so a reader thread is never kept from the GIL while a large list is freed. It never rebuilds the heap in one hold, which would take about 33ms at 100,000 entries. With 100,000 live entries and 600,000 stale records, a reader waits at most a few milliseconds between gets during the compaction, against 53–80ms when the stale records were freed in one go. Where no sweep runs, in a child forked without at-fork hooks, a store rebuilds the heap in one pass once stale records outnumber the entries eight to one. That happens at most once per seven stores per entry. With the sweep running, it happens only when stores outrun the sweep by more than seven per entry per interval: over 23,000 stores a second into one namespace of 100,000 entries at the default 30 seconds. That store then holds the lock for the pass. Not rebuilding would leave those stale records, about 70 bytes each, outside the budget until the next sweep.

## L2 Backend (Redis) Performance

An L2 hit with L1 disabled, the 100-user dict (23.5KB as MessagePack) and Redis in a container on the same runner takes 539 ± 3μs on the hosted EPYC 7763: the decorator, the round trip and deserialization. That is about 1,730 times an L1-only hit of the same dict on the same runner. A Redis on another machine adds its network latency on top; no guard measures that.

## Latency by Serving Layer

`tests/performance/terrain_canary.py` records what each call of a cached read workload costs, split by the layer that answered it. It drives a Zipf workload (by default exponent 0.99 over 10,000 keys) through one unencrypted `@cache` function with L1 and a Redis L2. One caller makes the calls one at a time, at Poisson arrival times (200/s by default); when a slow call makes it late, it starts the next call as soon as it is free and drops the rest of the backlog, so it never runs above `--rate` to catch up. Each miss outlasts several arrival gaps, so the caller runs below schedule all run, most during warm-up; the recording states the rate it achieved, overall and in its busiest slice. It also states the values' size as MessagePack, for the median call and the 95th percentile (0.9 KB and 4.4 KB with the default 10,000 keys). It times each call with `perf_counter_ns` and attributes it to an L1 hit, an L2 hit or a miss by the change in `cache_info()`, cross-checked by a flag the function sets; a call whose counters and flag disagree is counted as unattributed, never guessed. The function sleeps 20 ms in place of a database call, so a miss's figure is that sleep plus the cache's own work.

It writes per-layer latency histograms (16 log-spaced bins per decade from 100 ns to 1 s) for each slice of time (3 s by default) as JSON, and prints each layer's share with its exact p50 and p99. The run starts from an empty cache under a fresh namespace and deletes nothing: entries expire after their ttl. The recording names where the Redis runs in the words `--l2-where` gives, which every run must pass: a URL cannot tell, since a tunnel or port-forward on `localhost` can reach a remote Redis. Its figures are one run on one host, not a guard:

```bash
uv run python tests/performance/terrain_canary.py --redis redis://localhost:6379/0 \
  --l2-where "same host, loopback TCP" --minutes 10 --out terrain.json \
  --harness https://github.com/cachekit-io/cachekit-py/blob/<sha>/tests/performance/terrain_canary.py
```

## Not Measured Here

These have no figure from the run-level harness that supports a claim:
- **Encryption overhead through the decorator**: `test_encryption_overhead` interleaves its plaintext and encrypted runs and prints both results. On the 100-user dict, the difference between the arms' run medians stayed inside its own band in every pass on a shared host and on the hosted runner until the 2026-10-31 hosted run, which put it at 9.3 ± 1.0μs ([GitHub-Hosted Runner](#github-hosted-runner)). One run that resolves it, after runs that did not, does not pin the cost down. The deterministic costs are the `secure_l1_hit` and `serializer_encrypted` rows of [Instruction Budgets](#instruction-budgets), beside `l1_hit_backed`, the same value's L1 hit through plaintext `@cache` with a backend (the two presets differ in more than encryption); see [Zero-Knowledge Encryption](features/zero-knowledge-encryption.md) for how encryption works
- **Request latency under contention**: the 10-thread guard times each call after its thread is scheduled (see [Decorator + L1 Hit](#decorator--l1-hit-hot-path))
- **The async decorator, serializer comparisons and a Redis on another machine**

For choosing a serializer, see the [Serializer Guide](serializers/README.md).

## Performance Optimization Tips

### 1. Use L1 Cache Aggressively

**Default configuration already enables L1:**
```python
from cachekit import cache

@cache  # L1 enabled by default
def expensive_function(user_id: int):
    return fetch_user_data(user_id)
```

**L1 gives you** (hosted EPYC 7763 figures, [GitHub-Hosted Runner](#github-hosted-runner)):
- No network round trip on a hit
- An L1-only hit in 0.311μs, against 539μs for a Redis L2 hit of the same 100-user dict
- With an L2 backend configured, an L1 hit of the 100-user dict in 284μs, measured before hits stopped re-checking bytes they had already checked (about 20% less since): it skips the round trip but still deserializes

### 2. Choose the Right Serializer

**For DataFrames:**
```python
from cachekit.serializers import ArrowSerializer

@cache(serializer=ArrowSerializer())
def get_large_dataset(date: str):
    return load_dataframe(date)  # columnar Arrow format
```

**For everything else:**
```python
@cache  # StandardSerializer (msgpack) is fine
def get_user_config(user_id: int):
    return {"settings": {...}, "preferences": {...}}
```

### 3. Batch Similar Queries

**Bad (many small cache hits):**
```python notest
for user_id in user_ids:
    data = get_user_data(user_id)  # one decorator call per user
```

**Good (one large cache hit):**
```python notest
@cache
def get_users_batch(user_ids: list[int]):
    return [fetch_user_data(uid) for uid in user_ids]

data = get_users_batch(user_ids)  # one decorator call for the batch
```

### 4. Tune TTL for Hit Rate

**Short TTL (high freshness, lower hit rate):**
```python
@cache(ttl=60)  # 1 minute
def get_real_time_price(symbol: str):
    return fetch_current_price(symbol)
```

**Long TTL (lower freshness, higher hit rate):**
```python
@cache(ttl=86400)  # 24 hours
def get_historical_data(symbol: str, date: str):
    return fetch_historical(symbol, date)  # Immutable data
```

### 5. Monitor Cache Performance

**Hit rate per function:** every decorated function has `cache_info()`, with hits, misses, the L1/L2 split and the average L2 latency, on any backend and with no scrape ([API Reference](api-reference.md#per-function-statistics-via-cache_info)).

**Prometheus:** a decorator with a backend records three series on the default `prometheus_client` registry, with no configuration:
- `cache_operations_total`: operation count. Labels: `operation`, `namespace`, `success`, `serializer`
- `cache_operation_duration_ms`: operation latency histogram, in milliseconds (buckets 0.01 to 1000)
- `cache_operation_size_bytes`: payload size histogram, in bytes

`circuit_breaker_state` counts live circuit breakers per namespace and state. No series carries a hit/miss label, and the only tier label is `serializer` (`l1_memory` for an L1 hit, `rust` for the L2 path), so read the hit rate from `cache_info()`. An L1-only decorator records no `get` or `set` operations in the three series: one with `backend=None`, or one that resolves no backend ([Backend Resolution Priority](backends/README.md#backend-resolution-priority)). `MonitoringConfig(collect_stats=False)` turns the three series off, as `@cache.minimal` and `@cache.test` do. cachekit starts no HTTP server; your app exposes the registry. See [Prometheus Metrics](features/prometheus-metrics.md) for exposition, queries and alerts.

## Performance Bottlenecks and Mitigations

### Bottleneck 1: The L2 Round Trip

**Problem:** a Redis L2 hit takes 539μs on the hosted EPYC 7763, about 1,730 times an L1-only hit of the same 100-user dict (23.5KB as MessagePack). A Redis on another machine adds its network latency.

**Mitigations:**
- **Keep L1 on** (the default), so repeat reads skip the round trip
- **Tune L1 size:** raise `L1CacheConfig(max_size_mb=...)` or `CACHEKIT_L1_MAX_SIZE_MB` (default: 100MB per namespace) if entries are evicted early
- **Optimize L1 TTL:** Match L1 TTL to data freshness requirements
- **Reduce payload size:** cache only what you need; an L2 hit, and an L1 hit with a backend configured, deserialize the whole value

### Bottleneck 2: Decorator Overhead

**Problem:** every decorated call pays for its cache key and the decorator's bookkeeping: 0.311μs per L1-only hit on the hosted EPYC 7763 when the memo answers it, and more when the memo does not take the call, so its key is built every time.

**Mitigations:**
- **This is acceptable** for any function slow enough to be worth caching
- **Pass scalar arguments** (`int`, short `str`, ...) to hot `backend=None` functions, so the memo answers them (see [Decorator + L1 Hit](#decorator--l1-hit-hot-path))
- **Batch queries:** one call that returns many items pays the overhead once

### Bottleneck 3: Page Faults on Large Values

**Problem:** on Linux with glibc, an encrypted get or set of a 1MB value can spend a third of its CPU or more on page faults, and an incompressible set over three quarters.

**Mitigation:** set `MALLOC_MMAP_THRESHOLD_` and `MALLOC_TRIM_THRESHOLD_` together, if most cached values are around 1MB (see [Large Payloads and glibc malloc](#large-payloads-and-glibc-malloc)).

## Large Payloads and glibc malloc

A set holds several payload-sized buffers at once: the MessagePack output, the LZ4 output, the envelope, the ciphertext and the stored frame; a get holds as many. When they are freed, glibc hands the top of its heap back to the kernel once more than its trim threshold is free, and any buffer at or above its mmap threshold that neither a freed chunk nor the heap's free top can hold gets a fresh `mmap`. The next call takes those pages back, one page fault per 4KB. glibc raises both thresholds at run time to fit the largest `mmap`-ed buffer freed so far, so the fault count depends on every large allocation in the process and on the payload's shape: removing one buffer can raise the count for another shape.

Two glibc settings, set together, removed every fault from both 1MB sets measured and cut the JSON-like get's from 632 to 252 (the random-bytes get had none):

```bash
export MALLOC_MMAP_THRESHOLD_=4194304   # 4 MiB: buffers below this come from the heap, not a fresh mmap
export MALLOC_TRIM_THRESHOLD_=16777216  # 16 MiB: return the heap's free top to the kernel only once it exceeds this
```

Set them in the environment the process starts with, such as a container's `ENV` or a systemd unit's `Environment=`. glibc reads them once at startup, so setting `os.environ` from Python is too late. They apply to the whole process, not only to cachekit.

`tests/performance/large_payload_bench.py ab` measured each setting against the default allocator, in 30 interleaved process pairs per cell, in [the `Bench` workflow's run on 2026-10-25](https://github.com/cachekit-io/cachekit-py/actions/runs/40159568618) on a GitHub-hosted `ubuntu-latest` runner: AMD EPYC 7763, glibc 2.39, CPython 3.14.7, cachekit-core 0.7.0. The workload is `@cache.io` with encryption, L1 off and an in-process backend, so the HTTP response body a real backend allocates on every get is not in these figures. That is one more payload-sized buffer per get, and since every such buffer moves the fault count, the get columns can differ against a real backend in either direction; a set sends its value without one. The value is about 1MB of JSON-like text in a dict, or 1,000,000 random bytes. Each cell is the change in CPU per call, with the 95% CI of the paired change, and the faults per call under that setting:

| Setting | 1MB JSON-like get | 1MB JSON-like set | 1MB random-bytes get | 1MB random-bytes set |
|---|---|---|---|---|
| Default glibc malloc | 2.03ms, 632 faults | 2.91ms, 556 faults | 0.51ms, 0 faults | 3.06ms, 1,442 faults |
| Both: `MALLOC_MMAP_THRESHOLD_` 4 MiB and `MALLOC_TRIM_THRESHOLD_` 16 MiB | −28.5% [−32.1, −24.8], 252 faults | −33.1% [−33.4, −32.8], 0 | −0.1% [−1.3, +1.1], 0 | −77.9% [−78.3, −77.6], 0 |
| `MALLOC_TRIM_THRESHOLD_` 16 MiB alone | +34.8% [+29.5, +40.2], 981 | +26.9% [+26.6, +27.3], 921 | +494% [+492, +496], 1,719 | +38.5% [+37.9, +39.2], 2,212 |
| `MALLOC_TOP_PAD_` 16 MiB alone | −27.9% [−32.1, −23.5], 252 | −33.2% [−33.7, −32.7], 0 | +310% [+308, +313], 982 | −23.2% [−23.7, −22.6], 983 |
| `MALLOC_TOP_PAD_` 64 MiB alone | −29.9% [−34.0, −25.7], 252 | −34.0% [−34.3, −33.6], 0 | −1.1% [−2.8, +0.8], 0 | −78.7% [−79.4, −77.9], 0 |

The default row is the base arm of the "both" job. [A second run of that job](https://github.com/cachekit-io/cachekit-py/actions/runs/40159568618/attempts/2) agreed within about 1 point on every 1MB cell. Do not set one threshold alone: glibc stops adjusting either threshold once one is set, so the other stays at its 128KB default. `MALLOC_TRIM_THRESHOLD_` alone was slower on all four 1MB paths. `MALLOC_MMAP_THRESHOLD_` 4 MiB alone, on an Intel Xeon Platinum 8370C runner in the same run, slowed the JSON-like get by 26.6%, the random-bytes get by 80.0% and the JSON-like set by 2.3%, and sped the random-bytes set by 17.9%.

The cost of setting both, on the same runner:
- **Memory:** peak RSS per process rose by 0.2 to 1.1MB, in a single-threaded process. The trim threshold is not a cap on retained memory: it only decides when the free space at the top of a heap goes back to the kernel. `free()` never trims free chunks below a live allocation, and buffers under 4 MiB no longer get their own `mmap`, so a heap fragmented by long-lived objects can hold far more than 16 MiB of free memory, in each of the several arenas a threaded process has. Size memory limits from your own process's RSS under load, not from these settings; that was not measured here.
- **A 64KB incompressible get took about a third longer:** +32.7% [+31.3, +34.3], 67.0μs to 88.9μs, and +32.6% in the second run, with no page faults in either arm. Both `MALLOC_TOP_PAD_` settings did the same, and also slowed 64KB sets by 4% to 6%. Every other 64B, 1KB and 64KB cell changed by 1.2% or less.

So set both for a process whose cached values are mostly around 1MB, and measure your own machine first: `uv run python tests/performance/large_payload_bench.py ab --candidate-env MALLOC_MMAP_THRESHOLD_=4194304 --candidate-env MALLOC_TRIM_THRESHOLD_=16777216` runs the same comparison, and `--shapes json-1m bin-1m` narrows it to the 1MB cells. Nothing between 64KB and 1MB was measured, so where the settings start to pay off is not known. The MessagePack encoder grows its buffer to up to twice the value's size, so from about 2MB some buffers reach the 4 MiB mmap threshold and can be `mmap`-ed again once no freed chunk or free top in the heap can hold them; larger values were not measured. Free-threaded CPython (3.13t, 3.14t) allocates Python objects with its own allocator, and musl (Alpine) and macOS allocators work differently; none of them was measured.

## Performance Regression Testing

Wall-clock benchmarks do not gate CI, and no test in `tests/performance/` asserts a time budget: on a shared machine the run-level bands are too wide to gate on (see [Measured Figures](#measured-figures)). The wall-clock suites below run in the default `pytest tests/` invocation, report their figures, and fail only on behaviour:

```bash
uv run pytest tests/performance/ -v -m performance
```

The regression gate is the instruction budget: `make perf-ir` on your machine, and the `Instruction budget` check on every pull request (see [In CI](#in-ci)).

## Instruction Budgets

`make perf-ir` counts the instructions each hot path executes per call and fails when a path costs **1% or more** above its committed budget. It warns from 0.2%. The orjson round trip fails at 2% and warns from 1% (see Limits). Instruction counts are deterministic where wall clock is not: two full runs agree within 0.08% per path (0.01% on CPython 3.12).

**Method** (`tests/performance/ir_budget.py`; the measured process is `ir_workload.py`):
- Each path runs under `valgrind --tool=callgrind --separate-threads=yes`, and only the main thread is counted. cachekit's background threads (L1 cleanup) vary by tens of percent between identical runs.
- Per-call cost is `(Ir[3000 calls] - Ir[1000 calls]) / 2000`, so interpreter startup (~2 billion instructions) and warmup cancel. Interpreter teardown is skipped. The two real-data paths cost about 60 million instructions per call, so they run 10 and 30 calls after 5 warmup calls; startup still cancels, and two runs agreed within 0.03%.
- The measured process has a fixed environment (`PYTHONHASHSEED=0`, one BLAS/OpenMP thread, a one-day L1 cleanup interval, nothing inherited), seeded log sampling, and main-thread clocks that advance 1μs per read. Code that records its own duration otherwise executes more instructions when it runs slower. The cleanup sweep reads the real clock, so inside a run it would evict entries stamped with the pinned one.
- Nothing that runs on real time lands in the measured loop: no background thread wakes, cyclic GC is off, and the GIL switch interval is long enough that the main thread gives up the GIL only where the code releases it.
- Each path runs at five heap layouts (0 to 880 extra objects held before the workload) and its figure is the median. Any code change moves the layout, and the allocators take shorter or longer paths in some heap states: single layouts of the orjson round trip and the minimal L1 hit sat up to 2.4% from the rest. The median ignores one or two such layouts. The cheapest layout is not used, because a lucky one can sit 1% below the rest, and a budget recorded there makes every later run look like a regression.
- `ir_workload.py`'s text is compiled into every measured process, so editing it moves the layout the budgets were recorded at: re-record after any change to it. The gate itself (`ir_budget.py`) is kept out of the measured process, so editing it does not move a budget.
- The L2 paths and the backed L1 hit use an in-process dict backend, so they cost instructions only: no sockets, retries or timeouts.
- The metrics collector starts synchronous and switches to batched mode (generic metrics queue for a worker thread) when its 5 s check sees more than 100 records/s. Cache operations record into per-thread cells in either mode. Pinned clocks never reach that check, so the plain paths measure a synchronous collector, and `l2_hit_async_metrics` the batched collector a busy long-lived process runs; it checks that no cache operation queues. Every metrics path records once per call through the same method, so one batched path covers that mode.

**Budgets** (instructions per call, `tests/performance/ir_baselines.json`):

| Path | What one call does | CPython 3.12 | CPython 3.14 |
|------|--------------------|-------------:|-------------:|
| `l1_hit` | `@cache(backend=None)` L1 hit | 2,596 | 2,482 |
| `l1_hit_backed` | `@cache` with a backend configured, L1 hit (never reaches the in-process dict backend; decodes the verified MessagePack L1 holds after the first hit) | 89,226 | 90,146 |
| `minimal_l1_hit` | `@cache.minimal(backend=None)` L1 hit | 2,596 | 2,488 |
| `l2_hit` | `@cache`, L1 disabled, L2 hit, as one in-process single-flight trip | 172,539 | 167,686 |
| `miss` | `@cache`, L1 disabled, L2 miss, compute, L2 write, all as one in-process single-flight trip | 213,364 | 204,897 |
| `secure_l1_hit` | `@cache.secure` L1 hit (builds its key, decrypts the ciphertext L1 holds) | 140,428 | 141,391 |
| `l2_hit_async_metrics` | `l2_hit` with the metrics collector in batched mode | 171,976 | 167,728 |
| `serializer_default` | `StandardSerializer` round trip, small dict | 58,733 | 59,574 |
| `serializer_default_records` | `StandardSerializer` round trip, list of 100 six-field records (dict-heavy decode) | 1,420,388 | 1,424,292 |
| `serializer_auto` | `AutoSerializer` round trip | 65,942 | 66,628 |
| `serializer_orjson` | `OrjsonSerializer` round trip | 22,135 | 22,825 |
| `serializer_arrow` | `ArrowSerializer` round trip, 100-row DataFrame | 1,969,626 | 1,980,777 |
| `serializer_arrow_usgs` | `ArrowSerializer` round trip, real 12,535-row DataFrame (see Real data below) | 61,781,406 | 61,324,948 |
| `serializer_auto_usgs` | `AutoSerializer` round trip, same frame (it hands the frame to Arrow) | 61,793,577 | 61,344,459 |
| `serializer_encrypted` | `EncryptionWrapper` encrypt + decrypt round trip | 100,097 | 101,938 |
| `byte_storage_store_40b` | `ByteStorage.store()` alone, 40-byte compressible input (under cachekit-core's 64-byte literals-only LZ4 threshold) | 2,658 | 2,540 |
| `byte_storage_store_64k` | `ByteStorage.store()` alone, 64 KiB of address-like text | 720,631 | 720,751 |
| `file_set` | `FileBackend.set()` overwriting one key in a 1,000-entry cache (no eviction) | 76,035 | 74,112 |
| `object_cache_put_full` | `ObjectCache.put()` of a new key into a full 10,000-entry byte budget (evicts the LRU entry; nothing expired) | 165,014 | 160,608 |

Budgets are per interpreter (minor version, build flavour, machine); an interpreter without budgets fails with `no budget`. They were recorded on CPython 3.12.12 and 3.14.3, x86_64, glibc 2.39, with the release extension that `uv sync` builds. Counts depend on that whole build, so on a different interpreter, extension or C library, record a baseline on `main` first (`--update --allow-increase`) and compare your branch against it. `l2_hit` and `l2_hit_async_metrics` now cost the same within 0.4%: a cache operation records into per-thread cells in either mode. Against the previous budgets that is 14,700 to 16,600 fewer instructions per L2 hit than the synchronous Prometheus update, and 5,400 to 7,500 fewer than queueing the record for the batching worker.

**Sensitivity:** before the key memo, one extra BLAKE2b hash of the cache key per call raised every key-generating path by 4,700 to 4,900 instructions (`l1_hit` +6.1%, `l2_hit_async_metrics` +1.5%, `miss` +1.4%) and failed the gate, while the serializer paths, which generate no key, stayed within 0.11%. An interleaved wall-clock run agreed in sign (+286ns per L1 hit, median of 12 paired processes).

**Limits:** instruction counts do not weight cache misses or branch mispredictions. A claimed speed-up still needs an interleaved wall-clock comparison; the instruction count only guarantees the work did not grow. Paths that wait on a network backend are not covered. Cyclic-GC cost is outside the budgets; allocation and reference counting are inside. A Prometheus scrape, which sums each thread's cache-operation cells, runs outside the measured calls and is not budgeted. Part of pyarrow's work on the real-data paths is not budgeted either: about half of their instructions run on pyarrow's threads. The orjson round trip's 1 KB output buffer comes from glibc malloc, whose path length depends on heap state that no layout sample pins, so its figure moved 1.3% between unrelated changes; it is gated at 2%. Unrelated changes can still move another path's figure by up to 0.6% (a `WARN`), and a `LOWER` verdict on a path the change did not touch is a layout shift, not a saving: ratchet only the paths the change touched (`--update --path <path>`). A change to the Rust extension moves the paths that run through it more, with no change in their work: `main`'s own source, built with and without symbol stripping, moved the `ByteStorage` round trip of `serializer_default` by 1.9%, while every function in it cost the same over 5,000 round trips, so the move is a one-time allocator event that the binary's layout puts inside one loop size. After an extension change, re-record the serializer paths that fail (`--update --allow-increase --path <path>`) and say why in the PR.

```bash
make perf-ir         # gate: fail on a >=1% per-call regression (orjson 2%; needs valgrind; 190 runs, several minutes)
make perf-ir-update  # ratchet: write lower measured figures back as budgets, never higher
```

The gate runs at most 8 callgrind processes at a time (fewer on a smaller machine), and each path peaks at 0.5 to 1.0 GiB per run, so it can share a machine with other work. `--jobs N` changes that. Callgrind runs still need a bound: a workload that grows under callgrind can use many GiB, and a FileBackend `set()` workload passed 9 GiB in under four minutes. The gate kills a run that takes longer than `--child-timeout` minutes (default 15) and fails, and it kills every live run when it exits, fails, or gets SIGINT or SIGTERM. On Linux with systemd, also cap the whole gate's memory and run time:

```bash
systemd-run --user --scope -p MemoryMax=10G -p MemorySwapMax=0 -p RuntimeMaxSec=90min -- \
    uv run python tests/performance/ir_budget.py
```

The gate does not call `systemd-run` itself, because CI runners and macOS lack it. A deliberate cost increase (a new feature on the hot path) is recorded with `uv run python tests/performance/ir_budget.py --update --allow-increase`, and the PR states why.

**Real data.** `tests/data/usgs_earthquakes_2024-01.parquet` is a pinned slice of a public dataset: one month of the USGS earthquake catalogue, 12,535 rows by 13 columns, with timezone-aware timestamps, a nullable integer, categoricals and high-cardinality strings. `tests/data/README.md` records its source, licence, sha256 and the commands that made it. `tests/unit/test_real_dataset.py` runs on every pull request. It round-trips the frame and three results derived from it (a `groupby`, a `pivot_table` and a re-indexed filter) through `AutoSerializer` and `ArrowSerializer`, and a float64 array of its numeric columns through `AutoSerializer` only (ArrowSerializer takes tables, not bare arrays). Equality is exact, each encoding must carry its integrity checksum and stay at most 10% above a committed size, and the test serves a cached aggregation from a `FileBackend` through `@cache` and `@cache.secure`. The `serializer_*_usgs` paths budget the full-frame round trip:

```bash
uv run pytest tests/unit/test_real_dataset.py
uv run python tests/performance/ir_budget.py --path serializer_arrow_usgs --path serializer_auto_usgs
```

### In CI

Every pull request runs the gate in the `Instruction budget (shard K/4)` jobs of `.github/workflows/ci.yml`, on GitHub-hosted `ubuntu-latest` runners with CPython 3.12. They do not compare against the committed budgets: those belong to the machine and interpreter build that recorded them, and a CPython 3.12 patch release alone read 2.0% to 3.6% over them with no code change. Instead each job builds two checkouts side by side, the pull request (its merge commit) and its base (the base branch tip), measures both on the same runner, and fails when a path costs 1% or more above the base (2% for the orjson round trip). That is `ir_budget.py --base DIR`, which never writes the committed budgets; CI adds `--report FILE`, which writes the verdict and every path's figures as JSON.

- A release-please pull request (head branch `release-please--*`) is measured against the last release instead: the tag `v<version>`, where `<version>` is the `"."` entry of the base's `.release-please-manifest.json`. That pull request is rebased onto `main` on every push, so against `main` it would measure nothing but a version bump. Against the tag, the comment shows each path's cumulative drift since the previous release, including the increases accepted one pull request at a time, and paths added since then show as `new`. A tag that cannot be fetched fails the job; it never falls back to `main`. The `Instruction budget base` job picks the base once per workflow run with `.github/scripts/perf-ir-base.sh`, and the shards and the comment both use its result, so a re-run shard or a tag moved mid-run cannot make the comment name a base the shards did not measure.
- Both checkouts must sit at absolute paths of the same length, and the gate refuses a base that does not. The measured process's argv and import paths move its heap layout: a relative workload path against an absolute one read `l1_hit` 0.3% apart on identical source; at equal-length absolute paths the same pair agreed within 0.01%.
- The two `serializer_*_usgs` paths are skipped (`--skip`): pyarrow's parquet reader can execute an instruction valgrind does not support on some CPUs, and the run then dies with SIGILL.
- Four runners split the remaining paths (`--shard 1/4` to `--shard 4/4`), so the check takes minutes rather than half an hour.
- A path the base does not have yet is reported as `new` and not compared.
- Each pull request carries one comment from `github-actions[bot]` with the result of its latest completed run: the overall verdict, the head and base commits measured, the interpreter, and every path's verdict, Ir/op on both sides and delta. Each run updates the same comment in place. A shard that ends without a verdict (a crash, an environment error, a timeout) shows as "no result", linked to its job, and the verdict reads `INCOMPLETE`, never `PASS`. The `Instruction budget report` job renders it from the shards' `--report` files with `tests/performance/ir_report.py`, and `Instruction budget comment` posts it: only that job can write to the pull request, and it never checks the code out. Pull requests from forks get no comment, because their token cannot write to the pull request; their shard logs still carry the result.
- CI keeps no budgets of its own, so there is nothing to re-record for it, and it ignores the committed ones entirely. They are still ratcheted on your machine with `make perf-ir-update`. A deliberate cost increase (`--update --allow-increase`, with the reason in the PR) therefore shows as a red `Instruction budget` check and a `FAIL` in the pull request's instruction-budget comment: the check is not part of `CI Success`, and the reviewer accepts the increase at merge. A raised committed budget cannot waive it, because a budget diff cannot tell a deliberate increase from a re-record on another interpreter or extension build, which moved one path 8.8% with no code change.

To reproduce a CI verdict, check the base out beside your branch at a path of the same length, run `uv sync --group dev` in both, and run the gate from your branch:

```bash
git worktree add --detach ../base origin/main   # ../base must be as long as this checkout's path
(cd ../base && uv sync --python 3.12 --group dev)
uv run python tests/performance/ir_budget.py --base ../base --skip serializer_arrow_usgs --skip serializer_auto_usgs
```

---

## Next Steps

**Previous**: [Data Flow Architecture](data-flow-architecture.md) - Understand the system design
**Next**: [Comparison Guide](comparison.md) - How cachekit compares to alternatives

## See Also

- [Data Flow Architecture](data-flow-architecture.md) - Component breakdown and latency sources
- [Comparison Guide](comparison.md) - Performance vs. other libraries
- [Configuration Guide](configuration.md) - Tuning for your environment
- [Serializer Guide](serializers/README.md) - Serialization performance characteristics
- [API Reference](api-reference.md) - All configurable parameters

---

<div align="center">

**[GitHub Issues](https://github.com/cachekit-io/cachekit-py/issues)** · **[Documentation](README.md)**

</div>
