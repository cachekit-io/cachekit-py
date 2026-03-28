**[Home](../README.md)** › **Features** › **Adaptive Timeouts**

# Adaptive Timeouts - Auto-Tune to Infrastructure

**Available since v0.3.0**

## TL;DR

Adaptive timeouts auto-adjust to Redis latency (P95). If Redis is slow today, the timeout increases automatically. No need to tune timeout constants for different environments.

```python
@cache(ttl=300)  # Adaptive timeout enabled by default
def get_data(key):
    # If Redis P95 latency is 10ms → timeout auto-sets to 15ms (P95 * 1.5x buffer)
    # If Redis P95 latency is 100ms → timeout auto-sets to 150ms
    return db.query(key)
```

---

## Quick Start

Adaptive timeout is enabled by default:

```python notest
from cachekit import cache

@cache(ttl=300)  # Adaptive timeout active
def expensive_query(x):
    return db.query(x)

# Monitors Redis P95 latency automatically
# Adjusts timeout based on observed latency
result = expensive_query(1)
```

**Configuration** (optional):
```python notest
from cachekit.config.nested import TimeoutConfig

@cache(
    ttl=300,
    timeout=TimeoutConfig(
        enabled=True,       # Default: True
        initial=1.0,        # Initial timeout in seconds (default: 1.0s)
        min=0.1,            # Minimum timeout in seconds (default: 0.1s)
        max=5.0,            # Maximum timeout in seconds (default: 5.0s)
        percentile=95.0,    # Target percentile (default: P95)
        window_size=1000,   # Sliding window size (default: 1000 requests)
    )
)
def operation(x):
    return compute(x)
```

---

## What It Does

**Timeout calculation**:
```
Observe Redis latencies over last N requests (sliding window)
Calculate P95 (95th percentile)
Set timeout = P95 * 1.5  (fixed 50% safety buffer)

Example:
Redis is slow today:
  P95 latency: 100ms
  Timeout: 100ms * 1.5 = 150ms

Redis is fast:
  P95 latency: 10ms
  Timeout: 10ms * 1.5 = 15ms

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

**Cold start**: Until at least 10 samples are collected, the system uses a conservative default of `initial * 2` seconds.

---

## Why You'd Want It

**Scenario**: Service scales dynamically. Redis latency varies throughout day.

Without adaptive timeout:
```python notest
@cache(ttl=300, timeout=TimeoutConfig(enabled=False, initial=0.05))  # Static 50ms
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
    # Off-peak: P95 = 10ms, timeout auto = 15ms (snappy)
    # Peak: P95 = 100ms, timeout auto = 150ms (generous)
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
2. **High variance**: P95 doesn't match actual latency distribution
3. **Catastrophic failure**: Redis crashed (timeout doesn't help)

**Mitigation**: Static timeout with circuit breaker (better match):
```python notest
from cachekit.config.nested import TimeoutConfig

@cache(ttl=300, timeout=TimeoutConfig(enabled=False, initial=0.5))
def get_data():
    # Static 500ms timeout, circuit breaker handles outages
    return fetch_data()
```

---

## What Can Go Wrong

### P95 Biased by Slow Queries
```python
@cache(ttl=300)
def operation(x):
    if expensive_condition(x):
        # Takes 10 seconds
        return slow_compute(x)
    else:
        # Takes 10ms
        return fast_compute(x)

# P95 calculation sees: [10ms, 10ms, ..., 10s]
# P95 = 10s
# Timeout = 15s (very loose)
# Problem: Most queries are fast but timeout is very slow
# Solution: Break slow queries into separate cached function
```

### Timeout Constantly Changing
```python
# Load pattern very volatile
# P95 changes by 100x per minute
# Timeout thrashes: 15ms → 150ms → 15ms → 150ms

# Problem: Unstable system
# Solution: Increase TimeoutConfig(max=...) for a larger ceiling,
#           or use a static timeout
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
```python notest
@cache(ttl=3600)  # Adaptive timeout enabled
def get_user(user_id):
    return db.query(user_id)

# Timeout auto-adjusts to Redis latency
user = get_user(123)
```

### Tuning for Your Infrastructure
```python notest
from cachekit.config.nested import TimeoutConfig

# Tight bounds (low-latency infrastructure)
@cache(
    ttl=3600,
    timeout=TimeoutConfig(
        min=0.05,    # 50ms minimum
        initial=0.5, # 500ms initial
        max=2.0,     # 2s maximum
    )
)
def operation(x):
    return compute(x)

# Conservative bounds (variable latency infrastructure)
@cache(
    ttl=3600,
    timeout=TimeoutConfig(
        min=0.5,     # 500ms minimum
        initial=2.0, # 2s initial
        max=10.0,    # 10s maximum
    )
)
def operation(x):
    return compute(x)
```

### Disabling Adaptive Timeout
```python notest
from cachekit.config.nested import TimeoutConfig

@cache(
    ttl=3600,
    timeout=TimeoutConfig(enabled=False, initial=0.5),  # Static 500ms
)
def operation(x):
    return compute(x)
```

---

## Technical Deep Dive

### P95 Calculation

The actual algorithm, from `AdaptiveTimeout.get_timeout()`:

```python notest
# Collect latencies from last 1000 requests (sliding window)
latencies = [0.010, 0.012, 0.009, 0.011]  # seconds

# Calculate P95 from sorted durations
sorted_durations = sorted(latencies)
index = int(len(sorted_durations) * 95.0 / 100)
p95 = sorted_durations[index]

# Add 50% buffer (fixed, not configurable)
timeout = p95 * 1.5

# Apply min/max bounds
timeout = max(min_timeout, min(timeout, max_timeout))
```

Until at least 10 samples are collected, the timeout falls back to `min_timeout * 2`.

### Window Size
```
Observation window: Last 1000 requests (configurable via window_size)
Minimum samples before adapting: 10 requests
Memory: Circular buffer, bounded at window_size entries
```

### Percentile Choice
```
Why P95 and not P50/P99/P999?

P50 (median):
  - Too aggressive, timeouts too tight
  - 50% of requests close to timeout

P95 (default):
  - Industry standard for "typical worst case"
  - Catches most slow requests
  - Top 5% still might timeout (acceptable)

P99:
  - Looser, more buffer
  - Suitable for latency-sensitive workloads
  - Configure via: TimeoutConfig(percentile=99.0)

P999:
  - Too loose, outlier queries dominate
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
    # Lock acquisition uses AdaptiveTimeoutManager (separate from general timeout)
    # Lock-specific timeout adjusts based on lock contention and operation duration
    return fetch_data()
```

---

## Monitoring & Debugging

### Metrics Available
```prometheus
cachekit_adaptive_timeout_adjustments{namespace="get_user"}
  # How often the adaptive timeout changed

circuit_breaker_state{namespace="get_user"}
  # Circuit breaker state (proxy for timeout-related failures)

cache_operations_total{operation="get", status="error", namespace="get_user"}
  # Error count (includes timeouts)
```

### Debugging Timeout Issues
```python notest
from cachekit.reliability.adaptive_timeout import AdaptiveTimeout

# Inspect the timeout calculator state
timeout_calc = AdaptiveTimeout(percentile=95.0)
# After recording durations...
current_timeout = timeout_calc.get_timeout()
print(f"Current adaptive timeout: {current_timeout:.3f}s")

# If timeouts are too aggressive:
# → Increase TimeoutConfig(max=...) for a higher ceiling
# → Or widen TimeoutConfig(min=...) for a larger floor

# If system is always using the cold-start default:
# → Not enough traffic (need 10+ samples)
# → Consider a manual initial value: TimeoutConfig(initial=0.5)
```

---

## Troubleshooting

**Q: Getting backend timeout errors**
A: Increase `TimeoutConfig(max=...)` to allow a higher ceiling, or increase `TimeoutConfig(initial=...)` for a more generous starting point.

**Q: Timeout constantly changing**
A: System load is volatile. Consider a higher `max` value as a stable ceiling.

**Q: Want to disable adaptive timeout**
A: Set `timeout=TimeoutConfig(enabled=False, initial=0.5)` for a static 500ms timeout.

**Q: Timeout seems stuck at the same value**
A: You probably have fewer than 10 samples recorded. The system uses `initial * 2` until enough data accumulates.

---

## See Also

- [Circuit Breaker](circuit-breaker.md) - Catches timeout failures
- [Distributed Locking](distributed-locking.md) - Has separate lock-specific adaptive timeout
- [Prometheus Metrics](prometheus-metrics.md) - Monitor reliability features

---

<div align="center">

**[GitHub Issues](https://github.com/cachekit-io/cachekit-py/issues)** · **[Documentation](../README.md)**

</div>
