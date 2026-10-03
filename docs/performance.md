**[Home](README.md)** › **Architecture** › **Performance**

# Performance Guide

> **Sub-millisecond cache operations, measured and benchmarked**

---

## Key Numbers

> [!TIP]
> **Key numbers** (run median of 5 runs, two passes; CPython 3.14.3, x86_64 Linux, 2026-10-03; indicative wall clock on a shared host):
> - **Decorator + L1 hit, `@cache(backend=None)`, 10KB dict**: 5.6–6.4μs (61k instructions per call, see [Instruction Budgets](#instruction-budgets))
> - **Same hit, 10K-row DataFrame**: 5.1–5.4μs. An L1-only hit never serializes, so payload size barely matters
> - **Raw L1 byte-cache lookup** (`L1Cache.get`, no decorator): 354–362ns
> - **Redis L2 hit**, L1 disabled, 10KB dict, Redis on loopback: 0.41–0.42ms

## Measurement Methodology

Every figure on this page comes from a guard in `tests/performance/`:
- **Timer**: `time.perf_counter_ns()` around each call
- **Runs, not samples**: each guard takes 5 independent runs, with a forced garbage collection before each. The figure is the mean of the per-run medians with its 95% t band at df = 4 (`stats_utils.summarize`)
- **Every raw sample kept**: percentiles cover all samples; outliers are counted, never filtered
- **Warm-up**: each run warms up for at least 5,000 calls (2,000 for the raw L1 guard), stopping once the last 1,000 calls vary by less than 10%, and at most twice that minimum
- **Pre-flight**: each session prints whether the host is throttled or loaded (`measurement_env.py`)
- **No tail claims at 5 runs**: the harness prints a p95 only from at least 10 runs and 400 samples (a p99 from 10 runs and 2,000 samples). Every guard below runs 5, so its `P95` and `P99` lines read "inconclusive" and this page quotes no tail percentile

## Measured Figures

Two back-to-back passes of the same guards, on 2026-10-03, CPython 3.14.3, x86_64 Linux. The pre-flight reported no throttling or load, but the host is shared, so the figures are indicative: compare them with each other rather than with your machine.

| Path | Guard (`tests/performance/`) | Pass 1 | Pass 2 |
|------|------------------------------|-------:|-------:|
| Decorator + L1 hit, `@cache(backend=None)`, 10KB dict | `test_production_realism.py::test_decorator_overhead_complex_dict` | 6.39 ± 2.03μs | 5.55 ± 0.83μs |
| Decorator + L1 hit, `@cache(backend=None)`, `User` dataclass | `test_production_realism.py::test_decorator_overhead_dataclass` | 6.27 ± 2.10μs | 4.98 ± 0.07μs |
| Decorator + L1 hit, `@cache(backend=None, serializer="auto")`, 10K-row DataFrame | `test_production_realism.py::test_decorator_overhead_dataframe` | 5.38 ± 0.15μs | 5.08 ± 0.17μs |
| Raw L1 byte-cache lookup, `L1Cache.get` of 1KB, no decorator | `test_statistical_rigor.py::test_l1_cache_hit_statistically_rigorous` | 354 ± 68ns | 362 ± 41ns |
| Redis L2 hit, L1 disabled, 10KB dict, Redis on loopback | `test_production_realism.py::test_redis_l2_roundtrip` | 416 ± 139μs | 407 ± 159μs |

The bands run from ±1% to ±39% of their figure, and pass 2 sits between 2% above and 21% below pass 1. That spread is why these suites inform rather than gate (see [Performance Regression Testing](#performance-regression-testing)).

Run them yourself:
```bash
uv run pytest tests/performance/test_production_realism.py -v -s \
  -k "complex_dict or dataclass or dataframe or redis_l2"
uv run pytest tests/performance/test_statistical_rigor.py::test_l1_cache_hit_statistically_rigorous -v -s
# The Redis guard skips unless Redis answers at REDIS_URL (default redis://localhost:6379)
```

## Why an L1-Only Hit Costs the Same for Any Payload

With `backend=None`, cachekit keeps the returned object itself in memory and hands that same object back on a hit (see [backend=None](backends/none.md)). Nothing is serialized or deserialized, so a 10KB dict, a dataclass and a 10K-row DataFrame all cost about the same: key generation, the lookup and the decorator's bookkeeping.

With an L2 backend configured, L1 holds the serialized bytes instead, and a hit deserializes them, so that hit costs more as the payload grows. No guard measures it through the run-level harness yet, so this page quotes no figure for it.

### Decorator + L1 Hit (Hot Path)

An L1-only hit takes 5.0–6.4μs across the three payloads above.

The deterministic figure is **61,244 instructions per call** on CPython 3.12 (62,415 on 3.14), from the [instruction budget](#instruction-budgets). Key generation, the L1 lookup, the `cache_info()` hit counter and the decorator's own bookkeeping are all inside that count; this L1-only path records no Prometheus metric.

The raw L1 byte-cache lookup, without the decorator, takes 354–362ns. An L1-only hit uses a separate object cache, whose lookup no guard times on its own.

## L2 Backend (Redis) Performance

An L2 hit with L1 disabled, a 10KB dict and Redis on the same machine takes 0.41–0.42ms: the decorator, the round trip over loopback and deserialization. That is 65–73 times an L1-only hit of the same dict (pass by pass). A Redis on another machine adds its network latency on top; no guard measures that.

## Not Measured Here

These have no figure from the run-level harness, so this page quotes none:
- **An L1 hit with an L2 backend configured**, which deserializes the stored bytes
- **Concurrent access**: the 10-thread guard (`test_concurrent_cache_access`) collects every thread's samples into one list and computes its tail outside the run-level harness
- **Encryption overhead**: `test_encryption_overhead` prints only means and raw p95s. The deterministic costs are the `secure_l1_hit` and `serializer_encrypted` rows of [Instruction Budgets](#instruction-budgets); see [Zero-Knowledge Encryption](features/zero-knowledge-encryption.md) for how encryption works
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

**Problem:** a Redis L2 hit over loopback takes 0.41–0.42ms, 65–73 times an L1-only hit of the same 10KB dict. A Redis on another machine adds its network latency.

**Mitigations:**
- **Keep L1 on** (the default), so repeat reads skip the round trip
- **Tune L1 size:** raise `L1CacheConfig(max_size_mb=...)` or `CACHEKIT_L1_MAX_SIZE_MB` (default: 100MB per namespace) if entries are evicted early
- **Optimize L1 TTL:** Match L1 TTL to data freshness requirements
- **Reduce payload size:** cache only what you need; the L2 path deserializes the whole value

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
| `l1_hit` | `@cache(backend=None)` L1 hit | 61,244 | 62,415 |
| `minimal_l1_hit` | `@cache.minimal(backend=None)` L1 hit | 59,292 | 60,482 |
| `l2_hit` | `@cache`, L1 disabled, L2 hit | 346,876 | 348,273 |
| `miss` | `@cache`, L1 disabled, L2 miss, compute, L2 write | 332,060 | 332,697 |
| `secure_l1_hit` | `@cache.secure` L1 hit (decrypts the ciphertext L1 holds) | 281,500 | 282,257 |
| `l2_hit_async_metrics` | `l2_hit` with the metrics collector in batched mode | 290,734 | 293,554 |
| `serializer_default` | `StandardSerializer` round trip, small dict | 61,356 | 62,663 |
| `serializer_auto` | `AutoSerializer` round trip | 110,227 | 113,600 |
| `serializer_orjson` | `OrjsonSerializer` round trip | 23,792 | 23,833 |
| `serializer_arrow` | `ArrowSerializer` round trip, 100-row DataFrame | 1,946,487 | 1,955,979 |
| `serializer_encrypted` | `EncryptionWrapper` encrypt + decrypt round trip | 115,652 | 117,851 |

Budgets are per interpreter (minor version, build flavour, machine); an interpreter without budgets fails with `no budget`. They were recorded on CPython 3.12.12 and 3.14.3, x86_64, glibc 2.39, with the release extension that `uv sync` builds. Counts depend on that whole build, so on a different interpreter, extension or C library, record a baseline on `main` first (`--update --allow-increase`) and compare your branch against it. Batched mode costs the caller about 55,000 fewer instructions per L2 hit than synchronous recording, because the Prometheus update moves to the worker thread.

**Sensitivity:** one extra BLAKE2b hash of the cache key per call raised every key-generating path by 4,700 to 4,900 instructions (`l1_hit` +6.1%, `l2_hit_async_metrics` +1.5%, `miss` +1.4%) and failed the gate, while the serializer paths, which generate no key, stayed within 0.11%. An interleaved wall-clock run agreed in sign (+286ns per L1 hit, median of 12 paired processes).

**Limits:** instruction counts do not weight cache misses or branch mispredictions. A claimed speed-up still needs an interleaved wall-clock comparison; the instruction count only guarantees the work did not grow. Paths that wait on a network backend are not covered. Cyclic-GC cost is outside the budgets; allocation and reference counting are inside. In batched mode the worker's Prometheus update runs on its own thread and is not budgeted. The orjson round trip's 1 KB output buffer comes from glibc malloc, whose path length depends on heap state that no layout sample pins, so its figure moved 1.3% between unrelated changes; it is gated at 2%. Unrelated changes can still move another path's figure by up to 0.6% (a `WARN`), and a `LOWER` verdict on a path the change did not touch is a layout shift, not a saving: ratchet only the paths the change touched (`--update --path <path>`).

```bash
make perf-ir         # gate: fail on a >=1% per-call regression (orjson 2%; needs valgrind; 110 runs, several minutes)
make perf-ir-update  # ratchet: write lower measured figures back as budgets, never higher
```

The gate runs at most 8 callgrind processes at a time (fewer on a smaller machine), and each holds about half a gigabyte, so it can share a machine with other work. `--jobs N` changes that. A deliberate cost increase (a new feature on the hot path) is recorded with `uv run python tests/performance/ir_budget.py --update --allow-increase`, and the PR states why.

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
