**[Home](../README.md)** › **Features** › **Prometheus Metrics**

# Prometheus Metrics - Observability Built-In

**Version**: cachekit v1.0+

## TL;DR

cachekit exports Prometheus metrics automatically. No instrumentation code needed. Cache hits/misses/errors tracked per function.

```prometheus
cachekit_cache_hits_total{function="get_user"} 9847
cachekit_cache_misses_total{function="get_user"} 203
cachekit_cache_errors_total{function="get_user"} 5
```

---

## Quick Start

Metrics exported automatically:

```python
from cachekit import cache

@cache(ttl=300)  # Metrics exported by default
def get_user(user_id):
    return db.query(User).get(user_id)

# Prometheus scrape endpoint picks up metrics automatically
# No configuration needed
```

**Scrape configuration** (prometheus.yml):
```yaml
scrape_configs:
  - job_name: 'cachekit'
    static_configs:
      - targets: ['localhost:8000']  # Your app port
    metrics_path: '/metrics'  # Standard Prometheus endpoint
```

---

## Available Metrics

### Counter Metrics (always increasing)
```prometheus
# Cache hit count
cachekit_cache_hits_total{function="get_user"}

# Cache miss count
cachekit_cache_misses_total{function="get_user"}

# Error count (Redis, etc)
cachekit_cache_errors_total{function="get_user"}

# Circuit breaker events
cachekit_circuit_breaker_opens_total{function="get_user"}
cachekit_circuit_breaker_closes_total{function="get_user"}

# Lock events
cachekit_lock_acquisitions_total{function="get_user"}
cachekit_lock_timeouts_total{function="get_user"}
```

### Histogram Metrics (latency)
```prometheus
# Time to cache hit (L1 + L2)
cachekit_cache_hit_duration_seconds_bucket{function="get_user",le="0.001"}
cachekit_cache_hit_duration_seconds_bucket{function="get_user",le="0.01"}

# Time to cache miss + function execution
cachekit_cache_miss_duration_seconds_bucket{function="get_user",le="1.0"}

# L1 cache hit latency (always fast)
cachekit_l1_hit_duration_seconds{function="get_user"}

# L2 cache hit latency (Redis roundtrip)
cachekit_l2_hit_duration_seconds{function="get_user"}
```

### Gauge Metrics (current state)
```prometheus
# Number of items in L1 cache
cachekit_l1_cache_size{function="get_user"}

# Circuit breaker state (0=CLOSED, 1=OPEN, 2=HALF_OPEN)
cachekit_circuit_breaker_state{function="get_user"}
```

---

## Query Examples

### Cache Hit Rate
```promql
# Hit rate (percentage)
100 * sum(rate(cachekit_cache_hits_total[5m]))
    / (sum(rate(cachekit_cache_hits_total[5m]))
     + sum(rate(cachekit_cache_misses_total[5m])))
```

### Cache Latency (P99)
```promql
# 99th percentile latency for cache hits
histogram_quantile(0.99,
  rate(cachekit_cache_hit_duration_seconds_bucket[5m])
)
```

### Circuit Breaker Trips
```promql
# How often circuit breaker opens
rate(cachekit_circuit_breaker_opens_total[5m])
```

### L1 vs L2 Performance
```promql
# Average L1 latency
avg(cachekit_l1_hit_duration_seconds)

# Average L2 latency
avg(cachekit_l2_hit_duration_seconds)

# Ratio (L2 should be 40-100x slower)
avg(cachekit_l2_hit_duration_seconds)
  / avg(cachekit_l1_hit_duration_seconds)
```

### Lock Contention
```promql
# Average pods waiting for lock
avg(rate(cachekit_lock_wait_duration_seconds[5m]))
```

---

## Grafana Dashboard Example

```json
{
  "dashboard": {
    "title": "cachekit Metrics",
    "panels": [
      {
        "title": "Cache Hit Rate",
        "targets": [{
          "expr": "100 * sum(rate(cachekit_cache_hits_total[5m])) / (sum(rate(cachekit_cache_hits_total[5m])) + sum(rate(cachekit_cache_misses_total[5m])))"
        }]
      },
      {
        "title": "Cache Latency (P99)",
        "targets": [{
          "expr": "histogram_quantile(0.99, rate(cachekit_cache_hit_duration_seconds_bucket[5m]))"
        }]
      },
      {
        "title": "Circuit Breaker State",
        "targets": [{
          "expr": "cachekit_circuit_breaker_state"
        }]
      }
    ]
  }
}
```

---

## Alerting Examples

### Alert: Low Cache Hit Rate
```yaml
- alert: LowCacheHitRate
  expr: |
    100 * sum(rate(cachekit_cache_hits_total[5m]))
        / (sum(rate(cachekit_cache_hits_total[5m]))
         + sum(rate(cachekit_cache_misses_total[5m])))
    < 50  # Hit rate below 50%
  annotations:
    summary: "Cache hit rate is low (< 50%)"
```

### Alert: Circuit Breaker Open
```yaml
- alert: CircuitBreakerOpen
  expr: cachekit_circuit_breaker_state > 0  # Not CLOSED
  for: 1m
  annotations:
    summary: "Cache circuit breaker is open"
```

### Alert: Lock Timeouts
```yaml
- alert: CacheLockTimeouts
  expr: increase(cachekit_lock_timeouts_total[5m]) > 10
  annotations:
    summary: "Cache lock timeouts occurring"
```

---

## Configuration

### Enable/Disable Metrics
```python notest
from cachekit import cache

@cache(
    ttl=300,
    metrics_enabled=True,  # Default: True
)
def operation(x):
    return compute(x)
```

### Disable Metrics Globally
```bash
export CACHEKIT_METRICS_ENABLED=false
```

### Custom Metric Prefix
```python notest
# Default: "cachekit_"
@cache(ttl=300, metrics_prefix="myapp_cache_")
def operation(x):
    return compute(x)

# Produces: myapp_cache_hits_total, etc
```

---

## Performance Impact

Metrics collection is minimal overhead:
- Counter increments: ~10ns
- Histogram updates: ~50ns per call
- Total overhead: <1% latency impact

---

## Integration with Other Features

**Metrics + Circuit Breaker**:
```prometheus
cachekit_circuit_breaker_opens_total  # How many times CB opened
cachekit_circuit_breaker_state  # Current state
```

**Metrics + Distributed Locking**:
```prometheus
cachekit_lock_acquisitions_total  # Lock wins
cachekit_lock_timeouts_total  # Lock failures
cachekit_lock_wait_duration_seconds  # How long pods waited
```

---

## Troubleshooting

**Q: Metrics endpoint not responding**
A: Check METRICS_ENABLED=true, metrics port is exposed

**Q: Hit rate always 0**
A: Check if function is actually being called, L1/L2 cache working

**Q: Metrics growing unbounded**
A: Prometheus retention is configurable (default 15 days)

---

## See Also

- [Circuit Breaker](circuit-breaker.md) - Monitored by metrics
- [Distributed Locking](distributed-locking.md) - Lock metrics tracked
- [Prometheus Metrics Integration](../getting-started.md#prometheus)

---

<div align="center">

*Last Updated: 2025-12-02 · ✅ Feature implemented and tested*

</div>
