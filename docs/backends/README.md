**[Home](../README.md)** › **Backends**

# Backend Guide

Pluggable L2 cache storage for cachekit. Four backends are included out of the box — Redis (default), File (local), Memcached, and CachekitIO (managed SaaS). You can also run in [L1-only mode](none.md) with `backend=None`, or implement custom backends for any key-value store.

## Overview

cachekit uses a protocol-based backend abstraction (PEP 544) that allows pluggable storage backends for L2 cache. The `BaseBackend` protocol defines a minimal synchronous interface — five methods — that any backend must implement to be compatible with cachekit.

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
            True if key was deleted, False if key didn't exist. CachekitIO returns
            True on every successful delete, whether or not the key existed: the
            server does not report existence on DELETE.

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

    def health_check(self) -> tuple[bool, dict]:
        """Check backend health status.

        Returns:
            Tuple of (is_healthy, details_dict)
            Details must include 'latency_ms' and 'backend_type'

        Raises:
            BackendError: If backend operation fails
        """
        ...
```

## Backend Comparison

| Backend | Latency | Persistence | Cross-Process | TTL | Locking |
|---------|---------|-------------|---------------|-----|---------|
| **L1 (In-Memory)** | ~50ns | No | No | No | No |
| **[File](file.md)** | set = fsync + a scan of every entry, so slow on a large cache; get waits on a concurrent set ([cost model](file.md#performance-characteristics)) | Yes (disk) | Format shared; one writer process | Yes | File locks |
| **[Redis](redis.md)** | 1–7ms | Yes (RDB/AOF) | Yes | Yes | Yes |
| **[Memcached](memcached.md)** | 1–5ms | No | Yes | Yes (max 30d) | No |
| **[CachekitIO](cachekitio.md)** | 42–45ms p50 hit, measured from MEL ([varies by vantage](cachekitio.md#characteristics)) | Yes | Yes | Yes | Yes |
| **[HTTP (custom)](custom.md)** | 10–100ms | Varies | Yes | Varies | Varies |
| **[DynamoDB (custom)](custom.md)** | 100–500ms | Yes | Yes | Yes | No |

**TTL inspection/refresh** (`refresh_ttl_on_get` sliding expiration, via the
`TTLInspectableBackend` protocol): supported by **File**, **Redis**, and **CachekitIO**.
**Memcached** supports `refresh_ttl` (via `touch`) directly but not `get_ttl`, so
`refresh_ttl_on_get` does not apply to it (see [Memcached](memcached.md#ttl-inspection--refresh)).

**Cross-process whole-function invalidation** (a server-side key registry, via the
`KeyTrackableBackend` protocol): supported only by the tenant-scoped **Redis** backend that env
auto-detection and `RedisBackendProvider` hand out. Every
other backend — including a `RedisBackend` passed as `backend=` — deletes only the keys the
calling process knows (see [Whole-Function Invalidation](../features/l1-invalidation.md#whole-function-invalidation)).

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

**Use [CachekitIOBackend](cachekitio.md) when** *(closed beta — [request access](https://cachekit.io))*:
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

`@cache.io()` uses this same mechanism — it calls `DecoratorConfig.io()` which constructs a `CachekitIOBackend` (from `api_key=` or `CACHEKIT_API_KEY`) and passes it as an explicit `backend` kwarg. No magic, just convenience. Because the preset owns its backend, `@cache.io(backend=...)` raises `ConfigurationError` rather than silently ignoring the argument.

A backend inside `config=` counts as explicit too: `@cache(config=DecoratorConfig.production(backend=b))` uses `b` even when `set_default_backend()` is set. Only a `backend=` kwarg beats it.

### 2. Module-Level Default Backend (Middle Priority)

```python
import tempfile

from cachekit import cache
from cachekit.config.decorator import set_default_backend
from cachekit.backends.file import FileBackend, FileBackendConfig

# Set once at application startup
file_backend = FileBackend(FileBackendConfig(cache_dir=tempfile.mkdtemp()))
set_default_backend(file_backend)

# All decorators now use file backend — no backend= needed
@cache.minimal(ttl=300)
def fast_lookup(x: int) -> int:
    return x * 2

@cache.production(ttl=600)
def critical_function(x: int) -> int:
    return x * 3

assert fast_lookup(2) == 4 and critical_function(2) == 6

set_default_backend(None)  # clear the default
```

Call `set_default_backend(None)` to clear the default. Works with any backend (Redis, File, CachekitIO, custom).

**Import order does not matter, but configuration must happen before a decorated
function's first call.** A decorator applied
without `backend=` pins the default when it is first seen — at decoration if
already set, otherwise at first call — so the usual layout (business modules
imported at the top of the file, `set_default_backend()` in `main()`) works.
Later `set_default_backend()` calls do not re-point already-pinned functions.
Exception: an explicit `stale_ttl` validates SWR capability at decoration, so set a
CachekitIO default *before* importing modules that use it. `@cache.io` is not affected:
it builds its own `CachekitIOBackend` and never consults `set_default_backend()`.

### 3. Environment Variable Auto-Detection (Lowest Priority)

If no explicit backend and no module-level default, `DefaultBackendProvider`
picks a backend at the function's first call from the one environment selector that is
set. The four prefixed selectors are mutually exclusive, with no precedence between them:
set exactly one.

| Environment variable        | Backend            |
|-----------------------------|--------------------|
| `CACHEKIT_API_KEY`          | `CachekitIOBackend` (SaaS) |
| `CACHEKIT_REDIS_URL`        | Redis (tenant-scoped, keys prefixed `t:{tenant}:`) |
| `CACHEKIT_MEMCACHED_SERVERS`| `MemcachedBackend` |
| `CACHEKIT_FILE_CACHE_DIR`   | `FileBackend`      |
| none of the above: `REDIS_URL`, or nothing set | Redis, as above (localhost fallback) |

Setting more than one of the four `CACHEKIT_*` selectors is ambiguous and raises
`ConfigurationError` at first call. The decorator catches it, logs a WARNING on
the `cachekit.decorators.orchestrator` logger, and runs the function uncached:

```text
Cache operation 'client_creation' failed for key '<redacted:...>': ConfigurationError
```

The misconfiguration never heals on its own. After 5 failures within 60 s (the default) the
function's circuit breaker opens and logs one `transitioned to OPEN` WARNING on
`cachekit.reliability.circuit_breaker`. Calls keep running uncached, but no longer log each
failure at WARNING: the breaker re-probes every `recovery_timeout` (30 s by default), and each
probe logs one more `client_creation` failure and one more OPEN WARNING. The function's
`get_health_status()` reports the breaker as `open` and `check_health()` as unhealthy.

`REDIS_URL` is a 12-factor fallback and never counts as a conflict.

The Redis prefix scopes L2 only. L1 is shared by every tenant in the process; see
[Whole-Function Invalidation → Tenant scope](../features/l1-invalidation.md#whole-function-invalidation).

Set the tenant with `tenant_context` from `cachekit.backends.redis.provider`, to a `str`,
`bytes`, `int` or `UUID`. The prefix is the id's text, `str()` for an `int` or `UUID`: `1`,
`"1"` and `b"1"` are one tenant (`t:1:`), as are a `UUID` and `str(uuid)`, but `"01"` or an
upper-case UUID string is another. With env auto-detection, a call with no tenant set uses
`default`. If two kinds of tenant can share an id, namespace them before setting
`tenant_context`: `"org:1"`, `"team:1"`.

Any other type, such as a `float`, a `bool`, an `IntEnum` or an arbitrary object, is a bug in
the caller. The decorated call raises `TypeError` before the function runs, sync and async
alike. It raises on an L1 hit too, and while the circuit breaker is open. It is not treated as
a cache fault: the call does not fall back to running uncached, and it does not count against
the function's circuit breaker, which every tenant of that function shares.

The check needs the function's backend. A function whose backend is resolved at its first call
(see the resolution order below) skips the check until then. That covers a call made while the
circuit breaker is open before the backend was ever resolved, which runs the function uncached,
and an L1 hit on an async function before its first L1 miss. The check is on the id's type only. L1 is shared by every tenant (see
above), so do not rely on the `TypeError` to keep tenants apart.

**Resolution order**:
1. Explicit `backend` parameter in `@cache(backend=...)`, then a backend inside `config=`
2. Module-level default via `set_default_backend()` (checked at decoration, and
   again at first call if still unset)
3. Environment auto-detection per the table above

#### Upgrading to 0.20.0

If your deployment used cachekit under more than one tenant, purge the Redis entries that
earlier releases wrote. A call with no tenant set counts as the tenant `default`. A deployment
that only ever used one tenant is unaffected.

In earlier releases, a decorated function that resolved Redis from the environment
(`CACHEKIT_REDIS_URL`, `REDIS_URL` or the localhost default), or a backend taken from
`RedisBackendProvider.get_backend()`, stayed bound to the tenant that was current when the
backend was first obtained. Every tenant's L2 writes through it then landed under that one
tenant's `t:<tenant>:` prefix. The binding was per function, so a process serving one tenant
can still have left residue: for example, if an `invalidate_cache()` under another tenant
bound the function first, or if the process called `tenant_context.set("default")` before
`get_backend()`, as the earlier distributed-locking example did.

After the upgrade, the bound tenant keeps reading those entries as its own, and some of them
hold another tenant's value. Entries written with `ttl=None`, or kept alive by
`refresh_ttl_on_get=True`, never expire. A no-argument `invalidate_cache()` reaches only the
entries the calling tenant has read in that process since it started, because no key
registry recorded them.

1. Stop every process that reads or writes the cache, whatever release it runs. Stopping only
   the earlier releases is not enough: `SCAN` does not block reads, so a 0.20.0 process serving
   requests during the purge can read an entry still under the wrong tenant's prefix and
   return another tenant's value.
2. Delete every `t:*` key in each database cachekit uses. Run `FLUSHDB` instead only if the
   database is dedicated to cachekit. Use the Python client that cachekit installs, not a
   `redis-cli --scan` pipeline: a key set through `key=` can contain a newline, which a
   line-based pipeline splits into names that match nothing, so the key survives and the
   pipeline still exits 0. `scan_iter` returns each key whole, as bytes.

   ```python notest
   # Needs a live Redis: set the URL, then run once per database cachekit uses.
   import redis

   r = redis.Redis.from_url("redis://localhost:6379/0")
   pattern = "t:*"

   batch = []
   for key in r.scan_iter(match=pattern, count=1000):
       batch.append(key)
       if len(batch) == 1000:
           r.unlink(*batch)
           batch.clear()
   if batch:
       r.unlink(*batch)

   left = sum(1 for _ in r.scan_iter(match=pattern, count=1000))
   print(f"{left} keys left matching {pattern}")  # expect 0
   ```

3. Start processes on 0.20.0 only after the script reports 0 keys left in every database.
   Expect a cold cache.

On a database other applications share, `t:*` also matches their keys that start with `t:`.
Run the same script once per tenant instead, with `pattern = "t:<tenant>:*"`, for `default`
and for each tenant you have set. Percent-encode the tenant with
`urllib.parse.quote(tenant, safe='')`, converting an `int` or `UUID` tenant with `str()` first:
tenant `org:123` is `pattern = "t:org%3A123:*"`. The encoding also escapes `*`, `?` and `[`,
so a tenant id cannot widen the pattern.

Other changes you may notice:

- `RedisBackendProvider.get_backend()` no longer binds its backend to one tenant. The backend
  follows `tenant_context` on every operation, and falls back to the tenant that was current at
  the `get_backend()` call only when the calling context has none. For a backend bound to one
  tenant, construct `cachekit.backends.redis.provider.PerRequestRedisBackend(client, tenant)`
  directly, with `client` a `redis.Redis`.
- A no-argument `invalidate_cache()` on the Redis backend now deletes only the calling tenant's
  L2 entries.
- `int` and `UUID` tenants now work, keyed by their `str()` form (`42` and `"42"` share
  `t:42:`). No earlier release supported them. Any type other than `str`, `bytes`, `int` and
  `uuid.UUID` raises `TypeError`, as above; this includes a `bool`, an `IntEnum` member and a
  `bytearray`. Convert an `IntEnum` member with `int()` and a `bytearray` with `bytes()` first.

## Performance Considerations

### Backend Latency Comparison

| Backend | Latency | Use Case | Notes |
|---------|---------|----------|-------|
| **L1 (In-Memory)** | ~50ns | Repeated calls in same process | Process-local only |
| **File** | set = fsync, flat in cache size, except eviction, rejection and the 30 s rescan, which scan the directory; get waits on a concurrent set | Single-process local caching | Development, scripts, CLI tools |
| **Redis** | 1-7ms | Shared cache across pods | Production default |
| **CachekitIO** | 42–45ms p50 hit, measured from MEL | Managed SaaS, zero-ops | HTTPS (HTTP/1.1, async calls on a worker thread); depends on vantage, store region and edge hits ([measured](cachekitio.md#characteristics)); closed beta |
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

</div>
