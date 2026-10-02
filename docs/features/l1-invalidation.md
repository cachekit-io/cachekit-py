**[Home](../README.md)** › **Features** › **L1 Cache Invalidation**

# L1 Cache Invalidation and Stale-While-Revalidate (SWR)

> L1 invalidation and SWR freshness management are **process-local**. When an L2 backend is configured, invalidating a key also deletes it from shared L2 — but other processes keep serving their own L1 copy until it expires (L1 TTL). On the tenant-scoped Redis backend (env auto-detection, or a backend from `RedisBackendProvider`), whole-function invalidation also deletes the L2 entries *other* processes wrote for the calling tenant ([key registry](#whole-function-invalidation)). In L1-only mode (`backend=None`) invalidation is purely local. There is no cross-instance L1 invalidation broadcast — see [Multi-Instance Semantics](#multi-instance-semantics).

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

```python notest
# Without jitter: 1000 keys cached at T=0 all refresh at T=1800
# With jitter: Refreshes spread from T=1620 to T=1980

refresh_threshold = ttl * swr_threshold_ratio * random.uniform(0.9, 1.1)
```

This is automatic and transparent - no configuration needed.

---

## Invalidation API

Invalidation is exposed per decorated function via `invalidate_cache()`:

### Specific Call Invalidation

Clear the cache for a **specific function call**:

```python notest
from cachekit import cache

@cache
def get_user(user_id: int):
    return db.query("SELECT * FROM users WHERE id = %s", (user_id,))

# Clear cache only for user #123
get_user.invalidate_cache(user_id=123)

# Clear cache for multiple users
for uid in [1, 2, 3]:
    get_user.invalidate_cache(user_id=uid)
```

**Use cases:**
- Single record update
- User data refresh
- Post cache invalidation

**Effect:** The entry is removed from this process's L1 cache **and**, when an L2 backend is configured, deleted from shared L2. Cache keys are deterministic, so the L2 delete removes the entry no matter which process wrote it. On a generated key with a non-default serializer it also deletes the key a pre-v0.20.0 release wrote for the same arguments (see [the v0.20.0 key change](../serializers/README.md#breaking-change-in-v0200-the-key-carries-the-real-serializer)). In L1-only mode (`backend=None`) there is no L2 to delete from — the invalidation is purely local. If the L2 delete fails, cachekit logs an ERROR `Failed to delete L2 key` and tracks the key in this process, so a later no-args `invalidate_cache()` from the same process retries it. On the backends the [whole-function table](#whole-function-invalidation) marks "any process", a key whose write was tracked also stays in the server-side set, so a later no-args drain from any process retries it, within the `Key tracking failed` and **Set lifetime** caveats below. On the backends it marks "this process", tracking is process-local: another process, or this one after a restart, does not retry it.

### Whole-Function Invalidation

Calling `invalidate_cache()` with **no arguments** on a parameterized function clears every cached entry for that function. How far "every" reaches depends on the backend:

```python notest
@cache
def get_user(user_id: int):
    return db.query("SELECT * FROM users WHERE id = %s", (user_id,))

# Clear all get_user entries (L1 + L2)
get_user.invalidate_cache()
```

| Backend the decorator resolves | L2 entries deleted |
|--------------------------------|--------------------|
| Redis from `CACHEKIT_REDIS_URL` / `REDIS_URL` (the default, no `backend=`), or a backend from `RedisBackendProvider` | Every entry **any process** wrote for the function, for the calling tenant |
| `RedisBackend` passed as `backend=`, File, Memcached, CachekitIO | Only entries **this process** wrote or read |
| L1-only (`backend=None`) | No L2; this process's L1 only |

**Round trips on the "this process" backends.** A plain `RedisBackend` deletes the process's keys with one `UNLINK` per 10 000 keys. Memcached groups them by server and sends one pipelined, acknowledged `delete` batch per server per 1 000 keys. File and CachekitIO delete one key per call. If an `UNLINK` fails, cachekit logs a WARNING `Multi-key L2 delete failed` and retries that batch one key at a time, so one failed batch does not stop the rest of the invalidation. If a Memcached send fails, or its server is in the client's retry window, its keys count as failed and stay tracked for the next `invalidate_cache()`.

**Key registry (tenant-scoped Redis).** Every L2 write also adds the key to a server-side set for the function, `t:{tenant}:ck:reg:{namespace}:{hash}`. No-args invalidation drains that set and deletes every key in it, plus any key this process remembers that the set missed. The drain runs in bounded steps of 10 000 keys, so it never blocks Redis for long, and a write that lands during the drain is either deleted or left for the next drain: in the set, or, if its tracking failed, in the writing process's own record of its keys. It adds one pipelined round-trip (`SADD` + `EXPIRE`) to each L2 write — cache hits pay nothing. If that round-trip fails, the write still succeeds and cachekit logs a WARNING `Key tracking failed`: that key is then deleted only by this process's next drain, and other processes' drains miss it until its TTL. The warning fires at most once a minute per function in each process and carries the count of failures since the last one, so a registry outage doesn't flood the logs.

Things to know:

- **Server requirements.** A single-instance or primary/replica Redis **5.0 or newer**. Redis Cluster and sharding proxies (one endpoint in front of several shards) are not supported. A restricted ACL user needs the `@scripting` category. When the drain fails for any of these reasons, cachekit logs a WARNING and falls back to deleting only this process's keys. A drain removes a key from the set only after deleting it, so tracked keys a failed drain did not reach stay in the set for the next drain, unless the set expires first.
- **Other processes' L1.** The registry cleans L2 only. Other processes keep serving their L1 copies until the L1 TTL, as with single-key invalidation.
- **Invalidation announcements.** Once an invalidation's L2 change has succeeded, cachekit publishes one message on the Redis pub/sub channel `cachekit:py:invalidate:v1`: after a no-args drain returns, and after an `invalidate_cache(args)` whose delete of the key returned. A failed delete, or a failed drain that falls back to this process's own keys, publishes nothing. The message carries the function's registry id and, for `invalidate_cache(args)`, the invalidated key. A custom `key=` function's key is never sent, because it can embed caller identifiers; its message names only the function. Anyone allowed to `SUBSCRIBE` to the channel can see which functions are invalidated and which generated keys, and a generated key carries a hash of the arguments, not the arguments. A restricted ACL user needs the `publish` command and access to the channel (`&cachekit:py:invalidate:v1`). Without them the invalidation still completes, and cachekit logs a WARNING `Invalidation announcement failed` on each invalidation.
- **Set lifetime.** A tracking set expires 7 days after the last tracked write to its function; each write whose tracking round-trip succeeds refreshes that expiry. While the set exists, a key leaves it only when a no-args drain deletes it: the key's own expiry and `invalidate_cache(args)` do not remove it. A zero-parameter function has no drain — its `invalidate_cache()` deletes its one key directly and leaves that key's member in the set until the set expires. A parameterised function that is written continuously and never invalidated with no args therefore grows its set by one member per distinct key. Keys that outlive it — `ttl=None` or a TTL above 7 days — are no longer reachable by a drain from a process that never saw them, and keys written before an upgrade to this version were never tracked. Call `invalidate_cache()` before you decommission a function whose entries have no TTL.
- **Tenants.** The set is tenant-scoped like every other key. Each write is tracked in the set of the tenant in `tenant_context` for that call, and a drain empties only the calling tenant's set and can only delete keys inside that tenant's prefix — `default` when no tenant is set (see **Tenant scope** below). The INFO line `Key registry drained N keys` shows how many keys a drain deleted.
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

---

## Multi-Instance Semantics

CacheKit does **not** ship cross-instance L1 invalidation in Python. When running multiple processes or pods against a shared L2 backend:

- `invalidate_cache(args...)` deletes the key from shared L2, so any pod's next **L1 miss** fetches fresh data.
- `invalidate_cache()` with no arguments does the same for every key the calling tenant has for the function on the tenant-scoped Redis backend ([key registry](#whole-function-invalidation)); on other backends it reaches only the keys the calling process knows.
- Pods that still hold the entry in L1 keep serving it until their **L1 TTL** expires (L1 expires 1 second before L2 by design).
- Worst-case staleness after an invalidation is therefore bounded by the entry's remaining TTL. Size TTLs accordingly for data where cross-pod staleness matters.

The TypeScript SDK ships an opt-in Redis pub/sub invalidation channel; Python has no equivalent yet. See the [cross-SDK feature matrix](https://github.com/cachekit-io/protocol) for current per-SDK support.

### Forked Processes

A process created by `fork()`, such as a `multiprocessing` fork-context worker or a Gunicorn or Celery prefork worker, starts with an empty L1. None of its parent's L1 entries carry over, and the child refills from L2 on first use. A master that warmed L1 before forking (Gunicorn `--preload`) therefore no longer passes that warmth to its workers.

uWSGI forks its workers without running Python's at-fork hooks. Set `py-call-uwsgi-fork-hooks` (uWSGI 2.0.21 or later) or `py-call-osafterfork` so that it runs them, or `lazy-apps` so that each worker imports your app after the fork; any one of them keeps the rule above. Without one, a uWSGI worker keeps the L1 entries its master held at fork and runs no background sweep of expired L1 entries. An expired entry is still never served: it is evicted when it is read.

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
)
```

| Field | Type | Default | Purpose |
|-------|------|---------|---------|
| `enabled` | bool | `True` | Enable/disable L1 cache completely |
| `max_size_mb` | int | `100` | Maximum memory usage in MB |
| `swr_enabled` | bool | `True` | Enable stale-while-revalidate (L1-only mode, requires a `ttl`) |
| `swr_threshold_ratio` | float | `0.5` | Refresh at X% of TTL, in `(0.0, 1.0]` |

### Intent Presets

CacheKit includes preconfigured presets for common use cases:

```python notest
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

```python notest
from cachekit import cache
import database

@cache
def get_user(user_id: int):
    return database.get_user(user_id)

# User update endpoint
def update_user(user_id: int, data: dict):
    # Update database
    database.update_user(user_id, data)

    # Remove from local L1 and shared L2
    get_user.invalidate_cache(user_id=user_id)

    return {"status": "updated"}
```

In multi-pod deployments, other pods pick up the fresh value on their next L1 miss; until then they may serve their L1 copy for at most the remaining TTL (see [Multi-Instance Semantics](#multi-instance-semantics)).

### Pattern 2: Bulk Invalidation per Function

Clear everything cached for a function:

```python notest
@cache(namespace="products")
def get_product(product_id: int):
    return db.get_product(product_id)

# Category discount: drop all product entries
def apply_category_discount(category_id: int, discount: float):
    db.update_category_discount(category_id, discount)
    get_product.invalidate_cache()
```

On the tenant-scoped Redis backend this deletes every process's L2 entries for `get_product` under the calling tenant (`default` when none is set); to clear several tenants, call it once under each tenant's `tenant_context`. On other backends it deletes only the entries this process wrote or read, so for bulk updates where cross-process consistency matters there, prefer short TTLs over relying on invalidation. Either way, other processes' L1 copies live until their L1 TTL.

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

**Cause:** Expected behavior — L1 invalidation is process-local. The invalidating process deletes the key from shared L2, but other pods keep their L1 copy until it expires.

**Solution:** Bound acceptable staleness with the entry's TTL. If a class of data cannot tolerate any cross-pod staleness window, don't cache it in L1 (`l1=L1CacheConfig(enabled=False)`).

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
