**[Home](../README.md)** › **Features** › **Prometheus Metrics**

# Prometheus Metrics - Observability Built-In

**Available since v0.3.0**

## TL;DR

cachekit records Prometheus metrics for cache operations, latency, payload size, and
circuit-breaker state. Collection is on by default — no per-call instrumentation code is
needed. **Exposition is your responsibility**: cachekit does not start an HTTP server or
register a `/metrics` route. Wire up `prometheus_client` exposition in your app and the
metrics show up on your existing scrape endpoint.

```prometheus
cache_operations_total{operation="get",namespace="users",success="True",serializer="default"} 9847
redis_cache_operations_total{operation="get",status="hit",serializer="default",namespace="users"} 9847
```

---

## Quick Start

Metrics are recorded automatically once a decorated function runs:

```python notest
from cachekit import cache

@cache(ttl=300)  # Metrics recorded by default
def get_user(user_id):
    return db.query(User).get(user_id)
```

cachekit registers its metrics on the default `prometheus_client` registry. To make them
scrapeable you must expose that registry yourself — cachekit ships no HTTP server. The two
common options:

```python notest
# Option A: dedicated metrics HTTP server (e.g. a worker process or sidecar)
from prometheus_client import start_http_server

start_http_server(8000)  # serves the default registry at http://0.0.0.0:8000/metrics
```

```python notest
# Option B: mount exposition into an existing ASGI/WSGI app (here: a WSGI app)
from prometheus_client import make_wsgi_app
from wsgiref.simple_server import make_server

metrics_app = make_wsgi_app()  # serves the default registry
# mount metrics_app at /metrics in your framework of choice
make_server("0.0.0.0", 8000, metrics_app).serve_forever()
```

**Scrape configuration** (prometheus.yml) — point Prometheus at whatever endpoint your app
exposes:

```yaml
scrape_configs:
  - job_name: 'cachekit'
    static_configs:
      - targets: ['localhost:8000']  # The host:port your app exposes metrics on
    metrics_path: '/metrics'         # Whatever path you mounted exposition on
```

---

## Available Metrics

cachekit emits the following metrics on the default `prometheus_client` registry. The names
below are the **actual** series names — none carry a `cachekit_` prefix.

### Counters (always increasing)

```prometheus
# Cache operations from the async/sync metrics path.
# Labels: operation, namespace, success, serializer
cache_operations_total{operation="get",namespace="users",success="True",serializer="default"}

# Cache operations from the backpressure/load-control path.
# Labels: operation, status, serializer, namespace
redis_cache_operations_total{operation="get",status="hit",serializer="default",namespace="users"}
```

> Hits and misses are not separate series. Compute them from labels — the `success` label
> on `cache_operations_total` and the `status` label on `redis_cache_operations_total`
> distinguish hits from misses.

### Histograms (latency and size)

```prometheus
# Cache operation duration in milliseconds.
# Labels: operation, namespace, serializer
cache_operation_duration_ms{operation="get",namespace="users",serializer="default"}

# Cache operation payload size in bytes.
# Labels: operation, namespace, serializer
cache_operation_size_bytes{operation="get",namespace="users",serializer="default"}
```

### Gauges (current state)

```prometheus
# Circuit breaker state. Numeric value: 0=CLOSED, 1=OPEN, 2=HALF_OPEN.
# Labels: namespace, state
circuit_breaker_state{namespace="users",state="open"}
```

---

## Query Examples

### Cache Hit Rate

```promql
# Hit rate (percentage) using the success label on cache_operations_total
100 * sum(rate(cache_operations_total{success="True"}[5m]))
    / sum(rate(cache_operations_total[5m]))
```

```promql
# Hit rate from the load-control path using the status label
100 * sum(rate(redis_cache_operations_total{status="hit"}[5m]))
    / sum(rate(redis_cache_operations_total[5m]))
```

### Cache Latency (P99)

```promql
# 99th percentile cache operation duration (milliseconds)
histogram_quantile(0.99,
  rate(cache_operation_duration_ms_bucket[5m])
)
```

### Payload Size (P99)

```promql
# 99th percentile cache payload size (bytes)
histogram_quantile(0.99,
  rate(cache_operation_size_bytes_bucket[5m])
)
```

### Circuit Breaker State

```promql
# Current circuit breaker state per namespace (0=CLOSED, 1=OPEN, 2=HALF_OPEN)
circuit_breaker_state
```

---

## Alerting Examples

### Alert: Low Cache Hit Rate

```yaml
- alert: LowCacheHitRate
  expr: |
    100 * sum(rate(cache_operations_total{success="True"}[5m]))
        / sum(rate(cache_operations_total[5m]))
    < 50  # Hit rate below 50%
  annotations:
    summary: "Cache hit rate is low (< 50%)"
```

### Alert: Circuit Breaker Open

```yaml
- alert: CircuitBreakerOpen
  expr: circuit_breaker_state > 0  # Not CLOSED
  for: 1m
  annotations:
    summary: "Cache circuit breaker is open"
```

---

## Configuration

### Enable/Disable Metrics

Metric collection is controlled by `MonitoringConfig.enable_prometheus_metrics` (default:
`True`). Disabling it stops cachekit from recording series; it has no effect on exposition,
which your app owns regardless.

```python notest
from cachekit import cache
from cachekit.config.nested import MonitoringConfig

@cache(
    ttl=300,
    monitoring=MonitoringConfig(enable_prometheus_metrics=True),  # Default: True
)
def operation(x):
    return compute(x)
```

### Disable Metrics

```python notest
from cachekit import cache
from cachekit.config.nested import MonitoringConfig

@cache(
    ttl=300,
    monitoring=MonitoringConfig(enable_prometheus_metrics=False),
)
def operation(x):
    return compute(x)
```

### Disable All Observability

```python notest
from cachekit import cache
from cachekit.config.nested import MonitoringConfig

@cache(
    ttl=300,
    monitoring=MonitoringConfig(
        collect_stats=False,
        enable_tracing=False,
        enable_structured_logging=False,
        enable_prometheus_metrics=False,
    ),
)
def operation(x):
    return compute(x)
```

---

## Per-Function Statistics

For lightweight, in-process counters that do not require a Prometheus scrape, every
decorated function exposes `cache_info()` (modelled on `functools.lru_cache`). It returns a
`CacheInfo` named tuple with hit/miss counts, the L1/L2 split, and average L2 latency. See
the [API Reference](../api-reference.md#per-function-statistics-via-cache_info) for the full
field list and an example stats endpoint.

---

## Performance Impact

Metrics collection is minimal overhead:
- Counter increments: ~10ns
- Histogram updates: ~50ns per call
- Total overhead: <1% latency impact

---

## Troubleshooting

**Q: My `/metrics` endpoint returns nothing for cachekit**
A: cachekit does not expose metrics for you. Confirm your app starts
`prometheus_client` exposition (`start_http_server` or `make_wsgi_app`/`make_asgi_app`) on
the **default** registry, and that at least one decorated function has run — series are
created lazily on first use.

**Q: Hit rate always 0**
A: Check that the function is actually being called and that L1/L2 caching is working.
Remember hit/miss is derived from labels (`success` / `status`), not from separate series.

**Q: Metrics growing unbounded**
A: Prometheus retention is configurable (default 15 days). Keep label cardinality bounded —
avoid high-cardinality `namespace` values.

---

## See Also

- [API Reference – Monitoring and Observability](../api-reference.md#monitoring-and-observability) - Full metric list and `cache_info()`
- [Circuit Breaker](circuit-breaker.md) - Source of `circuit_breaker_state`
- [Distributed Locking](distributed-locking.md) - Multi-pod safety

---

<div align="center">

**[GitHub Issues](https://github.com/cachekit-io/cachekit-py/issues)** · **[Documentation](../README.md)**

</div>
