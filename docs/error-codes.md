**[Home](README.md)** › **Error Reference**

# Error Reference

The errors cachekit raises or logs, and how to fix them. cachekit has no numeric error codes: catch the class shown under **Exception**.

Configuration errors raise when the decorator is applied. Backend failures (connection, timeout, CachekitIO HTTP errors), serialization, deserialization, circuit-breaker and lock failures do not raise to the caller: a `@cache`-decorated call logs the failure and runs the function without caching. Where that holds, the entry reads **Exception**: none. Three exceptions to that rule:

- Decryption failures raise only when fail-closed is on.
- With `interop=...`, a return value the interop data model can't represent raises `InteropError`.
- An **async** function raises `UnboundLocalError` once the circuit breaker opens (a known defect; sync functions degrade as described).

## Encryption Errors

### CACHEKIT_MASTER_KEY not set

**Message**: `cache.secure requires master_key parameter or CACHEKIT_MASTER_KEY environment variable`

**Exception**: `ValueError`, raised when the decorator is applied

**Cause**: `@cache.secure()` with no `master_key=` argument and no `CACHEKIT_MASTER_KEY` set. The other encryption entry points raise different types for the same mistake: `@cache(encryption=True)` raises `ConfigurationError` (`encryption.enabled=True requires encryption.master_key. ...`), and `EncryptionWrapper()` raises `EncryptionError` (`Master key required. Set CACHEKIT_MASTER_KEY environment variable or pass master_key parameter.`).

**When it occurs**:
```python notest
# Master key not set
@cache.secure(ttl=300)  # Raises ValueError here, at decoration time
def get_sensitive_data():
    return secrets
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

### Invalid key format

**Message**: one of
- `CACHEKIT_MASTER_KEY must be hex-encoded: ...` (not hexadecimal)
- `CACHEKIT_MASTER_KEY must be at least 32 bytes (256 bits). Got ... bytes. ...` (too short)

**Exception**: `ConfigurationError` (`from cachekit.config.validation import ConfigurationError`), raised when the decorator is applied. It subclasses `Exception`, not `ValueError`, so `except ValueError` does not catch it.

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

### Decryption failed - authentication tag mismatch

**Message**: `Decryption failed: ...`. A key or tenant mismatch reads `Key fingerprint mismatch: ...` or `Tenant mismatch: ...` instead.

**Exception**: `DecryptionAuthenticationError` (`from cachekit.serializers.encryption_wrapper import DecryptionAuthenticationError`, a `SerializationError` subclass)

**What it means**: By default a `@cache.secure` read does not raise. It logs `... cache decrypt/integrity failure (auth_tamper) for ...`, evicts the entry and recomputes. The exception reaches your code only with fail-closed on: `CACHEKIT_ENCRYPTION_FAIL_CLOSED=true`, or `@cache.secure(fail_closed=True)` on the decorator.

**Cause**:
- Master key was changed (old encrypted data can't be decrypted)
- Cached data was corrupted during storage/retrieval
- Data was modified externally

**When it occurs**:
```python notest
# Old cached data with key A
@cache.secure(ttl=300)
def get_data():
    return sensitive_data()

# Later: key changed to key B
os.environ["CACHEKIT_MASTER_KEY"] = new_key

# Trying to read old cached data → decryption fails
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

None of these raise to a `@cache`-decorated caller. cachekit wraps the redis-py exception in a `BackendError`, logs it, and runs the function without caching. Depending on where the failure happens, the log line is one of:

- `Cache operation '...' failed for key '...': ...` (WARNING; the last part names the error, e.g. `BackendError(transient)`)
- `Backend error getting key ...: BackendError(...)` (ERROR)

The redis-py exceptions below reach your code only when you use a Redis client directly.

### Redis unreachable

**Message**: `Error ... connecting to ...` (redis-py), e.g. `Error 111 connecting to localhost:6379. Connection refused.`

**Exception**: none — `redis.exceptions.ConnectionError`, logged as above

**Cause**: Redis is not running, URL is incorrect, or network is unreachable

**When it occurs**:
```python
# Redis not running
@cache()
def my_function():
    return data()  # Logs a warning, runs data(), caches nothing
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

### Connection timeout

**Message**: `Timeout connecting to server` or `Timeout reading from ...` (redis-py)

**Exception**: none — `redis.exceptions.TimeoutError`, logged as above

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
# Increase connection and socket timeouts (both default to 5.0 seconds)
export CACHEKIT_SOCKET_TIMEOUT=10.0
export CACHEKIT_SOCKET_CONNECT_TIMEOUT=10.0
```

3. **Check network**:
```bash
# Ping Redis host
ping redis-server.example.com
# Check latency and packet loss
```

---

### Connection pool exhausted

**Message**: `Too many connections` (redis-py)

**Exception**: none — `redis.exceptions.MaxConnectionsError` (a `ConnectionError` subclass; the asyncio client raises plain `ConnectionError`), logged as above

**Cause**: Too many concurrent requests exceeding connection pool size

**Solution**:
```bash
# Increase connection pool size (default 10)
export CACHEKIT_CONNECTION_POOL_SIZE=50

# For high-concurrency applications
export CACHEKIT_CONNECTION_POOL_SIZE=100
```

---

## Serialization Errors

### Serialization unsupported type

**Message** (logged): `Serialization failed with ...: TypeError`, then `Failed to store in backend cache for ...: SerializationError`

**Exception**: none raised to a `@cache`-decorated caller: the result is returned but not stored in the backend. The exception is `@cache(interop=...)`: there an unsupported return value raises `InteropError` (a `ValueError` subclass, `from cachekit.interop import InteropError`) by design, so a cross-SDK entry is never silently skipped. Calling the serializer directly raises `TypeError`, e.g. `StandardSerializer does not support custom classes (Python-specific types). ...` or `StandardSerializer does not support pandas DataFrames or Series (Python-specific types). ...`

**Cause**: The default serializer handles `None`, `bool`, `int`, `float`, `str`, `bytes`, `list`, `tuple`, `dict`, `datetime`, `date` and `time`. Custom classes, dataclasses and DataFrames are not supported.

**When it occurs**:
```python
from cachekit import cache

class Point:
    def __init__(self, x, y):
        self.x, self.y = x, y

# WRONG - custom class not serializable by the default serializer
@cache()
def get_point():
    return Point(1, 2)  # Returned to the caller, never cached in the backend
```

**Solutions**:

1. **Convert objects to plain data** (use `dataclasses.asdict()` for dataclasses):
```python
from cachekit import cache

@cache()
def get_point():
    point = Point(1, 2)
    return {"x": point.x, "y": point.y}
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

3. **For plain data**, the default serializer (MessagePack) just works:
```python
import datetime

@cache()
def get_json_data():
    return {"key": "value", "count": 42, "ts": datetime.datetime.now()}  # Works
```

---

### Deserialization failed

**Error text**: `Cache entry failed envelope verification (corrupted cache entry): ...`, `Cache entry was written with integrity checking on but this reader has integrity checking disabled ...`, or `Cache entry is not a decodable MessagePack payload ...`

**Message** (logged): `L2 cache decrypt/integrity failure (corruption) for ...: ...`

**Exception**: none under `@cache` — the entry is evicted and the function recomputes. Calling a serializer's `deserialize()` directly raises `SerializationError` (there is no separate `DeserializationError` class).

**Cause**: Cached data is corrupted, or was written by an incompatible serializer/config. This is corruption *detection*, not tamper detection: the plaintext checksum is unkeyed xxHash3-64, which anyone with backend write access can recompute. Tamper detection requires encryption — see *Decryption failed* above.

**What it means**: A normal `@cache`-decorated call usually does not surface this to your code — `SerializationError` on a plaintext read is caught internally, the poisoned entry is evicted, and the function recomputes. You would typically only see it directly by calling a serializer's `deserialize()` method yourself, outside the cache decorator. (A tampered *encrypted* entry is a different code path — see *Decryption failed* above.)

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

### Circuit breaker open

**Message** (logged): `Circuit breaker ... transitioned to OPEN`

**Exception**: none for sync functions: while the breaker is open, `@cache` skips the backend and runs the function. Async functions currently raise `UnboundLocalError` (`cannot access local variable 'BackendError' ...`) on every call while the breaker is open — a known defect.

**Cause**: Consecutive failures reached `failure_threshold` (default 5). Backend errors count, and so do exceptions raised by the decorated function itself: five in a row open the breaker even when the backend is healthy.

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
- After `recovery_timeout` (default 30 seconds, `CircuitBreakerConfig`) the breaker goes half-open and tests the backend again
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

# When circuit breaker is open (sync functions):
# - Function still executes: expensive_operation() runs
# - Cache is bypassed: result is NOT cached
# - No exception raised: caller gets result normally
# - Warning is logged (if logging configured)
```

---

## Configuration Errors

### Redis URL not set

**Exception**: none. With neither `CACHEKIT_REDIS_URL` nor `REDIS_URL` set, cachekit connects to `redis://localhost:6379`. If nothing listens there, see *Redis unreachable*.

**Cause**: No Redis connection string provided

**Solution**:
```bash
# Set Redis URL
export CACHEKIT_REDIS_URL=redis://localhost:6379/0

# Or fallback
export REDIS_URL=redis://localhost:6379/0
```

---

### Wrong environment variable prefix

**Exception**: none. cachekit reads only `CACHEKIT_`-prefixed variables (plus `REDIS_URL`) and ignores any other name without a warning.

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

### Lock acquisition timeout

**Message** (logged): `Failed to acquire lock for ... after 5.0s, checking cache`

**Exception**: none. On a cache miss, an async `@cache` function on a lock-capable backend (Redis, CachekitIO) waits up to 5 seconds for the per-key lock. If another process still holds it, cachekit checks the cache once more, then runs the function itself.

**Cause**: Distributed lock could not be acquired (another process holds the lock)

**Solutions**:

1. **Check for stuck locks** (a lock expires on its own after 30 seconds):
```bash
# View locks in Redis
redis-cli KEYS "*:lock*"

# Clear stuck lock manually (if necessary)
redis-cli DEL <lock-key>
```

2. **Shorten the function**: the 5-second wait and 30-second lock lifetime are fixed, and the decorator has no parameter for them. A function slower than the wait makes waiters recompute in parallel.

---

## CachekitIO HTTP Errors

These errors occur when using `@cache.io()` with the CachekitIO SaaS backend. None raises to a `@cache`-decorated caller: each HTTP failure becomes a `BackendError` with a `BackendErrorType`, is logged as described under *Connection Errors*, and the function runs uncached. There is no automatic retry. Every failure type counts toward opening the circuit breaker unless you list it in `CircuitBreakerConfig.excluded_error_types` (empty by default).

### Authentication failure (401/403)

**Message**: `Authentication failed: HTTP 401` or `Authentication failed: HTTP 403`

**Exception**: none — logged as `BackendError` (`BackendErrorType.AUTHENTICATION`)

**Cause**:
- API key is missing, invalid, or revoked
- API key does not have permission for the requested operation

**Behavior**: Alert ops: every call runs uncached until the key is fixed.

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

### Rate limited (429)

**Message**: `Rate limit exceeded`

**Exception**: none — logged as `BackendError` (`BackendErrorType.TRANSIENT`)

**Cause**: Request volume exceeds the rate limit for the API key tier

**Behavior**: TRANSIENT — counts toward the circuit breaker's failure threshold.

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

### Server error (5xx)

**Message**: `Server error: HTTP 500` (or 502, 503, 504, etc.)

**Exception**: none — logged as `BackendError` (`BackendErrorType.TRANSIENT`)

**Cause**: Transient server-side error at the CachekitIO API

**Behavior**: TRANSIENT — if sustained, the circuit breaker opens.

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

### Client error (4xx)

**Message**: `Client error: HTTP 400` (or 404, etc.). A 413 reads `Value too large for cachekit.io backend (HTTP 413): value exceeds the server's maximum cache value size`.

**Exception**: none — logged as `BackendError` (`BackendErrorType.PERMANENT`)

**Cause**: Malformed request — invalid cache key format, payload too large, or other client-side issue

**Behavior**: PERMANENT — the same request fails the same way. Check your cache key and payload size.

---

### Request timeout

**Message**: `Request timeout: ...`

**Exception**: none — logged as `BackendError` (`BackendErrorType.TIMEOUT`)

**Cause**: HTTP request to the CachekitIO API exceeded the configured timeout

**Behavior**: TIMEOUT — counts toward the circuit breaker's failure threshold.

**Solution**:
```bash
# Increase the request timeout (seconds)
export CACHEKIT_TIMEOUT=10.0
```

---

### Connection error

**Message**: `Connection failed: ...`

**Exception**: none — logged as `BackendError` (`BackendErrorType.TRANSIENT`)

**Cause**: Network-level failure — DNS resolution failed, connection refused, or network unreachable

**Behavior**: TRANSIENT — the circuit breaker opens after sustained failures.

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

| HTTP Status / Exception | `BackendErrorType` |
|---|---|
| 401, 403 | `AUTHENTICATION` |
| 429 | `TRANSIENT` |
| 5xx | `TRANSIENT` |
| 413, other 4xx | `PERMANENT` |
| `TimeoutException` | `TIMEOUT` |
| `ConnectError`, `NetworkError` | `TRANSIENT` |
| Other | `UNKNOWN` |

No type is retried. All count toward the circuit breaker by default; `CircuitBreakerConfig(excluded_error_types=(...))` exempts the ones you list.

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
