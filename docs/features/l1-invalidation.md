**[Home](../README.md)** › **L1 Cache Invalidation**

# L1 Cache Invalidation and Stale-While-Revalidate (SWR)

> **For multi-pod deployments**: Ensure L1 caches remain consistent across pods with automatic invalidation and intelligent freshness management.

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

## Multi-Level Invalidation API

CacheKit supports invalidation at three granularity levels, allowing you to target exactly what needs refreshing:

### Level 1: Global Invalidation

Clear the **entire L1 cache** across all pods:

```python notest
from cachekit import cache

# Invalidate all entries
cache.invalidate_all()
```

**Use cases:**
- Schema migrations
- Security breaches requiring complete refresh
- Emergency cache flush

**Returns:** Number of entries invalidated

**Effect across pods:** Broadcast via Redis Pub/Sub - all pods receive invalidation event and clear their L1 caches.

### Level 2: Namespace Invalidation

Clear all entries in a **specific namespace**:

```python notest
from cachekit import cache

# Clear all user-related caches
cache.invalidate_namespace("users")

# Clear all product caches
cache.invalidate_namespace("products")
```

**Use cases:**
- Bulk updates (all users updated)
- Tenant deletion
- Category refresh

**Returns:** Number of entries invalidated

**Effect across pods:** Only entries matching the namespace are cleared.

**How namespaces work:**
- Namespaces are automatic based on function signature
- You can also set custom namespace per decorator

```python notest
from cachekit import cache

@cache(namespace="users")
def get_user(user_id: int):
    return db.query(f"SELECT * FROM users WHERE id={user_id}")

# Later, invalidate all user caches
cache.invalidate_namespace("users")
```

### Level 3: Per-Function Invalidation

Clear cache for a **specific function call**:

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

**Returns:** Boolean indicating success

**Effect across pods:** Only the specific cache key is cleared.

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

    # Invalidation Settings
    invalidation_enabled=True,       # Enable invalidation channels (default: True)
    namespace_index=True,            # Enable O(1) namespace lookups (default: True)
)
```

| Field | Type | Default | Purpose |
|-------|------|---------|---------|
| `enabled` | bool | `True` | Enable/disable L1 cache completely |
| `max_size_mb` | int | `100` | Maximum memory usage in MB |
| `swr_enabled` | bool | `True` | Enable stale-while-revalidate |
| `swr_threshold_ratio` | float | `0.5` | Refresh at X% of TTL (0.1-1.0) |
| `invalidation_enabled` | bool | `True` | Enable invalidation broadcasts |
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

| Preset | SWR | Invalidation | Namespace Index |
|--------|-----|--------------|-----------------|
| `minimal()` | ❌ | ❌ | ❌ |
| `test()` | ❌ | ❌ | ❌ |
| `dev()` | ✓ | ❌ | ❌ |
| `production()` | ✓ | ✓ | ✓ |
| `secure()` | ✓ | ✓ | ✓ |

---

## Common Patterns

### Pattern 1: Multi-Pod Consistency

Ensure all pods serve fresh data after an update:

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

    # Invalidate cache - broadcasts to all pods
    get_user.invalidate_cache(user_id=user_id)

    return {"status": "updated"}
```

### Pattern 2: Bulk Operations

Invalidate multiple related caches:

```python notest
@cache(namespace="products")
def get_product(product_id: int):
    return db.get_product(product_id)

@cache(namespace="products")
def get_product_reviews(product_id: int):
    return db.get_reviews(product_id)

# Category discount: invalidate all product caches
def apply_category_discount(category_id: int, discount: float):
    db.update_category_discount(category_id, discount)

    # One call clears both get_product and get_product_reviews
    cache.invalidate_namespace("products")
```

### Pattern 3: Emergency Shutdown

Clear all caches when disaster occurs:

```python notest
from cachekit import cache

# On security breach, schema change, etc.
cache.invalidate_all()
```

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

### Broadcast Overhead

Invalidation broadcasts are:
- **Asynchronous**: Don't block the invalidating pod
- **Fire-and-forget**: Delivered via Redis Pub/Sub (at-most-once)
- **Per-pod local**: Each pod handles its own L1 deletion

---

## Troubleshooting

### Problem: Stale data persists after invalidation

**Cause:** Invalidation broadcast not received (Redis disconnected)

**Solution:**
```python notest
# Check if invalidation channel is connected
from cachekit.l1_cache import get_l1_cache_manager

manager = get_l1_cache_manager()
if manager.invalidation_channel and manager.invalidation_channel.is_available():
    print("Invalidation channel connected")
else:
    print("WARNING: Invalidation not broadcasting - data may become stale")
```

### Problem: SWR refresh failing

**Cause:** L2 (Redis) timeout or network issue

**Behavior:** Stale L1 data continues to be served until TTL expiry. This is by design - SWR is resilient to L2 failures.

### Problem: High memory usage despite max_size_mb limit

**Cause:** L1 cache eviction strategy, or invalidation_enabled=False preventing eviction

**Solution:** Check L1 cache hit rate and consider reducing `max_size_mb` or increasing TTL to reduce churn.

---

## See Also

- [Configuration Guide](../configuration.md) - Complete configuration reference
- [Getting Started](../getting-started.md) - Quick start guide
- [Zero-Knowledge Encryption](zero-knowledge-encryption.md) - Secure caching
- [API Reference](../api-reference.md) - All decorator parameters
