**[Home](../README.md)** › **[Backends](index.md)** › **L1-Only Mode (No Backend)**

# L1-Only Mode (`backend=None`)

Use `backend=None` to run cachekit as a pure in-memory cache — no Redis, no Memcached, no external services. This is cachekit's equivalent of `functools.lru_cache`, but with all the decorator features (TTL, namespacing, metrics, encryption).

## Basic Usage

```python
from cachekit import cache

@cache(backend=None, ttl=300)
def expensive_computation(x: int) -> dict:
    return {"result": x ** 2}

# First call: computes
result = expensive_computation(42)

# Second call: served from L1 in-memory cache (~50ns)
result = expensive_computation(42)
```

No environment variables needed. No services to run. Works everywhere.

## When to Use

**Use L1-only when**:
- Building CLI tools, scripts, or batch processors
- Single-process applications (no multi-pod coordination needed)
- Local development and testing
- You want `lru_cache` but with TTL, metrics, and an upgrade path

**When NOT to use**:
- Multi-pod deployments (L1 cache is per-process, not shared)
- Need persistence across restarts (L1 is in-memory only)
- Cache must be shared between workers/processes

## How It Works

With `backend=None`, cachekit skips L2 entirely. The data flow is:

```
@cache(backend=None)
  └─ L1 In-Memory Cache (~50ns)
     ├─ Hit → return cached value
     └─ Miss → call function → store in L1 → return
```

No network calls. No serialization to bytes. No backend initialization.

## With Intent Presets

All presets work with `backend=None`:

```python notest
from cachekit import cache

# Speed-critical, no backend
@cache.minimal(backend=None, ttl=60)
def fast_lookup(key: str) -> dict:
    return fetch_data(key)

# With encryption, no backend (L1 stores ciphertext)
@cache.secure(backend=None, ttl=3600)
def sensitive_data(user_id: int) -> dict:
    return get_pii(user_id)
```

## Upgrade Path

The key advantage over `functools.lru_cache`: when you're ready to scale, just remove `backend=None`:

```python notest
# Development: L1-only
@cache(backend=None, ttl=300)
def get_user(user_id: int) -> dict:
    return db.fetch(user_id)

# Production: just remove backend=None
# Set REDIS_URL and cachekit auto-detects Redis
@cache(ttl=300)
def get_user(user_id: int) -> dict:
    return db.fetch(user_id)
```

No API changes. No code rewrite. Same decorator, same function signature.

## Characteristics

- Latency: ~50ns (in-memory, no network)
- Shared across processes: No (per-process only)
- Persistence: No (lost on restart)
- TTL support: Yes
- Encryption: Yes (L1 stores ciphertext)
- Metrics: Yes (if monitoring configured)

---

## See Also

- [Backend Overview](index.md) — Backend comparison and resolution priority
- [Redis](redis.md) — Shared distributed cache (upgrade from L1-only)
- [Getting Started](../getting-started.md) — Progressive tutorial

---

<div align="center">

**[GitHub Issues](https://github.com/cachekit-io/cachekit-py/issues)** · **[Documentation](../README.md)**

</div>
