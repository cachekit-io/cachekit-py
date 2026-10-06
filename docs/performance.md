**[Home](README.md)** › **Architecture** › **Performance**

# Performance Guide

> **Sub-millisecond cache operations, measured and benchmarked**

---

## Key Numbers

> [!TIP]
> **Key numbers** (mean of five run medians, two passes; CPython 3.14.3, x86_64 Linux, 2026-10-03 and 2026-10-04; indicative wall clock on a shared host):
> - **Decorator + L1 hit, `@cache(backend=None)`, 100-user nested dict (23.5KB as plain MessagePack)**: 5.6–6.4μs (61k instructions per call, see [Instruction Budgets](#instruction-budgets))
> - **Same hit, 10K-row DataFrame**: 5.1–5.4μs. An L1-only hit never serializes, so payload size barely matters
> - **Decorator + L1 hit with an L2 backend configured, same dict**: 239–276μs. With a backend, L1 holds bytes and every hit deserializes them
> - **Raw L1 byte-cache lookup** (`L1Cache.get`, no decorator): 354–362ns
> - **Redis L2 hit**, L1 disabled, same dict, Redis on loopback: 0.41–0.42ms

## Measurement Methodology

Every figure on this page comes from a guard in `tests/performance/`:
- **Timer**: `time.perf_counter_ns()` around each call
- **Runs, not samples**: each guard takes 5 independent runs, with a forced garbage collection before each. The figure is the mean of the per-run medians with its 95% t band at df = 4 (`stats_utils.summarize`)
- **Every raw sample kept**: percentiles cover all samples; outliers are counted, never filtered
- **Warm-up**: each run warms up for at least 5,000 calls (2,000 for the raw L1 guard), stopping once the last 1,000 calls vary by less than 10%, and at most twice that minimum. The 10-thread guard warms up 5,000 calls once, then 100 per thread before each run
- **Pre-flight**: each session prints whether the host is throttled or loaded (`measurement_env.py`)
- **No tail claims at 5 runs**: the `P95` line of a guard's summary reads "inconclusive" unless it has at least 10 runs and 400 samples (the `P99` line needs 10 runs and 2,000 samples). Every guard below runs 5, so both lines read "inconclusive" and this page quotes no tail percentile. The guards' own lines after the summary ("Total measured", the ✅ line, "Decorator + complex payload") still print the raw p95 of every sample, which their thresholds check; at 5 runs that number does not support a tail claim

## Measured Figures

Each cell is the mean of five run medians ± its 95% t band (the summary's `Run median:` line), from two back-to-back passes of the same guards, CPython 3.14.3, x86_64 Linux: the first five rows on 2026-10-03, the last two on 2026-10-04. The pre-flight reported no throttling or load, but the host is shared, so the figures are indicative: compare them with each other rather than with your machine.

| Path | Guard (`tests/performance/`) | Pass 1 | Pass 2 |
|------|------------------------------|-------:|-------:|
| Decorator + L1 hit, `@cache(backend=None)`, 100-user nested dict (23.5KB as MessagePack) | `test_production_realism.py::test_decorator_overhead_complex_dict` | 6.39 ± 2.03μs | 5.55 ± 0.83μs |
| Decorator + L1 hit, `@cache(backend=None)`, `User` dataclass | `test_production_realism.py::test_decorator_overhead_dataclass` | 6.27 ± 2.10μs | 4.98 ± 0.07μs |
| Decorator + L1 hit, `@cache(backend=None, serializer="auto")`, 10K-row DataFrame | `test_production_realism.py::test_decorator_overhead_dataframe` | 5.38 ± 0.15μs | 5.08 ± 0.17μs |
| Raw L1 byte-cache lookup, `L1Cache.get` of 1KB, no decorator | `test_statistical_rigor.py::test_l1_cache_hit_statistically_rigorous` | 354 ± 68ns | 362 ± 41ns |
| Redis L2 hit, L1 disabled, same 100-user dict, Redis on loopback | `test_production_realism.py::test_redis_l2_roundtrip` | 416 ± 139μs | 407 ± 159μs |
| Decorator + L1 hit with an L2 backend configured (in-process, never reached), same 100-user dict | `test_production_realism.py::test_decorator_overhead_l1_hit_with_backend` | 239 ± 31μs | 276 ± 120μs |
| Decorator + L1 hit, `@cache(backend=None)`, same 100-user dict, 10 threads on one key, per call | `test_production_realism.py::test_concurrent_cache_access` | 5.77 ± 0.47μs | 6.39 ± 1.53μs |

The bands run from ±1% to ±43% of their figure, and pass 2 sits between 2% above and 21% below pass 1 on the first five rows, and up to 15% above it on the last two. That spread is why these suites inform rather than gate (see [Performance Regression Testing](#performance-regression-testing)).

Run them yourself:
```bash
uv run pytest tests/performance/test_production_realism.py -v -s \
  -k "complex_dict or dataclass or dataframe or redis_l2"
uv run pytest tests/performance/test_statistical_rigor.py::test_l1_cache_hit_statistically_rigorous -v -s
uv run pytest tests/performance/test_production_realism.py -v -s \
  -k "with_backend or concurrent or encryption_overhead"
# The Redis guard skips unless Redis answers at REDIS_URL (default redis://localhost:6379)
# The encryption guard skips unless CACHEKIT_MASTER_KEY is set (64 hex characters)
```

## Why an L1-Only Hit Costs the Same for Any Payload

With `backend=None`, cachekit keeps the returned object itself in memory and hands that same object back on a hit (see [backend=None](backends/none.md)). Nothing is serialized or deserialized, so the 100-user dict, a dataclass and a 10K-row DataFrame all cost about the same: key generation, the lookup and the decorator's bookkeeping.

With an L2 backend configured, L1 holds the serialized bytes instead, and a hit deserializes them, so that hit costs more as the payload grows. For the 100-user dict it takes 239–276μs, against 5.6–6.4μs for an L1-only hit of the same dict: 37 to 50 times as much. Nearly all of the difference is deserializing 23.5KB of MessagePack back into about 400 nested dicts. It is still 57–68% of a loopback Redis L2 hit (0.41–0.42ms), which deserializes the same bytes after the round trip.

### Decorator + L1 Hit (Hot Path)

An L1-only hit takes 5.0–6.4μs across the three payloads above.

With ten threads calling one function on the same key at once, each call took 5.8–6.4μs at the median once its thread was running. That is execution time after scheduling, not request latency. Each sample starts inside its thread's loop, so on a GIL build, where only one thread runs Python at a time, the time a thread waits for its turn falls outside every sample. No guard measures how long a request waits under contention.

The deterministic figure is the `l1_hit` row of the [instruction budgets](#instruction-budgets): about **61,000 instructions per call** on CPython 3.12 and 62,000 on 3.14. Key generation, the L1 lookup, the `cache_info()` hit counter and the decorator's own bookkeeping are all inside that count; this L1-only path records no Prometheus metric.

The raw L1 byte-cache lookup, without the decorator, takes 354–362ns. An L1-only hit uses a separate object cache, whose lookup no guard times on its own.

## L2 Backend (Redis) Performance

An L2 hit with L1 disabled, the 100-user dict (23.5KB as MessagePack) and Redis on the same machine takes 0.41–0.42ms: the decorator, the round trip over loopback and deserialization. That is 65–73 times an L1-only hit of the same dict (pass by pass). A Redis on another machine adds its network latency on top; no guard measures that.

## Not Measured Here

These have no figure from the run-level harness that supports a claim:
- **Encryption overhead through the decorator**: `test_encryption_overhead` interleaves its plaintext and encrypted runs and prints both results, but on this shared host the difference between the arms' run medians stayed inside its own band in all four passes on the 100-user dict (from −0.4 ± 14.6μs to −97 ± 194μs, Welch 95%): two on a quiet host with one arm's runs after the other's, and two interleaved on a loaded host. That bounds nothing: decryption may cost little next to deserialization, or the noise may hide it. The deterministic costs are the `secure_l1_hit` and `serializer_encrypted` rows of [Instruction Budgets](#instruction-budgets); see [Zero-Knowledge Encryption](features/zero-knowledge-encryption.md) for how encryption works
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

**L1 gives you:**
- No network round trip on a hit
- An L1-only hit in 5.0–6.4μs, against 0.41–0.42ms for a loopback Redis L2 hit
- With an L2 backend configured, an L1 hit of the 100-user dict in 239–276μs: it skips the round trip but still deserializes

### 2. Choose the Right Serializer

**For DataFrames (10K+ rows):**
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

**Built-in metrics via Prometheus:**
```python notest
from cachekit.config import DecoratorConfig
from cachekit.config.nested import PrometheusConfig

config = DecoratorConfig(
    prometheus=PrometheusConfig(
        enabled=True,
        port=9090,
        namespace="my_app"
    )
)

@cache(config=config)
def cached_function():
    return expensive_computation()
```

**Available metrics:**
- `cache_hits_total`: Total cache hits
- `cache_misses_total`: Total cache misses
- `cache_latency_seconds`: Latency histogram
- `cache_serialization_seconds`: Serialization time
- `circuit_breaker_state`: Circuit breaker state

See [Prometheus Metrics](features/prometheus-metrics.md) for details.

## Performance Bottlenecks and Mitigations

### Bottleneck 1: The L2 Round Trip

**Problem:** a Redis L2 hit over loopback takes 0.41–0.42ms, 65–73 times an L1-only hit of the same 100-user dict (23.5KB as MessagePack). A Redis on another machine adds its network latency.

**Mitigations:**
- **Keep L1 on** (the default), so repeat reads skip the round trip
- **Tune L1 size:** raise `L1CacheConfig(max_size_mb=...)` or `CACHEKIT_L1_MAX_SIZE_MB` (default: 100MB per namespace) if entries are evicted early
- **Optimize L1 TTL:** Match L1 TTL to data freshness requirements
- **Reduce payload size:** cache only what you need; an L2 hit, and an L1 hit with a backend configured, deserialize the whole value

### Bottleneck 2: Decorator Overhead

**Problem:** every decorated call pays for key generation and the decorator's bookkeeping, 5.0–6.4μs per L1-only hit.

**Mitigations:**
- **This is acceptable** for any function slow enough to be worth caching
- **Batch queries:** one call that returns many items pays the overhead once

## Performance Regression Testing

Wall-clock benchmarks do not gate CI: on a shared machine one guard's run-level band reaches ±39% (see [Measured Figures](#measured-figures)). The wall-clock suites below are informational:

```bash
uv run pytest tests/performance/ -v -m performance
```

The regression gate is the instruction budget, run locally with `make perf-ir`.

## Instruction Budgets

`make perf-ir` counts the instructions each hot path executes per call and fails when a path costs **1% or more** above its committed budget. It warns from 0.2%. The orjson round trip fails at 2% and warns from 1% (see Limits). Instruction counts are deterministic where wall clock is not: two full runs agree within 0.08% per path (0.01% on CPython 3.12).

**Method** (`tests/performance/ir_budget.py`; the measured process is `ir_workload.py`):
- Each path runs under `valgrind --tool=callgrind --separate-threads=yes`, and only the main thread is counted. cachekit's background threads (log writer, L1 cleanup) vary by tens of percent between identical runs.
- Per-call cost is `(Ir[3000 calls] - Ir[1000 calls]) / 2000`, so interpreter startup (~2 billion instructions) and warmup cancel. Interpreter teardown is skipped.
- The measured process has a fixed environment (`PYTHONHASHSEED=0`, one BLAS/OpenMP thread, a one-day log flush and L1 cleanup interval, nothing inherited), seeded log sampling, and main-thread clocks that advance 1μs per read. Code that records its own duration otherwise executes more instructions when it runs slower. The cleanup sweep reads the real clock, so inside a run it would evict entries stamped with the pinned one.
- Nothing that runs on real time lands in the measured loop: no background thread wakes, cyclic GC is off, and the GIL switch interval is long enough that the main thread gives up the GIL only where the code releases it.
- Each path runs at five heap layouts (0 to 880 extra objects held before the workload) and its figure is the median. Any code change moves the layout, and the allocators take shorter or longer paths in some heap states: single layouts of the orjson round trip and the minimal L1 hit sat up to 2.4% from the rest. The median ignores one or two such layouts. The cheapest layout is not used, because a lucky one can sit 1% below the rest, and a budget recorded there makes every later run look like a regression.
- `ir_workload.py`'s text is compiled into every measured process, so editing it moves the layout the budgets were recorded at: re-record after any change to it. The gate itself (`ir_budget.py`) is kept out of the measured process, so editing it does not move a budget.
- The L2 paths use an in-process dict backend, so they cost instructions only: no sockets, retries or timeouts.
- The metrics collector starts synchronous and switches to batched mode (each call queues its record for a worker thread) when its 5 s check sees more than 100 records/s. Pinned clocks never reach that check, so the plain paths measure synchronous recording, and `l2_hit_async_metrics` measures the batched mode a busy long-lived process runs. Every metrics path records once per call through the same method, so one batched path covers that mode.

**Budgets** (instructions per call, `tests/performance/ir_baselines.json`):

| Path | What one call does | CPython 3.12 | CPython 3.14 |
|------|--------------------|-------------:|-------------:|
| `l1_hit` | `@cache(backend=None)` L1 hit | 61,218 | 62,690 |
| `minimal_l1_hit` | `@cache.minimal(backend=None)` L1 hit | 59,065 | 60,655 |
| `l2_hit` | `@cache`, L1 disabled, L2 hit | 313,831 | 315,952 |
| `miss` | `@cache`, L1 disabled, L2 miss, compute, L2 write | 313,907 | 311,730 |
| `secure_l1_hit` | `@cache.secure` L1 hit (decrypts the ciphertext L1 holds) | 236,193 | 237,396 |
| `l2_hit_async_metrics` | `l2_hit` with the metrics collector in batched mode | 262,890 | 266,031 |
| `serializer_default` | `StandardSerializer` round trip, small dict | 60,843 | 61,954 |
| `serializer_default_records` | `StandardSerializer` round trip, list of 100 six-field records (dict-heavy decode) | 1,441,598 | 1,442,386 |
| `serializer_auto` | `AutoSerializer` round trip | 69,441 | 70,650 |
| `serializer_orjson` | `OrjsonSerializer` round trip | 23,679 | 23,775 |
| `serializer_arrow` | `ArrowSerializer` round trip, 100-row DataFrame | 1,953,212 | 1,953,407 |
| `serializer_encrypted` | `EncryptionWrapper` encrypt + decrypt round trip | 114,998 | 118,412 |
| `file_set` | `FileBackend.set()` overwriting one key in a 1,000-entry cache (no eviction) | 100,723 | 86,297 |

Budgets are per interpreter (minor version, build flavour, machine); an interpreter without budgets fails with `no budget`. They were recorded on CPython 3.12.12 and 3.14.3, x86_64, glibc 2.39, with the release extension that `uv sync` builds. Counts depend on that whole build, so on a different interpreter, extension or C library, record a baseline on `main` first (`--update --allow-increase`) and compare your branch against it. Batched mode costs the caller about 50,000 fewer instructions per L2 hit than synchronous recording, because the Prometheus update moves to the worker thread.

**Sensitivity:** one extra BLAKE2b hash of the cache key per call raised every key-generating path by 4,700 to 4,900 instructions (`l1_hit` +6.1%, `l2_hit_async_metrics` +1.5%, `miss` +1.4%) and failed the gate, while the serializer paths, which generate no key, stayed within 0.11%. An interleaved wall-clock run agreed in sign (+286ns per L1 hit, median of 12 paired processes).

**Limits:** instruction counts do not weight cache misses or branch mispredictions. A claimed speed-up still needs an interleaved wall-clock comparison; the instruction count only guarantees the work did not grow. Paths that wait on a network backend are not covered. Cyclic-GC cost is outside the budgets; allocation and reference counting are inside. In batched mode the worker's Prometheus update runs on its own thread and is not budgeted. The orjson round trip's 1 KB output buffer comes from glibc malloc, whose path length depends on heap state that no layout sample pins, so its figure moved 1.3% between unrelated changes; it is gated at 2%. Unrelated changes can still move another path's figure by up to 0.6% (a `WARN`), and a `LOWER` verdict on a path the change did not touch is a layout shift, not a saving: ratchet only the paths the change touched (`--update --path <path>`).

```bash
make perf-ir         # gate: fail on a >=1% per-call regression (orjson 2%; needs valgrind; 130 runs, several minutes)
make perf-ir-update  # ratchet: write lower measured figures back as budgets, never higher
```

The gate runs at most 8 callgrind processes at a time (fewer on a smaller machine), and each path peaks at 0.5 to 1.0 GiB per run, so it can share a machine with other work. `--jobs N` changes that. Callgrind runs still need a bound: a workload that grows under callgrind can use many GiB, and a FileBackend `set()` workload passed 9 GiB in under four minutes. The gate kills a run that takes longer than `--child-timeout` minutes (default 15) and fails, and it kills every live run when it exits, fails, or gets SIGINT or SIGTERM. On Linux with systemd, also cap the whole gate's memory and run time:

```bash
systemd-run --user --scope -p MemoryMax=10G -p MemorySwapMax=0 -p RuntimeMaxSec=90min -- \
    uv run python tests/performance/ir_budget.py
```

The gate does not call `systemd-run` itself, because CI runners and macOS lack it. A deliberate cost increase (a new feature on the hot path) is recorded with `uv run python tests/performance/ir_budget.py --update --allow-increase`, and the PR states why.

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
