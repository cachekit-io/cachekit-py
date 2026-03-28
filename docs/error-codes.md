**[Home](README.md)** › **Error Codes**

# Error Codes Reference

Comprehensive reference for all cachekit error codes and solutions.

## Encryption Errors

### E001: CACHEKIT_MASTER_KEY not set

**Message**: `CACHEKIT_MASTER_KEY environment variable must be set`

**Error Code**: `ConfigurationError`

**Cause**: Attempted to use `@cache.secure()` without setting the encryption master key

**When it occurs**:
```python notest
import os
# Master key not set
@cache.secure(ttl=300)
def get_sensitive_data():
    return secrets  # Raises E001
```

**Solution**:
```bash
# Generate and export master key
export CACHEKIT_MASTER_KEY=$(openssl rand -hex 32)
```

**Verification**:
```bash
# Verify key is set and correct length
python -c "import os; k = os.getenv('CACHEKIT_MASTER_KEY', ''); print(f'Key length: {len(k)} (need 64)')"
# Output: Key length: 64 (need 64)
```

---

### E002: Invalid key format

**Message**: `CACHEKIT_MASTER_KEY must be hex-encoded, minimum 32 bytes`

**Error Code**: `ConfigurationError`

**Cause**: Master key is not valid hexadecimal or too short (< 32 bytes = 64 hex chars)

**When it occurs**:
```bash
# WRONG - not hex
export CACHEKIT_MASTER_KEY="my-secret-key"

# WRONG - too short
export CACHEKIT_MASTER_KEY="abcd1234"

# WRONG - contains non-hex characters
export CACHEKIT_MASTER_KEY="gghhiijj1234567890abcdef1234567890"
```

**Solution**:
```bash
# Generate valid 64-character hex string (32 bytes)
export CACHEKIT_MASTER_KEY=$(openssl rand -hex 32)

# Verify it's valid
python -c "
import os
key = os.getenv('CACHEKIT_MASTER_KEY', '')
try:
    bytes.fromhex(key)
    print(f'Valid key: {len(key)} hex chars ({len(key)//2} bytes)')
except ValueError:
    print('Invalid hex')
"
```

---

### E003: Decryption failed - authentication tag mismatch

**Message**: `Decryption failed: authentication tag verification failed`

**Error Code**: `DecryptionError`

**Cause**:
- Master key was changed (old encrypted data can't be decrypted)
- Cached data was corrupted during storage/retrieval
- Data was modified externally
- Nonce collision (extremely rare)

**When it occurs**:
```python notest
# Old cached data with key A
@cache.secure(ttl=300)
def get_data():
    return sensitive_data()

# Later: key changed to key B
os.environ["CACHEKIT_MASTER_KEY"] = new_key

# Trying to read old cached data → E003
get_data()  # Can't decrypt old data with new key
```

**Solutions**:

**Option 1: Key was rotated (most common)**
```bash
# Clear cache of incompatible data
redis-cli FLUSHDB

# Keep new key and restart application
export CACHEKIT_MASTER_KEY=$(openssl rand -hex 32)
python app.py
# Function will recompute and cache with new key
```

**Option 2: Revert to original key**
```bash
# If you have the original key stored safely
export CACHEKIT_MASTER_KEY=<original-key-here>
python app.py
# Cached data can now be decrypted
```

**Option 3: Data corruption**
```bash
# If you suspect data corruption, clear cache
redis-cli FLUSHDB

# Restart application - will recompute all cached values
python app.py
```

**Prevention**:
```bash
# Before rotating keys, clear cache
redis-cli FLUSHDB

# Then update key
export CACHEKIT_MASTER_KEY=new_key

# Restart application
python app.py
```

---

## Connection Errors

### E010: Redis connection error

**Message**: `ConnectionError: Error -2 connecting to localhost:6379`

**Error Code**: `ConnectionError`

**Cause**: Redis is not running, URL is incorrect, or network is unreachable

**When it occurs**:
```python
# Redis not running
@cache()
def my_function():
    return data()  # Raises E010 if Redis unavailable
```

**Solutions**:

1. **Start Redis**:
```bash
# Using Docker (recommended)
docker run -d -p 6379:6379 redis:latest

# Verify connection
redis-cli ping
# Output: PONG
```

2. **Verify connection URL**:
```bash
# Check environment variable
echo $CACHEKIT_REDIS_URL
echo $REDIS_URL

# Test connection
redis-cli -h localhost -p 6379 ping
# Output: PONG
```

3. **Check for firewall issues**:
```bash
# Test if port is accessible
telnet localhost 6379
# Should connect (Ctrl+C to exit)

# On macOS
nc -zv localhost 6379
# Output: Connection to localhost port 6379 [tcp/*] succeeded!
```

---

### E011: Connection timeout

**Message**: `TimeoutError: Connection timeout`

**Error Code**: `TimeoutError`

**Cause**: Network latency too high or Redis is slow to respond

**Solutions**:

1. **Check Redis performance**:
```bash
# Monitor Redis
redis-cli
> INFO stats
> INFO latency

# Test latency
redis-benchmark -h localhost -p 6379
```

2. **Increase timeout values**:
```bash
# Increase connection and socket timeouts
export CACHEKIT_SOCKET_TIMEOUT=5.0
export CACHEKIT_SOCKET_CONNECT_TIMEOUT=5.0
```

3. **Check network**:
```bash
# Ping Redis host
ping redis-server.example.com
# Check latency and packet loss
```

---

### E012: Connection pool exhausted

**Message**: `ConnectionPoolError: Connection pool is exhausted`

**Error Code**: `ConnectionPoolError`

**Cause**: Too many concurrent requests exceeding connection pool size

**Solution**:
```bash
# Increase connection pool size
export CACHEKIT_CONNECTION_POOL_SIZE=50

# For high-concurrency applications
export CACHEKIT_CONNECTION_POOL_SIZE=100
```

---

## Serialization Errors

### E020: Serialization unsupported type

**Message**: `TypeError: Object of type X is not JSON serializable` or `msgpack.PackValueError`

**Error Code**: `SerializationError`

**Cause**: Data type not supported by chosen serializer

**When it occurs**:
```python
from cachekit import cache
import datetime

# WRONG - datetime not serializable by MessagePack
@cache()
def get_timestamp():
    return datetime.datetime.now()  # Raises E020
```

**Solutions**:

1. **For datetime objects**, use OrjsonSerializer (native datetime support):
```python notest
from cachekit import cache
from cachekit.serializers import OrjsonSerializer
import datetime

@cache(serializer=OrjsonSerializer())
def get_timestamp():
    return {"ts": datetime.datetime.now()}  # Converts to ISO-8601 string
```

2. **For DataFrames**, use ArrowSerializer:
```python
from cachekit import cache
from cachekit.serializers import ArrowSerializer
import pandas as pd

@cache(serializer=ArrowSerializer())
def get_dataframe():
    return pd.DataFrame({"a": [1, 2, 3]})  # Works
```

3. **For JSON-compatible data**, use default (MessagePack):
```python
# Default serializer handles: dict, list, str, int, float, bool, None
@cache()
def get_json_data():
    return {"key": "value", "count": 42}  # Works
```

4. **Convert unsupported types to JSON-compatible**:
```python
from cachekit import cache
import datetime

@cache()
def get_data():
    # Convert datetime to ISO string before returning
    return {
        "timestamp": datetime.datetime.now().isoformat(),
        "data": [1, 2, 3]
    }
```

---

### E021: Deserialization failed

**Message**: `msgpack.exceptions.UnpackException` or `json.JSONDecodeError`

**Error Code**: `DeserializationError`

**Cause**: Cached data is corrupted or wrong serializer used for decoding

**Solution**:
```bash
# Clear corrupted cache entry
redis-cli DEL <cache-key>

# Or clear entire cache
redis-cli FLUSHDB

# Function will recompute and cache correctly
```

---

## Circuit Breaker Errors

### E030: Circuit breaker open

**Message**: `CircuitBreakerError: Circuit breaker is open`

**Error Code**: `CircuitBreakerError`

**Cause**: Too many transient errors (ConnectionError, TimeoutError) detected

**What it means**:
- Redis or backend is experiencing issues
- Circuit breaker is protecting against cascading failures
- Cache is temporarily disabled

**Solutions**:

1. **Check backend health**:
```bash
redis-cli ping
# Output: PONG means Redis is healthy
```

2. **Wait for circuit breaker to reset**:
- Circuit breaker automatically resets after timeout (default 60 seconds)
- During recovery, requests execute function without caching

3. **Fix the underlying issue**:
```bash
# Check Redis logs
docker logs <redis-container>

# Restart Redis if needed
docker restart <redis-container>

# Verify connection after restart
redis-cli ping
```

**How function behaves when circuit breaker is open**:
```python
@cache()
def my_function():
    return expensive_operation()

# When circuit breaker is open:
# - Function still executes: expensive_operation() runs
# - Cache is bypassed: result is NOT cached
# - No exception raised: caller gets result normally
# - Warning is logged (if logging configured)
```

---

## Configuration Errors

### E040: Missing Redis URL

**Message**: `ConfigurationError: CACHEKIT_REDIS_URL or REDIS_URL not set`

**Error Code**: `ConfigurationError`

**Cause**: No Redis connection string provided

**Solution**:
```bash
# Set Redis URL
export CACHEKIT_REDIS_URL=redis://localhost:6379/0

# Or fallback
export REDIS_URL=redis://localhost:6379/0
```

---

### E041: Invalid environment variable prefix

**Message**: `ConfigurationError: Invalid environment variable CACHE_REDIS_URL`

**Error Code**: `ConfigurationError`

**Cause**: Wrong prefix used (CACHE_ instead of CACHEKIT_)

**Solution**:
```bash
# WRONG prefix - won't be read
export CACHE_REDIS_URL=redis://localhost:6379

# CORRECT prefix - will be read
export CACHEKIT_REDIS_URL=redis://localhost:6379
```

---

## Lock Errors

### E050: Lock acquisition timeout

**Message**: `LockError: Could not acquire lock within timeout`

**Error Code**: `LockError`

**Cause**: Distributed lock could not be acquired (another process holds the lock)

**Solutions**:

1. **Check for stuck locks**:
```bash
# View locks in Redis
redis-cli KEYS "*:lock*"

# Clear stuck lock manually (if necessary)
redis-cli DEL <lock-key>
```

2. **Increase lock timeout**:
```python notest
from cachekit import cache

@cache(lock_timeout=5.0)  # Increase from default
def my_function():
    return expensive_operation()
```

---

## CachekitIO HTTP Errors

These errors occur when using `@cache.io()` with the CachekitIO SaaS backend. Each HTTP response is classified into a `BackendErrorType` that drives circuit breaker and retry behavior.

### E060: Authentication failure (401/403)

**Message**: `Authentication failed: HTTP 401` or `Authentication failed: HTTP 403`

**Error Code**: `BackendError` / `BackendErrorType.AUTHENTICATION`

**Cause**:
- API key is missing, invalid, or revoked
- API key does not have permission for the requested operation

**Behavior**: No retry. Alert ops immediately.

**Solution**:
```bash
# Verify API key is set
echo $CACHEKIT_API_KEY

# Set a valid API key
export CACHEKIT_API_KEY=ck_live_your_key_here
```

```python notest
from cachekit import cache

# API key can also be passed at decorator level
@cache.io(ttl=300)
def get_data():
    return fetch()  # Requires valid CACHEKIT_API_KEY env var
```

---

### E061: Rate limited (429)

**Message**: `Rate limit exceeded`

**Error Code**: `BackendError` / `BackendErrorType.TRANSIENT`

**Cause**: Request volume exceeds the rate limit for the API key tier

**Behavior**: TRANSIENT — auto-retry with exponential backoff. Circuit breaker counts toward failure threshold.

**Solutions**:

1. **Reduce request volume**: Increase TTL so cache hits serve more traffic:
```python notest
from cachekit import cache

@cache.io(ttl=3600)  # Longer TTL reduces backend requests
def get_data():
    return fetch()
```

2. **Upgrade tier**: Increase plan limits at [cachekit.io](https://cachekit.io)

3. **Review cache key design**: Overly granular keys cause unnecessary misses

---

### E062: Server error (5xx)

**Message**: `Server error: HTTP 500` (or 502, 503, 504, etc.)

**Error Code**: `BackendError` / `BackendErrorType.TRANSIENT`

**Cause**: Transient server-side error at the CachekitIO API

**Behavior**: TRANSIENT — auto-retry with backoff. If sustained, circuit breaker opens and function executes without caching.

**What happens when circuit breaker opens**:
```python notest
@cache.io(ttl=300)
def my_function():
    return expensive_operation()

# When server errors persist and circuit breaker opens:
# - Function still executes: expensive_operation() runs
# - Cache is bypassed: result is NOT cached
# - No exception raised: caller gets result normally
# - Warning is logged (if logging configured)
```

---

### E063: Client error (4xx)

**Message**: `Client error: HTTP 400` (or 404, 413, etc.)

**Error Code**: `BackendError` / `BackendErrorType.PERMANENT`

**Cause**: Malformed request — invalid cache key format, payload too large, or other client-side issue

**Behavior**: PERMANENT — no retry. Check your cache key and payload.

**Common causes and fixes**:
```python notest
from cachekit import cache

# WRONG - cache key contains invalid characters
@cache.io(ttl=300, key="my key with spaces")
def get_data(user_id):
    return fetch(user_id)

# CORRECT - use valid key characters
@cache.io(ttl=300, key="my_key_{user_id}")
def get_data(user_id):
    return fetch(user_id)
```

---

### E064: Request timeout

**Message**: `Request timeout: ...`

**Error Code**: `BackendError` / `BackendErrorType.TIMEOUT`

**Cause**: HTTP request to the CachekitIO API exceeded the configured timeout

**Behavior**: TIMEOUT — configurable retry behavior. Circuit breaker counts toward failure threshold.

**Solution**:
```python notest
from cachekit import cache

# Increase timeout for slow network conditions
@cache.io(ttl=300, timeout=10.0)  # seconds
def get_data():
    return fetch()
```

```bash
# Or via environment variable
export CACHEKIT_TIMEOUT=10.0
```

---

### E065: Connection error

**Message**: `Connection failed: ...`

**Error Code**: `BackendError` / `BackendErrorType.TRANSIENT`

**Cause**: Network-level failure — DNS resolution failed, connection refused, or network unreachable

**Behavior**: TRANSIENT — auto-retry with backoff. Circuit breaker opens after sustained failures, allowing function to execute without caching.

**Solutions**:

1. **Verify network connectivity**:
```bash
curl -I https://api.cachekit.io/health
```

2. **Check custom API URL** (if overridden):
```bash
echo $CACHEKIT_API_URL
# Default: https://api.cachekit.io
```

3. **Check for proxy or firewall rules blocking outbound HTTPS to api.cachekit.io**

---

### CachekitIO error classification summary

| HTTP Status / Exception | `BackendErrorType` | Auto-Retry | Circuit Breaker |
|---|---|---|---|
| 401, 403 | `AUTHENTICATION` | No | No |
| 429 | `TRANSIENT` | Yes (backoff) | Yes |
| 5xx | `TRANSIENT` | Yes (backoff) | Yes |
| 4xx (other) | `PERMANENT` | No | No |
| `TimeoutException` | `TIMEOUT` | Configurable | Yes |
| `ConnectError`, `NetworkError` | `TRANSIENT` | Yes (backoff) | Yes |
| Other | `UNKNOWN` | No | No |

---

## Error Handling Best Practices

### Log errors for debugging

```python
import logging
from cachekit import cache

logger = logging.getLogger(__name__)

@cache()
def safe_function(x):
    try:
        return expensive_operation(x)
    except Exception as e:
        logger.error(f"Caching error: {e}", exc_info=True)
        raise
```

### Graceful degradation

```python
from cachekit import cache

@cache()
def resilient_function(x):
    try:
        return compute(x)
    except Exception:
        logger.warning("Using fallback value")
        return fallback_value(x)
```

### Monitor for errors

```python
from cachekit import cache
import time

error_count = 0

@cache()
def monitored_function(x):
    global error_count
    try:
        return compute(x)
    except Exception as e:
        error_count += 1
        if error_count > 10:
            logger.critical("Too many errors, disabling cache")
        raise
```

---

## See Also

- [Troubleshooting Guide](troubleshooting.md) - Detailed error solutions
- [Configuration Guide](configuration.md) - Environment setup
- [API Reference](api-reference.md) - Decorator parameters
- [Zero-Knowledge Encryption](features/zero-knowledge-encryption.md) - Encryption errors

---

<div align="center">

**[GitHub Issues](https://github.com/cachekit-io/cachekit-py/issues)** · **[Documentation](README.md)**

</div>
