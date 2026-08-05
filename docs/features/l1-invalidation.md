**[Home](../README.md)** › **Features** › **L1 Cache Invalidation**

# L1 Cache Invalidation and Stale-While-Revalidate (SWR)

> L1 invalidation and SWR freshness management are **process-local**. When an L2 backend is configured, invalidating a key also deletes it from shared L2 — but other processes keep serving their own L1 copy until it expires (L1 TTL). In L1-only mode (`backend=None`) invalidation is purely local. There is no cross-instance invalidation broadcast — see [Multi-Instance Semantics](#multi-instance-semantics).

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

If the background refresh fails (your function raises), the entry is left as-is: the cached value keeps being served until its original hard expiry, and the next qualifying hit retries the refresh.

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

**Effect:** The entry is removed from this process's L1 cache **and**, when an L2 backend is configured, deleted from shared L2. Cache keys are deterministic, so the L2 delete removes the entry no matter which process wrote it. In L1-only mode (`backend=None`) there is no L2 to delete from — the invalidation is purely local.

### Whole-Function Invalidation

Calling `invalidate_cache()` with **no arguments** on a parameterized function clears every cached entry this process has written for that function:

```python notest
@cache
def get_user(user_id: int):
    return db.query("SELECT * FROM users WHERE id = %s", (user_id,))

# Clear all get_user entries written by this process (L1 + L2)
get_user.invalidate_cache()
```

**Limitation:** Key tracking is process-local. Entries written to L2 by *other* processes for the same function are not deleted; they remain until their TTL expires.

---

## Multi-Instance Semantics

CacheKit does **not** ship cross-instance L1 invalidation in Python. When running multiple processes or pods against a shared L2 backend:

- `invalidate_cache(args...)` deletes the key from shared L2, so any pod's next **L1 miss** fetches fresh data.
- Pods that still hold the entry in L1 keep serving it until their **L1 TTL** expires (L1 expires 1 second before L2 by design).
- Worst-case staleness after an invalidation is therefore bounded by the entry's remaining TTL. Size TTLs accordingly for data where cross-pod staleness matters.

The TypeScript SDK ships an opt-in Redis pub/sub invalidation channel; an equivalent for Python (paired with server-side key tracking for whole-function invalidation) is a potential future feature. See the [cross-SDK feature matrix](https://github.com/cachekit-io/protocol) for current per-SDK support.

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
@cache.secure()
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
| `secure()` | L1-only¹ |
| `io()` | ✓² |

¹ Within-TTL SWR runs only in L1-only mode (`backend=None`) — with a backend configured, `swr_enabled` has no effect (see the callout at the top of this page).
² `@cache.io` ships past-TTL SWR via [`stale_ttl`](../configuration.md#stale-while-revalidate-stale_ttl) (default-on), using the CachekitIO backend's freshness signal — a different mechanism from the L1-only within-TTL refresh described here.

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

Clear everything this process cached for a function:

```python notest
@cache(namespace="products")
def get_product(product_id: int):
    return db.get_product(product_id)

# Category discount: drop all product entries written by this process
def apply_category_discount(category_id: int, discount: float):
    db.update_category_discount(category_id, discount)
    get_product.invalidate_cache()
```

For bulk updates where cross-process consistency matters, prefer short TTLs over relying on invalidation: whole-function invalidation only tracks keys written by the local process.

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

**Cause:** Your function raised during the background re-run (L1-only mode)

**Behavior:** The cached value continues to be served until its hard expiry, and the next qualifying hit retries the refresh. This is by design - a failed refresh never evicts a servable value.

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
