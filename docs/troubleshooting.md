**[Home](README.md)** › **Troubleshooting Guide**

# Troubleshooting Guide

> **Solutions for common cachekit issues and error messages**

---

## Common Errors

<details>
<summary><strong>Circuit Breaker Errors</strong></summary>

**Issue**: Circuit breaker is open and calls run uncached

**What it means**:
- Five failures within a 60-second rolling window (five is the default [`failure_threshold`](features/circuit-breaker.md); successes do not reset the count, and older failures stop counting): a failure to create the backend client, or another failure listed under [Circuit breaker open](error-codes.md#circuit-breaker-open). Exceptions raised by the decorated function itself never count. A return value that fails to serialize or encrypt for the cache write does not count. A cached entry that fails to deserialize or decrypt does not count under either policy: fail-open (the default) evicts it and recomputes, and with `fail_closed=True` an authentication failure raises and keeps the entry. Backend read and write failures do not currently count
- Calls to this function that miss L1 run uncached until the breaker recovers (L1 hits are still served): after the cooldown (30 seconds by default) it goes HALF_OPEN and probes, then closes after three successes or reopens on a counted failure

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

3. **Let the breaker recover** — no restart is needed:
- After the cooldown (`recovery_timeout`, 30 seconds by default) it goes HALF_OPEN with three probe slots (a probe whose function raises hands its slot to the next call, so while it keeps raising, more than three calls can probe in one cycle); three successes close it, and a counted failure reopens it for another cooldown
- While open, sync and async functions with an L2 backend still serve L1 hits and run without caching on an L1 miss (L1-only mode, `backend=None`, never consults the breaker) — see [Circuit breaker open](error-codes.md#circuit-breaker-open)

4. **Increase timeout if network is slow** (both default to 5.0 seconds):
```bash
export CACHEKIT_SOCKET_TIMEOUT=10.0
export CACHEKIT_SOCKET_CONNECT_TIMEOUT=10.0
```

Exceptions raised by your own function reach the caller unchanged, and `@cache` runs the function at most once per call: an exception it raises, a `BackendError` included, is never retried. Your function's exceptions never count toward the circuit breaker, so a function that raises does not stop its own caching.

</details>

<details>
<summary><strong>Serialization Failures</strong></summary>

**Issue**: results are never cached, and the log shows `Serialization failed with ...: TypeError` followed by `Failed to store in backend cache for ...: SerializationError`

**What it means**:
- Cache attempted to serialize function result
- Data type is not compatible with chosen serializer
- The default serializer (MessagePack) supports `None`, `bool`, `int`, `float`, `str`, `bytes`, `list`, `tuple`, `dict`, `datetime`, `date` and `time`
- The call still returns the result, except with `@cache(interop=...)`, where an unsupported type raises `InteropError` — see [Serialization unsupported type](error-codes.md#serialization-unsupported-type)

**Solutions**:

1. **For custom objects**, convert to a supported type before caching:
```python notest
from cachekit import cache

# Convert to dict before caching
@cache
def get_custom_object():
    obj = MyCustomClass()
    return obj.__dict__  # or use obj.to_dict() / dataclasses.asdict(obj)
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

   **Why not auto-detect Pydantic models?** See [Serializer Guide - Caching Pydantic Models](serializers/pydantic.md) for the detailed rationale.

</details>

<details>
<summary><strong>Connection Issues</strong></summary>

**Issue**: Redis connection timeout or refused

`@cache` does not raise these: it logs the failure and runs the function. If Redis was unreachable when the backend was first built (the `Connection refused` line below comes from that first ping), nothing is cached, the failure counts toward the circuit breaker, and the next call tries again. If Redis went away later, the result is still stored in L1. The log line names a `BackendError` wrapping one of these redis-py errors (see [Connection Errors](error-codes.md#connection-errors)):
```
Error 111 connecting to localhost:6379. Connection refused.
Timeout connecting to server
Timeout reading from ...
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

4. **For timeout issues**, increase timeout values (both default to 5.0 seconds):
```bash
export CACHEKIT_SOCKET_TIMEOUT=10.0
export CACHEKIT_SOCKET_CONNECT_TIMEOUT=10.0
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

**Error messages** (types and when each raises: [Encryption Errors](error-codes.md#encryption-errors)):
```
cache.secure requires master_key parameter or CACHEKIT_MASTER_KEY environment variable
A master key is present (CACHEKIT_MASTER_KEY) but this cache states no encryption intent ...
EncryptionWrapper (or the serializer name 'encrypted') cannot be a cache decorator's serializer: ...
CACHEKIT_MASTER_KEY must be hex-encoded: ...
CACHEKIT_MASTER_KEY must be at least 32 bytes (256 bits). Got ... bytes. ...
Decryption failed: ...
```

**Solutions**:

See [Zero-Knowledge Encryption - Troubleshooting](features/zero-knowledge-encryption.md#troubleshooting)

**Common causes**:
1. Master key not set when using `@cache.secure()`
2. Master key set, but a cache states no encryption intent — add `encryption=False` for plaintext ([details](error-codes.md#master-key-present-no-encryption-intent))
3. An `EncryptionWrapper` passed as a decorator's `serializer=` — pass its inner serializer to `@cache.secure(master_key=..., serializer=...)` instead ([details](error-codes.md#encrypting-serializer-on-a-decorator))
4. Master key format invalid (not hex-encoded)
5. Master key rotated (can't decrypt old cached data)
6. Data corruption during storage/retrieval

**Quick fix**:
```bash
# First-time setup only: generate a valid encryption key
export CACHEKIT_MASTER_KEY=$(openssl rand -hex 32)

# Restart application
python app.py
```

If the key was rotated, do not generate a new key or flush: keep the old key
decrypt-only in `CACHEKIT_PREVIOUS_MASTER_KEYS` and follow the
[key rotation runbook](https://docs.cachekit.io/concepts/key-rotation/).

</details>

---

## CachekitIO Backend Issues

<details>
<summary><strong>Can't Connect to cachekit.io</strong></summary>

**Issue**: Requests to cachekit.io fail immediately or time out

**What it means**:
- API key not configured
- Wrong endpoint URL
- Network/firewall blocking outbound HTTPS

**Solutions**:

1. **Verify API key is set**:
```bash
echo $CACHEKIT_API_KEY
# Should output your key — if blank, set it:
export CACHEKIT_API_KEY=your_api_key_here
```

2. **Check the API URL**:
```bash
# Default — leave unset unless self-hosting
echo $CACHEKIT_API_URL
# Expected: unset or https://api.cachekit.io
```

3. **Test network connectivity**:
```bash
curl -sf https://api.cachekit.io/healthz
# Should return 200 OK — if it hangs, check firewall/proxy
```

</details>

<details>
<summary><strong>401 Unauthorized</strong></summary>

**Issue**: cachekit.io returns `401 Unauthorized`

**What it means**:
- API key is invalid, revoked, or expired
- Key is set but doesn't match the project

**Solutions**:

1. **Confirm the key is correct**:
```bash
# Compare against the key shown in your cachekit.io dashboard
echo $CACHEKIT_API_KEY
```

2. **Request a new key** at [cachekit.io](https://cachekit.io) and rotate:
```bash
export CACHEKIT_API_KEY=new_key_here
```

3. **Check the key for a bad copy.** A key with a character outside the RFC 6750 bearer-token set cannot cause a 401: cachekit raises `ConfigurationError: ... is not a valid bearer token` when the backend is built, before any request is sent (see [the rules](backends/cachekitio.md#convenience-shorthand-via-cacheio)). A key with extra, missing or altered characters from inside the set (`A-Z a-z 0-9 - . _ ~ + /`, then any number of trailing `=`) still passes that check and returns 401 — for example a trailing `.` copied from a sentence, an extra `=`, or a truncated or doubled paste. Copy the key again from the dashboard.

</details>

<details>
<summary><strong>429 Rate Limited</strong></summary>

**Issue**: cachekit.io returns `429 Too Many Requests`

**What it means**:
- Request rate exceeds your plan's limit
- Burst traffic spike hitting per-second cap

**Solutions**:

1. **Reduce cache miss rate** (more hits = fewer upstream calls):
```python
# Increase TTL to reduce backend round-trips
@cache(ttl=3600)  # 1-hour TTL instead of short TTL
def expensive_query(id):
    return fetch(id)
```

2. **Sync calls do not retry a rate-limited request** — the function runs, its result is still stored in L1, and the failure does not count toward the circuit breaker. Async calls do not retry it either: a 429 on the lock request ends the lock wait, and the function runs without the lock (see [CachekitIO HTTP Errors](error-codes.md#cachekitio-http-errors)). If you're hitting 429 consistently, reduce request concurrency or upgrade your plan.

3. **Check your current usage** at [cachekit.io](https://cachekit.io) dashboard.

</details>

<details>
<summary><strong>SSRF Rejection</strong></summary>

**Issue**: Request rejected with SSRF protection error

**What it means**:
- A custom `CACHEKIT_API_URL` points to an internal/private host
- SSRF protection blocks requests to non-allowlisted destinations
- Only `api.cachekit.io` is permitted by default

**Solutions**:

1. **Use the default endpoint** (unset any custom URL):
```bash
unset CACHEKIT_API_URL
```

2. **If self-hosting**, confirm your host is correctly configured and reachable:
```bash
export CACHEKIT_API_URL=https://your-self-hosted-endpoint.example.com
curl -sf $CACHEKIT_API_URL/healthz
```

3. **Never point `CACHEKIT_API_URL` at localhost or internal IPs** — these are blocked by SSRF protection regardless of environment.

</details>

<details>
<summary><strong>Connection Timeout</strong></summary>

**Issue**: Requests to cachekit.io hang and eventually time out

**What it means**:
- High network latency between your environment and api.cachekit.io
- Timeout configured too low for your network conditions
- Transient outage or overloaded backend

**Solutions**:

1. **Check configured timeout** (the `CACHEKIT_SOCKET_*` variables apply to Redis only):
```bash
echo $CACHEKIT_TIMEOUT
```

2. **Increase timeout for high-latency environments**:
```bash
export CACHEKIT_TIMEOUT=10.0
```

3. **Measure actual latency**:
```bash
curl -o /dev/null -s -w "Connect: %{time_connect}s  Total: %{time_total}s\n" \
    https://api.cachekit.io/healthz
```

4. **A timeout does not fail the call**: the request is logged, the function runs, and its result is still stored in L1. Timeouts do not count toward the circuit breaker. A timed-out async lock request ends the lock wait at once (see [CachekitIO HTTP Errors](error-codes.md#cachekitio-http-errors)).

</details>

---

## Error Reference

The errors cachekit raises or logs, with whether each reaches your code, are in the [Error Reference](error-codes.md).

---

## Recovery Strategies

<details>
<summary><strong>Cache Invalidation</strong></summary>

**One entry or one function**: call the decorated function's invalidation method. With
arguments it derives the key the call used, including a custom `key=`, and deletes it from
L1 and L2:

```python notest
from cachekit import cache

@cache(namespace="users")
def get_user(user_id):
    return fetch_user(user_id)

user = update_user(user_id, data)
get_user.invalidate_cache(user_id)  # an async function: await get_user.ainvalidate_cache(user_id)

get_user.invalidate_cache()  # every get_user entry
```

How far the no-argument form reaches depends on the backend: see
[Whole-Function Invalidation](features/l1-invalidation.md#whole-function-invalidation).

**By prefix**: run the `scan_iter` + `unlink` script under *Option 3: Data corruption* in
[Decryption failed](error-codes.md#decryption-failed---authentication-tag-mismatch).
Flush the database (`redis-cli FLUSHDB`) only if it is dedicated to cachekit.

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

With the CachekitIO backend, a `DEBUG` root also shows urllib3's request lines, raw cache keys in their paths, never the API key or lock tokens. Read [transport logs](../SECURITY.md#cache-key-redaction-in-logs-cwe-532) if your keys carry identifiers.

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
from cachekit.serializers import StandardSerializer

serializer = StandardSerializer()

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

**[GitHub Issues](https://github.com/cachekit-io/cachekit-py/issues)** · **[Documentation](README.md)**

</div>
