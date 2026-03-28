**[Home](../README.md)** › **[Backends](README.md)** › **Custom Backends**

# Custom Backends

Implement any key-value store as a cachekit backend by satisfying the `BaseBackend` protocol. Five methods. No inheritance required.

## Implementation Guide

### Step 1: Implement Protocol

Create a class that implements all 5 required methods:

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

    def health_check(self) -> tuple[bool, dict]:
        try:
            self.client.ping()
            return True, {"backend_type": "custom", "latency_ms": 0}
        except Exception as e:
            return False, {"backend_type": "custom", "error": str(e)}
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

---

## HTTPBackend Example

A generic HTTP API backend — useful as a starting point for integrating cloud-based cache services (Cloudflare KV, Vercel KV, etc.). For managed cachekit.io storage, use [`CachekitIOBackend`](cachekitio.md) instead.

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
- Network latency: ~10–100ms per operation (network dependent)
- Works across process/machine boundaries
- Requires HTTP endpoint availability
- Good for distributed systems

---

## DynamoDBBackend Example

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
- Slower than Redis (~100–500ms)
- Good for low-traffic applications

---

## Testing Your Backend

The `test_custom_backend` function below is a reusable test harness. Substitute `CustomBackend()` with your own implementation:

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

## See Also

- [Backend Guide](README.md) — Backend comparison and resolution priority
- [CachekitIO Backend](cachekitio.md) — Managed SaaS backend (no custom implementation needed)
- [Redis Backend](redis.md) — Default production backend
- [API Reference](../api-reference.md) — Decorator parameters

---

<div align="center">

**[GitHub Issues](https://github.com/cachekit-io/cachekit-py/issues)** · **[Documentation](../README.md)**

</div>
