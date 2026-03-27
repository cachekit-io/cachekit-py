**[Home](../README.md)** › **Backends**

# Backend Guide

Pluggable L2 cache storage for cachekit. Four backends are included out of the box — Redis (default), File (local), Memcached, and CachekitIO (managed SaaS). You can also implement custom backends for any key-value store.

## Overview

cachekit uses a protocol-based backend abstraction (PEP 544) that allows pluggable storage backends for L2 cache. The `BaseBackend` protocol defines a minimal synchronous interface — four methods — that any backend must implement to be compatible with cachekit.

**Key insight**: Backends are completely optional. If you don't specify a backend, cachekit uses RedisBackend with your configured Redis connection.

## BaseBackend Protocol

All backends must implement this protocol to be compatible with cachekit:

```python
from typing import Optional, Protocol

class BaseBackend(Protocol):
    """Protocol defining the L2 backend storage contract."""

    def get(self, key: str) -> Optional[bytes]:
        """Retrieve value from backend storage.

        Args:
            key: Cache key to retrieve

        Returns:
            Bytes value if found, None if key doesn't exist

        Raises:
            BackendError: If backend operation fails
        """
        ...

    def set(self, key: str, value: bytes, ttl: Optional[int] = None) -> None:
        """Store value in backend storage.

        Args:
            key: Cache key to store
            value: Bytes value (encrypted or plaintext msgpack)
            ttl: Time-to-live in seconds (None = no expiry)

        Raises:
            BackendError: If backend operation fails
        """
        ...

    def delete(self, key: str) -> bool:
        """Delete key from backend storage.

        Args:
            key: Cache key to delete

        Returns:
            True if key was deleted, False if key didn't exist

        Raises:
            BackendError: If backend operation fails
        """
        ...

    def exists(self, key: str) -> bool:
        """Check if key exists in backend storage.

        Args:
            key: Cache key to check

        Returns:
            True if key exists, False otherwise

        Raises:
            BackendError: If backend operation fails
        """
        ...
```

## Backend Comparison

| Backend | Latency | Persistence | Cross-Process | TTL | Locking |
|---------|---------|-------------|---------------|-----|---------|
| **L1 (In-Memory)** | ~50ns | No | No | No | No |
| **[File](file.md)** | 100μs–5ms | Yes (disk) | No | Yes | File locks |
| **[Redis](redis.md)** | 1–7ms | Yes (RDB/AOF) | Yes | Yes | Yes |
| **[Memcached](memcached.md)** | 1–5ms | No | Yes | Yes (max 30d) | No |
| **[CachekitIO](cachekitio.md)** | ~10–50ms | Yes | Yes | Yes | Yes |
| **[HTTP (custom)](custom.md)** | 10–100ms | Varies | Yes | Varies | Varies |
| **[DynamoDB (custom)](custom.md)** | 100–500ms | Yes | Yes | Yes | No |

## When to Use Which Backend

**Use [FileBackend](file.md) when**:
- You're building single-process applications (scripts, CLI tools)
- You're in development and don't have Redis available
- You need local caching without network overhead
- You have modest cache sizes (< 10GB)
- Your application runs on a single machine

**Use [RedisBackend](redis.md) when**:
- You need sub-10ms latency with shared cache
- Cache is shared across multiple processes
- You need persistence options
- You're building a typical web application
- You require multi-process or distributed caching

**Use [MemcachedBackend](memcached.md) when**:
- Hot in-memory caching with very high throughput
- Simple key-value caching without persistence needs
- Existing Memcached infrastructure you want to reuse
- Read-heavy workloads where sub-5ms latency is sufficient

**Use [CachekitIOBackend](cachekitio.md) when** *(closed alpha — [request access](https://cachekit.io))*:
- You want managed, zero-ops distributed caching
- Multi-region caching without operating Redis
- Building zero-knowledge architecture with `@cache.secure`
- Team velocity matters more than absolute lowest latency

**Use a [custom HTTPBackend](custom.md) when**:
- You're integrating a cloud cache service with a non-standard API
- Your cache needs to be globally distributed via a custom service
- You want to decouple cache from application with your own HTTP layer

**Use [DynamoDBBackend](custom.md) when**:
- You're fully on AWS and serverless
- You don't want to manage infrastructure
- Cache traffic is low/bursty
- You need automatic TTL management

**Use L1-only when**:
- You're in development with single-process code
- You have a single-process application
- You don't need cross-process cache sharing
- You need the lowest possible latency (nanoseconds)

## Backend Resolution Priority

When `@cache` is used without an explicit `backend` parameter, resolution follows this priority:

### 1. Explicit Backend Parameter (Highest Priority)

```python notest
from cachekit.backends.cachekitio import CachekitIOBackend

custom_backend = CachekitIOBackend()

@cache(backend=custom_backend)  # Uses custom backend explicitly
def explicit_backend():
    return data()
```

`@cache.io()` uses this same mechanism — it calls `DecoratorConfig.io()` which constructs a `CachekitIOBackend` and passes it as an explicit `backend` kwarg. No magic, just convenience.

### 2. Module-Level Default Backend (Middle Priority)

```python notest
from cachekit import cache
from cachekit.config.decorator import set_default_backend
from cachekit.backends.file import FileBackend, FileBackendConfig

# Set once at application startup
file_backend = FileBackend(FileBackendConfig(cache_dir="/var/cache/myapp"))
set_default_backend(file_backend)

# All decorators now use file backend — no backend= needed
@cache.minimal(ttl=300)
def fast_lookup():
    return data()

@cache.production(ttl=600)
def critical_function():
    return data()
```

Call `set_default_backend(None)` to clear the default. Works with any backend (Redis, File, CachekitIO, custom).

### 3. Environment Variable Auto-Detection (Lowest Priority)

```bash
# Primary: CACHEKIT_REDIS_URL
CACHEKIT_REDIS_URL=redis://prod.example.com:6379/0

# Fallback: REDIS_URL
REDIS_URL=redis://localhost:6379/0
```

If no explicit backend and no module-level default, cachekit creates a RedisBackend from environment variables.

**Resolution order**:
1. Check for explicit `backend` parameter in `@cache(backend=...)`
2. Check for module-level default via `set_default_backend()`
3. Create RedisBackend from environment variables (CACHEKIT_REDIS_URL > REDIS_URL)

## Performance Considerations

### Backend Latency Comparison

| Backend | Latency | Use Case | Notes |
|---------|---------|----------|-------|
| **L1 (In-Memory)** | ~50ns | Repeated calls in same process | Process-local only |
| **File** | 100μs-5ms | Single-process local caching | Development, scripts, CLI tools |
| **Redis** | 1-7ms | Shared cache across pods | Production default |
| **CachekitIO** | ~10-50ms | Managed SaaS, zero-ops | HTTP/2, region-dependent; closed alpha |
| **HTTP API** | 10-100ms | Custom cloud services | Network dependent |
| **DynamoDB** | 100-500ms | Serverless, low-traffic | High availability |
| **Memcached** | 1-5ms | Alternative to Redis | No persistence |

---

## Backend Pages

- [Redis Backend](redis.md) — Default, production-grade, shared cache
- [File Backend](file.md) — Local disk, single-process, no infrastructure
- [Memcached Backend](memcached.md) — High-throughput, volatile, multi-process
- [CachekitIO Backend](cachekitio.md) — Managed SaaS, zero-ops, zero-knowledge
- [Custom Backends](custom.md) — HTTP, DynamoDB, and your own implementations

## See Also

- [API Reference](../api-reference.md) - Decorator parameters
- [Configuration Guide](../configuration.md) - Environment setup
- [Zero-Knowledge Encryption](../features/zero-knowledge-encryption.md) - Client-side encryption
- [Data Flow Architecture](../data-flow-architecture.md) - How backends fit in the system

---

<div align="center">

**[GitHub Issues](https://github.com/cachekit-io/cachekit-py/issues)** · **[Documentation](../README.md)**

*Last Updated: 2026-03-18*

</div>
