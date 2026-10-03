**[Home](README.md)** › **Architecture** › **Performance**

# Performance Guide

> **Sub-millisecond cache operations, measured and benchmarked**

---

## Key Numbers

> [!WARNING]
> Most microsecond figures on this page predate the current stack and have not been re-measured.
> On 2026-10-03 the guards in `tests/performance/test_production_realism.py` measured the decorator
> + L1 hit on the 10KB dict at a raw p95 of 14–16μs, not 242μs, and the 10-thread case at 13μs.
> Treat the other figures as stale until they are re-run. [Instruction Budgets](#instruction-budgets)
> is current.

> [!TIP]
> **Key numbers (p95 latency):**
> - **L1 cache hit**: 500ns (pure dict lookup)
> - **Decorator + L1 hit**: ~5.6μs median, CPython 3.12 (indicative wall clock; 78k instructions per call, see [Instruction Budgets](#instruction-budgets))
> - **Complex payload (10KB dict)**: 242μs with serialization
> - **DataFrame (10K rows, Arrow)**: 800μs total roundtrip
> - **Concurrent access (10 threads)**: 231μs (minimal contention)
> - **Encryption overhead**: 1.03x (only 3% slower)

## Measurement Methodology

All benchmarks use:
- **time.perf_counter_ns()**: Nanosecond-precision performance counter
- **Statistical rigor**: 5 independent runs; the estimate is a 95% t-interval over the per-run medians
- **Every raw sample kept**: percentiles cover all samples; outliers are counted, never filtered
- **Warmup**: 1,000 iterations before measurement
- **Realistic payloads**: 10KB dicts, 10K row DataFrames, custom dataclasses
- **Production configuration**: All reliability features enabled (circuit breaker, backpressure, timeouts)

Run benchmarks yourself:
```bash
# Component-level profiling
uv run pytest tests/performance/test_cache_profiler.py -v -s

# End-to-end decorator overhead
uv run pytest tests/performance/test_end_to_end_latency.py -v -s

# Production-realistic scenarios
uv run pytest tests/performance/test_production_realism.py -v -s

# Serializer comparison
uv run pytest tests/performance/test_serializer_benchmarks.py -v -s
```

## End-to-End Latency Breakdown

### 10KB Complex Dict (Typical API Response)

**Total p95: 242μs** (241,708ns)

Component breakdown:
- **Serialization (msgpack)**: 100μs (41%)
- **Deserialization**: 100μs (41%)
- **Decorator overhead**: 20μs (8%)
- **Key generation**: 2μs (1%)
- **L1 cache lookup**: 0.5μs (0.2%)
- **Other (Python interpreter)**: 20μs (8%)

**Validated with 95% CI:** [208.5μs, 208.8μs] across 5 runs, 49,548 samples

### User Dataclass (Smaller Payload)

**Total p95: 122μs** (121,546ns)

Faster due to smaller serialization overhead. Same component ratios.

### DataFrame (10K Rows)

**With ArrowSerializer:**
- **Serialize**: 0.48ms
- **Deserialize**: 0.32ms
- **Total roundtrip**: 0.80ms
- **Decorator overhead**: ~20μs
- **Grand total**: ~820μs

**With MessagePack (default):**
- **Serialize**: 1.64ms
- **Deserialize**: 2.32ms
- **Total roundtrip**: 3.96ms
- **Speedup**: **5.0x slower** than Arrow

> [!IMPORTANT]
> Use ArrowSerializer for DataFrames with 10K+ rows (see [Serializer Guide](serializers/README.md)).

## L1 Cache Component Profiling

Pure L1 cache performance (no decorator, direct cache.get() calls):

**Total p95: 458ns**

Component breakdown:
- **Lock acquisition (RLock)**: 250ns (54.6%)
- **TTL check (time.time())**: 208ns (45.4%)
- **Dict lookup**: 125ns (27.3%)
- **LRU move (OrderedDict)**: 125ns (27.3%)
- **Counter increment**: 125ns (27.3%)

> [!NOTE]
> Lock acquisition dominates L1 latency, but it's necessary for thread safety. The 458ns is the practical limit for a thread-safe Python cache.

**Scaling characteristics:**
- Dict lookup is **O(1)**: 125ns for 1 entry, 125ns for 10,000 entries
- Cache size has **zero impact** on lookup speed
- Lock contention remains minimal up to 4 concurrent threads (500ns p95)

## Decorator Overhead Analysis

### Isolated Decorator (No Caching)

**Mean: 110μs, p95: 160μs**

This measures the decorator machinery alone:
- Argument binding and inspection
- Context extraction (thread/async detection)
- Function invocation
- Key generation

### Decorator + L1 Hit (Hot Path)

**About 5.6μs per call** (median of 12 processes, CPython 3.12, `@cache(backend=None)` returning a small dict; indicative wall clock on a shared host).

The deterministic figure is **78,368 instructions per call** on CPython 3.12 (81,212 on 3.14), from the [instruction budget](#instruction-budgets). Key generation, the L1 lookup, the `cache_info()` hit counter and the decorator's own bookkeeping are all inside that count; this L1-only path records no Prometheus metric.

**About 11x the raw L1 lookup:** the decorator stack adds ~5μs on top of the sub-microsecond dict lookup, still **several hundred times faster** than a Redis round trip (2-7ms).

## Concurrent Access Performance

**Workload:** 10 threads hammering the same cache key (worst-case contention)

**Results:**
- **Single-threaded**: 242μs p95
- **10 threads**: 231μs p95
- **Degradation**: Essentially none (within measurement noise)

**Key takeaway:** RLock contention is **not a bottleneck** for realistic concurrency levels. The L1 cache is designed for high-throughput, multi-threaded applications.

## Encryption Overhead

**Without encryption:** 210μs mean, 228μs p95
**With AES-256-GCM:** 215μs mean, 236μs p95

**Overhead:** 1.03x (only **3% slower**)

**Why so low?**
- Encryption happens in Rust (PyO3 FFI)
- AES-NI hardware acceleration on modern CPUs
- Zero-copy memory handling

**Security benefit:**
- Client-side encryption (no plaintext PII in cache)
- L1 stores encrypted bytes only

See [Zero-Knowledge Encryption](features/zero-knowledge-encryption.md) for details.

## Serializer Performance Comparison

### DataFrame Serialization (10K rows)

| Serializer | Serialize | Deserialize | Total | Speedup |
|------------|-----------|-------------|-------|---------|
| **Arrow** | 0.48ms | 0.32ms | 0.80ms | **Baseline** |
| **MessagePack** | 1.64ms | 2.32ms | 3.96ms | 5.0x slower |

### DataFrame Serialization (100K rows)

| Serializer | Serialize | Deserialize | Total | Speedup |
|------------|-----------|-------------|-------|---------|
| **Arrow** | 2.93ms | 1.13ms | 4.06ms | **Baseline** |
| **MessagePack** | 16.42ms | 22.62ms | 39.04ms | 9.6x slower |

**Arrow advantages:**
- **Zero-copy deserialization**: Memory-mapped, no full data copy
- **Columnar format**: Efficient for numeric/datetime columns
- **20x faster deserialization** for large DataFrames

**MessagePack advantages:**
- **Broad type support**: Handles all Python objects (dicts, lists, custom classes)
- **Lower overhead for small data**: Faster than Arrow for <1K rows
- **Integrated compression**: LZ4 + xxHash3-64 checksums (Rust layer)

See [Serializer Guide](serializers/README.md) for decision matrix.

## L2 Backend (Redis) Performance

**Local Redis (localhost):**
- **Network RTT**: 1-2ms
- **Total L2 hit latency**: 2-5ms (network + deserialization)

**Remote Redis (same datacenter):**
- **Network RTT**: 5-15ms
- **Total L2 hit latency**: 10-30ms

**L1 cache value proposition:**
- L1 hit: **242μs** (0.242ms)
- L2 hit: **2-5ms** (local Redis)
- **Speedup**: **8-20x faster** with L1 cache

## Async Decorator Performance

**Async decorator + L1 hit:** 192μs mean, 201μs p95

**Compared to sync:** ~6x faster than sync decorator (which showed 32μs mean in other tests, but this is likely due to measurement differences)

**Why async is competitive:**
- Same L1 cache path (no await needed for memory lookups)
- Async overhead is minimal for cache hits
- Async benefits show up during cache misses (non-blocking I/O to Redis)

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
- 8-20x faster than Redis
- Sub-millisecond latency
- No network overhead

### 2. Choose the Right Serializer

**For DataFrames (10K+ rows):**
```python
from cachekit.serializers import ArrowSerializer

@cache(serializer=ArrowSerializer())
def get_large_dataset(date: str):
    return load_dataframe(date)  # 5-10x faster serialization
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
    data = get_user_data(user_id)  # 100 cache hits = 24ms total
```

**Good (one large cache hit):**
```python notest
@cache
def get_users_batch(user_ids: list[int]):
    return [fetch_user_data(uid) for uid in user_ids]

data = get_users_batch(user_ids)  # 1 cache hit = 242μs
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

### Bottleneck 1: Serialization Dominates Latency (82%)

**Problem:** MessagePack serialization takes 100μs for a 10KB dict, which is 200x slower than the L1 lookup (500ns).

**Mitigations:**
- **Reduce payload size:** Cache only what you need
- **Use Arrow for DataFrames:** 5-10x faster serialization
- **Enable compression:** Already enabled by default (Rust layer)

**Reality check:** Even with serialization overhead, 242μs is still 8-20x faster than Redis.

### Bottleneck 2: Network Latency (L2 Cache)

**Problem:** Redis L2 hit adds 2-5ms network RTT.

**Mitigations:**
- **L1 cache already handles this:** 90%+ of cache hits should come from L1
- **Tune L1 size:** Increase `max_memory_mb` if needed (default: 100MB)
- **Optimize L1 TTL:** Match L1 TTL to data freshness requirements

### Bottleneck 3: Decorator Overhead (~5μs)

**Problem:** Decorator machinery adds ~5μs on top of the L1 lookup.

**Mitigations:**
- **This is acceptable:** ~5μs is negligible compared to function execution time
- **For ultra-low-latency:** Use direct `StandardCacheHandler` API (bypasses decorator)
- **Batch queries:** Amortize decorator overhead across multiple items

**Example (advanced):**
```python notest
from cachekit.cache_handler import StandardCacheHandler

handler = StandardCacheHandler(backend=redis_backend)

# Direct cache access (no decorator overhead)
found, value = handler.get("my_key")
if not found:
    value = expensive_function()
    handler.put("my_key", value, ttl=3600)
```

### Bottleneck 4: Lock Contention (High Concurrency)

**Problem:** RLock acquisition takes 250ns, which can become a bottleneck at 100+ threads.

**Mitigations:**
- **Most apps don't hit this:** Lock contention is minimal up to 10-20 threads
- **Shard your cache:** Use multiple L1Cache instances with consistent hashing
- **Use async:** Async decorator avoids blocking on locks

**Reality check:** Lock overhead (250ns) is 0.1% of total latency (242μs). Not worth optimizing unless you have extreme concurrency (100+ threads).

## Performance Regression Testing

Wall-clock benchmarks do not gate CI: on a shared machine their run-to-run noise is several percent. The wall-clock suites below are informational:

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
| `l1_hit` | `@cache(backend=None)` L1 hit | 78,368 | 81,212 |
| `minimal_l1_hit` | `@cache.minimal(backend=None)` L1 hit | 76,335 | 79,325 |
| `l2_hit` | `@cache`, L1 disabled, L2 hit | 362,785 | 366,419 |
| `miss` | `@cache`, L1 disabled, L2 miss, compute, L2 write | 349,201 | 351,292 |
| `secure_l1_hit` | `@cache.secure` L1 hit (decrypts the ciphertext L1 holds) | 296,646 | 300,581 |
| `l2_hit_async_metrics` | `l2_hit` with the metrics collector in batched mode | 306,710 | 311,742 |
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

## Real-World Performance Context

**Typical use case:** API response caching

**Without cachekit:**
- Database query: 50-200ms
- Redis cache hit: 2-5ms
- **Best case**: 2ms (Redis hit)

**With cachekit:**
- L1 cache hit: **242μs** (0.242ms)
- L2 cache hit: 2-5ms (Redis)
- Cache miss: 50-200ms (database)

**With 90% L1 hit rate:**
- Average latency: 0.9 × 0.242ms + 0.1 × 2ms = **0.42ms**
- **Speedup**: 4.8x faster than Redis-only caching

**Network latency dominates real-world performance.** Even a "slow" 1ms cache operation is fast when you consider:
- Typical API response time: 100-500ms
- Database query: 50-200ms
- External API call: 200-1000ms

## Appendix: Raw Benchmark Data

**L1 Cache Component Breakdown:**
```
Lock acquisition:        250ns p95
Dict lookup:            125ns p95
TTL check:              208ns p95
LRU move:               125ns p95
Counter increment:      125ns p95
-----------------------------------
Total (measured):       458ns p95
```

**Decorator Overhead (10KB dict):**
```
Serialization:          100μs (41%)
Deserialization:        100μs (41%)
Decorator machinery:     20μs (8%)
Key generation:           2μs (1%)
L1 lookup:              0.5μs (0.2%)
Other (interpreter):     20μs (8%)
-----------------------------------
Total (measured):       242μs p95
95% CI:                 [208.5, 208.8]μs
```

**Concurrent Access (10 threads):**
```
Total operations:       10,000
Mean:                   844μs
Median:                 210μs
P95:                    231μs
P99:                    284μs
```

**Arrow vs MessagePack (10K rows):**
```
Arrow serialize:        0.48ms
Arrow deserialize:      0.32ms
MessagePack serialize:  1.64ms
MessagePack deserialize: 2.32ms

Serialization speedup:  3.4x
Deserialization speedup: 7.1x
Total speedup:          5.0x
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
