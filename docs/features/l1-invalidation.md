**[Home](../README.md)** › **Features** › **L1 Cache Invalidation**

# L1 Cache Invalidation and Stale-While-Revalidate (SWR)

> L1 invalidation and SWR freshness management are **process-local**. Invalidating a key also deletes it from the shared L2 backend, but other processes keep serving their own L1 copy until it expires (L1 TTL). There is no cross-instance invalidation broadcast — see [Multi-Instance Semantics](#multi-instance-semantics).

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

### Key Concept: SWR Does NOT Extend TTL

When stale data is refreshed in the background, the **original expiry time remains unchanged**:

```python
# Original cache entry (1 hour TTL)
cached_at = 0
expires_at = 3600  # Hard expiry time

# At T=1800 (50% of TTL): User gets hit, SWR triggers
# Returns stale data immediately
# Background refresh completes at T=1850
cached_at = 1850   # Reset for next freshness check
expires_at = 3600  # UNCHANGED - still expires at T=3600
```

**Why?** SWR refreshes *content* from L2, not *lifetime*. L1 and L2 TTLs are synchronized at write time and stay synchronized.

**Edge Case: L2 Miss During SWR Refresh**

If Redis evicts the entry or it's manually deleted during refresh:
1. Background refresh finds cache miss in L2
2. L1 entry is deleted immediately
3. Next request calls the original function (full cache miss)

---

## Stale-While-Revalidate (SWR) Explained

SWR is an optimization that improves perceived latency by serving stale data while fetching fresh data in the background.

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

# Default: SWR enabled, refresh at 50% of TTL
@cache(backend=None)
def my_function():
    """SWR configured with defaults."""
    pass

# Custom: Refresh at 25% of TTL (refresh more frequently)
@cache(
    l1=L1CacheConfig(
        swr_enabled=True,
        swr_threshold_ratio=0.25  # Refresh at 25% of TTL
    ),
    backend=None
)
def aggressive_refresh():
    """Refreshes more often, better freshness."""
    pass

# Disable SWR: Always wait for fresh data
@cache(
    l1=L1CacheConfig(
        swr_enabled=False
    ),
    backend=None
)
def always_fresh():
    """Returns stale data only on L2 miss/timeout."""
    pass
```

### Jitter: Preventing Thundering Herd

When you have many concurrent requests to the same key at the stale threshold, all would trigger refresh simultaneously, overwhelming the backend.

CacheKit applies **jitter** (±10% randomness) to the threshold to spread refresh attempts:

```python notest
# Without jitter: All 1000 requests refresh at T=1800
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
    return db.query(f"SELECT * FROM users WHERE id={user_id}")

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

**Effect:** The entry is removed from this process's L1 cache **and** deleted from the shared L2 backend. Cache keys are deterministic, so the L2 delete removes the entry no matter which process wrote it.

### Whole-Function Invalidation

Calling `invalidate_cache()` with **no arguments** on a parameterized function clears every cached entry this process has written for that function:

```python notest
@cache
def get_user(user_id: int):
    return db.query(f"SELECT * FROM users WHERE id={user_id}")

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

    namespace_index=True,            # Enable O(1) namespace lookups (default: True)
)
```

| Field | Type | Default | Purpose |
|-------|------|---------|---------|
| `enabled` | bool | `True` | Enable/disable L1 cache completely |
| `max_size_mb` | int | `100` | Maximum memory usage in MB |
| `swr_enabled` | bool | `True` | Enable stale-while-revalidate |
| `swr_threshold_ratio` | float | `0.5` | Refresh at X% of TTL (0.1-1.0) |
| `namespace_index` | bool | `True` | Enable fast namespace lookups |

### Intent Presets

CacheKit includes preconfigured presets for common use cases:

```python notest
from cachekit import cache

# Development: SWR only, no namespace indexing
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

| Preset | SWR | Namespace Index |
|--------|-----|-----------------|
| `minimal()` | ❌ | ❌ |
| `test()` | ❌ | ❌ |
| `dev()` | ✓ | ❌ |
| `production()` | ✓ | ✓ |
| `secure()` | ✓ | ✓ |
| `io()` | ✓ | ✓ |

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

### SWR Latency Characteristics

- **Fresh hit** (~50ns): Return from L1 memory
- **Stale hit** (~100ns): Return from L1 + start background refresh (non-blocking)
- **L2 hit** (~2ms): Miss L1, fetch from Redis
- **L2 miss** (~5-50ms): Fetch from original source, populate L1+L2

SWR keeps most hits at L1 speed, even when serving slightly stale data.

### Memory Impact

- Namespace indexing: ~100 bytes per unique namespace
- Version tracking: ~8 bytes per cached key
- Refreshing flag: ~8 bytes per key in refresh state

For typical workloads (1000s of keys), overhead is <1MB.

---

## Troubleshooting

### Problem: Another pod serves stale data after invalidation

**Cause:** Expected behavior — L1 invalidation is process-local. The invalidating process deletes the key from shared L2, but other pods keep their L1 copy until it expires.

**Solution:** Bound acceptable staleness with the entry's TTL. If a class of data cannot tolerate any cross-pod staleness window, don't cache it in L1 (`l1=L1CacheConfig(enabled=False)`).

### Problem: SWR refresh failing

**Cause:** L2 (Redis) timeout or network issue

**Behavior:** Stale L1 data continues to be served until TTL expiry. This is by design - SWR is resilient to L2 failures.

### Problem: High memory usage despite max_size_mb limit

**Cause:** L1 cache eviction churn under a working set larger than the configured budget

**Solution:** Check L1 cache hit rate and consider reducing `max_size_mb` or increasing TTL to reduce churn.

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
