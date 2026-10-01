**[Home](../README.md)** › **[Backends](README.md)** › **Memcached Backend**

# Memcached Backend

> Requires: `pip install cachekit[memcached]`

Store cache in Memcached with consistent hashing across multiple servers. High-throughput, volatile in-memory caching shared across processes and pods.

## Basic Usage

```python notest
from cachekit.backends.memcached import MemcachedBackend, MemcachedBackendConfig
from cachekit import cache

# Use default configuration (127.0.0.1:11211)
backend = MemcachedBackend()

@cache(backend=backend)
def cached_function():
    return expensive_computation()
```

## Configuration via Environment Variables

```bash
# Server list (JSON array format)
export CACHEKIT_MEMCACHED_SERVERS='["mc1:11211", "mc2:11211"]'

# Timeouts
export CACHEKIT_MEMCACHED_CONNECT_TIMEOUT=2.0    # Default: 2.0 seconds
export CACHEKIT_MEMCACHED_TIMEOUT=1.0             # Default: 1.0 seconds

# Connection pool
export CACHEKIT_MEMCACHED_MAX_POOL_SIZE=10        # Default: 10 connections per server (see Concurrency)
export CACHEKIT_MEMCACHED_RETRY_ATTEMPTS=2        # Default: 2

# Optional key prefix
export CACHEKIT_MEMCACHED_KEY_PREFIX="myapp:"     # Default: "" (none)
```

## Configuration via Python

Config objects don't require a running Memcached server:

```python
from cachekit.backends.memcached import MemcachedBackendConfig

config = MemcachedBackendConfig(
    servers=["mc1:11211", "mc2:11211", "mc3:11211"],
    connect_timeout=1.0,
    timeout=0.5,
    max_pool_size=20,
    key_prefix="myapp:",
)
```

To use the config with a live backend:

```python notest
from cachekit.backends.memcached import MemcachedBackend, MemcachedBackendConfig

config = MemcachedBackendConfig(
    servers=["mc1:11211", "mc2:11211", "mc3:11211"],
    connect_timeout=1.0,
    timeout=0.5,
    max_pool_size=20,
    key_prefix="myapp:",
)

backend = MemcachedBackend(config)
```

## When to Use

**Use MemcachedBackend when**:
- Hot in-memory caching with sub-millisecond reads
- Shared cache across multiple processes/pods (like Redis but simpler)
- High-throughput read-heavy workloads
- Applications already using Memcached infrastructure

**When NOT to use**:
- Need persistence (Memcached is volatile — data lost on restart)
- Need distributed locking (use [Redis](redis.md) instead)
- Need automatic sliding expiration via `refresh_ttl_on_get` (requires TTL *inspection*, which Memcached lacks — see [TTL Inspection & Refresh](#ttl-inspection--refresh))
- Cache values exceed 1MB (Memcached default slab limit)

## Characteristics

- Latency: 1–5ms per operation (network-dependent)
- Throughput: Very high (multi-threaded C server)
- TTL support: Yes (max 30 days); `refresh_ttl` yes (via `touch`), `get_ttl` no
- Cross-process: Yes (shared across pods)
- Persistence: No (volatile memory only)
- Consistent hashing: Yes (via pymemcache HashClient)
- Thread-safe: Yes (per-server connection pool; see [Concurrency](#concurrency))

## Concurrency

Each server gets a pool of up to `max_pool_size` connections (default 10). Every operation
checks out its own connection, so threads sharing one backend never share a socket.

- **The pool does not wait.** An operation that would need connection `max_pool_size + 1`
  to one server, in one process, raises a TRANSIENT `BackendError` at once. pymemcache does
  not mark the server failed, and later operations succeed once connections are returned.
  Under `@cache` it is handled like any backend error: a failed read is a miss and the
  function runs, and a failed write skips L2 only. Backend errors do not currently count
  toward the [circuit breaker](../features/circuit-breaker.md#integration-with-caching), so
  pool exhaustion does not open it. Set `max_pool_size` at or above the number of threads
  that can hit one server at the same time.
- **Server recovery is not race-free.** pymemcache does not lock its server-failover state.
  While a failed server is being retried, concurrent operations on it can raise a spurious
  `BackendError` wrapping a `KeyError`, whether or not the command itself ran. Under `@cache`
  it degrades like any other backend error.
- **Not fork-safe.** A child process must not reuse a backend its parent created: the pooled
  sockets would be shared across processes. Create the backend (or make the first cached
  call) after `fork()`, for example in a pre-fork server's post-fork worker hook.

## Limitations

1. **No persistence**: All data is in-memory. Server restart = data loss.
2. **No locking**: No distributed lock support (use Redis or CachekitIO for stampede prevention).
3. **30-day TTL maximum**: TTLs exceeding 30 days are automatically clamped.
4. **1MB value limit**: Default Memcached slab size limits values to ~1MB.
5. **No TTL inspection (`get_ttl`)**: see below — `refresh_ttl` is supported, inspection is not.

## TTL Inspection & Refresh

Memcached ships only **half** of the `TTLInspectableBackend` protocol, by design:

- **`refresh_ttl` — supported.** Backed by the Memcached `touch` command; call it directly to
  extend a key's life (returns `True` if the key existed, `False` if it was already gone).
- **`get_ttl` — not supported.** The classic Memcached protocol has no command to *read* a
  key's remaining TTL, and pymemcache's `HashClient` exposes no meta protocol (`mg <key> t`,
  memcached ≥ 1.6). Without `get_ttl`, Memcached is **not** a `TTLInspectableBackend`.

Consequence: `@cache(..., refresh_ttl_on_get=True)` performs *threshold-based* sliding
expiration, which needs to read the remaining TTL first — so it **does not apply to
Memcached** and is ignored (the decorator warns once, then continues serving the hit). For
automatic sliding expiration use the [Redis](redis.md), [CachekitIO](cachekitio.md), or
[File](file.md) backend. You can still call `backend.refresh_ttl(key, ttl)` yourself.

## See Also

- [Backend Guide](README.md) — Backend comparison and resolution priority
- [Redis Backend](redis.md) — Persistent shared caching with locking support
- [File Backend](file.md) — Single-process local caching without infrastructure
- [Configuration Guide](../configuration.md) — Full environment variable reference

---

<div align="center">

**[GitHub Issues](https://github.com/cachekit-io/cachekit-py/issues)** · **[Documentation](../README.md)**

</div>
