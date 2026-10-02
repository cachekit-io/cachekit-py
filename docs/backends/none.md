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

## Callers Share the Cached Object

> [!WARNING]
> L1-only returns the cached object by reference. Every caller gets the same object until the entry expires, so a mutation by one caller is seen by every later caller.

```python
from cachekit import cache

@cache(backend=None)
def get_roles(user_id: int) -> list:
    return ["reader"]

roles = get_roles(1)
roles.append("admin")  # mutates the cached object

assert get_roles(1) == ["reader", "admin"]  # the next caller sees the change
assert get_roles(1) is roles
```

Treat a cached result as read-only: return an immutable value, or copy it (`copy.deepcopy`) before you change it. Adding a backend changes this; see [Upgrade Path](#upgrade-path).

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

The decorator and the function signature stay the same. What a call returns does not, because a backend stores serialized bytes instead of the object itself:

1. **Each call gets a fresh copy.** A hit decodes the stored bytes, so a caller's mutation no longer reaches other callers.
2. **Tuples come back as lists** under the default serializer, which has no tuple type. The call that computes the value returns your tuple; later hits return a list. `serializer="auto"` keeps tuples.
3. **Unsupported return types stop being cached.** L1-only caches any object. With a backend, a value the serializer cannot encode (a Pydantic model, a dataclass, a custom class, or a `set` under the default serializer) is returned to the caller but never stored, so the function runs on every call and each call logs the failure. See [Troubleshooting → Serialization Failures](../troubleshooting.md#common-errors).

Each change, on a `FileBackend` in a temporary directory:

```python
import tempfile
from dataclasses import dataclass
from pathlib import Path

from cachekit import cache
from cachekit.backends.file import FileBackend
from cachekit.backends.file.config import FileBackendConfig

def temp_backend() -> FileBackend:
    return FileBackend(FileBackendConfig(cache_dir=Path(tempfile.mkdtemp())))

# 1. Each call gets a fresh copy
@cache(backend=temp_backend())
def get_roles(user_id: int) -> list:
    return ["reader"]

roles = get_roles(1)
roles.append("admin")
assert get_roles(1) == ["reader"]  # the change stayed with the first caller

# 2. Tuples come back as lists under the default serializer
@cache(backend=temp_backend())
def get_point() -> tuple:
    return (1, 2)

assert get_point() == (1, 2)  # miss: the function's own tuple
assert get_point() == [1, 2]  # hit: decoded as a list

@cache(backend=temp_backend(), serializer="auto")
def get_point_auto() -> tuple:
    return (1, 2)

get_point_auto()
assert get_point_auto() == (1, 2)  # hit: still a tuple

# 3. Unsupported return types stop being cached
@dataclass
class Point:
    x: int
    y: int

calls = []

@cache(backend=temp_backend())
def get_origin() -> Point:
    calls.append(1)
    return Point(0, 0)

get_origin()
get_origin()
assert len(calls) == 2  # ran on both calls: nothing was stored
```

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
