**[Home](../README.md)** › **Features** › **Distributed Locking**

# Distributed Locking - Prevent Cache Stampedes

**Available since v0.3.0**

**Related**: See [Architecture: L1+L2 Caching](../data-flow-architecture.md#l1-cache-layer-in-memory) for how distributed locking fits into the overall cache architecture.

## TL;DR

Distributed locking prevents "cache stampede" - when multiple pods simultaneously call an expensive function on cache miss. With locking, only one pod calls the function; others wait for the cache result.

```python
@cache(ttl=300)  # Distributed locking enabled by default (via LockableBackend)
async def expensive_query(key):
    return db.expensive_query(key)

# 1000 pods call simultaneously on L2 miss
# Only 1 pod calls expensive_query()
# 999 pods wait for L2 cache to be populated
```

Locking requires an **async** decorated function and a `LockableBackend`
(`RedisBackend` or `CachekitIOBackend`) — see the note below.

---

## Quick Start

Distributed locking is enabled by default when the backend supports it:

```python notest
from cachekit import cache

@cache(ttl=300)  # Locking active on LockableBackend (e.g. RedisBackend, CachekitIOBackend)
async def get_report(date):
    return db.generate_report(date)  # Expensive operation

# Multiple pods calling simultaneously on cache miss
# Only one executes generate_report()
report = await get_report("2025-01-15")
```

> [!NOTE]
> Locking requires **both** of:
>
> 1. A backend implementing the `LockableBackend` protocol. `RedisBackend` and `CachekitIOBackend` (the SaaS backend behind `api.cachekit.io`) both do. `FileBackend` and pure-L1 (zero-config) caching don't — they silently skip lock acquisition; the function still works, just without stampede protection.
> 2. An **async** decorated function. Sync wrappers never take the lock path on any backend — see [Async-only](#async-only-sync-functions-are-never-lock-protected).

---

## Async-Only: Sync Functions Are Never Lock-Protected

The `LockableBackend` protocol is async-only (`acquire_lock` is an async context
manager), so **only async decorated functions get distributed locking**. The sync
wrapper executes the function directly on cache miss — on every backend, including
Redis and CachekitIO. Twelve concurrent sync callers on a cold key mean twelve
recomputes and zero lock traffic; the same probe through the async wrapper means
exactly one recompute.

If a function is expensive enough that a stampede matters, decorate the async
variant:

```python
import asyncio
from cachekit import cache

@cache(ttl=300)
async def compute_report(report_id):
    return expensive_operation()  # Only one concurrent caller executes this

result = asyncio.run(compute_report("daily"))
assert result["computed"] is True
```

```python notest
# Sync variant of the same function: NO stampede protection.
@cache(ttl=300)
def compute_report_sync(report_id):
    return expensive_operation()  # Every concurrent cold-key caller executes this
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
999 pods wait for lock
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

> [!NOTE]
> Scenarios where locking adds overhead without benefit:
>
> 1. **Inexpensive functions** (<1ms execution): Lock overhead isn't worth it
> 2. **Low concurrency** (1-2 pods): No stampede risk
> 3. **Cache always hits** (TTL never expires): Locking never used

When locking overhead matters, use a backend that doesn't implement `LockableBackend`, or raise the issue — per-decorator toggle is being tracked.

---

## Lock Timing: 30-Second Expiry, 5-Second Wait

The decorator wrapper uses two fixed constants (not currently configurable
per decorator):

| Constant | Value | What it does |
|----------|-------|--------------|
| `lock_timeout` | **30 s** | How long the winner holds the lock before it self-expires. Protects against a crashed holder deadlocking everyone. |
| `blocking_timeout` | **5 s** | How long waiters poll to acquire the lock before giving up. |

Two behavioural edges to design around:

1. **Waiter fallthrough at 5 s.** A waiter that can't acquire the lock within
   5 seconds re-checks the cache one last time and, if it's still empty,
   **executes the function itself without the lock**. So a computation slower
   than 5 seconds gets a bounded number of extra recomputes (one per waiter
   that times out) — degraded stampede protection, not an unbounded stampede,
   and never an error.
2. **Lock self-expiry at 30 s.** If the function runs longer than 30 seconds,
   the lock expires while the winner is still computing and another pod may
   start a concurrent recompute. Keep expensive functions well under 30 s, or
   split the work.

---

## What Can Go Wrong

### Function Slower Than the 5 s Blocking Timeout
```python notest
@cache(ttl=300)
async def operation(x):
    return slow_compute(x)  # Takes 10 seconds

# Waiters poll for 5 s (blocking_timeout), then fall through:
# each re-checks the cache and, still empty, executes the function itself.
# Solution: keep execution time under 5 s for full single-flight behaviour.
```

### Lock Holder Crashes
```python
# Pod A acquires lock
# Pod A crashes while holding lock
# Waiters poll up to 5 s (blocking_timeout), then fall through and recompute;
# the lock itself self-expires after 30 s (lock_timeout) as the safety net
# (Redis key TTL / CachekitIO server-side expiry).
```

### TTL Expires During Lock Wait
```python
@cache(ttl=5)  # 5 second TTL
async def operation(x):
    time.sleep(2)
    return slow_compute(x)  # Takes 2 seconds

# Lock acquired, Pod B waits 2 seconds
# TTL expires while Pod B waits
# Solution: Ensure TTL > function execution time
```

---

## How to Use It

### Basic Usage (Default)
```python notest
@cache(ttl=3600)  # Locking enabled by default on LockableBackend
async def get_leaderboard():
    return db.expensive_leaderboard_query()

# 1000 users request leaderboard simultaneously
# Only 1 computes leaderboard
# 999 wait for result
leaderboard = await get_leaderboard()
```

### With Redis Backend (Explicit)
```python notest
from cachekit import cache
from cachekit.backends.redis import RedisBackend

backend = RedisBackend()  # Implements LockableBackend

@cache(ttl=300, backend=backend)
async def generate_stats(date):
    # Computation takes <5 seconds (blocking_timeout) for full single-flight
    return stats_engine.compute(date)
```

### With CachekitIO Backend (SaaS)
```python notest
from cachekit import cache
from cachekit.backends.cachekitio import CachekitIOBackend

backend = CachekitIOBackend()  # Implements LockableBackend — API-level locking

@cache(ttl=300, backend=backend)
async def generate_stats(date):
    return stats_engine.compute(date)
```

The SaaS lock is protocol-level (`POST /v1/cache/{key}/lock`) and available to
every authenticated API key — no extra configuration or plan tier required.

### Disabling for Cheap Operations
```python notest
# Use a non-LockableBackend for operations where stampede isn't a concern,
# or just accept the minimal overhead — locking only activates on cache miss.

@cache(ttl=300)
def cheap_lookup(x):
    # <1ms operation; even if 1000 pods hit simultaneously, DB load is trivial
    return simple_dict.get(x)
```

---

## Technical Deep Dive

### Lock Implementation (LockableBackend Protocol)

The `LockableBackend` protocol defines how backends provide distributed locking:

```python notest
async def acquire_lock(
    self,
    key: str,              # Lock key, e.g. "lock:function_name:args_hash"
    timeout: float,        # How long to hold the lock (seconds)
    blocking_timeout: Optional[float] = None,  # Max wait to acquire (None = non-blocking)
) -> AsyncIterator[bool]:
    # Yields True if lock acquired, False if timeout waiting
    ...
```

The decorator wrapper calls it with `timeout=30.0` (lock self-expiry) and
`blocking_timeout=5.0` (max wait to acquire) — see
[Lock Timing](#lock-timing-30-second-expiry-5-second-wait).

**Lock flow (Redis)**:
```
1. Try to SET lock key (NX - only if not exists)
2. If SET succeeds → lock acquired, yield True
3. If SET fails → lock held, wait up to blocking_timeout
4. On context exit: DEL lock key (only if still holder)
   Lock auto-expires via Redis TTL if holder crashes
```

**Lock flow (CachekitIO / SaaS)**:
```
1. POST /v1/cache/{key}/lock with {"timeout_ms": 30000}
2. Response carries a lock_id → lock acquired, yield True
3. Lock held elsewhere → client polls the endpoint with exponential
   backoff + jitter (50 ms doubling to a 500 ms cap) until
   blocking_timeout elapses. Each poll is a billable API request.
4. On context exit: DELETE /v1/cache/{key}/lock with the lock_id header
   (only the holder's lock_id releases it)
   Server-side expiry (timeout_ms) is the safety net if the holder crashes
```

### Integration with Cache Layers
```
L1 miss, L2 miss detected
Distributed lock acquisition begins (via backend.acquire_lock)
Only one pod wins lock
That pod calls function
Function executes
Result written to L1 and L2
Lock released
Other pods read from L2 (now populated)
```

### Performance Impact
- **Lock already held**: Waiters poll with exponential backoff for up to `blocking_timeout` (5 s)
- **Lock acquisition**: <10ms (Redis SET NX operation)
- **Lock release**: <5ms (Redis DEL operation)
- **Waiting cost**: Function execution cost saved * (pods_waiting - 1)

**Example**: 1000 pods, 10s function call, 999 waiting
- Cost without locking: 10,000 seconds total CPU
- Cost with locking: 10 seconds + lock overhead ≈ ~60 seconds total CPU
- Savings: 99.4% reduction

---

## Interaction with Other Features

**Distributed Locking + Circuit Breaker**:
```python
@cache(ttl=300)  # Both enabled
async def operation(x):
    # L2 backend down while holding lock
    # Circuit breaker catches error
    # Lock TTL ensures lock eventually expires
    return compute(x)
```

**Distributed Locking + Encryption**:
```python notest
@cache.secure(ttl=300)  # Both enabled
async def fetch_sensitive(x):
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
# → Lock timeout is too short relative to function execution time
```

---

## Troubleshooting

**Q: Getting "Failed to acquire lock" warnings**
A: Your function takes longer than the 5 s `blocking_timeout`, so waiters fall through and recompute. Keep execution time under 5 s for full single-flight behaviour (and under 30 s so the lock doesn't self-expire mid-computation).

**Q: Locking doesn't seem to be working**
A: Two things to check:
1. The decorated function must be **async** — sync wrappers never lock ([Async-only](#async-only-sync-functions-are-never-lock-protected)).
2. The backend must implement `LockableBackend` (`RedisBackend`, `CachekitIOBackend`). Check with `from cachekit.backends.base import LockableBackend; isinstance(backend, LockableBackend)`.

**Q: How do I know if stampedes are happening?**
A: Check Prometheus: `rate(cachekit_cache_misses_total[1m])` spike = stampede risk.

---

## See Also

- [Circuit Breaker](circuit-breaker.md) - Prevents cascading failures
- [Prometheus Metrics](prometheus-metrics.md) - Monitor lock performance
- [Comparison Guide](../comparison.md) - Only cachekit + dogpile.cache have locking

---

<div align="center">

**[GitHub Issues](https://github.com/cachekit-io/cachekit-py/issues)** · **[Documentation](../README.md)**

</div>
