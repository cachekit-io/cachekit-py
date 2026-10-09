**[Home](../README.md)** › **Features** › **Circuit Breaker**

# Circuit Breaker - Prevent Cascading Failures

**Available since v0.3.0**

## TL;DR

Circuit breaker takes the L2 backend out of the call path after repeated failures. After N failures within 60 s, circuit opens: calls skip the backend and run your function uncached instead of failing, while values already in the in-process L1 cache are still served. Auto-recovers after cooldown. A failure is a backend that cannot be built, such as an auto-detected Redis that is down at the first call; an exception raised by the decorated function, except from an async function that goes through distributed locking; or another failure listed under [Circuit breaker open](../error-codes.md#circuit-breaker-open). Read and write errors on a backend that is already built do not currently count toward the breaker.

```python notest
@cache(ttl=300)  # Circuit breaker enabled by default
def get_data(key):
    return db.query(key)  # illustrative - if Redis is down at startup, 5 failed connects within 60 s open the circuit
```

---

## Quick Start

Circuit breaker is enabled by default. No configuration needed:

```python
from cachekit import cache

@cache(ttl=300)  # Circuit breaker active
def expensive_operation(x):
    return do_expensive_computation()

# Redis working: Normal cache behavior
result = expensive_operation(1)  # L1 hit or L2 hit or compute

# Backend down: the error is logged and the function runs, app continues
# Down since startup: nothing is cached, and 5 failed connects within 60 s open the circuit
# Lost after the first call: the result still goes to L1, and the circuit stays closed
result = expensive_operation(2)  # Computed directly instead of raising
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
        failure_threshold=3,  # Open after 3 failures within 60 s (default: 5)
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
| `failure_threshold` | `int` | `5` | Failures within a 60 s rolling window that open the circuit. Successes do not reset the count; older failures stop counting |
| `success_threshold` | `int` | `3` | Consecutive successes in HALF_OPEN before it closes |
| `recovery_timeout` | `float` | `30.0` | Cooldown in seconds before an OPEN circuit admits a recovery probe (reported as `timeout_seconds`). Must be finite and `> 0`: it also caps a cycle's probe slots at `half_open_requests` per cooldown |
| `half_open_requests` | `int` | `3` | Probe slots per HALF_OPEN cycle (not a concurrency limit). Every admitted probe holds one until the cycle ends, cancelled ones included, except a probe whose function raises: it gives its slot back, so more calls than this can reach the backend in one cycle. Must be `>= success_threshold`, or `@cache` raises `ConfigurationError`: every success holds one of a cycle's slots, so a HALF_OPEN cycle could never close |

> [!NOTE]
> The breaker guards L2 backend calls only: in L1-only mode (`backend=None`, used in the configuration
> examples on this page so they run anywhere) it is never consulted. Those examples show configuration,
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
| **HALF_OPEN** | Each cycle has 3 probe slots (`half_open_requests`); L1 misses that find none free run uncached. L1 hits are served and are neither probes nor successes. Every admitted probe holds its slot until the cycle ends, whether it succeeds, fails or never reports back. Only a probe whose function raises gives it back: it records no outcome and the next call probes in its place, so while the function keeps raising, more than 3 calls in one cycle can reach the backend | 3 successes (`success_threshold`) → CLOSED, any recorded failure → OPEN. While HALF_OPEN, only the outcomes of the current cycle's own probes count: a slow probe from a cycle that ended (another probe failed, or the cycle was restarted), or a call admitted while the circuit was still CLOSED, cannot close or reopen it. If all 3 slots are held and the cycle is still undecided a cooldown after it began (for example, a cancelled async probe never reported back), a fresh cycle of 3 probes starts and any successes already counted are discarded |

**Example scenario**:
```
Pod A starts at 12:00:00 with Redis down (auto-detected backend, built on the first call)
Requests 1-5: each call tries to build the backend, the connect fails, fetch runs uncached;
             the 5th failure within 60 s OPENS the circuit
Requests 6-34: Circuit OPEN, fetch runs uncached without trying to connect
Request 35 (once 30s have passed since the circuit opened): Circuit goes HALF_OPEN, probe 1 of 3
Requests 35-37: Redis is back, the backend builds, all 3 probes succeed → Circuit CLOSES
Request 38: Normal operation resumes
```

---

## Why You'd Want It

**Production scenario**: Service depends on Redis for caching. Redis is down when the service starts.

**Without circuit breaker**:
```
Every call tries to build the Redis backend and waits for the connect to fail
(up to the 5 s connect timeout when the host does not answer)
The function runs uncached; nothing goes to L1
```

**With circuit breaker**:
```
After 5 failed builds within 60 s: Circuit OPENS
Calls run the function at once, without trying to connect
After the cooldown, up to 3 probe calls try Redis again; 3 successes close the circuit
```

Once the backend is built, losing Redis later does not open the circuit: a failed read is a miss, a failed write still stores the result in L1, and these errors do not currently count toward the breaker.

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
    # Problem: a 1s cooldown re-probes a still-failing function every second
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

@cache(ttl=3600)  # Circuit breaker ON by default
def get_user(user_id):
    return db.query(User).filter_by(id=user_id).first()  # illustrative - not defined

# App continues working even if backend is down
user = get_user(123)  # Backend down: the query runs instead of raising
```

### With Graceful Fallback
```python notest
@cache(ttl=3600)
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
    # Tune these to how often counted failures occur (see error-codes.md, Circuit breaker open)
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
    failure_times: list[float]  # Recent failures (monotonic clock); len() is the failure count
    last_failure_time: float  # Failures recorded while OPEN do not move it
    half_open_since: float
    cycle: int  # Numbers each HALF_OPEN cycle

    def call(self, func):
        cycle = self.admit()
        if cycle is None:
            return uncached(func)  # Rejected: no backend call, not counted as a failure
        try:
            result = cached(func)
        except BackendFailure:  # The function's own exceptions never count
            self.record_failure(cycle)
            raise
        except Exception:
            self.release_probe(cycle)  # The function raised: no outcome, and the next call probes in its place
            raise
        self.record_success(cycle)
        return result

    def admit(self):
        """None when rejected; otherwise the cycle that admitted the call (the latest one while CLOSED)."""
        if self.state == "OPEN":
            if time.time() - self.last_failure_time <= cooldown:  # cooldown = config value
                return None
            self.start_cycle()  # Try recovery
        if self.state == "HALF_OPEN":
            if self.probes >= half_open_requests:  # probe budget, default 3
                if time.time() - self.half_open_since <= cooldown:
                    return None  # Budget spent: rejected, not a failure
                self.start_cycle()  # Spent and undecided a cooldown after it began: fresh cycle
            self.probes += 1
        return self.cycle

    def start_cycle(self):
        self.state = "HALF_OPEN"
        self.probes = self.successes = 0
        self.half_open_since = time.time()
        self.cycle += 1

    def from_another_cycle(self, cycle):
        # The one rule: a call can still be in flight when its cycle ends (another probe
        # failed, or the spent cycle started over), or it was admitted while CLOSED. While
        # HALF_OPEN, its outcome is ignored. Every other outcome counts toward the current state.
        return self.state == "HALF_OPEN" and cycle != self.cycle

    def record_failure(self, cycle):
        if self.state == "OPEN" or self.from_another_cycle(cycle):
            return  # OPEN ignores failures; HALF_OPEN reopens only on its own probes
        now = time.monotonic()
        # Rolling 60 s window: older failures stop counting, successes never reset it
        self.failure_times = [t for t in self.failure_times if now - t <= 60] + [now]
        self.last_failure_time = time.time()
        if self.state == "HALF_OPEN" or len(self.failure_times) >= threshold:  # threshold = config value
            self.state = "OPEN"  # Recovery failed, or too many failures

    def record_success(self, cycle):
        if self.state != "HALF_OPEN" or self.from_another_cycle(cycle):
            return  # CLOSED counts no successes
        self.successes += 1
        if self.successes >= success_threshold:  # default 3
            self.state = "CLOSED"  # Recovered!
            self.failure_times = []

    def release_probe(self, cycle):
        if self.state == "HALF_OPEN" and cycle == self.cycle:
            self.probes -= 1
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
    # Encryption happens before L2 write; an encryption failure skips the write and never counts toward the breaker
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
# Number of live breakers in each state, per namespace
# (one breaker per decorated function with the circuit breaker enabled)
circuit_breaker_state{namespace="users",state="OPEN"}
```

Alert on `circuit_breaker_state{state="OPEN"} > 0`. See the
[Prometheus Metrics guide](prometheus-metrics.md) for exposition setup.

### Debugging Circuit State
```python notest
# Example of checking circuit breaker state (API may vary)
# Check function's health status instead:
@cache(ttl=300)
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
A: Raise `failure_threshold` or `recovery_timeout`, and look in the logs for the failures that count (listed under [Circuit breaker open](../error-codes.md#circuit-breaker-open)). The usual ones are a Redis that is unreachable when the backend is first built, and the function raising. Read and write errors on a built backend do not currently count.

**Q: My function runs on every call while the circuit is OPEN**
A: That's the OPEN state: calls skip L2 and run your function, and nothing new is written to L1; only values already in L1 are still served. Once the cooldown has passed, the circuit admits probe calls again and closes after 3 successful probes.

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
