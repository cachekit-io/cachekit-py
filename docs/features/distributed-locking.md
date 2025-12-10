**[Home](../README.md)** › **Features** › **Distributed Locking**

# Distributed Locking - Prevent Cache Stampedes

**Version**: cachekit v1.0+

**Related**: See [Architecture: L1+L2 Caching](../data-flow-architecture.md#l1-cache-layer-in-memory) for how distributed locking fits into the overall cache architecture.

## TL;DR

Distributed locking prevents "cache stampede" - when multiple pods simultaneously call expensive function on cache miss. With locking, only one pod calls function, others wait for cache result.

```python
@cache(ttl=300)  # Distributed locking enabled by default
def expensive_query(key):
    return db.expensive_query(key)

# 1000 pods call simultaneously on L2 miss
# Only 1 pod calls expensive_query()
# 999 pods wait for L2 cache to be populated
```

---

## Quick Start

Distributed locking enabled by default:

```python
from cachekit import cache

@cache(ttl=300)  # Locking active
def get_report(date):
    return db.generate_report(date)  # Expensive operation

# Multiple pods calling simultaneously on cache miss
# Only one executes generate_report()
report = get_report("2025-01-15")
```

**Configuration** (optional):
```python notest
@cache(
    ttl=300,
    distributed_locking_enabled=True,  # Default: True
    distributed_locking_timeout_seconds=30,  # Timeout waiting for lock
)
def operation(x):
    return compute(x)
```

---

## What It Does

**Cache stampede scenario**:
```
Cache miss happens (L1 and L2 miss)
1000 pods call expensive function simultaneously
→ 1000 times load on database (BAD)
→ Database overloaded, queries slow/fail (BAD)
→ Cache takes longer to populate (BAD)
→ More stampedes happen (BAD cascade)

With distributed locking:
1000 pods call expensive function
Distributed lock acquired by Pod A
999 pods wait for lock (in memory, ~50ns checks)
Pod A calls function once
Pod A populates L2 cache
Pod A releases lock
999 pods wake up, read from L2 cache
→ Function called 1 time instead of 1000 (GOOD)
→ Database handles 1 query instead of 1000 (GOOD)
```

---

## Why You'd Want It

**Production scenario**: Popular data being cached. Cache expires simultaneously across all pods.

**Without locking**:
```
Cache miss
1000 pods hit database simultaneously
Database gets 1000 queries for same data
Database overloaded
Queries timeout
Stampede cascades
```

**With locking**:
```
Cache miss
1000 pods contend for lock
1 pod wins, queries database (normal load)
999 pods wait
Database serves 1 query
Lock released, cache populated
999 pods read from cache
No overload, no cascade
```

**Real example**: News site, trending story expires from cache
- Without locking: 10,000 requests = 10,000 DB queries
- With locking: 10,000 requests = 1 DB query

---

## Why You Might Not Want It

**Scenarios where locking adds overhead**:

1. **Inexpensive functions** (<1ms execution): Lock overhead isn't worth it
2. **Low concurrency** (1-2 pods): No stampede risk
3. **Cache always hits** (TTL never expires): Locking never used

**Mitigation**: Disable if stampedes don't matter:
```python notest
@cache(ttl=300, distributed_locking_enabled=False)
def cheap_operation(x):
    return simple_compute(x)
```

---

## What Can Go Wrong

### Lock Timeout (Deadlock)
```python notest
@cache(ttl=300, distributed_locking_timeout_seconds=5)
def operation(x):
    return slow_compute(x)  # Takes 10 seconds
# Problem: Lock times out after 5s, pod falls through
# Solution: Increase timeout to 30+ seconds
```

### Lock Holder Crashes
```python
# Pod A acquires lock
# Pod A crashes while holding lock
# 999 pods wait forever (or until timeout)
# Solution: Redis expiry + timeout handles this
```

### TTL Expires During Lock Wait
```python
@cache(ttl=5)  # 5 second TTL
def operation(x):
    time.sleep(2)
    return slow_compute(x)  # Takes 2 seconds
# Lock acquired, Pod B waits 2 seconds
# TTL expires while Pod B waits
# Solution: Ensure timeout < TTL
```

---

## How to Use It

### Basic Usage (Default)
```python notest
@cache(ttl=3600)  # Locking enabled by default
def get_leaderboard():
    return db.expensive_leaderboard_query()

# 1000 users request leaderboard simultaneously
# Only 1 computes leaderboard
# 999 wait for result
leaderboard = get_leaderboard()
```

### With Expiration-Safe Configuration
```python notest
@cache(
    ttl=300,  # 5 minute cache
    distributed_locking_timeout_seconds=30,  # 30s timeout
)
def generate_stats(date):
    # Computation takes <30 seconds
    return stats_engine.compute(date)
```

### Disabling for Cheap Operations
```python notest
@cache(ttl=300, distributed_locking_enabled=False)
def cheap_lookup(x):
    # <1ms operation, no stampede risk
    return simple_dict.get(x)

# vs.

@cache(ttl=300)  # Locking enabled
def expensive_query(x):
    # 10s+ operation, stampede risk high
    return db.complex_query(x)
```

---

## Technical Deep Dive

### Lock Implementation
```
Lock key: f"cache_lock:{function_name}:{args_hash}"
Lock value: UUID (identifies lock holder)
Lock TTL: timeout_seconds + function_execution_time + buffer

Acquisition flow:
1. Try to SET lock key (NX - only if not exists)
2. If SET succeeds → lock acquired
3. If SET fails → lock held, wait for it

Wait flow:
1. Poll lock key existence (~50ms intervals)
2. If lock released → proceed
3. If timeout expires → raise error or fallback

Release flow:
1. Delete lock key (only if we still hold it)
2. All waiting pods wake up immediately
```

### Integration with Cache Layers
```
L1 miss, L2 miss detected
Distributed lock acquisition begins
Only one pod wins lock
That pod calls function
Function executes
Result written to L1 and L2
Lock released
Other pods read from L2 (now populated)
```

### Performance Impact
- **Lock already held**: ~50ms check interval
- **Lock acquisition**: <10ms (Redis SET operation)
- **Lock release**: <5ms (Redis DEL operation)
- **Waiting cost**: Function execution cost saved * (pods_waiting - 1)

**Example**: 1000 pods, 10s function call, 999 waiting
- Cost without locking: 10,000 seconds total CPU
- Cost with locking: 10 seconds + 50ms * 999 = ~60 seconds total CPU
- Savings: 99.4% reduction

---

## Interaction with Other Features

**Distributed Locking + Circuit Breaker**:
```python
@cache(ttl=300)  # Both enabled
def operation(x):
    # Redis down while holding lock
    # Circuit breaker catches error
    # Lock TTL ensures lock eventually expires
    return compute(x)
```

**Distributed Locking + Encryption**:
```python notest
@cache.secure(ttl=300)  # Both enabled
def fetch_sensitive(x):
    # Lock protects function execution
    # Encryption happens on write to L2
    # Both work transparently together
    return compute(x)
```

---

## Monitoring & Debugging

### Metrics Available
```prometheus
cachekit_lock_acquisitions_total{function="get_leaderboard"}
  # How many times lock was acquired

cachekit_lock_timeouts_total{function="get_leaderboard"}
  # How many times lock timeout occurred

cachekit_lock_wait_duration_seconds{function="get_leaderboard"}
  # How long waiting pods waited for lock
```

### Detecting Stampedes
```python
# If metrics show:
# - High cache_misses_total
# - Low lock_acquisitions_total (relative to misses)
# → Stampede is happening

# Check log:
# - "lock_timeout" errors
# → Lock timeout is too short
```

---

## Troubleshooting

**Q: Getting "lock_timeout" errors**
A: Increase `distributed_locking_timeout_seconds` to be > function execution time

**Q: Want to disable locking for specific function**
A: Pass `distributed_locking_enabled=False` or env: `CACHEKIT_DISTRIBUTED_LOCKING_ENABLED=false`

**Q: How do I know if stampedes are happening?**
A: Check Prometheus: `rate(cachekit_cache_misses_total[1m])` spike = stampede risk

---

## See Also

- [Circuit Breaker](circuit-breaker.md) - Prevents cascading failures
- [Adaptive Timeouts](adaptive-timeouts.md) - Auto-tune Redis timeouts
- [Prometheus Metrics](prometheus-metrics.md) - Monitor lock performance
- [Comparison Guide](../comparison.md) - Only cachekit + dogpile.cache have locking

---

<div align="center">

*Last Updated: 2025-12-02 · ✅ Feature implemented and tested*

</div>
