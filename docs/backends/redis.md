**[Home](../README.md)** › **[Backends](index.md)** › **Redis Backend**

# Redis Backend

The default L2 backend. Connects to Redis via environment variable or explicit configuration. Production-grade shared caching across multiple processes and pods.

## Basic Usage

```python
from cachekit.backends import RedisBackend
from cachekit import cache

# Explicit backend configuration
backend = RedisBackend()

@cache(backend=backend)
def cached_function():
    return expensive_computation()
```

RedisBackend reads `REDIS_URL` or `CACHEKIT_REDIS_URL` from the environment automatically. No configuration needed for the common case.

## Configuration via Environment Variables

```bash
CACHEKIT_REDIS_URL=redis://prod.example.com:6379  # Primary
REDIS_URL=redis://localhost:6379                  # Fallback
```

`CACHEKIT_REDIS_URL` takes precedence over `REDIS_URL`. If neither is set and no explicit backend is configured, cachekit will attempt to connect to `redis://localhost:6379`.

## Configuration via Python

```python notest
from cachekit.backends.redis import RedisBackend
from cachekit.backends.redis.config import RedisBackendConfig

config = RedisBackendConfig(
    redis_url="redis://prod.example.com:6379",
    connection_pool_size=25,
    socket_keepalive=True,
    disable_hiredis=False,
)

backend = RedisBackend(config)
```

| Field | Default | Description |
|-------|---------|-------------|
| `redis_url` | `redis://localhost:6379` | Redis connection URL |
| `connection_pool_size` | `10` | Maximum connections in the pool |
| `socket_keepalive` | `True` | Enable TCP keepalive for connections |
| `disable_hiredis` | `False` | Use pure Python parser instead of hiredis |

## When to Use

**Use RedisBackend when**:
- You need sub-10ms latency with shared cache
- Cache is shared across multiple processes or pods
- You need persistence options (RDB/AOF)
- You're building a typical web application
- You require distributed caching

**When NOT to use**:
- Sub-millisecond latency requirements — use L1 cache only
- Offline or air-gapped environments without a Redis instance
- Single-process scripts where [FileBackend](file.md) is simpler

## Characteristics

- Network latency: ~1–7ms per operation
- Automatic TTL support (Redis `EXPIRE`)
- Connection pooling built-in
- Supports large values (up to Redis limits)
- Cross-process: Yes (shared across pods)
- Persistence: Yes (RDB/AOF, server-configured)
- Distributed locking: Yes

## Limitations

1. **Network dependency**: Every L2 operation requires a network round-trip. Use L1 cache to mitigate (enabled by default).
2. **Redis instance required**: Unlike FileBackend, RedisBackend requires a running Redis server.
3. **No TTL inspection**: The base RedisBackend does not expose remaining TTL on cached keys.
4. **Connection pool exhaustion**: Under very high concurrency, the connection pool (`connection_pool_size=10`) can become a bottleneck. Increase via config or env var.

## See Also

- [Backend Guide](index.md) — Backend comparison and resolution priority
- [Memcached Backend](memcached.md) — Alternative in-memory shared backend
- [CachekitIO Backend](cachekitio.md) — Managed SaaS alternative to self-hosted Redis
- [Configuration Guide](../configuration.md) — Full environment variable reference

---

<div align="center">

**[GitHub Issues](https://github.com/cachekit-io/cachekit-py/issues)** · **[Documentation](../README.md)**

</div>
