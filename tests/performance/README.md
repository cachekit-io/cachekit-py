# Performance Test Suite

**Purpose**: Measure cachekit performance under realistic production workloads.

**Philosophy**: Test what users ACTUALLY experience, not idealized microbenchmarks.

---

## Test Organization

### 1. `test_production_realism.py` - **Comprehensive Production Tests**

Tests the full stack with realistic payloads and workloads:

| Test | Payload | What It Measures | Target (P95) | Status |
|------|---------|------------------|--------------|--------|
| `test_decorator_overhead_complex_dict` | 10KB nested dict | Full decorator stack | < 300μs | ✅ 250μs |
| `test_decorator_overhead_dataclass` | User model | Dataclass serialization | < 200μs | ✅ 100μs |
| `test_decorator_overhead_dataframe` | 10K row DataFrame | DataFrame with msgpack | < 5ms | ⏳ |
| `test_concurrent_cache_access` | 10KB dict, 10 threads | Lock contention | < 500μs | ✅ 258μs |
| `test_encryption_overhead` | 10KB dict + AES-256-GCM | Encryption cost | < 3x plain | ⏳ |
| `test_redis_l2_roundtrip` | 10KB dict + Redis | Network + serialize | < 10ms | ⏳ |
| `test_async_decorator_overhead` | 10KB dict + async | Async machinery | < 50μs | ⏳ |

**Key Insight**: Decorator + serialization overhead is **500x** larger than raw cache lookup.

### 2. `test_reliability_under_load.py` - **Reliability Framework Tests**

Tests that reliability features work under production stress:

| Test | Feature Tested | What It Validates | Status |
|------|----------------|-------------------|--------|
| `test_circuit_breaker_failure_injection` | Circuit breaker | Opens after failures, fast-fail behavior | ⏳ |
| `test_backpressure_limit` | Backpressure | Queue limits, timeout enforcement | ⏳ |
| `test_adaptive_timeout_behavior` | Adaptive timeout | Timeout adaptation, performance impact | ⏳ |
| `test_full_stack_under_load` | All features | Production config performance | ⏳ |

**Key Insight**: Reliability features add overhead but protect against failures.

### 3. `test_l1_cache_latency.py` - **L1 Cache Microbenchmarks**

Tests raw L1 cache performance (dict operations only):

- L1 hit: ~500ns p95 (raw dict lookup)
- L1 miss: ~200ns p95 (key not found)
- L1 put: ~2μs p95 (insert + LRU)

**WARNING**: These numbers are NOT what users experience. Full decorator stack is 500x slower.

### 4. `test_end_to_end_latency.py` - **Decorator Path Analysis**

Tests decorator overhead in isolation:

- Decorator + L1 hit (int payload): ~36μs p95
- Async decorator: ~17μs p95

**WARNING**: Uses trivial payloads (int). Real payloads are 10-100x slower.

### 5. `test_serializer_benchmarks.py` - **Serialization Performance**

Compares serializers for DataFrames:

- ArrowSerializer: ~50-100x faster than msgpack
- Memory-mapped deserialization (Arrow)
- Serialized size comparison

**Key Insight**: For DataFrames, ArrowSerializer is critical.

### 6. `test_backend_wrapper_overhead.py` - **Per-Request Pattern**

Tests per-request wrapper creation overhead:

- Wrapper creation: <1μs p95
- ContextVar access: <200ns p95
- URL encoding: <600ns p95

**Key Insight**: Per-request pattern adds negligible overhead (<0.15% of network latency).

### 7. `stats_utils.py` - **Statistical Utilities**

Provides rigorous performance measurement tools:

- `benchmark_with_gc_handling()`: Multiple runs + GC filtering
- `confidence_interval_95()`: Statistical confidence
- `detect_gc_pauses()`: Outlier filtering
- `measure_with_jit_warmup()`: Variance-based warmup

**Key Insight**: Statistical rigor prevents misleading results.

---

## Running Tests

### Quick Check (Basic Tests)
```bash
make test-performance-quick
```

### Full Suite (All Tests)
```bash
uv run pytest tests/performance/ -v -s -m performance
```

### Production-Realistic Only
```bash
uv run pytest tests/performance/test_production_realism.py -v -s
```

### Reliability Tests (Requires Redis)
```bash
export REDIS_URL=redis://localhost:6379
uv run pytest tests/performance/test_reliability_under_load.py -v -s -m integration
```

### Specific Test
```bash
uv run pytest tests/performance/test_production_realism.py::test_decorator_overhead_complex_dict -v -s
```

---

## Key Findings

### What Users Actually Experience (P95)

| Scenario | Latency | Context |
|----------|---------|---------|
| **Complex dict (10KB)** | **250μs** | Full decorator stack + msgpack |
| **User dataclass** | **100μs** | Smaller payload |
| **Concurrent (10 threads)** | **258μs** | Minimal lock contention (+8μs) |
| **Redis L2 (local)** | **2-5ms** | Network RTT dominates |

### What Microbenchmarks Show (MISLEADING)

| Scenario | Latency | Why Misleading |
|----------|---------|----------------|
| **L1 cache hit** | 500ns | Ignores decorator + serialization |
| **L1 cache miss** | 200ns | No serialization, trivial payload |
| **Decorator + int** | 36μs | Trivial payload, no serialization |

**Reality Check**: Full stack with realistic payloads is **500x slower** than raw cache microbenchmarks.

---

## Marketing Guidelines

### ✅ Conservative Claims (Validated)

1. **"200-300μs for realistic payloads"**
   - Measured: 250μs p95 for 10KB dicts
   - Context: Full decorator stack

2. **"Eliminates 90% of network latency"**
   - L1 hit: 0.25ms
   - Redis RTT: 2-5ms
   - Savings: ~2-4.75ms

3. **"Concurrent-safe with minimal overhead"**
   - Lock contention: Only +8μs (3% overhead)

4. **"ArrowSerializer: 50-100x faster for DataFrames"**
   - Validated in test_serializer_benchmarks.py

### ❌ Avoid These Claims (Misleading)

1. **"Sub-microsecond cache hits"**
   - Only true for raw dict lookup, not user experience

2. **"500ns latency"**
   - Ignores decorator + serialization overhead

3. **"Zero overhead"**
   - Decorator + serialization + reliability features have cost

---

## Performance Budget

| Component | Budget | Measured | Status |
|-----------|--------|----------|--------|
| Decorator machinery | 20μs | ~20μs | ✅ |
| Key generation | 5μs | ~2μs | ✅ |
| Serialization (10KB) | 150μs | ~100μs | ✅ |
| L1 cache lookup | 1μs | 0.5μs | ✅ |
| Deserialization (10KB) | 150μs | ~100μs | ✅ |
| **Total** | **<350μs** | **~250μs** | ✅ |

**Result**: Under budget by 100μs (28% margin).

---

## Next Steps

1. ✅ Implement comprehensive test suite
2. ⏳ Measure encryption overhead
3. ⏳ Test Redis L2 roundtrip
4. ⏳ Exercise circuit breaker under failures
5. ⏳ Test backpressure limits
6. ⏳ Update PROFILING_RESULTS.md
7. ⏳ Update marketing materials

---

## References

- [PRODUCTION_FINDINGS.md](PRODUCTION_FINDINGS.md) - Detailed performance analysis
- [stats_utils.py](stats_utils.py) - Statistical measurement tools
- [test_production_realism.py](test_production_realism.py) - Production-realistic tests
- [test_reliability_under_load.py](test_reliability_under_load.py) - Reliability framework tests
