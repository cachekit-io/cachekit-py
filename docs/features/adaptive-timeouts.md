**[Home](../README.md)** › **Features** › **Adaptive Timeouts**

# Adaptive Timeouts - Auto-Tune to Infrastructure

**Version**: cachekit v1.0+

## TL;DR

Adaptive timeouts auto-adjust to Redis latency (P99). If Redis is slow today, timeout increases automatically. No need to tune timeout constants for different environments.

```python
@cache(ttl=300)  # Adaptive timeout enabled by default
def get_data(key):
    # If Redis P99 latency is 10ms → timeout auto-sets to 30ms (3x)
    # If Redis P99 latency is 100ms → timeout auto-sets to 300ms (3x)
    return db.query(key)
```

---

## Quick Start

Adaptive timeout enabled by default:

```python notest
from cachekit import cache

@cache(ttl=300)  # Adaptive timeout active
def expensive_query(x):
    return db.query(x)

# Monitors Redis P99 latency automatically
# Adjusts timeout based on observed latency
result = expensive_query(1)
```

**Configuration** (optional):
```python notest
@cache(
    ttl=300,
    adaptive_timeout_enabled=True,  # Default: True
    timeout_base_milliseconds=100,  # Minimum timeout (default: 100ms)
    timeout_multiplier=3.0,  # Timeout = P99 * 3 (default: 3x)
)
def operation(x):
    return compute(x)
```

---

## What It Does

**Timeout calculation**:
```
Observe Redis latencies over last N requests
Calculate P99 (99th percentile)
Set timeout = P99 * timeout_multiplier

Example:
Redis is slow today:
  P99 latency: 100ms
  Timeout: 100ms * 3 = 300ms (gives 3x buffer)

Redis is fast:
  P99 latency: 10ms
  Timeout: 10ms * 3 = 30ms (tight timeout)

Auto-adjusts throughout day without code change
```

**Behavior**:
```
Request hits Redis
└─ If response within timeout: success
└─ If timeout expires: circuit breaker catches, graceful failure

Timeout automatically adjusts to:
- Time of day (peak vs off-peak latency)
- Infrastructure changes (scaling, load)
- Network conditions (congestion, etc)
```

---

## Why You'd Want It

**Scenario**: Service scales dynamically. Redis latency varies throughout day.

Without adaptive timeout:
```python notest
@cache(ttl=300, timeout_milliseconds=50)  # Static timeout
def get_data():
    # Off-peak: Redis is fast (10ms), timeout is 50ms (fine)
    # Peak hours: Redis is slow (100ms), timeout is 50ms (too short!)
    # Peak: 30% of requests timeout, circuit breaks
    # Solution: Increase timeout to 500ms
    # Off-peak: Timeout is overkill, slow responses
    return fetch_data()
```

With adaptive timeout:
```python
@cache(ttl=300)  # Adaptive
def get_data():
    # Off-peak: P99 = 10ms, timeout auto = 30ms (snappy)
    # Peak: P99 = 100ms, timeout auto = 300ms (generous)
    # Automatically adjusts without code change
    return fetch_data()
```

**Real example**: E-commerce site
- Midnight-9am: Low traffic, Redis fast, tight timeouts
- 9am-9pm: High traffic, Redis slower, loose timeouts
- 9pm-midnight: Checkout rush, Redis maxed, very loose timeouts
- All automatic, zero configuration changes

---

## Why You Might Not Want It

**Scenarios where adaptive timeout doesn't help**:

1. **Static load**: Redis always same latency (no variation)
2. **High variance**: P99 doesn't match actual latency distribution
3. **Catastrophic failure**: Redis crashed (timeout doesn't help)

**Mitigation**: Static timeout with circuit breaker (better match):
```python notest
@cache(ttl=300, adaptive_timeout_enabled=False, timeout_milliseconds=500)
def get_data():
    # Or: Use circuit breaker for failure handling
    return fetch_data()
```

---

## What Can Go Wrong

### P99 Biased by Slow Queries
```python
@cache(ttl=300)
def operation(x):
    if expensive_condition(x):
        # Takes 10 seconds
        return slow_compute(x)
    else:
        # Takes 10ms
        return fast_compute(x)

# P99 calculation sees: [10ms, 10ms, ..., 10s]
# P99 = 10s
# Timeout = 30s (very loose)
# Problem: Most queries are fast but timeout is slow
# Solution: Break slow queries into separate cached function
```

### Timeout Constantly Changing
```python
# Load pattern very volatile
# P99 changes by 100x per minute
# Timeout thrashes: 30ms → 300ms → 30ms → 300ms

# Problem: Unstable system
# Solution: Increase timeout_multiplier (more buffer) or use static timeout
```

### Redis Completely Down
```python
# Redis crashes
# Adaptive timeout can't help
# Circuit breaker catches and returns None
# Timeout still relevant (how long to wait for circuit to open)
```

---

## How to Use It

### Basic Usage (Automatic)
```python
@cache(ttl=3600)  # Adaptive timeout enabled
def get_user(user_id):
    return db.query(user_id)

# Timeout auto-adjusts to Redis latency
user = get_user(123)
```

### Tuning for Your Infrastructure
```python notest
# If you want tighter timeouts (aggressive)
@cache(
    ttl=3600,
    timeout_base_milliseconds=50,  # Minimum 50ms
    timeout_multiplier=2.0,  # Timeout = P99 * 2 (less buffer)
)
def operation(x):
    return compute(x)

# If you want looser timeouts (conservative)
@cache(
    ttl=3600,
    timeout_base_milliseconds=500,  # Minimum 500ms
    timeout_multiplier=5.0,  # Timeout = P99 * 5 (more buffer)
)
def operation(x):
    return compute(x)
```

### Disabling Adaptive Timeout
```python notest
@cache(
    ttl=3600,
    adaptive_timeout_enabled=False,
    timeout_milliseconds=500,  # Static 500ms
)
def operation(x):
    return compute(x)
```

---

## Technical Deep Dive

### P99 Calculation
```python
import numpy

# Collect latencies from last N requests
latencies = [10, 12, 9, 8, 11]  # milliseconds

# Calculate percentile
# P99 = value where 99% of latencies are below
p99 = numpy.percentile(latencies, 99)

# Example: [5, 10, 15, 20, 25, ..., 995, 1000]
# P99 = 990ms (99% below, 1% above)

# Set timeout
timeout_multiplier = 3.0
timeout = p99 * timeout_multiplier  # 990ms * 3.0 = 2970ms
```

### Window Size
```
Observation window: Last 1000 requests (configurable)
Update frequency: Every 100 requests
Advantages:
  - Responsive to changes (updated frequently)
  - Stable (100-request window, not 1-request)
  - Reasonable memory (1000-request circular buffer)
```

### Percentile Choice
```
Why P99 and not P50/P95/P999?

P50 (median):
  - Too aggressive, timeouts too tight
  - 50% of requests close to timeout

P95:
  - Close to P99, middle ground

P99:
  - Industry standard
  - Catches most slow requests
  - Top 1% still might timeout (acceptable)

P999:
  - Too loose, timeouts very long
  - Outlier queries control behavior
```

---

## Interaction with Other Features

**Adaptive Timeout + Circuit Breaker**:
```python
@cache(ttl=300)  # Both enabled
def get_data():
    # Adaptive timeout auto-adjusts to Redis latency
    # If timeout expires → Circuit breaker catches
    # Both work together for reliability
    return fetch_data()
```

**Adaptive Timeout + Distributed Locking**:
```python
@cache(ttl=300)  # Both enabled
def get_data():
    # Distributed locking timeout is separate from Redis timeout
    # Adaptive timeout only affects Redis operations
    # Lock timeout is static (configurable separately)
    return fetch_data()
```

---

## Monitoring & Debugging

### Metrics Available
```prometheus
cachekit_redis_latency_p99_milliseconds{function="get_user"}
  # P99 latency (basis for timeout)

cachekit_timeout_milliseconds{function="get_user"}
  # Current adaptive timeout

cachekit_timeout_exceeded_total{function="get_user"}
  # How often timeout was exceeded
```

### Debugging Timeout Issues
```python notest
from cachekit import get_cache_metrics

metrics = get_cache_metrics("get_user")
print(f"P99 latency: {metrics.redis_p99_ms}ms")
print(f"Current timeout: {metrics.timeout_ms}ms")
print(f"Timeouts exceeded: {metrics.timeout_exceeded}")

# If timeout_exceeded is high:
# → Increase timeout_multiplier
# → Or increase timeout_base_milliseconds
```

---

## Troubleshooting

**Q: Getting "timeout exceeded" errors**
A: Increase `timeout_multiplier` or `timeout_base_milliseconds`

**Q: Timeout constantly changing**
A: Increase `timeout_multiplier` for more stability

**Q: Want to disable adaptive timeout**
A: Set `adaptive_timeout_enabled=False` and specify static `timeout_milliseconds`

---

## See Also

- [Circuit Breaker](circuit-breaker.md) - Catches timeout failures
- [Distributed Locking](distributed-locking.md) - Has separate timeout
- [Prometheus Metrics](prometheus-metrics.md) - Monitor adaptive timeout

---

<div align="center">

*Last Updated: 2025-12-02 · ✅ Feature implemented and tested*

</div>
