**[Home](../README.md)** › **Features** › **Distributed Locking**

# Distributed Locking - Prevent Cache Stampedes

**Available since v0.3.0**

**Related**: See [Architecture: L1+L2 Caching](../data-flow-architecture.md#l1-cache-layer-in-memory) for how distributed locking fits into the overall cache architecture.

## TL;DR

Distributed locking prevents "cache stampede" - when multiple pods simultaneously call an expensive function on cache miss. With locking, only one pod calls the function; others wait for the cache result. Inside one pod, concurrent misses on a key never reach the lock more than once: they share one trip ([in-process single-flight](#in-process-single-flight-before-the-lock)), so the lock only dedups across processes.

```python
@cache(ttl=300)  # Distributed locking enabled by default (via LockableBackend)
async def expensive_query(key):
    return db.expensive_query(key)

# 1000 pods call simultaneously on L2 miss
# Only 1 pod calls expensive_query()
# 999 pods wait for L2 cache to be populated
```

---

## Quick Start

Distributed locking is enabled by default when the backend supports it:

```python notest
from cachekit import cache

@cache(ttl=300)  # Locking active on LockableBackend (e.g. the tenant-scoped Redis backend, CachekitIOBackend)
async def get_report(date):
    return db.generate_report(date)  # Expensive operation

# Multiple pods calling simultaneously on cache miss
# Only one executes generate_report()
report = await get_report("2025-01-15")
```

> [!NOTE]
> Locking requires **both** of:
>
> 1. A backend implementing the `LockableBackend` protocol. `CachekitIOBackend` (the SaaS backend behind `api.cachekit.io`) does, and so does the tenant-scoped Redis backend you get from env auto-detection or `RedisBackendProvider(...).get_shared_backend()`. A `RedisBackend` you construct yourself and pass as `backend=` does **not** — it has no `acquire_lock`. Neither do `FileBackend` or pure-L1 (zero-config) caching. All of them silently skip lock acquisition; the function still works, just without cross-process stampede protection. Concurrent misses inside one process still share one call ([in-process single-flight](#in-process-single-flight-before-the-lock)).
> 2. An **async** decorated function. Sync wrappers never take the lock path on any backend — see [Async-only](#async-only-sync-functions-are-never-lock-protected).

---

## In-Process Single-Flight (Before the Lock)

Concurrent misses on one key inside one process share one call. The first caller to miss starts it, and every caller that misses the same key while it runs waits for its outcome instead of starting its own. In async backed mode the shared call is the whole trip after the L1 miss (the L2 read, the lock, the function and the write), so a herd in one process costs one trip and the lock only has to dedup across processes. It is always on and costs no round trip. The exception is a trip that leaves no serialized value to share: a value the serializer rejects, a hit read through the mmap fast path, or a lock release that failed after the write. Each joined caller then takes its own trip once the first one ends, so that herd costs one trip per caller, as it did before.

| Mode | Sync | Async | A caller that joins gets |
|------|------|-------|--------------------------|
| `@cache(backend=None)` (L1-only) | Yes | Yes | The starter's object, as a later hit does |
| `@cache.local()` | Yes | Yes | The starter's object, as a later hit does |
| Backed (`@cache` with a backend, `@cache.io`, `@cache.secure`, ...) | No | Yes | Its own copy, decoded from the starter's serialized value, as an L1 hit does |

The sync backed path is not coalesced: each sync caller that misses reads L2 and runs the function (see [Async-only](#async-only-sync-functions-are-never-lock-protected)).

- **Failure.** If the call raises, every caller waiting on it gets that exception, nothing is cached, and the next call starts a new one.
- **Cancellation.** Cancelling an async caller (a timeout, a dropped client) cancels only its own wait: the call runs on for the callers still waiting. Once every waiting caller is cancelled, the call is cancelled too, and the last caller returns once it has unwound, as an unshared call would. On Python 3.11 and later, a joined caller whose shared call was cancelled under it (the function raised `CancelledError`), without being cancelled itself, runs its own call, as it would have alone.
- **Invalidation.** A read that starts after `invalidate_cache()` (or `cache_clear()`) returns never joins a miss that started before it. That earlier miss still stores its result when it finishes, as any [write in flight](l1-invalidation.md#whole-function-invalidation) does.
- **Statistics.** In `cache_info()`, a caller that joins and gets the value counts as an L1 hit; one whose shared call raised counts as neither. The miss is the call that ran the function.
- **Isolation.** Callers share a call only under the same backend key prefix and, with multi-tenant encryption (`tenant_extractor`), the same tenant: no caller gets another tenant's value or exception. A caller whose tenant cannot be extracted shares nothing. A joined caller decodes its copy under its own tenant, and takes its own trip if that fails.
- **Circuit breaker.** A joined caller makes no backend request and records no outcome. In HALF_OPEN it hands its probe slot straight back, so a herd spends one probe.
- **Event loops.** Async callers share a call only on the event loop running it; a caller on another thread's loop runs its own.
- **Task and context.** Every async miss runs the function in its own task, a lone caller's miss included, in a copy of the starting caller's context. A context variable the function sets is not visible to its caller afterwards. Anything scoped by `asyncio.current_task()` binds to that short-lived task, not to the caller's: SQLAlchemy's `async_scoped_session(scopefunc=current_task)`, used inside the function, creates a new session per miss, which the caller's `remove()` at the end of its request never cleans up. Pass such resources in as arguments, or scope them by a context variable instead.
- **Waiting on yourself.** A function must not wait on another thread or task that calls it with the same arguments: that would wait on its own call. A direct recursive call on the same thread or task runs on its own.

---

## Async-Only: Sync Functions Are Never Lock-Protected

The `LockableBackend` protocol is async-only (`acquire_lock` is an async context
manager), so **only async decorated functions get distributed locking**. The sync
wrapper executes the function directly on cache miss — on every backend, including
Redis and CachekitIO — and its backed path has no in-process single-flight either.
Twelve concurrent sync callers on a cold key mean twelve recomputes and zero lock
traffic; the same probe through the async wrapper means exactly one recompute, one
L2 read and one lock request.

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

**Real example**: News site, trending story expires from cache
- Without locking: 10,000 requests = 10,000 DB queries
- With locking: 10,000 requests = 1 DB query

Single-flight behaviour assumes the function completes within the 5 s
`blocking_timeout` and the lock backend is reachable — see
[Lock Timing](#lock-timing-30-second-expiry-5-second-wait) for the
degradation paths.

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

Three behavioural edges to design around:

1. **Waiter fallthrough at 5 s.** A waiter that can't acquire the lock within
   5 seconds re-checks the cache one last time and, if it's still empty,
   **executes the function itself without the lock**. Only one caller per
   process waits on the lock (the others share its trip), so the bound is one
   recompute per waiting process: for a function slower than 5 seconds, every
   waiting process times out and recomputes, so cross-process protection is
   effectively nil. Keep execution time under 5 s for full single-flight
   behaviour.
2. **Lock self-expiry at 30 s.** If the function runs longer than 30 seconds,
   the lock expires while the winner is still computing and another pod may
   start a concurrent recompute. This applies whether the holder is slow or
   crashed — the expiry is the crash-recovery safety net (Redis key TTL /
   CachekitIO server-side expiry). Keep expensive functions well under 30 s,
   or split the work.
3. **Lock backend errors degrade to no lock.** If lock acquisition raises a
   backend error (lock backend outage, authentication failure), the wrapper
   logs a warning and **executes the function without the lock** — once per
   process, since a process's concurrent callers share one trip, i.e. a
   stampede across processes. A failed lock request is not retried; only a
   lock another caller holds is waited on. A failed release never runs the
   function again: the Redis and CachekitIO backends swallow a backend error
   on release, any other release failure is logged and the call keeps its
   result, and the lock may stay until its 30 s timeout. The lock is
   best-effort stampede mitigation, never load-bearing mutual exclusion: do
   not rely on it for correctness of non-idempotent operations.

---

## What Can Go Wrong

### Lock Holder Crashes
```python
# Pod A acquires lock
# Pod A crashes while holding lock
# Waiters poll up to 5 s (blocking_timeout), then fall through and recompute;
# the lock itself self-expires after 30 s (lock_timeout) as the safety net.
```

On the tenant-scoped Redis backend and `CachekitIOBackend`, cancelling the task mid-`acquire_lock`
does not orphan the lock: the in-flight acquire (the `SET NX`, or the
`POST …/lock` request) and the release both run to completion — however many
cancellations land — before the `CancelledError` propagates. The cancellation
always wins over an acquire that failed meanwhile; the failure is logged. Only
the backend failing the release leaves the lock, until the same 30 s timeout as
the crash case above. One limit on `CachekitIOBackend`: its lock requests are drained
as asyncio Tasks, and `asyncio.run()` teardown cancels every Task, so a request
cut short there falls back on that same server-side timeout. The decorator's own
release is the exception (see below): it is sent from a worker thread, which
`asyncio.run()` waits for, so it lands even when the decorated call is the last
thing the process does, and even if `close_http_clients()` runs straight after. On CPython 3.10.0-3.10.7 and 3.11.0 only, a loop closed by hand
(`loop.close()`, not `asyncio.run()`) before that release finishes logs one
`concurrent.futures` "Event loop is closed" error; the release still lands.

### Request Count on CachekitIO

On `CachekitIOBackend`, an uncontended async miss makes three requests the caller
waits on: the read, the lock `POST`, and the write. Two steps are trimmed:

- **No re-read when nobody else held the lock.** The post-lock re-read only
  runs when the lock was granted after a wait, or when the first read failed
  (the entry may still be live, and the re-read is its retry). When the first
  lock request wins after a clean miss, this caller never waited behind another
  holder, so the re-read would almost always miss again and be billed as a miss.
  A fill that completes between the read and the lock request is not seen: the
  value is recomputed and written again, and the last write wins.
- **The release does not block the return.** The `DELETE …/lock` is sent in the
  background once the value is stored, as cachekit-ts does. Waiters in other
  processes see the lock released just as before. If the event loop's default
  executor has already been shut down, the release is sent inline on the event
  loop's thread instead, one blocking round trip, so the lock is still released.

The Redis backend keeps both steps: its release is one Redis round trip.

### TTL Shorter Than Compute Time
```python
@cache(ttl=1)  # 1 second TTL
async def operation(x):
    return slow_compute(x)  # Takes 2 seconds

# Winner computes for 2 s; the cached value expires 1 s after the write,
# so waiters that fell through keep finding an empty cache.
# Solution: Ensure TTL comfortably exceeds function execution time
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
from cachekit.backends.redis.provider import RedisBackendProvider

# The tenant-scoped Redis backend implements LockableBackend; a bare RedisBackend() does not.
# It reads tenant_context on every operation, so one instance serves every request;
# get_shared_backend() scopes a context with no tenant set to "default".
backend = RedisBackendProvider(redis_url="redis://localhost:6379").get_shared_backend()

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

The SaaS lock is available to every authenticated API key — no extra
configuration or plan tier required.

### Disabling for Cheap Operations
```python notest
# Use a non-LockableBackend for operations where stampede isn't a concern,
# or just accept the minimal overhead — locking only activates on cache miss.
# (A sync function like this never locks anyway — zero lock overhead.)

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
def acquire_lock(
    self,
    key: str,              # Bare cache key (same key as get/set); backend derives lock namespace
    timeout: float,        # How long to hold the lock (seconds)
    blocking_timeout: Optional[float] = None,  # Max wait to acquire (None = non-blocking)
) -> AbstractAsyncContextManager[bool]:
    # Entering the context yields True if the lock was acquired, False if the wait timed out
    ...
```

Implementations are `async` generators wrapped in `@asynccontextmanager`, so the
protocol declares the *decorated* shape — a backend author must apply the
decorator for `async with` to work.

The decorator wrapper calls it with `timeout=30.0` (lock self-expiry) and
`blocking_timeout=5.0` (max wait to acquire) — see
[Lock Timing](#lock-timing-30-second-expiry-5-second-wait).

**Lock flow (Redis)**:
```
1. Try to SET lock key (NX - only if not exists)
2. If SET succeeds → lock acquired, yield True
3. If SET fails → lock held, retry every 0.1 s for up to blocking_timeout.
   Each retry is one non-blocking SET NX; the wait between retries is an
   asyncio.sleep on the event loop, so a waiter never holds an executor thread
   while waiting between attempts
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

### Performance Impact
- **Lock already held**: Waiters poll for up to `blocking_timeout` (5 s) — fixed 0.1 s interval on Redis, exponential backoff + jitter on CachekitIO
- **Lock acquisition**: <10ms (Redis SET NX operation)
- **Lock release**: <5ms (Redis DEL operation)
- **Waiting cost**: Function execution cost saved * (pods_waiting - 1)

**Example**: 1000 pods, 3s function call, 999 waiting
- Cost without locking: 3,000 seconds total CPU
- Cost with locking: 3 seconds + lock overhead
- (A 10s function would gain nothing: every waiter falls through at 5 s and
  recomputes — see [Lock Timing](#lock-timing-30-second-expiry-5-second-wait).)

---

## Interaction with Other Features

**Distributed Locking + Circuit Breaker**:
```python
@cache(ttl=300)  # Both enabled
async def operation(x):
    # L2 backend down: lock acquisition fails too (the lock lives in L2),
    # so each process's concurrent callers share one call without the lock
    # and the function keeps serving. Locking resumes when the backend recovers.
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

Lock waiters that time out log a `Failed to acquire lock for {key} after 5.0s`
warning; lock backend errors log a `Lock operation failed … executing without
lock` warning. For miss-rate monitoring (stampede detection), watch `operation="set"` on
`cache_operations_total` as a cache-write proxy — misses write back, but failed writes
record nothing, so treat it as a proxy, not an exact miss count — see
[Prometheus Metrics](prometheus-metrics.md).

---

## Troubleshooting

**Q: Getting "Failed to acquire lock" warnings**
A: Your function takes longer than the 5 s `blocking_timeout`, so waiting processes fall through and recompute. Keep execution time under 5 s for full single-flight behaviour (and under 30 s so the lock doesn't self-expire mid-computation).

**Q: Locking doesn't seem to be working**
A: Two things to check:
1. The decorated function must be **async** — sync wrappers never lock ([Async-only](#async-only-sync-functions-are-never-lock-protected)).
2. The backend must implement `LockableBackend` (`CachekitIOBackend`, or the env-resolved / `RedisBackendProvider(...).get_shared_backend()` Redis backend — a bare `RedisBackend()` does not). Check it the way the SDK does: `hasattr(backend, "acquire_lock")`. Avoid `isinstance(backend, LockableBackend)` — from CPython 3.12 a `runtime_checkable` protocol check resolves members with `inspect.getattr_static`, so it reports `False` for a backend that delegates `acquire_lock` through `__getattr__`.

**Q: How do I know if stampedes are happening?**
A: Check Prometheus: a spike in `rate(cache_operations_total{operation="set"}[1m])` (a cache-write proxy — misses write back) suggests stampede risk. See [Prometheus Metrics](prometheus-metrics.md).

---

## See Also

- [Circuit Breaker](circuit-breaker.md) - Prevents cascading failures
- [Prometheus Metrics](prometheus-metrics.md) - Monitor lock performance
- [Comparison Guide](../comparison.md) - Only cachekit + dogpile.cache have locking

---

<div align="center">

**[GitHub Issues](https://github.com/cachekit-io/cachekit-py/issues)** · **[Documentation](../README.md)**

</div>
