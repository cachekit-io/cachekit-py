**[Home](../README.md)** › **Guides** › **Backend Guide**

# Backend Guide - Custom Cache Backends

Implement custom storage backends for L2 cache beyond the default Redis.

## Overview

cachekit uses a protocol-based backend abstraction (PEP 544) that allows pluggable storage backends for L2 cache. While Redis is the default, you can implement custom backends for HTTP APIs, DynamoDB, file storage, or any key-value store.

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

## Built-in Backends

### RedisBackend (Default)

The default backend connects to Redis via REDIS_URL or CACHEKIT_REDIS_URL:

```python
from cachekit.backends import RedisBackend
from cachekit import cache

# Explicit backend configuration
backend = RedisBackend()

@cache(backend=backend)
def cached_function():
    return expensive_computation()
```

**When to use**:
- Production applications
- High-performance requirements
- Shared cache across multiple processes/pods
- Need for cache expiration (TTL)

**Characteristics**:
- Network latency: ~1-7ms per operation
- Automatic TTL support (Redis EXPIRE)
- Connection pooling built-in
- Supports large values (up to Redis limits)

### CachekitIOBackend (Managed SaaS)

> *cachekit.io is in closed alpha — [request access](https://cachekit.io)*

`CachekitIOBackend` connects to the cachekit.io managed cache API over HTTP/2. It implements the full `BaseBackend` protocol plus distributed locking (`LockableBackend`) and TTL inspection (`TTLInspectableBackend`).

**Setup**:

```bash
export CACHEKIT_API_KEY="ck_live_..."
```

**Basic usage** — loads config from environment:

```python notest
from cachekit import cache
from cachekit.backends.cachekitio import CachekitIOBackend

backend = CachekitIOBackend()

@cache(backend=backend)
def cached_function(x):
    return expensive_computation(x)
```

**Explicit configuration**:

```python notest
from cachekit.backends.cachekitio import CachekitIOBackend

backend = CachekitIOBackend(
    api_url="https://api.cachekit.io",  # required if not using env
    api_key="ck_live_...",              # required if not using env
    timeout=5.0,                        # optional, default: 5.0 seconds
)
```

**Convenience shorthand via `@cache.io()`**:

```python notest
from cachekit import cache

# Equivalent to: @cache(backend=CachekitIOBackend())
# @cache.io() creates its own CachekitIOBackend via DecoratorConfig.io()
# and passes it as an explicit backend kwarg — Tier 1 resolution, not magic.
@cache.io(ttl=300, namespace="my-app")
def cached_function(x):
    return expensive_computation(x)
```

**Health check**:

```python notest
backend = CachekitIOBackend()
is_healthy, details = backend.health_check()
# details: {"backend_type": "saas", "latency_ms": 12.4, "api_url": "...", "version": "..."}
```

**Async support** — all protocol methods have async counterparts:

```python notest
from cachekit import cache
from cachekit.backends.cachekitio import CachekitIOBackend

backend = CachekitIOBackend()

@cache(backend=backend)
async def async_cached_function(x):
    return await fetch_data(x)

# Direct async calls also available:
# await backend.get_async(key)
# await backend.set_async(key, value, ttl=60)
# await backend.delete_async(key)
# await backend.exists_async(key)
# is_healthy, details = await backend.health_check_async()
```

**Distributed locking** (async only):

```python notest
backend = CachekitIOBackend()

lock_id = await backend.acquire_lock("my-lock", timeout=5)
if lock_id:
    try:
        # do work
        pass
    finally:
        await backend.release_lock("my-lock", lock_id)
```

**TTL inspection** (async only):

```python notest
backend = CachekitIOBackend()

remaining = await backend.get_ttl("my-key")      # seconds remaining, or None
refreshed = await backend.refresh_ttl("my-key", ttl=300)  # update TTL in place
```

**Timeout override** — returns a new instance:

```python notest
backend = CachekitIOBackend()
fast_backend = backend.with_timeout(1.0)  # 1-second timeout variant
```

**Security**: The API URL is validated on construction — HTTPS required, private/internal IP addresses blocked. The default allowlist restricts connections to `api.cachekit.io` and `api.staging.cachekit.io`. Set `CACHEKIT_ALLOW_CUSTOM_HOST=true` to override (testing only).

**Environment variables**:

```bash
CACHEKIT_API_KEY=ck_live_...          # Required — API key for authentication
CACHEKIT_API_URL=https://api.cachekit.io  # Optional — defaults to api.cachekit.io
CACHEKIT_TIMEOUT=5.0                  # Optional — request timeout in seconds
```

**When to use**:
- Managed, zero-infrastructure caching
- Multi-region distributed caching without operating Redis
- Teams that want caching without DevOps overhead
- Zero-knowledge architecture (compose with `@cache.secure` — see below)

**When NOT to use**:
- Sub-millisecond latency requirements — use Redis or L1 cache
- Fully offline/air-gapped environments
- Applications that cannot tolerate HTTP/2 dependency

**Characteristics**:
- Latency: ~10-50ms L2 (HTTP/2, region-dependent)
- Sync and async support (hybrid client architecture)
- Connection pooling built-in (default: 10 connections)
- Automatic retries on transient errors (default: 3)
- Distributed locking via server-side Durable Objects
- TTL inspection and in-place refresh supported

---

### FileBackend

Store cache on the local filesystem with automatic LRU eviction:

```python
from cachekit.backends.file import FileBackend
from cachekit.backends.file.config import FileBackendConfig
from cachekit import cache

# Use default configuration
config = FileBackendConfig()
backend = FileBackend(config)

@cache(backend=backend)
def cached_function():
    return expensive_computation()
```

**Configuration via environment variables**:

```bash
# Directory for cache files
export CACHEKIT_FILE_CACHE_DIR="/var/cache/myapp"

# Size limits
export CACHEKIT_FILE_MAX_SIZE_MB=1024           # Default: 1024 MB
export CACHEKIT_FILE_MAX_VALUE_MB=100           # Default: 100 MB (max single value)
export CACHEKIT_FILE_MAX_ENTRY_COUNT=10000      # Default: 10,000 entries

# Lock configuration
export CACHEKIT_FILE_LOCK_TIMEOUT_SECONDS=5.0   # Default: 5.0 seconds

# File permissions (octal, owner-only by default for security)
export CACHEKIT_FILE_PERMISSIONS=0o600          # Default: 0o600 (owner read/write)
export CACHEKIT_FILE_DIR_PERMISSIONS=0o700      # Default: 0o700 (owner rwx)
```

**Configuration via Python**:

```python
import tempfile
from pathlib import Path
from cachekit.backends.file import FileBackend
from cachekit.backends.file.config import FileBackendConfig

# Custom configuration
config = FileBackendConfig(
    cache_dir=Path(tempfile.gettempdir()) / "myapp_cache",
    max_size_mb=2048,
    max_value_mb=200,
    max_entry_count=50000,
    lock_timeout_seconds=10.0,
    permissions=0o600,
    dir_permissions=0o700,
)

backend = FileBackend(config)
```

**When to use**:
- Single-process applications (scripts, CLI tools, development)
- Local development and testing
- Systems where Redis is unavailable
- Low-traffic applications with modest cache sizes
- Temporary caching needs

**When NOT to use**:
- Multi-process web servers (gunicorn, uWSGI) - use Redis instead
- Distributed systems - use Redis or HTTP backend
- High-concurrency scenarios - file locking overhead becomes limiting
- Applications requiring sub-1ms latency - use L1-only cache

**Characteristics**:
- Latency: p50: 100-500μs, p99: 1-5ms
- Throughput: 1000+ operations/second (single-threaded)
- LRU eviction: Triggered at 90%, evicts to 70% capacity
- TTL support: Yes (automatic expiration checking)
- Cross-process: No (single-process only)
- Platform support: Full on Linux/macOS, limited on Windows (no O_NOFOLLOW)

**Limitations and Security Notes**:

1. **Single-process only**: FileBackend uses file locking that doesn't prevent concurrent access from multiple processes. Do NOT use with multi-process WSGI servers.

2. **File permissions**: Default permissions (0o600) restrict access to cache files to the owning user. Changing these permissions is a security risk and generates a warning.

3. **Platform differences**: Windows does not support the O_NOFOLLOW flag used to prevent symlink attacks. FileBackend still works but has slightly reduced symlink protection on Windows.

4. **Wall-clock TTL**: Expiration times rely on system time. Changes to system time (NTP, manual adjustments) may affect TTL accuracy.

5. **Disk space**: FileBackend will evict least-recently-used entries when reaching 90% capacity. Ensure sufficient disk space beyond max_size_mb for temporary writes.

**Performance characteristics**:

```
Sequential operations (single-threaded):
- Write (set):   p50: 120μs, p99: 800μs
- Read (get):    p50: 90μs, p99: 600μs
- Delete:        p50: 70μs, p99: 400μs

Concurrent operations (10 threads):
- Throughput: ~887 ops/sec
- Latency p99: ~30μs per operation

Large values (1MB):
- Write p99: ~15μs per operation
- Read p99: ~13μs per operation
```

## Encrypted SaaS Pattern (Zero-Knowledge)

> *cachekit.io is in closed alpha — [request access](https://cachekit.io)*

Compose `@cache.secure` with `CachekitIOBackend` for end-to-end zero-knowledge encryption over managed SaaS storage. The backend stores opaque ciphertext — it never sees plaintext data or your master key.

```python notest
from cachekit import cache
from cachekit.backends.cachekitio import CachekitIOBackend

# Required env: CACHEKIT_MASTER_KEY (hex, min 32 bytes) + CACHEKIT_API_KEY
backend = CachekitIOBackend()

@cache.secure(backend=backend, ttl=3600, namespace="sensitive-data")
def get_user_profile(user_id: str) -> dict:
    """Result is AES-256-GCM encrypted before storage.

    Data flow:
      serialize(result) -> encrypt(HKDF-derived key) -> PUT /v1/cache/{key}
      GET /v1/cache/{key} -> decrypt() -> deserialize() -> return result

    The cachekit.io API sees only encrypted bytes. Zero-knowledge.
    """
    return fetch_user_from_db(user_id)
```

**Why this matters**:
- `@cache.secure` applies AES-256-GCM client-side encryption before any data leaves the process
- Per-tenant key derivation via HKDF — cryptographic isolation between namespaces
- The SaaS backend is a zero-knowledge conduit: it stores whatever bytes arrive
- With `@cache.secure`: SaaS is out of scope for HIPAA/PCI (stores only ciphertext)
- Without `@cache.secure`: SaaS stores plaintext, may be in compliance scope

**Requirements**:

```bash
CACHEKIT_MASTER_KEY=<hex string, min 32 bytes>  # Never leaves the client
CACHEKIT_API_KEY=ck_live_...
```

See [Zero-Knowledge Encryption](../features/zero-knowledge-encryption.md) for full details on key derivation and serialization format implications.

## Custom Backend Examples

### HTTPBackend Example

A generic HTTP API backend — useful as a starting point for integrating cloud-based cache services (Cloudflare KV, Vercel KV, etc.). For managed cachekit.io storage, use `CachekitIOBackend` above instead.

```python notest
from cachekit import cache
import httpx

class HTTPBackend:
    """Custom backend storing cache in HTTP API."""

    def __init__(self, api_url: str):
        self.api_url = api_url
        self.client = httpx.Client()

    def get(self, key: str) -> Optional[bytes]:
        """Retrieve from HTTP API."""
        response = self.client.get(f"{self.api_url}/cache/{key}")
        if response.status_code == 404:
            return None
        response.raise_for_status()
        return response.content

    def set(self, key: str, value: bytes, ttl: Optional[int] = None) -> None:
        """Store to HTTP API."""
        params = {"ttl": ttl} if ttl else {}
        response = self.client.put(
            f"{self.api_url}/cache/{key}",
            content=value,
            params=params
        )
        response.raise_for_status()

    def delete(self, key: str) -> bool:
        """Delete from HTTP API."""
        response = self.client.delete(f"{self.api_url}/cache/{key}")
        return response.status_code == 200

    def exists(self, key: str) -> bool:
        """Check existence via HTTP HEAD."""
        response = self.client.head(f"{self.api_url}/cache/{key}")
        return response.status_code == 200

# Use custom backend
http_backend = HTTPBackend("https://cache-api.company.com")

@cache(backend=http_backend)
def api_cached_function():
    return fetch_data()
```

**When to use**:
- Integrating a custom internal cache service with a non-standard API
- Cloud-based cache services (Cloudflare KV, Vercel KV)
- Microservices with dedicated cache service

**Characteristics**:
- Network latency: ~10-100ms per operation (network dependent)
- Works across process/machine boundaries
- Requires HTTP endpoint availability
- Good for distributed systems

### DynamoDBBackend Example

Store cache in AWS DynamoDB:

```python notest
import boto3
from typing import Optional
from decimal import Decimal

class DynamoDBBackend:
    """Backend storing cache in AWS DynamoDB."""

    def __init__(self, table_name: str, region: str = "us-east-1"):
        self.dynamodb = boto3.resource("dynamodb", region_name=region)
        self.table = self.dynamodb.Table(table_name)

    def get(self, key: str) -> Optional[bytes]:
        """Retrieve from DynamoDB."""
        response = self.table.get_item(Key={"key": key})
        if "Item" not in response:
            return None
        # DynamoDB returns binary data as bytes
        return response["Item"]["value"]

    def set(self, key: str, value: bytes, ttl: Optional[int] = None) -> None:
        """Store to DynamoDB with optional TTL."""
        item = {
            "key": key,
            "value": value,
        }
        if ttl:
            import time
            # DynamoDB TTL is Unix timestamp
            item["ttl"] = int(time.time()) + ttl

        self.table.put_item(Item=item)

    def delete(self, key: str) -> bool:
        """Delete from DynamoDB."""
        response = self.table.delete_item(Key={"key": key})
        # DynamoDB always succeeds, check if item existed
        return response.get("Attributes") is not None

    def exists(self, key: str) -> bool:
        """Check existence in DynamoDB."""
        response = self.table.get_item(Key={"key": key}, ProjectionExpression="key")
        return "Item" in response
```

**When to use**:
- AWS-native applications
- Need for automatic TTL (DynamoDB streams)
- Scale without managing infrastructure

**Characteristics**:
- Serverless (pay per request)
- Automatic TTL support via DynamoDB TTL attribute
- Slower than Redis (~100-500ms)
- Good for low-traffic applications

## Custom Backend Implementation

### Step 1: Implement Protocol

Create a class that implements all 4 required methods:

```python notest
from typing import Optional
import your_storage_library

class CustomBackend:
    """Backend for your custom storage."""

    def __init__(self, config: dict):
        self.client = your_storage_library.Client(config)

    def get(self, key: str) -> Optional[bytes]:
        value = self.client.retrieve(key)
        return value if value else None

    def set(self, key: str, value: bytes, ttl: Optional[int] = None) -> None:
        if ttl:
            self.client.store_with_ttl(key, value, ttl)
        else:
            self.client.store(key, value)

    def delete(self, key: str) -> bool:
        return self.client.remove(key)

    def exists(self, key: str) -> bool:
        return self.client.contains(key)
```

### Step 2: Error Handling

All methods should raise `BackendError` for storage failures:

```python notest
from cachekit.backends import BackendError

class CustomBackend:
    def get(self, key: str) -> Optional[bytes]:
        try:
            return self.client.retrieve(key)
        except ConnectionError as e:
            raise BackendError(f"Connection failed: {e}") from e
        except Exception as e:
            raise BackendError(f"Retrieval failed: {e}") from e
```

### Step 3: Use with Decorator

Pass your backend to the `@cache` decorator:

```python notest
from cachekit import cache

backend = CustomBackend({"host": "storage.example.com"})

@cache(backend=backend)
def cached_function(x):
    return expensive_computation(x)
```

## Backend Resolution Priority

When `@cache` is used without explicit `backend` parameter, resolution follows this priority:

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

### When to Use Each Backend

**Use FileBackend when**:
- You're building single-process applications (scripts, CLI tools)
- You're in development and don't have Redis available
- You need local caching without network overhead
- You have modest cache sizes (< 10GB)
- Your application runs on a single machine

**Use RedisBackend when**:
- You need sub-10ms latency with shared cache
- Cache is shared across multiple processes
- You need persistence options
- You're building a typical web application
- You require multi-process or distributed caching

**Use CachekitIOBackend when** *(closed alpha — [request access](https://cachekit.io))*:
- You want managed, zero-ops distributed caching
- Multi-region caching without operating Redis
- Building zero-knowledge architecture with `@cache.secure`
- Team velocity matters more than absolute lowest latency

**Use a custom HTTPBackend when**:
- You're integrating a cloud cache service with a non-standard API
- Your cache needs to be globally distributed via a custom service
- You want to decouple cache from application with your own HTTP layer

**Use DynamoDBBackend when**:
- You're fully on AWS and serverless
- You don't want to manage infrastructure
- Cache traffic is low/bursty
- You need automatic TTL management

**Use L1-only when**:
- You're in development with single-process code
- You have a single-process application
- You don't need cross-process cache sharing
- You need the lowest possible latency (nanoseconds)

### Testing Your Backend

```python
def test_custom_backend():
    backend = CustomBackend()

    # Test set/get
    backend.set("key", b"value")
    assert backend.get("key") == b"value"

    # Test delete
    assert backend.delete("key")
    assert backend.get("key") is None

    # Test exists
    backend.set("key2", b"value2")
    assert backend.exists("key2")

    # Test TTL (if applicable)
    backend.set("ttl_key", b"value", ttl=1)
    import time
    time.sleep(1.5)
    assert backend.get("ttl_key") is None  # Expired
```

---

## Next Steps

**Previous**: [Serializer Guide](serializer-guide.md) - Choose the right data format
**Next**: [API Reference](../api-reference.md) - Complete decorator documentation

## See Also

- [API Reference](../api-reference.md) - Decorator parameters
- [Configuration Guide](../configuration.md) - Environment setup
- [Zero-Knowledge Encryption](../features/zero-knowledge-encryption.md) - Client-side encryption with custom backends
- [Data Flow Architecture](../data-flow-architecture.md) - How backends fit in the system

---

<div align="center">

**[GitHub Issues](https://github.com/cachekit-io/cachekit-py/issues)** · **[Documentation](../README.md)**

*Last Updated: 2026-03-18*

</div>
