**[Home](README.md)** › **Troubleshooting Guide**

# Troubleshooting Guide

> **Solutions for common cachekit issues and error messages**

---

## Common Errors

<details>
<summary><strong>Circuit Breaker Errors</strong></summary>

**Issue**: Circuit breaker is open and requests are failing

**What it means**:
- Too many transient errors (ConnectionError, TimeoutError) detected
- Circuit breaker protection is preventing cascading failures
- Cache is temporarily disabled to avoid overwhelming backend

**Solutions**:

1. **Check Redis availability**:
```bash
redis-cli ping
# Should output: PONG
```

2. **Verify Redis connection string**:
```bash
# Check what URL is being used
env | grep REDIS
export CACHEKIT_REDIS_URL=redis://localhost:6379/0
```

3. **Wait for circuit breaker to reset**:
- Circuit breaker automatically resets after timeout
- Default: 60 seconds (configurable)
- During recovery: requests execute function without caching

4. **Increase timeout if network is slow**:
```bash
export CACHEKIT_SOCKET_TIMEOUT=5.0
export CACHEKIT_SOCKET_CONNECT_TIMEOUT=5.0
```

**Example handling**:
```python
import logging
from cachekit import cache

logger = logging.getLogger(__name__)

@cache()
def safe_computation(data):
    try:
        return expensive_operation(data)
    except Exception as e:
        logger.error(f"Computation error: {e}")
        raise  # Let circuit breaker catch it
```

</details>

<details>
<summary><strong>Serialization Failures</strong></summary>

**Issue**: "Could not serialize data" or TypeError during caching

**What it means**:
- Cache attempted to serialize function result
- Data type is not compatible with chosen serializer
- MessagePack (default) only supports basic types

**Solutions**:

1. **For custom objects**, use appropriate serializer:
```python notest
from cachekit import cache
from cachekit.serializers import PickleSerializer

# Pickle supports arbitrary Python objects
@cache(serializer=PickleSerializer())
def get_custom_object():
    return MyCustomClass()
```

2. **For DataFrames**, use ArrowSerializer:
```python
from cachekit import cache
from cachekit.serializers import ArrowSerializer
import pandas as pd

@cache(serializer=ArrowSerializer())
def get_dataframe():
    return pd.DataFrame({"a": [1, 2, 3]})
```

3. **For JSON-compatible data**, use default (MessagePack):
```python
# Default serializer handles: dict, list, str, int, float, bool, None
@cache()
def get_json_data():
    return {"key": "value", "count": 42}
```

4. **For Pydantic models**, convert to dict first:
```python
from pydantic import BaseModel
from cachekit import cache

class User(BaseModel):
    id: int
    name: str

# Convert model to dict before caching
@cache()
def get_user(user_id: int) -> dict:
    user = fetch_user_model(user_id)  # Returns Pydantic model
    return user.model_dump()  # Explicit conversion
```

   **Why not auto-detect Pydantic models?** See [Serializer Guide - Caching Pydantic Models](guides/serializer-guide.md#caching-pydantic-models) for the detailed rationale.

5. **Check what serializer is installed**:
```python notest
from cachekit.serializers import DEFAULT_SERIALIZER
print(f"Active serializer: {DEFAULT_SERIALIZER}")
```

</details>

<details>
<summary><strong>Connection Issues</strong></summary>

**Issue**: Redis connection timeout or refused

**Error messages**:
```
ConnectionError: Error -2 connecting to localhost:6379
TimeoutError: Connection timeout
ConnectionRefusedError: [Errno 111] Connection refused
```

**Solutions**:

1. **Start Redis locally**:
```bash
# Using Docker (recommended)
docker run -d -p 6379:6379 redis:latest

# Verify connection
redis-cli ping
# Output: PONG
```

2. **Verify connection URL**:
```python
import redis

# Test connection before using decorator
try:
    r = redis.from_url("redis://localhost:6379/0")
    print(r.ping())
except Exception as e:
    print(f"Connection failed: {e}")
```

3. **Check firewall/network**:
```bash
# On same machine
redis-cli -h localhost -p 6379 ping

# Across network (replace host)
redis-cli -h redis-server.example.com -p 6379 ping
```

4. **For timeout issues**, increase timeout values:
```bash
export CACHEKIT_SOCKET_TIMEOUT=5.0
export CACHEKIT_SOCKET_CONNECT_TIMEOUT=5.0
```

5. **Verify Redis is running**:
```bash
# Check if port 6379 is listening
netstat -tulpn | grep 6379
# or
lsof -i :6379
```

</details>

<details>
<summary><strong>Encryption Issues</strong></summary>

**Issue**: Decryption failures or key-related errors

**Error messages**:
```
"CACHEKIT_MASTER_KEY not set"
"CACHEKIT_MASTER_KEY must be hex-encoded, minimum 32 bytes"
"Decryption failed: authentication tag verification failed"
```

**Solutions**:

See [Zero-Knowledge Encryption - Troubleshooting](features/zero-knowledge-encryption.md#troubleshooting)

**Common causes**:
1. Master key not set when using `@cache.secure()`
2. Master key format invalid (not hex-encoded)
3. Master key rotated (can't decrypt old cached data)
4. Data corruption during storage/retrieval

**Quick fix**:
```bash
# Generate valid encryption key
export CACHEKIT_MASTER_KEY=$(openssl rand -hex 32)

# Clear cache if key was rotated
redis-cli FLUSHDB

# Restart application
python app.py
```

</details>

---

## Error Code Reference

<details>
<summary><strong>E001: CACHEKIT_MASTER_KEY not set</strong></summary>

**Message**: "CACHEKIT_MASTER_KEY environment variable must be set"

**Cause**: Using `@cache.secure()` without encryption key configured

**When it occurs**:
```python notest
# WRONG - will raise E001
@cache.secure(ttl=300)
def get_sensitive_data():
    return secrets
```

**Solution**:
```bash
# Generate and export master key
export CACHEKIT_MASTER_KEY=$(openssl rand -hex 32)
```

</details>

<details>
<summary><strong>E002: Invalid Key Format</strong></summary>

**Message**: "CACHEKIT_MASTER_KEY must be hex-encoded, minimum 32 bytes"

**Cause**: Master key is not valid hex or too short

**Invalid examples**:
```bash
export CACHEKIT_MASTER_KEY="my-secret-key"  # Not hex
export CACHEKIT_MASTER_KEY="abcd1234"  # Too short
```

**Solution**:
```bash
# Generate valid 64-character hex string (32 bytes)
export CACHEKIT_MASTER_KEY=$(openssl rand -hex 32)

# Verify length
python -c "import os; print(len(os.getenv('CACHEKIT_MASTER_KEY', '')))"
# Output: 64
```

</details>

<details>
<summary><strong>E003: Decryption Failed - Authentication Tag Mismatch</strong></summary>

**Message**: "Decryption failed: authentication tag verification failed"

**Cause**:
- Master key was changed (can't decrypt old data)
- Data corruption during storage or retrieval
- Encrypted data was modified

**Solutions**:

1. **Key was rotated** (most common):
```bash
# Clear Redis to remove incompatible cached data
redis-cli FLUSHDB

# Keep new key and restart application
export CACHEKIT_MASTER_KEY=$(openssl rand -hex 32)
python app.py
```

2. **Wrong key still in use**:
```bash
# Verify current key
python -c "import os; print(os.getenv('CACHEKIT_MASTER_KEY')[:16] + '...')"

# Revert to original key if available
export CACHEKIT_MASTER_KEY=<original-key>
```

3. **Data corruption**:
```bash
# If data is corrupted, clearing cache is safe
redis-cli FLUSHDB

# Function will recompute and re-cache with current key
```

</details>

<details>
<summary><strong>E004: Serialization Compatibility Error</strong></summary>

**Message**: "Could not serialize object of type X"

**Cause**: Data type not supported by serializer

**When it occurs**:
```python notest
from cachekit import cache
import datetime

# WRONG - datetime not serializable by default serializer
@cache()
def get_timestamp():
    return datetime.datetime.now()
```

**Solution**:
```python notest
from cachekit import cache
from cachekit.serializers import PickleSerializer
import datetime

# Use PickleSerializer for complex types
@cache(serializer=PickleSerializer(), backend=None)
def get_timestamp():
    return datetime.datetime.now()
```

</details>

---

## Recovery Strategies

<details>
<summary><strong>Cache Invalidation</strong></summary>

**Clear entire cache**:
```bash
redis-cli FLUSHDB
```

**Clear by namespace** (if implemented):
```python notest
from cachekit import cache

@cache(namespace="users")
def get_user(user_id):
    return fetch_user(user_id)

# Manual invalidation
# Note: Current cachekit doesn't provide built-in invalidation
# Clear Redis and re-cache on next call
```

```bash
redis-cli FLUSHDB
```

**Per-function cache clearing** (workaround):
```python notest
from cachekit import cache
import redis

r = redis.from_url("redis://localhost:6379/0")

def invalidate_user_cache(user_id):
    key = f"users:get_user:{user_id}"
    r.delete(key)

@cache(namespace="users")
def get_user(user_id):
    return fetch_user(user_id)

# Invalidate when user data changes
user = update_user(user_id, data)
invalidate_user_cache(user_id)
```

</details>

<details>
<summary><strong>Graceful Degradation</strong></summary>

**Fallback when cache fails**:
```python
from cachekit import cache
import logging

logger = logging.getLogger(__name__)

@cache(ttl=3600)
def expensive_operation(x):
    try:
        return compute_expensive_result(x)
    except Exception as e:
        logger.warning(f"Computation failed: {e}")
        # Return fallback value or raise
        return fallback_value(x)
```

**Check cache health**:
```python
import redis

def is_redis_healthy():
    try:
        r = redis.from_url("redis://localhost:6379/0")
        r.ping()
        return True
    except Exception:
        return False

# Use in monitoring
if not is_redis_healthy():
    logger.warning("Redis unavailable - cache disabled")
```

</details>

<details>
<summary><strong>Health Monitoring</strong></summary>

**Monitor cache hits/misses**:
```python
from cachekit import cache
import time

cache_stats = {"hits": 0, "misses": 0}

@cache(ttl=3600)
def monitored_function(x):
    return expensive_operation(x)

# Manual tracking (built-in metrics coming soon)
def get_hit_rate():
    total = cache_stats["hits"] + cache_stats["misses"]
    if total == 0:
        return 0
    return cache_stats["hits"] / total
```

**Health check endpoint**:
```python notest
import redis
from flask import jsonify

@app.route("/health/cache")
def cache_health():
    try:
        r = redis.from_url("redis://localhost:6379/0")
        r.ping()
        return jsonify({"status": "healthy"}), 200
    except Exception as e:
        return jsonify({"status": "unhealthy", "error": str(e)}), 503
```

</details>

---

## Debugging

<details>
<summary><strong>Enable detailed logging</strong></summary>

```python
import logging

# Set cachekit to DEBUG level
logging.getLogger("cachekit").setLevel(logging.DEBUG)

# Set Redis client to DEBUG level
logging.getLogger("redis").setLevel(logging.DEBUG)

# View logs
logging.basicConfig(level=logging.DEBUG)
```

</details>

<details>
<summary><strong>Check cache key format</strong></summary>

```python notest
from cachekit.core import generate_cache_key

# See what key is generated for function
key = generate_cache_key("get_user", (123,), {})
print(f"Cache key: {key}")
# Output: cachekit:get_user:abc123def456...
```

</details>

<details>
<summary><strong>Test serializer independently</strong></summary>

```python notest
from cachekit.serializers import DefaultSerializer

serializer = DefaultSerializer()

# Test serialization
data = {"key": "value"}
encoded = serializer.serialize(data)
print(f"Encoded: {encoded[:50]}...")

# Test deserialization
decoded = serializer.deserialize(encoded)
print(f"Decoded matches: {decoded == data}")
```

</details>

---

## See Also

- [Configuration Guide](configuration.md) - Environment variable setup
- [API Reference](api-reference.md) - Decorator parameters and options
- [Zero-Knowledge Encryption](features/zero-knowledge-encryption.md) - Encryption troubleshooting
- [Circuit Breaker](features/circuit-breaker.md) - Circuit breaker behavior
- [Getting Started](getting-started.md) - Basic usage examples

---

<div align="center">

*Last Updated: 2025-12-02*

</div>
