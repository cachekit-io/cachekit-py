**[Home](../README.md)** › **[Backends](README.md)** › **L1-Only Mode (No Backend)**

# L1-Only Mode (`backend=None`)

Use `backend=None` to run cachekit as a pure in-memory cache — no Redis, no Memcached, no external services. This is cachekit's equivalent of `functools.lru_cache`, but with all the decorator features (TTL, namespacing, metrics). Encryption is the one exception — see below.

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

`@cache.minimal`, `@cache.production`, `@cache.dev` and `@cache.test` accept `backend=None`:

```python notest
from cachekit import cache

# Speed-critical, no backend
@cache.minimal(backend=None, ttl=60)
def fast_lookup(key: str) -> dict:
    return fetch_data(key)
```

Three presets do not. `@cache.secure(master_key=…, backend=None)` is refused at decoration
time with `ConfigurationError`: L1-only stores raw Python objects, which cannot be ciphertext.
The same applies to `encryption=True` and to an `EncryptionWrapper` serializer. `@cache.io`
builds its own backend and raises `ConfigurationError` on any `backend=`, `None` included.
`@cache.local` is always in-process and raises `TypeError` on a `backend=` argument.

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
- Encryption: No — `@cache.secure(master_key=…)` / `encryption=True` / `EncryptionWrapper` with `backend=None` raise `ConfigurationError` (raw objects cannot be ciphertext). A fleet-wide `CACHEKIT_MASTER_KEY` does not encrypt L1-only caches either: with the key set, `@cache(backend=None)` must state `encryption=False`, or it raises the no-intent `ConfigurationError`.
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
