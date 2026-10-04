**[Home](../README.md)** › **Features** › **L1 Cache Invalidation**

# L1 Cache Invalidation and Stale-While-Revalidate (SWR)

> L1 invalidation and SWR freshness management are **process-local**. When an L2 backend is configured, invalidating a key also deletes it from shared L2 — but other processes keep serving their own L1 copy until it expires (L1 TTL), unless they run the opt-in [invalidation listener](#cross-process-l1-eviction). On the tenant-scoped Redis backend (env auto-detection, or a backend from `RedisBackendProvider`), whole-function invalidation also deletes the L2 entries *other* processes wrote for the calling tenant ([key registry](#whole-function-invalidation)), and the listener runs only on that backend. In L1-only mode (`backend=None`) invalidation is purely local. See [Multi-Instance Semantics](#multi-instance-semantics).

> [!IMPORTANT]
> The within-TTL SWR described on this page runs **only in L1-only mode** (`backend=None`): past the freshness threshold, the SDK serves the cached value and **re-runs your function** in the background. With a backend configured (Redis, File, Memcached), `swr_enabled` has no effect — there is no within-TTL SWR in backed modes. The one backed SWR that exists is `@cache.io`'s past-TTL [`stale_ttl` mode](../configuration.md#stale-while-revalidate-stale_ttl), which uses the CachekitIO backend's read-side freshness signal.

---

## Freshness vs Expiry: Two Distinct Timers

L1 cache behavior is governed by **two independent timers**:

```
Time →  T0          T1800 (50%)        T3600 (100%)
        │             │                  │
        ▼             ▼                  ▼
        ┌─────────────┬──────────────────┐
        │   FRESH     │      STALE       │ EXPIRED (deleted)
        │  (serve)    │ (serve + refresh)│
        └─────────────┴──────────────────┘
                      ↑                  ↑
               refresh_threshold     expires_at (TTL)
```

| Timer | Controls | Behavior |
|-------|----------|----------|
| **Freshness** | When to refresh | Serve immediately + trigger background refresh |
| **Expiry** | When to delete | Hard deadline - entry removed from cache |

### Key Concept: A Successful Refresh Restarts Both Timers

A background refresh **re-runs your function** and stores the fresh result — there is no other source of truth in L1-only mode, so the refreshed entry restarts **both** the freshness clock and the hard-expiry deadline:

```python
# Original cache entry (1 hour TTL)
cached_at = 0
expires_at = 3600  # Hard expiry time

# At T=1800 (50% of TTL): caller gets a hit, SWR triggers
# Returns the cached value immediately
# Background re-run of your function completes at T=1850
cached_at = 1850   # Freshness clock restarts
expires_at = 5450  # Hard expiry restarts too (1850 + 3600)
```

If the background refresh fails (your function raises), the entry is left as-is: the cached value keeps being served until its original hard expiry, the next qualifying hit retries the refresh, and cachekit logs a WARNING `L1-only SWR refresh failed` (see [SWR refresh failing](#problem-swr-refresh-failing)).

---

## Stale-While-Revalidate (SWR) Explained

SWR is an optimization that improves perceived latency by serving the cached value while **re-running your function** in the background to compute a fresh one.

### SWR State Machine

For any cached entry, there are three possible states:

```
            fresh_threshold = cached_at + (TTL * swr_threshold_ratio * jitter)
                                                      ↓
Time ──────────────┬──────────────────┬──────────────┬─────────────────→
                   │                  │              │
              cached_at              stale        expired
                   │                  │              │
          ┌─────────────┐  ┌──────────────────┐  ┌───────┐
          │   FRESH     │  │     STALE        │  │DELETE │
          │   (serve)   │  │ (serve + refresh)│  │ MISS  │
          └─────────────┘  └──────────────────┘  └───────┘
                               ↓
                        Background refresh
```

**Three states on cache hit:**

1. **FRESH** (elapsed < threshold):
   - Return cached value immediately
   - No background refresh

2. **STALE** (threshold < elapsed < TTL):
   - Return cached value immediately  ← Fast!
   - Trigger background refresh (non-blocking)
   - Version token prevents race conditions

3. **EXPIRED** (elapsed > TTL):
   - Entry deleted from cache
   - Full cache miss → call original function

### Configuring SWR

SWR is controlled by two settings:

```python
from cachekit import cache
from cachekit.config import L1CacheConfig

# Default: SWR enabled, refresh at 50% of TTL.
# A ttl is required — with ttl=None entries never go stale, so SWR never fires.
@cache(ttl=3600, backend=None)
def my_function():
    """SWR configured with defaults."""
    pass

# Custom: Refresh at 25% of TTL (refresh more frequently)
@cache(
    ttl=3600,
    l1=L1CacheConfig(
        swr_enabled=True,
        swr_threshold_ratio=0.25  # Refresh at 25% of TTL
    ),
    backend=None
)
def aggressive_refresh():
    """Refreshes more often, better freshness."""
    pass

# Disable SWR: no background refresh
@cache(
    ttl=3600,
    l1=L1CacheConfig(
        swr_enabled=False
    ),
    backend=None
)
def always_fresh():
    """Serves the cached value until hard expiry, then re-runs synchronously."""
    pass
```

### Jitter: Preventing Thundering Herd

Only one refresh runs per key at a time — an in-flight marker dedups concurrent triggers for the same key. But when many *keys* were cached together, they all cross the stale threshold together, and their refreshes would re-run many functions at once.

CacheKit applies **jitter** (±10% randomness) to the threshold to stagger refreshes across keys:

```python
import random

ttl, swr_threshold_ratio = 3600, 0.5

# Without jitter: 1000 keys cached at T=0 all refresh at T=1800
# With jitter: Refreshes spread from T=1620 to T=1980
refresh_threshold = ttl * swr_threshold_ratio * random.uniform(0.9, 1.1)
assert 1620 <= refresh_threshold <= 1980
```

This is automatic and transparent - no configuration needed.

---

## Invalidation API

Invalidation is exposed per decorated function via `invalidate_cache()`:

### Specific Call Invalidation

Clear the cache for a **specific function call**:

```python
from cachekit import cache

calls = []

# backend=None keeps this example self-contained; with a backend the same calls also delete from L2.
@cache(backend=None)
def get_user(user_id: int):
    calls.append(user_id)
    return fetch_from_database(user_id)

get_user(123)
get_user(456)

# Clear cache only for user #123: pass the arguments the way the cached call did
get_user.invalidate_cache(123)
get_user(123)  # recomputed
get_user(456)  # still cached
assert calls == [123, 456, 123]

# A different spelling of the same call is a different entry, so this leaves get_user(123) cached
get_user.invalidate_cache(user_id=123)
get_user(123)
assert calls == [123, 456, 123]

# Clear cache for multiple users
for uid in [1, 2, 3]:
    get_user.invalidate_cache(uid)
```

**Spell the call the same way.** A generated key is built from the arguments as passed, not as bound to the function's signature, so `get_user(123)` and `get_user(user_id=123)` are two cache entries, and a call that leaves a defaulted parameter out is a different entry from one that passes the default. `invalidate_cache(...)` removes only the entry for the spelling you give it. When callers use more than one spelling, invalidate each one, or use the no-args form below.

**Use cases:**
- Single record update
- User data refresh
- Post cache invalidation

**Effect:** The entry is removed from this process's L1 cache **and**, when an L2 backend is configured, deleted from shared L2. Cache keys are deterministic, so the L2 delete removes the entry no matter which process wrote it. On a generated key with a non-default serializer it also deletes the key a pre-v0.20.0 release wrote for the same arguments (see [the v0.20.0 key change](../serializers/README.md#breaking-change-in-v0200-the-key-carries-the-real-serializer)). In L1-only mode (`backend=None`) there is no L2 to delete from — the invalidation is purely local. If the L2 delete fails, cachekit logs an ERROR `Failed to delete L2 key` and tracks the key in this process, so a later no-args `invalidate_cache()` from the same process retries it. On the backends the [whole-function table](#whole-function-invalidation) marks "any process", a key whose write was tracked also stays in the server-side set, so a later no-args drain from any process retries it, within the `Key tracking failed` and **Set lifetime** caveats below. On the backends it marks "this process", tracking is process-local: another process, or this one after a restart, does not retry it.

### Whole-Function Invalidation

Calling `invalidate_cache()` with **no arguments** on a parameterized function clears every cached entry for that function. How far "every" reaches depends on the backend:

```python
calls = []

@cache(backend=None)
def get_user(user_id: int):
    calls.append(user_id)
    return fetch_from_database(user_id)

get_user(1)
get_user(2)

# Clear all get_user entries (L1, and L2 when a backend is configured)
get_user.invalidate_cache()
get_user(1)
get_user(2)
assert calls == [1, 2, 1, 2]
```

| Backend the decorator resolves | L2 entries deleted |
|--------------------------------|--------------------|
| Redis from `CACHEKIT_REDIS_URL` / `REDIS_URL` (the default, no `backend=`), or a backend from `RedisBackendProvider` | Every entry **any process** wrote for the function, for the calling tenant |
| `RedisBackend` passed as `backend=`, File, Memcached, CachekitIO | Only entries **this process** wrote or read |
| L1-only (`backend=None`) | No L2; this process's L1 only |

On a generated key with a non-default serializer, each entry this process wrote or read also takes with it the key a pre-v0.20.0 release wrote for the same arguments (see [the v0.20.0 key change](../serializers/README.md#breaking-change-in-v0200-the-key-carries-the-real-serializer)). The pre-v0.20.0 key of an entry only another process, or this one before a restart, saw is not reached, with one exception on tenant-scoped Redis: a v0.20.0 or later decorator on the default serializer for the same function, namespace, tenant and `integrity_checking` setting writes that same `:{integrity_flag}s` key and registers it, so the drain deletes it while the registration lasts (see **Set lifetime** below).

**Round trips on the "this process" backends.** A plain `RedisBackend` deletes the process's keys with one `UNLINK` per 10 000 keys. Memcached groups them by server and sends one pipelined, acknowledged `delete` batch per server per 1 000 keys. File deletes one key per call. CachekitIO has no bulk delete, so it sends one `DELETE` per key, up to 16 at a time: 100 keys take about 7 round trips instead of 100. Each `DELETE` still counts against your tier's rate limit, and 16 at a time spends it much faster than one at a time. So once a `DELETE` is rate limited (a 429 with `Retry-After`), cachekit starts no more concurrent deletes: it sends the remaining keys one at a time, waiting out each `Retry-After`, for at most about as long as one-at-a-time deletes would have taken. Keys it could not delete in that time, keys refused with a 429 that has no `Retry-After` (a quota or balance deny), and keys whose `DELETE` failed for any other reason stay tracked for the next `invalidate_cache()`. When the time runs out, cachekit logs a WARNING `CachekitIO rate limit: stopped pacing invalidation deletes` with the number of keys left. If an `UNLINK` fails, cachekit logs a WARNING `Multi-key L2 delete failed` and retries that batch one key at a time, so one failed batch does not stop the rest of the invalidation. If a Memcached send fails, or its server is in the client's retry window, its keys count as failed and stay tracked for the next `invalidate_cache()`.

**Key registry (tenant-scoped Redis).** Every L2 write also adds the key to a server-side set for the function, `t:{tenant}:ck:reg:{namespace}:{hash}`. No-args invalidation drains that set and deletes every key in it, plus any key this process remembers that the set missed. The drain runs in bounded steps of 10 000 keys, so it never blocks Redis for long, and a write that lands during the drain is either deleted or left for the next drain: in the set, or, if its tracking failed, in the writing process's own record of its keys. It adds one pipelined round-trip (`SADD` + `EXPIRE`) to each L2 write — cache hits pay nothing. If that round-trip fails, the write still succeeds and cachekit logs a WARNING `Key tracking failed`: that key is then deleted only by this process's next drain, and other processes' drains miss it until its TTL. The warning fires at most once a minute per function in each process and carries the count of failures since the last one, so a registry outage doesn't flood the logs.

Things to know:

- **Server requirements.** A single-instance or primary/replica Redis **5.0 or newer**. Redis Cluster and sharding proxies (one endpoint in front of several shards) are not supported. A restricted ACL user needs the `@scripting` category. When the drain fails for any of these reasons, cachekit logs a WARNING and falls back to deleting only this process's keys. A drain removes a key from the set only after deleting it, so tracked keys a failed drain did not reach stay in the set for the next drain, unless the set expires first.
- **Other processes' L1.** The registry cleans L2 only. Other processes keep serving their L1 copies until the L1 TTL, as with single-key invalidation, unless they run the [invalidation listener](#cross-process-l1-eviction).
- **Invalidation announcements.** Once an invalidation's L2 change has succeeded, cachekit publishes one message on the Redis pub/sub channel `cachekit:py:invalidate:v1`, which processes running the [invalidation listener](#cross-process-l1-eviction) act on: after a no-args drain returns, and after an `invalidate_cache(args)` whose delete of the key returned. A failed delete, or a failed drain that falls back to this process's own keys, publishes nothing. The message carries the function's registry id and, for `invalidate_cache(args)`, the invalidated key. A custom `key=` function's key is never sent because it can embed caller identifiers; its message names only the function. Anyone allowed to `SUBSCRIBE` to the channel sees a live feed of which functions are invalidated, with their generated keys. A generated key spells out the function and carries an unkeyed hash of the arguments, so arguments that are easy to guess, such as small integer ids, can be recovered by trying candidates (see [Invalidation Channel](../../SECURITY.md#invalidation-channel-redis-pubsub)). Redis delivers pub/sub messages whatever a client's database number, so grant `subscribe` on the channel only to your application's own Redis users. A restricted ACL user needs the `publish` command and access to the channel (`&cachekit:py:invalidate:v1`). Without them the invalidation still completes, and cachekit logs a WARNING `Invalidation announcement failed` at most once a minute per process, with the count of failures since the last one; each failure in between logs at DEBUG.
- **Set lifetime.** A tracking set expires 7 days after the last tracked write to its function; each write whose tracking round-trip succeeds refreshes that expiry. While the set exists, a key leaves it only when a no-args drain deletes it: the key's own expiry and `invalidate_cache(args)` do not remove it. A zero-parameter function has no drain — its `invalidate_cache()` deletes its one key directly and leaves that key's member in the set until the set expires. A parameterised function that is written continuously and never invalidated with no args therefore grows its set by one member per distinct key. Keys that outlive it — `ttl=None` or a TTL above 7 days — are no longer reachable by a drain from a process that never saw them. Keys written before the upgrade to v0.20.0 sit in no set: a drain deletes them only in an upgraded process that wrote or read them since it started, and otherwise they stay until their TTL, or for good with `ttl=None`. Call `invalidate_cache()` before you decommission a function whose entries have no TTL or a TTL above 7 days.
- **Tenants.** The set is tenant-scoped like every other key. Each write is tracked in the set of the tenant in `tenant_context` for that call, and a drain empties only the calling tenant's set and can only delete keys inside that tenant's prefix — `default` when no tenant is set (see **Tenant scope** below). The INFO line `Key registry drained N keys` shows how many keys a drain deleted. An operator shell or one-off script in a multi-tenant deployment must enter the tenant's `tenant_context` before it invalidates: without it the drain empties the `default` tenant's set, and `Key registry drained 0 keys` is the usual sign.
- **Reserved namespace.** `namespace="ck"` and any namespace starting with `ck:` are rejected at decoration: a key written there could overwrite a tracking set.
- **Same module path everywhere.** The set is named by the function's `module.qualname`, so every process must import the function from the same module path.

**Custom `key=` functions.** Both forms work. `invalidate_cache(args...)` derives the key with the same `key=` function the write path used, so it deletes the exact entry from this process's L1 and from shared L2. No-args `invalidate_cache()` reaches what the table above says for the resolved backend — the registry tracks the key the write path actually wrote, custom or not. On the backends limited to "this process", tracked keys do not survive a restart, so after a deploy use the exact-args form.

**Backend failures.** Invalidation never raises on a backend failure: `invalidate_cache()` and `ainvalidate_cache()` return `None` whether or not the L2 deletes succeed. (The interop prefix guard is not a backend failure and still raises its `ConfigurationError`; see [Interop Mode](interop-mode.md).) When a no-args invalidation deletes this process's own keys — on a backend without a key registry, or after a failed drain — and some of those deletes fail, cachekit logs one ERROR `Failed to delete N L2 key(s)` per call, with the count, rather than one line per key. A key counts as failed when its delete raised, or when a batch could not confirm it, for example because its Memcached server did not answer. Each of those keys stays tracked in this process, so this process's next no-args `invalidate_cache()` retries it. Until then, the entries are still served from L2.

**`str` subclass namespaces.** A `str` subclass namespace is used as its plain `str` value everywhere, so a `StrEnum` or `(str, Enum)` member `USERS = "users"` is the namespace `users`. Earlier releases rendered a `(str, Enum)` member as `NS.USERS` in some places: in metrics labels on every Python version, and in custom `key=` keys, key registry set names and log-message prefixes on Python 3.11 and later. Plain `str` and `StrEnum` namespaces are unaffected. For a `(str, Enum)` namespace, upgrading changes the following.

- **Custom `key=` entries move (Python 3.11+).** They move from `NS.USERS:k` to `users:k`, and the old entries are no longer served, so each distinct key is recomputed once. On a function that takes parameters, one no-args `invalidate_cache()` from an upgraded process deletes every old entry still tracked in the old registry set; run it once per tenant, since it reaches only the tenant set in `tenant_context`. On a zero-parameter function, a no-args `invalidate_cache()` deletes only the new `users:k` entry and never drains a registry set, so it does not erase the old one. Old entries that are not tracked, because their set expired seven days after its last write or because the backend does not track keys, and every zero-parameter function's old entry, retire only by TTL, and never if none was set. To erase them, run the `scan_iter` + `unlink` script from [Upgrading to 0.20.0](../backends/README.md#upgrading-to-0200) with `pattern = "t:*:NS.USERS:*"` on the tenant-scoped Redis backend (env auto-detection or `RedisBackendProvider`), or `pattern = "NS.USERS:*"` on a plain `RedisBackend`. A `redis-cli SCAN NS.USERS:*` against the tenant-scoped backend matches nothing, because its keys carry the `t:{tenant}:` prefix.
- **The key registry set is renamed (Python 3.11+).** It moves from `ck:reg:NS.USERS:…` to `ck:reg:users:…`. Auto-mode keys do not move. On a function that takes parameters, a no-args `invalidate_cache()` drains both sets, so entries tracked before the upgrade are still invalidated. If the old set's drain fails, cachekit logs a WARNING `Legacy key registry drain failed` and still applies the new set's drain; the old set's entries that the failed drain had not yet deleted stay in it for the next no-args `invalidate_cache()`. A drain that fails part-way through a set larger than 10 000 keys has already deleted some of them from L2, and other wrappers of the same function in this process can keep serving their L1 copies of those keys until the L1 TTL, as when the new set's drain fails. A zero-parameter function has a single auto-mode key, which its no-args `invalidate_cache()` deletes directly.
- **Rolling deploys and rollbacks leave stale entries (Python 3.11+).** During a rollout, a no-args `invalidate_cache()` from a process on the earlier release misses entries the upgraded processes serve. On a function that takes parameters, it drains only the old set, so it misses every entry tracked in the new one. On a zero-parameter function, it deletes only the key the earlier release derives: in auto mode that is the shared key, so nothing is missed, but with `key=` it deletes `NS.USERS:k` and leaves `users:k`. Missed entries stay stale after the rollout completes, until their TTL (never, if none was set) or until an upgraded process invalidates them. Once the rollout completes, run one no-args `invalidate_cache()` per tenant from an upgraded process for each affected function. A rollback leaves the same gap in the other direction for entries tracked in the new set, which lives seven days after its last write. For `key=` functions, the earlier release also serves `NS.USERS:k` entries again, which this release's exact-args and zero-parameter invalidations never deleted. After a rollback, run one no-args `invalidate_cache()` per tenant from a one-off script on this release, then erase the old `key=` entries with the `scan_iter` + `unlink` script above.
- **Observability names change.** The `namespace` metrics label reads `users` instead of `NS.USERS` on every Python version, 3.10 included, so update dashboards and alerts that match on it. On Python 3.11 and later, the log-message prefix `[NS.USERS]` becomes `[users]`.

**Tenant scope:** with the tenant-scoped Redis backend (env auto-detection, or `RedisBackendProvider(...).get_shared_backend()`), each tenant's entries live under its own `t:{tenant}:` prefix. `invalidate_cache()` — with or without arguments — deletes only the L2 entries of the tenant set in `tenant_context` for the calling context (`default` when none is set); other tenants' entries stay cached and tracked. L1 is not tenant-scoped: within a process, all tenants share one L1 entry per cache key, so a tenant can be served the value another tenant cached, and `invalidate_cache()` evicts that entry for every tenant. The exception is an encrypted cache whose `tenant_extractor` resolves the same tenant as `tenant_context`: an L1 entry encrypted for another tenant is skipped as a miss, and the read goes on to L2. Disable L1 on functions whose results differ by tenant: `@cache(..., l1_enabled=False)`, or with a preset `@cache.production(..., l1_enabled=False)`, which keeps the preset's other L1 settings.

**Writes in flight.** An invalidation does not stop a write that began before it. A call that missed the cache before an invalidation still stores its result after it, and so does a `@cache.io` [`stale_ttl`](../configuration.md#stale-while-revalidate-stale_ttl) refresh. The value it stores was computed no earlier than the call started, and it expires by its TTL. With `ttl=None` that is never for an L2 entry, and one year for an entry in L1-only mode. With a backend, the store is two steps, L2 then L1, so an invalidation that arrives between them, from this process or [announced](#cross-process-l1-eviction) by another, can leave the value in this process's L1 alone. Likewise, a read that fetched the old value from L2 just before an invalidation can copy it into this process's L1 just after. Either copy expires by its L1 TTL. In L1-only mode a background SWR refresh never lands after an invalidation: it is dropped when the entry it refreshes has been removed.

---

## Multi-Instance Semantics

Without the [listener](#cross-process-l1-eviction), L1 invalidation is process-local. When running multiple processes or pods against a shared L2 backend:

- `invalidate_cache(args...)` deletes the key from shared L2, so any pod's next **L1 miss** fetches fresh data.
- `invalidate_cache()` with no arguments does the same for every key the calling tenant has for the function on the tenant-scoped Redis backend ([key registry](#whole-function-invalidation)); on other backends it reaches only the keys the calling process knows.
- Pods that still hold the entry in L1 keep serving it until their **L1 TTL** expires (L1 expires 1 second before L2 by design), unless they run the [invalidation listener](#cross-process-l1-eviction).
- Without the listener, worst-case staleness after an invalidation is therefore bounded by the entry's remaining TTL. Size TTLs accordingly for data where cross-pod staleness matters.

### Cross-Process L1 Eviction

On the tenant-scoped Redis backend, a process can also evict its L1 copies when another process invalidates them. Set one environment variable in that process:

```bash
export CACHEKIT_INVALIDATION_LISTENER_ENABLED=true
```

The process then runs one invalidation listener: a background thread with its own Redis connection, subscribed to the channel that every invalidation is [announced on](#whole-function-invalidation). It starts on the process's first cache operation that reaches Redis. When another process's `invalidate_cache(args)` deletes a key, the listener evicts that key from this process's L1. When another process's no-args `invalidate_cache()` drains a function, the listener evicts every key this process cached for that function. Eviction is tenant-blind, like L1 itself: an invalidation under any tenant evicts the one shared L1 entry.

- **Off by default.** No preset and no decorator argument turns it on. With the variable unset, a process opens no thread and no extra connection. Every process on this backend announces its own invalidations either way, so a cron job or an operator shell that calls `invalidate_cache()` needs no setting for listening workers to evict.
- **Delivery.** At most once. A subscribed process usually evicts within milliseconds, and cachekit's tests hold it to one second. An event sent while the listener is not subscribed is lost, and that L1 copy expires by its L1 TTL as before: before the process's first cache operation that reaches Redis, while the listener starts, and while it reconnects. The start waits for Redis to confirm the subscription, so a sync cache operation that starts the listener reads L2 only once it is subscribed. An async operation starts it without waiting, and other threads' operations go on while a start is in progress, so a value they read from L2 just before the subscription can outlive an invalidation sent then, until its L1 TTL. A connection that Redis closes, on a restart, a failover or `CLIENT KILL`, is noticed at once: the listener reconnects and subscribes again on its own, logging a WARNING `Invalidation listener error (errors since the last warning: N)` while Redis is unreachable, at most once a minute per process, carrying the count of errors since the last one; each error in between logs at DEBUG. The connection PINGs Redis after 10 idle seconds, which keeps idle-timeout proxies and NAT gateways from dropping it, but the listener does not wait for the reply: a connection that dies silently, with packets dropped and no reset, is noticed only when TCP gives up on it, which can take minutes. Events sent meanwhile are lost.
- **Backends.** Only the tenant-scoped Redis backend carries events (`CACHEKIT_REDIS_URL` / `REDIS_URL` without `backend=`, or a backend from `RedisBackendProvider`). With the variable set, a function cached on any other backend, such as a `RedisBackend` passed as `backend=`, File, Memcached or CachekitIO, gets no cross-process eviction, and cachekit logs one WARNING per process, `... does not carry invalidation events`. A process has one listener, on the Redis server of the first cache operation that starts it, so give every function in a listening process the same Redis.
- **Redis permissions.** To listen, a restricted ACL user needs the `subscribe` and `ping` commands (`+@connection` includes `ping`) and access to the channel (`&cachekit:py:invalidate:v1`), besides `publish` on it to announce. From Redis 7 a new ACL user gets no channels unless granted, so `~* +@all` alone is refused. The start waits up to 5 seconds for Redis to confirm the subscription and answer a PING. A refusal, or no answer, fails the start with a WARNING `Invalidation listener failed to start`, and the next cache operation that reaches Redis (an L1 miss) retries it, no sooner than a minute later: a process that serves only L1 hits does not retry. Once the listener runs, a refusal, after an ACL change for instance, is a WARNING `Invalidation listener refused by Redis`, and the listener subscribes again every minute on its own, with no restart and no cache operation.
- **One Redis, several apps.** Redis delivers pub/sub messages across database numbers, so apps on different databases of one Redis server see each other's events. An event names its function by namespace and `module.qualname`, so the most it costs another app is an extra L1 miss on a function with the same name and namespace.
- **Untrusted messages.** Anyone allowed to publish on the channel can make every listening process evict L1 entries, and can spend its resources. A message is decoded only after a size check, so a forged one cannot run code or stop the listener, and a malformed one is dropped, with a WARNING `Invalidation event dropped (drops since the last warning: N)` at most once a minute per process, carrying the count of drops since the last one; each drop in between logs at DEBUG. But Redis delivers a message whole before cachekit can check it: a large one costs each listener its size in memory, and one beyond Redis's pub/sub output-buffer limit (32 MB by default) makes Redis drop every listener's connection until it reconnects. A whole-function event costs time in proportion to the keys the process recorded for that function. Grant `publish` on the channel only to your application's own Redis users (see [Invalidation Channel](../../SECURITY.md#invalidation-channel-redis-pubsub)).
- **Python only.** The channel is cachekit-py's own. The TypeScript SDK's opt-in channel uses a different channel and format, and the two do not interoperate. See the [cross-SDK feature matrix](https://github.com/cachekit-io/protocol) for per-SDK support.

### Forked Processes

A process created by `fork()`, such as a `multiprocessing` fork-context worker or a Gunicorn or Celery prefork worker, starts with an empty L1. None of its parent's L1 entries carry over, and the child refills from L2 on first use. A master that warmed L1 before forking (Gunicorn `--preload`) therefore no longer passes that warmth to its workers. With the listener enabled, each child runs its own, on its own connection, from its first cache operation; it never shares its parent's.

The parent's entries stay in the child's memory, shared with the parent, until the child first stores a value in its L1. Then a background thread frees them. Freeing copies the memory they held into the child, about 2.3 times the size of the parent's L1 in cachekit's own measurement, because each free also writes to the memory allocator's bookkeeping beside the entry; the child's own L1 then reuses that memory. That copy avoids a larger one. The parent's background sweep rewrites those entries as they expire, within one L1 TTL, and a child that kept them would from then on hold a private copy that nothing can reach, beside its own L1. A master that fills no L1 before it forks avoids both.

uWSGI forks its workers without running Python's at-fork hooks. Set `py-call-uwsgi-fork-hooks` (uWSGI 2.0.21 or later) or `py-call-osafterfork` so that it runs them, or `lazy-apps` so that each worker imports your app after the fork; any one of them keeps the rule above. Without one, a uWSGI worker keeps the L1 entries its master held at fork, runs no background sweep of expired L1 entries, and runs no invalidation listener, so its L1 heals by TTL. An expired entry is still never served: it is evicted when it is read. Under uWSGI with none of these options, cachekit logs one WARNING `uWSGI forks its workers without running Python's at-fork hooks` when it is imported.

---

## Configuration Reference

### L1CacheConfig Fields

The `L1CacheConfig` class controls L1 behavior with these fields:

```python
from cachekit.config import L1CacheConfig

config = L1CacheConfig(
    enabled=True,                    # Enable L1 cache (default: True)
    max_size_mb=100,                 # Max memory (default: 100 MB)

    # SWR Settings
    swr_enabled=True,                # Enable SWR (default: True)
    swr_threshold_ratio=0.5,         # Refresh at X% of TTL (default: 0.5 = 50%)
    swr_retry_interval=10.0,         # Back-off after a failed refresh (default: 10 s)
)
```

| Field | Type | Default | Purpose |
|-------|------|---------|---------|
| `enabled` | bool | `True` | Enable/disable L1 cache completely |
| `max_size_mb` | int | `100` | Maximum memory usage in MB. With a backend, a single value larger than an eighth of it is not kept in L1: it is served from L2, or recomputed if L2 did not store it; in [L1-only mode](../configuration.md#l1-only-mode-backendnone) only a value larger than the whole budget is uncached |
| `swr_enabled` | bool | `True` | Enable stale-while-revalidate (L1-only mode, requires a `ttl`) |
| `swr_threshold_ratio` | float | `0.5` | Refresh at X% of TTL, in `(0.0, 1.0]` |
| `swr_retry_interval` | float | `10.0` | Seconds after a failed refresh before that key refreshes again, `>= 0` (`0` = next stale read) |

### Intent Presets

CacheKit includes preconfigured presets for common use cases:

```python
from cachekit import cache

# Development: SWR only
@cache.dev()
def dev_function():
    pass

# Production: All features enabled
@cache.production()
def prod_function():
    pass

# Minimal: Zero overhead, features disabled
@cache.minimal()
def minimal_function():
    pass

# Secure: All features + encryption
@cache.secure(master_key=secret_key)
def secure_function():
    pass

# Testing: All features disabled for deterministic behavior
@cache.test()
def test_function():
    pass
```

**Feature Behavior by Preset:**

| Preset | SWR |
|--------|-----|
| `minimal()` | ❌ |
| `test()` | ❌ |
| `dev()` | L1-only¹ |
| `production()` | L1-only¹ |
| `secure()` | ❌³ |
| `io()` | ✓² |

¹ Within-TTL SWR runs only in L1-only mode (`backend=None`) — with a backend configured, `swr_enabled` has no effect (see the callout at the top of this page).
² `@cache.io` ships past-TTL SWR via [`stale_ttl`](../configuration.md#stale-while-revalidate-stale_ttl) (default-on), using the CachekitIO backend's freshness signal — a different mechanism from the L1-only within-TTL refresh described here.
³ `@cache.secure` raises `ConfigurationError` with `backend=None` (L1-only stores raw objects, which cannot be ciphertext), so the L1-only SWR never runs for it.

---

## Common Patterns

### Pattern 1: Invalidate on Write

Delete the cached entry when the underlying data changes:

```python
from cachekit import cache

@cache
def get_user(user_id: int):
    return db.get_user(user_id)  # db: your data layer

# User update endpoint
def update_user(user_id: int, data: dict):
    # Update database
    db.update_user(user_id, data)

    # Remove from local L1 and shared L2, spelled the way the reads call get_user
    get_user.invalidate_cache(user_id)

    return {"status": "updated"}
```

In multi-pod deployments, other pods pick up the fresh value on their next L1 miss; until then they may serve their L1 copy for at most the remaining TTL (see [Multi-Instance Semantics](#multi-instance-semantics)). Pods that run the [invalidation listener](#cross-process-l1-eviction) evict their copy as soon as the announcement arrives.

### Pattern 2: Bulk Invalidation per Function

Clear everything cached for a function:

```python
@cache(namespace="products")
def get_product(product_id: int):
    return db.get_product(product_id)

# Category discount: drop all product entries
def apply_category_discount(category_id: int, discount: float):
    db.update_category_discount(category_id, discount)
    get_product.invalidate_cache()
```

On the tenant-scoped Redis backend this deletes every process's L2 entries for `get_product` under the calling tenant (`default` when none is set); to clear several tenants, call it once under each tenant's `tenant_context`. On other backends it deletes only the entries this process wrote or read, so for bulk updates where cross-process consistency matters there, prefer short TTLs over relying on invalidation. Either way, other processes' L1 copies live until their L1 TTL, unless they run the [invalidation listener](#cross-process-l1-eviction).

---

## Performance Notes

### SWR Latency Characteristics (L1-only mode)

- **Fresh hit** (~50ns): Return from L1 memory
- **Stale hit** (~100ns): Return from L1 + schedule background re-run of your function (non-blocking)

SWR keeps hits at L1 speed, even when serving slightly stale data. In backed modes (no within-TTL SWR) the usual tiers apply: **L2 hit** ~2ms (miss L1, fetch from Redis), **L2 miss** ~5-50ms (run your function, populate L1+L2).

### Memory Impact

- L1-only SWR bookkeeping: a per-entry version counter plus ~8 bytes per key with a refresh in flight

For typical workloads (1000s of keys), overhead is <1MB.

---

## Troubleshooting

### Problem: Another pod serves stale data after invalidation

**Cause:** Expected behavior without the [invalidation listener](#cross-process-l1-eviction) — L1 invalidation is process-local. The invalidating process deletes the key from shared L2, but other pods keep their L1 copy until it expires.

**Solution:** On the tenant-scoped Redis backend, set `CACHEKIT_INVALIDATION_LISTENER_ENABLED=true` in the pods that serve reads. Otherwise, bound acceptable staleness with the entry's TTL. Disabling L1 (`l1=L1CacheConfig(enabled=False)`) removes the per-pod window, which the listener only narrows because its delivery is at most once. It does not remove every stale read: a call that began before an invalidation can still store its old result in L2 after it, where every pod reads it until its TTL or the next invalidation (see **Writes in flight** under [Invalidation API](#invalidation-api)). Data that cannot tolerate any stale read should not be cached.

### Problem: The listener is enabled but a pod still serves stale data

**Cause and diagnosis**, by the WARNING each case logs:

- `... does not carry invalidation events`: the function's backend is not the tenant-scoped Redis backend, so no event reaches it.
- `uWSGI forks its workers without running Python's at-fork hooks`: the workers run no listener; set one of the uWSGI options under [Forked Processes](#forked-processes).
- `Invalidation listener failed to start`: Redis was unreachable, refused the listener's SUBSCRIBE or PING, or did not answer them within 5 seconds. The message ends with the error type: `NoPermissionError` for a refusal, usually for the ACL (see **Redis permissions** under [Cross-Process L1 Eviction](#cross-process-l1-eviction)), `TimeoutError` for no answer. The next cache operation that reaches Redis (an L1 miss) retries it, no sooner than a minute later. Events sent meanwhile are lost, and those L1 copies expire by TTL.
- `Invalidation listener error (errors since the last warning: N)`: a running listener lost its connection; it reconnects and subscribes again on its own, retrying every second. The WARNING repeats at most once a minute per process while Redis stays unreachable, and N counts the errors since the last one. Events sent meanwhile are lost.
- `Invalidation listener refused by Redis`: Redis refused a running listener's subscription or PING, usually after an ACL change; see **Redis permissions** under [Cross-Process L1 Eviction](#cross-process-l1-eviction). The listener tries again every minute on its own, with no cache operation.
- No WARNING: the event was published before the pod's listener subscribed, which happens during the pod's first cache operation that reaches Redis; or the invalidating process published nothing, because its L2 delete or drain failed (an ERROR or WARNING in that process), its publish failed (a WARNING `Invalidation announcement failed` there), or the function's namespace is over 1000 bytes, too long for an event (a WARNING `Invalidation not announced` there, at most once a minute with the count since the last one); or the pod was reading the old value from L2 when the eviction arrived, and stored it in L1 just after; or the listener's connection died silently, with packets dropped and no reset, so the listener has not noticed yet (see **Delivery** under [Cross-Process L1 Eviction](#cross-process-l1-eviction)). That copy expires by its L1 TTL.

### Problem: SWR refresh failing

**Cause:** Your function raised during the background re-run (L1-only mode), or the refresh never ran: the call's arguments cannot be deep-copied (a lock, an open connection), or its thread could not start.

**Behavior:** The cached value continues to be served until its hard expiry, and the next qualifying hit retries the refresh. This is by design - a failed refresh never evicts a servable value. Arguments that cannot be copied fail the same way on every retry, so for that call refresh-ahead never runs and the value is recomputed in the foreground after expiry.

**Diagnosis:** cachekit logs a WARNING `L1-only SWR refresh failed`, `L1-only SWR refresh skipped` or `L1-only SWR refresh could not be started`, with the function and the key as `<redacted:…>` digests and the exception type (never its message). The function's digest is that of its `module.qualname`: `cachekit.hash_utils.redact_cache_key("app.sources.fetch")` gives the value to match. Each fires at most once a minute per function and carries the count since the last one; the occurrences in between log at DEBUG.

### Problem: High memory usage despite max_size_mb limit

**Cause:** L1 cache eviction churn under a working set larger than the configured budget

**Solution:** Check L1 cache hit rate and either increase `max_size_mb` to fit the working set or reduce TTL so entries expire before LRU has to evict them.

---

## See Also

- [Configuration Guide](../configuration.md) - Complete configuration reference
- [Getting Started](../getting-started.md) - Quick start guide
- [Zero-Knowledge Encryption](zero-knowledge-encryption.md) - Secure caching
- [API Reference](../api-reference.md) - All decorator parameters

---

<div align="center">

**[GitHub Issues](https://github.com/cachekit-io/cachekit-py/issues)** · **[Documentation](../README.md)**

</div>
