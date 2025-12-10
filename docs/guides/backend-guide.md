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

### HTTPBackend

Store cache in HTTP API endpoints:

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
- Cloud-based cache services (Cloudflare KV, Vercel KV)
- Microservices with dedicated cache service
- Zero-knowledge caching (backend never sees plaintext)

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
custom_backend = HTTPBackend("https://api.example.com")

@cache(backend=custom_backend)  # Uses custom backend explicitly
def explicit_backend():
    return data()
```

### 2. Default RedisBackend (Middle Priority)

```python
@cache()  # Automatically uses RedisBackend
def implicit_redis():
    return data()
```

This uses RedisBackend configured with environment variables.

### 3. Environment Variable Configuration (Lowest Priority)

```bash
# Primary: CACHEKIT_REDIS_URL
CACHEKIT_REDIS_URL=redis://prod.example.com:6379/0

# Fallback: REDIS_URL
REDIS_URL=redis://localhost:6379/0
```

**Resolution order**:
1. Check for explicit `backend` parameter in `@cache(backend=...)`
2. If not provided, create RedisBackend from environment variables
3. Priority for Redis URL: CACHEKIT_REDIS_URL > REDIS_URL

## Performance Considerations

### Backend Latency Comparison

| Backend | Latency | Use Case | Notes |
|---------|---------|----------|-------|
| **L1 (In-Memory)** | ~50ns | Repeated calls in same process | Process-local only |
| **Redis** | 1-7ms | Shared cache across pods | Production default |
| **HTTP API** | 10-100ms | Cloud services, multi-region | Network dependent |
| **DynamoDB** | 100-500ms | Serverless, low-traffic | High availability |
| **Memcached** | 1-5ms | Alternative to Redis | No persistence |

### When to Use Each Backend

**Use RedisBackend when**:
- You need sub-10ms latency
- Cache is shared across multiple processes
- You need persistence options
- You're building a typical web application

**Use HTTPBackend when**:
- You're using a cloud cache service
- Your cache needs to be globally distributed
- You want to decouple cache from application
- You're building a zero-knowledge caching system

**Use DynamoDBBackend when**:
- You're fully on AWS and serverless
- You don't want to manage infrastructure
- Cache traffic is low/bursty
- You need automatic TTL management

**Use L1-only when**:
- You're in development
- You have a single-process application
- You don't need cross-process cache sharing

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

*Last Updated: 2025-12-02*

</div>
