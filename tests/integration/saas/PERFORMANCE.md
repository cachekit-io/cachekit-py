# Performance Statistics Tracking

The SaaS test suite automatically tracks and reports performance statistics for all cache operations.

## Features

- **Automatic tracking**: All HTTP operations (GET, SET, DELETE, LIST) are timed automatically
- **Statistical rigor**: Reports mean, median, P95, P99, min, max, and standard deviation
- **End-of-session summary**: Performance stats printed at the end of test runs

## Output Example

```
================================================================================
PERFORMANCE STATISTICS
================================================================================
HTTP_DELETE:
  Count:         42
  Mean:        125.34ms
  Median:      118.56ms
  P95:         187.23ms
  P99:         203.45ms
  Min:          95.12ms
  Max:         215.67ms
  StdDev:       28.91ms

HTTP_GET:
  Count:        156
  Mean:        102.78ms
  Median:       98.23ms
  P95:         142.56ms
  P99:         158.91ms
  Min:          85.45ms
  Max:         172.34ms
  StdDev:       18.42ms

HTTP_SET:
  Count:        143
  Mean:        135.67ms
  Median:      130.12ms
  P95:         178.34ms
  P99:         195.67ms
  Min:         110.23ms
  Max:         208.91ms
  StdDev:       22.15ms

================================================================================
OVERALL
================================================================================
  Total operations:      341
  Mean latency:      118.26ms
  Median latency:    115.34ms
  P95 latency:       167.89ms
  P99 latency:       189.23ms
================================================================================
```

## How It Works

### Automatic Tracking

The `CacheClient` class in `test_cache_integrity.py` automatically wraps all operations with performance tracking:

```python
def get(self, namespace: str, key: str) -> requests.Response:
    """GET a value from cache"""
    url = f"{self.base_url}/cache/get/{namespace}/{key}"
    with global_tracker.timed_operation("HTTP_GET"):
        return self.session.get(url)
```

### Manual Tracking in Tests

You can also track custom operations in your tests using the `perf_tracker` fixture:

```python
def test_custom_operation(perf_tracker):
    """Test with custom performance tracking."""
    with perf_tracker.timed_operation("CUSTOM_OP"):
        # Your operation here
        result = do_something()
```

## Performance Targets

Based on the cachekit library's performance suite, we target:

| Operation | Target (P95) | Context |
|-----------|--------------|---------|
| HTTP GET (cache hit) | < 150ms | Network RTT to dev.cachekit.io |
| HTTP SET | < 200ms | Network + serialization |
| HTTP DELETE | < 150ms | Network RTT |
| HTTP LIST | < 250ms | Network + pagination |

**Note**: These targets are for **network operations** to the SaaS backend, which are 50-100x slower than local L1 cache hits (~0.25ms).

## Interpreting Results

### Good Performance Indicators

- **Low P95/P99 spread**: Indicates consistent latency (< 20% difference)
- **Low StdDev**: < 20% of mean suggests stable performance
- **P95 under target**: Meets service level objectives

### Performance Issues

- **High P99 spikes**: May indicate:
  - Network congestion
  - Cold start (Cloudflare Workers)
  - Backend overload
  - Rate limiting

- **High StdDev**: May indicate:
  - Inconsistent network conditions
  - Variable backend load
  - Measurement noise (run more samples)

## Comparing Local vs Dev vs Prod

The test suite shows which environment is being tested:

```
════════════════════════════════════════════════════════════════
🔥 SMOKE TEST
════════════════════════════════════════════════════════════════
📦 Package source: Local editable install (../../../cachekit)
🌐 Target API:     https://api.dev.cachekit.io
🔑 API key:        ck_sdk_DubdzW2...
════════════════════════════════════════════════════════════════
```

**Expected latency differences**:
- **Local** (`http://localhost:8787`): 1-5ms (loopback, no TLS)
- **Dev** (`https://api.dev.cachekit.io`): 50-150ms (internet, Cloudflare edge)
- **Prod** (`https://api.cachekit.io`): 50-150ms (similar to dev, may vary by region)

## Statistical Significance

Performance measurements use techniques from `cachekit/tests/performance/stats_utils.py`:

- **Multiple samples**: Each operation measured individually
- **Percentiles**: P95 and P99 show tail latency (important for SLAs)
- **Standard deviation**: Measures consistency

## Related

- **Library performance**: See `cachekit/tests/performance/README.md`
- **Load testing**: See `saas/tests/locust/` for sustained load tests
- **Benchmarking**: Run `make smoke` or `make full-test` to see stats
