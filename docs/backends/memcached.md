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
export CACHEKIT_MEMCACHED_MAX_POOL_SIZE=10        # Default: 10 per server
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
- Need TTL inspection/refresh (Memcached doesn't support it)
- Cache values exceed 1MB (Memcached default slab limit)

## Characteristics

- Latency: 1–5ms per operation (network-dependent)
- Throughput: Very high (multi-threaded C server)
- TTL support: Yes (max 30 days)
- Cross-process: Yes (shared across pods)
- Persistence: No (volatile memory only)
- Consistent hashing: Yes (via pymemcache HashClient)

## Limitations

1. **No persistence**: All data is in-memory. Server restart = data loss.
2. **No locking**: No distributed lock support (use Redis for stampede prevention).
3. **30-day TTL maximum**: TTLs exceeding 30 days are automatically clamped.
4. **1MB value limit**: Default Memcached slab size limits values to ~1MB.
5. **No TTL inspection**: Cannot query remaining TTL on a key.

## See Also

- [Backend Guide](README.md) — Backend comparison and resolution priority
- [Redis Backend](redis.md) — Persistent shared caching with locking support
- [File Backend](file.md) — Single-process local caching without infrastructure
- [Configuration Guide](../configuration.md) — Full environment variable reference

---

<div align="center">

**[GitHub Issues](https://github.com/cachekit-io/cachekit-py/issues)** · **[Documentation](../README.md)**

</div>
