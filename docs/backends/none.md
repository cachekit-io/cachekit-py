**[Home](../README.md)** › **[Backends](README.md)** › **L1-Only Mode (No Backend)**

# L1-Only Mode (`backend=None`)

Use `backend=None` to run cachekit as a pure in-memory cache — no Redis, no Memcached, no external services. This is cachekit's equivalent of `functools.lru_cache`, but with the decorator
features that do not need a backend (TTL, namespacing, metrics).

> [!WARNING]
> **Encryption is not one of them. `backend=None` does not encrypt anything.**
> L1-only mode stores **live Python object references** in process memory and never
> serializes, so the encryption layer is never reached: a `master_key` is validated
> at decoration time and then discarded. `@cache.secure(master_key=..., backend=None)`
> raises nothing and encrypts nothing — the values stay readable in a heap or core
> dump. The same object is handed to every caller, so mutating a returned value
> corrupts the cached entry for everyone else. Use L1-only mode for non-sensitive
> data; for encrypted caching pass a real backend (`RedisBackend`,
> `CachekitIOBackend`, …), where L1 then holds ciphertext like L2 does. See
> [Zero-Knowledge Encryption](../features/zero-knowledge-encryption.md).

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

All presets accept `backend=None`, but a preset's backend-dependent behaviour does
not survive it — `@cache.secure` in particular accepts the key and encrypts nothing
(see the warning above):

```python notest
from cachekit import cache

# Speed-critical, no backend
@cache.minimal(backend=None, ttl=60)
def fast_lookup(key: str) -> dict:
    return fetch_data(key)

# Reliability features, no backend
@cache.production(backend=None, ttl=300)
def resilient_lookup(key: str) -> dict:
    return fetch_data(key)

# NOT supported: @cache.secure(backend=None) stores plaintext objects, not ciphertext.
# For encrypted caching, pass a real backend:
#     @cache.secure(master_key=os.environ["CACHEKIT_MASTER_KEY"], backend=RedisBackend(...))
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

- [Backend Overview](README.md) — Backend comparison and resolution priority
- [Redis](redis.md) — Shared distributed cache (upgrade from L1-only)
- [Getting Started](../getting-started.md) — Progressive tutorial

---

<div align="center">

**[GitHub Issues](https://github.com/cachekit-io/cachekit-py/issues)** · **[Documentation](../README.md)**

</div>
