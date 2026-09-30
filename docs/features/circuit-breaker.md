**[Home](../README.md)** › **Features** › **Circuit Breaker**

# Circuit Breaker - Prevent Cascading Failures

**Available since v0.3.0**

## TL;DR

Circuit breaker prevents cascading failures when the L2 backend is down. After N errors, circuit opens: calls skip the backend and run your function uncached instead of failing, while values already in the in-process L1 cache are still served. Auto-recovers after cooldown.

```python notest
@cache(ttl=300, backend=None)  # Circuit breaker enabled by default
def get_data(key):
    return db.query(key)  # illustrative - If backend fails, circuit breaker catches it
```

---

## Quick Start

Circuit breaker is enabled by default. No configuration needed:

```python
from cachekit import cache

@cache(ttl=300, backend=None)  # Circuit breaker active
def expensive_operation(x):
    return do_expensive_computation()

# Redis working: Normal cache behavior
result = expensive_operation(1)  # L1 hit or L2 hit or compute

# Backend down: Circuit breaker catches error
# Behavior: the function runs uncached, app continues
result = expensive_operation(1)  # Computed directly instead of raising
```

**Configuration** (optional):
```python
from cachekit import cache
from cachekit.config.nested import CircuitBreakerConfig

@cache(
    ttl=300,
    backend=None,
    circuit_breaker=CircuitBreakerConfig(
        enabled=True,  # Default: True
        failure_threshold=3,  # Open after 3 consecutive failures (default: 5)
        recovery_timeout=10.0,  # Cooldown before a recovery probe (default: 30.0)
    )
)
def operation(x):
    return do_expensive_computation()

# The live breaker reports the settings it was built with
live = operation.get_health_status()["circuit_breaker"]["config"]
assert live["failure_threshold"] == 3
assert live["timeout_seconds"] == 10.0  # recovery_timeout
```

| Field | Type | Default | Meaning |
|-------|------|---------|---------|
| `enabled` | `bool` | `True` | Turn the breaker on or off |
| `failure_threshold` | `int` | `5` | Consecutive failures before the circuit opens |
| `success_threshold` | `int` | `3` | Consecutive successes in HALF_OPEN before it closes |
| `recovery_timeout` | `float` | `30.0` | Cooldown in seconds before an OPEN circuit admits a recovery probe (reported as `timeout_seconds`). Must be finite and `> 0`: it also caps probing at `half_open_requests` per cooldown |
| `half_open_requests` | `int` | `3` | Total probe requests admitted per HALF_OPEN cycle (not a concurrency limit). Must be `>= success_threshold`, or `@cache` raises `ConfigurationError`, because a HALF_OPEN cycle could never close |

> [!NOTE]
> The breaker guards L2 backend calls only: in L1-only mode (`backend=None`, used in the examples
> on this page so they run anywhere) it is never consulted. The examples show configuration,
> not protection.

> [!IMPORTANT]
> `circuit_breaker=` takes `cachekit.config.nested.CircuitBreakerConfig`. The top-level
> `from cachekit import CircuitBreakerConfig` is a different class: it configures a standalone
> `cachekit.reliability.CircuitBreaker` (fields `timeout_seconds`, `excluded_error_types`), and
> `@cache` rejects it with a `TypeError` that names the class to use.

---

## What It Does

**Circuit breaker is a state machine**:

| State | Behavior | Transition |
|-------|----------|------------|
| **CLOSED** | Normal cache operation, count failures | After N failures → OPEN |
| **OPEN** | Skip the backend: an L1 hit is still served, and an L1 miss runs the function uncached (sync and async). No failure is counted | First call once the cooldown has passed (default 30s after the circuit opened) → HALF_OPEN |
| **HALF_OPEN** | Admit up to 3 probe calls to the backend (`half_open_requests`); further L1 misses run uncached. L1 hits are served and are neither probes nor successes | 3 successes (`success_threshold`) → CLOSED, any recorded failure → OPEN. If all 3 probes have been admitted and the cycle is still undecided a cooldown after it began (for example, a cancelled async probe never reported back), a fresh cycle of 3 probes starts and any successes already counted are discarded |

**Example scenario**:
```
Pod A tries to cache fetch at 12:00:00
Backend working: CLOSED state, success
Backend fails at 12:00:05
Requests 1-5: Errors accumulated; the 5th failure OPENS the circuit
Requests 6-34: Circuit OPEN, function runs uncached (no backend calls)
Request 35 (once 30s have passed since the circuit opened): Circuit goes HALF_OPEN, probe 1 of 3
Requests 35-37: Backend back up, all 3 probes succeed → Circuit CLOSES
Request 38: Normal operation resumes
```

---

## Why You'd Want It

**Production scenario**: Service depends on the L2 backend for caching. Backend becomes unavailable.

**Without circuit breaker**:
```
Backend is down
Cache decorator catches errors
Caller gets exception: "ConnectionError: backend unreachable"
Service crashes if error not handled by caller
Cascades to dependent services
```

**With circuit breaker**:
```
Backend is down
Circuit breaker catches errors
After N failures: Circuit OPENS
Caller gets: the function's result, computed without the cache
Service continues working (degraded but up)
No cascading failures
```

---

## Why You Might Not Want It

> [!NOTE]
> Scenarios where circuit breaker adds overhead without benefit:
>
> 1. **Highly reliable backend** (failures truly exceptional): Overhead with no benefit
> 2. **Designed-to-fail cache** (failures expected): May mask bugs
> 3. **High-volume, low-margin calls**: Cooldown delay might matter

**Mitigation**: Disable if not needed:
```python notest
from cachekit.config.nested import CircuitBreakerConfig

@cache(ttl=300, circuit_breaker=CircuitBreakerConfig(enabled=False), backend=None)
def operation(x):
    return compute(x)  # illustrative - not defined
```

---

## What Can Go Wrong

### Misconfiguration: Threshold Too Low
```python
from cachekit.config.nested import CircuitBreakerConfig

@cache(ttl=300, circuit_breaker=CircuitBreakerConfig(failure_threshold=1), backend=None)
def operation(x):
    return compute(x)  # illustrative - not defined
# Problem: Circuit opens after 1 failure
# Solution: Increase threshold to 5-10
assert operation.get_health_status()["circuit_breaker"]["config"]["failure_threshold"] == 1
```

### Misconfiguration: Cooldown Too Short
```python
from cachekit.config.nested import CircuitBreakerConfig

@cache(ttl=300, circuit_breaker=CircuitBreakerConfig(recovery_timeout=1.0), backend=None)
def problematic_function():
    # Problem: a 1s cooldown re-probes a still-failing backend every second
    # (OPEN → HALF_OPEN → OPEN).
    # Solution: Increase cooldown to 30-60 seconds
    return expensive_operation()  # illustrative - not defined

assert problematic_function.get_health_status()["circuit_breaker"]["config"]["timeout_seconds"] == 1.0
```

### Full Load on Your Data Source While OPEN
```python
@cache(ttl=300)  # 5 minute cache
def get_data():
    # Circuit OPEN: L2 is skipped. An L1 hit is still served, but L1 is per process and
    # expires with the TTL, and nothing is written to it while the circuit is OPEN
    # (backend errors do not currently count toward the breaker, so an outage alone does not open it)
    # Result: every L1 miss runs this function, so the database takes close to full load
    # Solution: size the data source for uncached traffic during an outage
    return fetch_data()
```

---

## How to Use It

### Basic Usage (Default)
```python notest
from cachekit import cache

@cache(ttl=3600, backend=None)  # Circuit breaker ON by default
def get_user(user_id):
    return db.query(User).filter_by(id=user_id).first()  # illustrative - not defined

# App continues working even if backend is down
user = get_user(123)  # Backend down: the query runs uncached
```

### With Graceful Fallback
```python notest
@cache(ttl=3600, backend=None)
def get_config(key):
    return db.get_config(key)  # illustrative - not defined

# No None check for outages: an OPEN circuit runs get_config uncached.
# An exception here comes from get_config itself.
try:
    config = get_config("feature_flag")
except Exception as e:
    logger.warning(f"Config fetch failed: {e}")
    config = get_default_config("feature_flag")  # illustrative - not defined
```

### Tuning for Your Infrastructure
```python
from cachekit.config.nested import CircuitBreakerConfig

@cache(
    ttl=3600,
    backend=None,
    # Tune these based on your Redis reliability
    circuit_breaker=CircuitBreakerConfig(
        failure_threshold=10,  # Open after 10 failures
        recovery_timeout=60.0,  # Cooldown before a recovery probe
    )
)
def fetch_data(key):
    return db.fetch(key)  # illustrative - not defined

# Confirm what the live breaker runs with
live = fetch_data.get_health_status()["circuit_breaker"]["config"]
assert (live["failure_threshold"], live["timeout_seconds"]) == (10, 60.0)
```

---

## Technical Deep Dive

### State Machine Implementation
```python notest
from typing import Literal
import time

# Circuit breaker state transitions (illustrative pseudocode)
class CircuitBreaker:
    state: Literal["CLOSED", "OPEN", "HALF_OPEN"]
    failure_count: int
    last_failure_time: float  # Failures recorded while OPEN do not move it
    half_open_since: float

    def call(self, func):
        if self.state == "CLOSED":
            try:
                return func()  # Normal operation
            except Exception:
                self.failure_count += 1
                self.last_failure_time = time.time()
                if self.failure_count >= threshold:  # threshold = config value
                    self.state = "OPEN"  # Open circuit
                raise

        if self.state == "OPEN":
            if time.time() - self.last_failure_time <= cooldown:  # cooldown = config value
                return uncached(func)  # Rejected: no backend call, not counted as a failure
            self.state = "HALF_OPEN"  # Try recovery
            self.probes = self.successes = 0
            self.half_open_since = time.time()

        if self.state == "HALF_OPEN":
            if self.probes >= half_open_requests:  # probe budget, default 3
                if time.time() - self.half_open_since <= cooldown:
                    return uncached(func)  # Budget spent: rejected, not a failure
                self.probes = self.successes = 0  # Spent and undecided a cooldown after it began: fresh cycle
                self.half_open_since = time.time()
            self.probes += 1
            try:
                result = func()
            except Exception:
                self.state = "OPEN"  # Recovery failed
                self.last_failure_time = time.time()
                raise
            self.successes += 1
            if self.successes >= success_threshold:  # default 3
                self.state = "CLOSED"  # Recovered!
                self.failure_count = 0
            return result
```

### Integration with Caching
```
Circuit CLOSED → L1, then L2, then the function, as usual
Circuit OPEN → L1 is still read (a hit is served); L2 is skipped, so an L1 miss runs the function uncached
Redis error → Logged: a failed read is a miss, a failed write skips L2 only, and L1 still stores the result (backend errors do not currently count toward the breaker)
```

### Performance Impact
- **CLOSED state**: ~10ns overhead per call (state check)
- **OPEN state**: <1ns overhead per call (returns immediately)
- **HALF_OPEN state**: Normal L2 backend latency (~2-50ms)

---

## Interaction with Other Features

**Circuit Breaker + Distributed Locking**:
```python notest
@cache(ttl=300)  # Both features enabled (they need an L2 backend)
def fetch(key):
    # L2 miss → Distributed lock acquired
    # Only one pod calls fetch()
    # If L2 fails → logged; backend errors do not currently count toward the breaker
    # Each pod runs fetch() on its own L1 miss and keeps the result in its L1
    return db.fetch(key)  # illustrative - not defined
```

**Circuit Breaker + Encryption**:
```python notest
@cache.secure(master_key=secret_key, ttl=300)  # Both features enabled
def fetch_sensitive(key):
    # Encryption happens before L2 write
    # If L2 fails → logged; the result still goes to L1; backend errors do not currently count toward the breaker
    # A decrypt/integrity failure on read never counts toward the breaker.
    # Fail-open (default): it is a cache miss and the function runs.
    # fail_closed=True: an authentication failure raises DecryptionAuthenticationError
    return db.fetch(key)  # illustrative - not defined
```

---

## Monitoring & Debugging

### Metrics Available
```prometheus
cachekit_circuit_breaker_state{function="fetch_user"}
  0 = CLOSED, 1 = OPEN, 2 = HALF_OPEN

cachekit_circuit_breaker_failures_total{function="fetch_user"}
  # Number of failures before circuit opened

cachekit_circuit_breaker_recoveries_total{function="fetch_user"}
  # Number of times circuit recovered from OPEN
```

### Debugging Circuit State
```python notest
# Example of checking circuit breaker state (API may vary)
# Check function's health status instead:
@cache(ttl=300, backend=None)
def fetch_user(user_id):
    return {"id": user_id}

# Use get_health_status() method added to decorated function
health = fetch_user.get_health_status()
print(f"Circuit state: {health['circuit_breaker']['state']}")
print(f"Failures: {health['circuit_breaker']['failure_count']}")
```

---

## Troubleshooting

**Q: Circuit breaker keeps opening**
A: Raise `failure_threshold` or `recovery_timeout`. Investigate why Redis is failing.

**Q: My function runs on every call while the backend is down**
A: That's the OPEN state: calls skip the cache and run your function. Once the cooldown has passed, the circuit probes the backend again and closes after 3 successful probes.

**Q: Want to disable circuit breaker for testing**
A: Pass `circuit_breaker=CircuitBreakerConfig(enabled=False)`.

---

## See Also

- [Distributed Locking](distributed-locking.md) - Prevents cache stampedes
- [Prometheus Metrics](prometheus-metrics.md) - Monitor circuit breaker state
- [Comparison Guide](../comparison.md) - How cachekit's reliability beats competitors

---

<div align="center">

**[GitHub Issues](https://github.com/cachekit-io/cachekit-py/issues)** · **[Documentation](../README.md)**

</div>
