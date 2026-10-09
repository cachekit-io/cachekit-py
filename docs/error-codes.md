**[Home](README.md)** › **Error Reference**

# Error Reference

The errors cachekit raises or logs, and how to fix them. cachekit has no numeric error codes: catch the class shown under **Exception**.

Configuration errors raise when the decorator is applied. Backend failures (connection, timeout, CachekitIO HTTP errors), serialization, deserialization, circuit-breaker and lock failures do not raise to the caller: a `@cache`-decorated call logs the failure and runs the function. Where that holds, the entry reads **Exception**: none. Two exceptions to that rule:

- Decryption failures raise only when fail-closed is on.
- With `interop=...`, a return value the interop data model can't represent raises `InteropError`.

After a read or write failure on a backend that is already built, the result is still stored in L1 (on by default), so later calls in the same process hit it. Two backend failures cache nothing: a backend that cannot be built on the first call, such as an auto-detected Redis that is down then (see [Connection Errors](#connection-errors)), and a failed streamed write from a sync function using the plaintext Arrow serializer on the File backend, which never goes to L1 (see [Circuit breaker open](#circuit-breaker-open)).

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

Exporting it makes every preset except `@cache.secure` and `@cache.local` that states no encryption
intent raise ([below](#master-key-present-no-encryption-intent)); passing `master_key=`
to `@cache.secure` instead affects only that cache.

**Verification**:
```bash
# Verify key is set and correct length
python -c "import os; k = os.getenv('CACHEKIT_MASTER_KEY', ''); print(f'Key length: {len(k)} (need 64)')"
# Output: Key length: 64 (need 64)
```

---

### Master key present, no encryption intent

**Message**: `A master key is present (CACHEKIT_MASTER_KEY) but this cache states no encryption intent ...` (or `(master_key=)` when the key was passed)

**Exception**: `ConfigurationError`, raised when the decorator is applied

**Cause**: a master key is available — `CACHEKIT_MASTER_KEY` is set, or `master_key=` was passed (flat on bare `@cache`, or `EncryptionConfig(master_key=...)`) — but the cache states no encryption intent: no `encryption=`, or an `EncryptionConfig` without `enabled=`. A key is a key source, not an activation switch, so cachekit refuses to guess between encrypting and storing plaintext. This applies to every preset except `@cache.secure` and `@cache.local`, including `backend=None` and caches with a tenant extractor ([activation table](features/zero-knowledge-encryption.md#activation-the-master-key-is-a-source-not-a-switch)).

**When it occurs**:
```python notest
# CACHEKIT_MASTER_KEY is set in the environment
@cache.production(ttl=600)  # Raises ConfigurationError here, at decoration time
def get_catalog():
    return fetch_catalog()
```

**Solution**: state the intent on each such cache.
```python notest
@cache.production(ttl=600, encryption=False)  # plaintext; stale ciphertext still decrypts on read
def get_catalog():
    return fetch_catalog()

@cache.production(ttl=600, encryption=EncryptionConfig(enabled=True, single_tenant_mode=True))  # encrypt
def get_orders():
    return fetch_orders()
```

Or use `@cache.secure(...)` for the encrypted ones. On bare `@cache` the flat spelling is `encryption=True, single_tenant_mode=True`.

---

### Encrypting serializer on a decorator

**Message**: `EncryptionWrapper (or the serializer name 'encrypted') cannot be a cache decorator's serializer: ...`

**Exception**: `ConfigurationError`, raised when the decorator is applied

**Cause**: an `EncryptionWrapper` instance, or the `"encrypted"` serializer name, was passed as `serializer=` to a cache decorator: bare `@cache`, any preset that takes `serializer=` (`@cache.secure` included), or a `DecoratorConfig`. The decorator never gives that serializer the cache key each ciphertext is bound to, so it could not store an entry. Earlier releases accepted some of these spellings and ran the function on every call. With `backend=None` you get a different error instead: the L1-only `encryption requires a backend` error, or an error from a check that runs before that one, such as `@cache.secure`'s missing-key error when no key is set outside the wrapper.

**Solution**: pass the master key and the inner serializer to `@cache.secure`, which applies `EncryptionWrapper` itself. Omit `serializer=` for the default MessagePack.
```python notest
from cachekit.serializers import OrjsonSerializer

@cache.secure(master_key=secret_key, serializer=OrjsonSerializer())  # the serializer EncryptionWrapper wrapped
def get_api_keys(tenant_id: str):
    return fetch_api_keys(tenant_id)
```

`EncryptionWrapper` stays available for direct use outside a decorator.

---

### Serializer refused under encryption

**Message**: `Encryption requires a serializer that decodes one fixed format after decryption (protocol ENC-2) ...`

**Exception**: `ConfigurationError`, raised when the decorator is applied or when an `EncryptionWrapper` is built

**Cause**: encryption was combined with `serializer="auto"` (or `"pythonic"`), an `AutoSerializer` instance, or a custom serializer whose class does not declare `cross_sdk_compatible = True`. The protocol requires the step after decryption to be the reader's configured serializer, never a guess from the decrypted bytes, and `AutoSerializer` picks its format by inspecting them. The rule holds on every backend, a local file backend included. It is not about other-language readers: `cross_sdk_compatible` only declares a fixed post-decryption format, and `ArrowSerializer` passes although its envelope is not a cross-SDK wire format. The decorators already refuse these. `EncryptionWrapper` built directly now refuses them too; earlier releases accepted any serializer there.

**Solution**: use `StandardSerializer` (the default), `OrjsonSerializer` or `ArrowSerializer`. Do not subclass `AutoSerializer` to set the flag. Set `cross_sdk_compatible = True` only on a custom serializer whose `deserialize` decodes one fixed format and never inspects the bytes to choose one ([Custom Serializers](serializers/custom.md#under-encryption)).

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
    n = len(bytes.fromhex(key))
except ValueError:
    print('Invalid hex')
else:
    print(f'Valid key: {n} bytes' if n >= 32 else f'Too short: {n} bytes (need at least 32)')
"
```

---

### Raw key is not 32 bytes

**Message**: one of
- `master_key must be exactly 32 bytes (256 bits), got .... It takes the raw key: decode a hex key with bytes.fromhex().`
- `Previous master key at position ... must be exactly 32 bytes (256 bits), got ... — per-key requirements are identical to master_key.`

**Exception**: `EncryptionError` for `master_key=`, `KeyringConfigurationError` (a `ValueError` subclass) for `previous_master_keys=`, raised when an `EncryptionWrapper` is built

**Cause**: `EncryptionWrapper` takes raw key bytes, exactly 32 of them, from 0.23.0. A hex string's own bytes, such as `hex_key.encode()`, are 64 bytes and are refused. Earlier releases accepted any key of at least 32 bytes, so that call derived a key that no other SDK, and not `@cache.secure`, derives from the same hex key. The decorators and `CACHEKIT_MASTER_KEY` take hex and keep the hex rule above: at least 32 bytes once decoded.

**Solution**: decode the hex key before passing it.
```python notest
from cachekit.serializers import EncryptionWrapper

wrapper = EncryptionWrapper(master_key=bytes.fromhex(hex_key))
```

---

### Bytes key where a hex string is taken

**Message**: `master_key takes a hex string, not bytes: pass key.hex(). Only EncryptionWrapper takes raw key bytes.`

**Exception**: `TypeError`, raised when `DecoratorConfig.secure()`, `EncryptionConfig`, `CacheSerializationHandler` or `validate_encryption_config` is called, or when `@cache` or `@cache.secure` is applied

**Cause**: `master_key=` takes the key as a hex string everywhere except `EncryptionWrapper`, the one entry point that takes raw key bytes ([Raw key is not 32 bytes](#raw-key-is-not-32-bytes)). Earlier releases did not check the type, so what a bytes key did depended on the encryption setting. With encryption on, the decorator and `validate_encryption_config` raised `AttributeError`. With encryption unset, the decorator raised `ConfigurationError`, as for any key passed without an encryption setting. With encryption off (`encryption=False`, `EncryptionConfig(enabled=False)`), the key was accepted, and failed only when an entry encrypted before encryption was turned off was read. It now raises `TypeError` where it is passed, whatever the encryption setting. On Python 3.14, `CacheSerializationHandler` also read a bytes key holding ASCII hex digits as that hex; that key raises too.

**Solution**: pass the key's hex.
```python
import secrets

from cachekit import DecoratorConfig

key = secrets.token_bytes(32)  # raw key bytes, as from a key management service
config = DecoratorConfig.secure(master_key=key.hex())
```

---

### Decryption failed - authentication tag mismatch

**Message**: `Decryption failed: ...`. A key or tenant mismatch reads `Key fingerprint mismatch: ...` or `Tenant mismatch: ...` instead.

**Exception**: `DecryptionAuthenticationError` (`from cachekit.serializers.encryption_wrapper import DecryptionAuthenticationError`, a `SerializationError` subclass); a tenant mismatch raises its subclass `TenantMismatchError`

**What it means**: By default a `@cache.secure` read does not raise. It logs `... cache decrypt/integrity failure (auth_tamper) for ...`, attempts to evict the entry (best effort) and recomputes. The exception reaches your code only with fail-closed on: `CACHEKIT_ENCRYPTION_FAIL_CLOSED=true`, or `@cache.secure(fail_closed=True)` on the decorator. Fail-closed keeps the entry as evidence.

**Cause**:
- Master key was changed (old encrypted data can't be decrypted)
- Cached data was corrupted during storage/retrieval
- Data was modified externally
- Another tenant's entry sits at the same key (`Tenant mismatch: ...`, a cache with a `tenant_extractor`)

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

Keep the old key decrypt-only in `CACHEKIT_PREVIOUS_MASTER_KEYS` (comma-separated
hex, max 3) so entries it wrote stay readable, and follow the
[key rotation runbook](https://docs.cachekit.io/concepts/key-rotation/). Do not flush or mint another key: a flush drops
unrelated data on a shared database, and a fresh key only repeats the failure.

**Option 2: Wrong key on some instances (configuration drift)**

Compare a hash of the key across instances — never print the key itself:
```bash
python -c "import hashlib, os; print(hashlib.sha256(bytes.fromhex(os.environ['CACHEKIT_MASTER_KEY'])).hexdigest()[:16])"
```

An instance whose hash differs from the rest is misconfigured. Do not fix it by
setting `CACHEKIT_MASTER_KEY` back to a key that has already encrypted and been
retired: re-promoting a key resumes its used AES-GCM nonce budget (see
[Key Rotation Pattern](features/zero-knowledge-encryption.md#key-rotation-pattern)). Move the drifted instance forward to the fleet's
current key, and keep the key it wrote with decrypt-only in
`CACHEKIT_PREVIOUS_MASTER_KEYS`.

**Option 3: Data corruption**

Evict the suspect entry through the decorated function, with the same
arguments and in the same tenant context as the failing read. It derives the key
the read used and clears both L1 and L2:

```python notest
get_data.invalidate_cache(user_id)         # sync function
await get_data.ainvalidate_cache(user_id)  # async function
```

Called with no arguments on the tenant-scoped Redis backend it deletes every
process's L2 entries for the calling tenant; on other backends it evicts only the
keys this process has cached, not the fleet's. A function with a custom `key=`
works the same way: `invalidate_cache(<args>)` derives the key `key=` wrote (see
[Whole-Function Invalidation](features/l1-invalidation.md#whole-function-invalidation)
for the no-argument form).

For bulk eviction on Redis, delete one namespace's or one function's generated
keys with the redis-py client cachekit installs, not a `redis-cli --scan` pipeline:
a line-based pipeline can stop at a key containing a quote and splits a key
containing a newline into names that match nothing, and it still exits 0 with keys
left behind. `scan_iter` returns each key whole. A `SCAN` pattern alone cannot
scope the delete either, because a namespace may contain `:` or glob characters:
pattern `ns:users:*` also matches namespace `users:admin`. The script below uses
`SCAN` only to find candidates and deletes a key only if it has exactly the shape
cachekit generates for that namespace or function. Shape is all a key carries, so
the script cannot tell a generated key from a custom one of the same shape (see
below).

```python notest
# Needs a live Redis: set the URL, including the database number cachekit uses.
import re
from urllib.parse import quote

import redis

r = redis.Redis.from_url("redis://localhost:6379/0")

# The Redis backend stores keys under t:<tenant>: with <tenant> "default" unless
# you set one, percent-encoded (an int or UUID tenant as its str() first).
# One run reaches one tenant. If you set tenant_context, run it for every tenant
# your application uses. Each key from r.scan_iter(match="t:*") names a tenant
# between its first two colons, already encoded, so set prefix = "t:" + that + ":"
# without quote(). In a database another application shares, a segment found that
# way may be the other application's: run only for segments you know are yours.
# A RedisBackend you pass as backend= adds no prefix: set prefix = "".
prefix = "t:" + quote("default", safe="") + ":"
# The namespace exactly as passed to @cache(namespace=...), or None (or "") for none.
namespace = "users"
# One function's name as it appears in the key (see below), or None for every
# function in the namespace (or, with namespace None, every un-namespaced one).
function = None

# The key builder replaces spaces, \r and \n with _.
ns = "" if not namespace else "ns:" + re.sub(r"[ \r\n]", "_", namespace) + ":"
fn = r"[A-Za-z0-9_.]+" if function is None else re.escape(function)
generated = re.compile(re.escape(prefix + ns + "func:") + fn + r":args:[0-9a-f]{64}:[^:]+")
# A key over 250 characters is stored as its first 50, ":" and a hash (see below).
head = (ns + "func:" + ("" if function is None else function + ":args:"))[:50]
shortened = re.compile(re.escape(prefix + head) + f".{{{50 - len(head)}}}:[0-9a-f]{{32}}")
candidates = prefix + ("ns:*" if namespace else "func:*")


def matching():
    for key in r.scan_iter(match=candidates, count=1000):
        if generated.fullmatch(key.decode("utf-8", "replace")):
            yield key


batch = []
for key in matching():
    batch.append(key)
    if len(batch) == 1000:
        r.unlink(*batch)
        batch.clear()
if batch:
    r.unlink(*batch)

left = sum(1 for _ in matching())
print(f"{left} generated keys left")  # expect 0
stale = sum(1 for k in r.scan_iter(match=candidates, count=1000) if shortened.fullmatch(k.decode("utf-8", "replace")))
if stale:
    print(f"{stale} shortened keys may belong here: evict them with invalidate_cache(<args>)")
```

A function's name in the key is `<module>.<qualname>` with every character outside
`A-Z a-z 0-9 _ .` replaced by `_`, so `outer.<locals>.inner` becomes
`outer._locals_.inner`.

The script reaches the keys cachekit generates from a call's arguments, up to 250
characters long before the `t:<tenant>:` prefix. The key builder stores a longer
key as its first 50 characters, `:` and a hash, which nothing can tie back to its
function. That happens when the namespace and the function's name together run
past about 170 characters. The script counts keys of that shape under the same
first 50 characters and prints a warning instead of deleting them, because another
namespace or function can share those characters.

A function with a custom `key=` stores its entries at
`t:<tenant>:<namespace, or default>:<your key>`, and one with `fast_mode=True` or
`interop=` uses its own key shape, so the script normally leaves them alone. Evict
them with `invalidate_cache(<args>)` or a no-argument
[whole-function invalidation](features/l1-invalidation.md#whole-function-invalidation).
The exception is a custom entry whose stored key, `<namespace>:<your key>`, itself
has the generated shape. That needs a custom-`key=` namespace of `ns` or `func`, or
one starting `ns:` or `func:`, such as `ns:users` with a key
`func:<name>:args:<64 hex>:1s`. It is the very key a generated function could
write, so the script deletes it with namespace `users`. Do not give a custom-`key=`
function such a namespace if it shares a database with generated keys.

Flush the database (`FLUSHDB`) only if it is dedicated to cachekit.

The function recomputes and re-caches on the next call.

**Option 4: Tenant mismatch**

Cache keys carry no tenant, so tenants calling with identical arguments share an
entry, and a read refuses the one another tenant wrote. Evicting it helps only until
that tenant writes it again. Keep tenants on separate keys: give each its own
`namespace` or deployment, or make the tenant id a keyword argument of the cached
function (see [Multi-Tenant Isolation](features/zero-knowledge-encryption.md#multi-tenant-isolation)).

**Prevention**: rotate with the keyring, not a key swap — follow the
[key rotation runbook](https://docs.cachekit.io/concepts/key-rotation/) (see also [Key Rotation Pattern](features/zero-knowledge-encryption.md#key-rotation-pattern)).

---

## Connection Errors

None of these raise to a `@cache`-decorated caller, sync or async. cachekit wraps the redis-py exception in a `BackendError`, logs it, and runs the function. What happens to the result depends on when Redis fails:

- **Unreachable when the backend is built.** Without `backend=` or `set_default_backend()`, cachekit builds its Redis backend on the first call and pings Redis then. If the ping fails, the call runs the function uncached: nothing goes to L1 or L2, the failure counts toward the circuit breaker, and the next call tries the build again. Once the breaker opens (five failures within 60 s by default), calls run the function without trying to connect until the cooldown ends. A `RedisBackend` you build yourself does not ping, so it never fails this way.
- **Lost after the backend is built.** A failed read is a miss, and a failed write still stores the result in L1, so later calls in the same process hit it. These errors do not currently count toward the breaker.

Depending on where the failure happens, the log line is one of:

- `Cache operation '...' failed for key '...': ...` (WARNING; the last part names the error, e.g. `BackendError(transient)`)
- `Backend error getting key ...: BackendError(...)` (ERROR)

The redis-py exceptions below reach your code only when you use a Redis client directly. A direct call on a cachekit backend, such as `RedisBackend.get()`, raises a `BackendError` instead, which keeps only the redis-py exception's class ([Error Categories](api-reference.md#error-categories)).

### Redis unreachable

**Message**: `Error ... connecting to ...` (redis-py), e.g. `Error 111 connecting to localhost:6379. Connection refused.`

**Exception**: none — `redis.exceptions.ConnectionError`, logged as above

**Cause**: Redis is not running, URL is incorrect, or network is unreachable

**When it occurs**:
```python
# Redis not running
@cache()
def my_function():
    return data()  # Logs a warning, runs data(), caches nothing, counts toward the breaker
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

**Message**: `No connection available.` (redis-py)

**Exception**: none — `redis.exceptions.ConnectionError`, logged as above. The pool waits up to the socket timeout for a connection to be released before raising it: `CACHEKIT_SOCKET_TIMEOUT`, unless the Redis URL sets `?socket_timeout=`, which wins. An asyncio client from `get_async_client()` does not wait: it raises `ConnectionError: Too many connections` at once

**Cause**: More concurrent Redis operations than the pool size, each holding its connection longer than the timeout

**Solution**:
```bash
# Increase connection pool size (Redis default 50)
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

**Error text**: `Cache entry failed envelope verification (corrupted cache entry): ...`, `Cache entry failed envelope verification: decoded to a ByteStorage envelope shape ...`, `Cache entry was written with integrity checking on but this reader has integrity checking disabled ...`, `Cache entry is not a decodable MessagePack payload ...`, `NumPy payload disagrees with header format '...'`, `Cache entry header claims a <type> format, not a string`, or `Cache entry header claims format '...', which no writer emits`

**Message** (logged): `L2 cache decrypt/integrity failure (corruption) for ...: ...`, or `(envelope_shape)` in place of `(corruption)` for the envelope-shape refusal described below

**Exception**: none under `@cache` — cachekit attempts to evict the entry (best effort; a failed delete is logged and the entry stays) and the function recomputes. Calling a serializer's `deserialize()` directly raises `SerializationError` (there is no separate `DeserializationError` class).

**Cause**: Cached data is corrupted, or was written by an incompatible serializer/config. This is corruption *detection*, not tamper detection: the plaintext checksum is unkeyed xxHash3-64, which anyone with backend write access can recompute. Tamper detection requires encryption — see *Decryption failed* above.

One specific cause worth naming: `... envelope format 'X' disagrees with header format 'Y'`, `... unknown envelope format 'X'`, or `NumPy payload disagrees with header format 'Y'`. The stored format is recorded twice — once inside the ByteStorage envelope, once in the plaintext CK header — and the checksum covers neither (it covers the payload bytes only). Rather than letting either copy override the other, `AutoSerializer` decodes only a format it could have written and only when both copies agree; a disagreement is read as corruption and the entry is evicted and recomputed. For an integrity-on entry that still carries its header, bit rot in a single field therefore produces a clean miss instead of a silently wrong Python type. An integrity-off entry has no envelope, so its header claim is the only copy and is held to the same rule on its own: a claim naming no format `AutoSerializer` writes (`msgpack`, `dataframe`, `series`, `numpy`, `arrow`), or no claim at all, raises `Cache entry header claims format '...', which no writer emits` and the entry is evicted and recomputed, so a single rotted header byte cannot return a stored `Series` or `DataFrame` as a `dict`. An `Arrow` or `NUMPY_RAW` entry that lost its claim still decodes, because its bytes identify it. Where a copy survives unopposed it still decides the type: an integrity-on entry whose header lost `original_type` has no header claim, so a `Series` whose envelope `format` is rewritten to `msgpack` comes back as a `dict`, and an integrity-off header rewritten to another writable format is taken at its word. Closing that needs the format inside the digest, which is a protocol change. The same agreement rule covers `NUMPY_RAW` bytes, whose own magic is the second copy: a header naming any other format contradicts them, and the entry is evicted and recomputed rather than returning an array. A header claim that is not a string at all (`Cache entry header claims a list format, not a string`) is read the same way — the header is plaintext and nothing types that field, so a non-string there is rot, not a format. The message names only the type; the claim itself is never echoed.

**One cause that is not corruption:** an `AutoSerializer` with integrity checking off (`@cache.minimal(serializer="auto")`, or the direct API) cannot cache a value that is a top-level 4-element list of which three or more slots look like a ByteStorage envelope's — `bytes`, a list of eight small ints, a non-negative int, a known format string — judged as its reader decodes them, so a `bytearray` counts as `bytes` and an `IntEnum` as an int. Neither the bytes slot nor the format slot is required, so `[b"\x89PNG", [255, 0, 0, 255, 0, 255, 0, 255], 4096, "rgb"]` qualifies. The read path cannot tell such a value from a rotted envelope, so it refuses both rather than return a rotted envelope's compressed payload as your object. The writer therefore refuses it first: `serialize()` raises `EnvelopeShapeError` (a `SerializationError`) with `Value cannot be cached by an AutoSerializer with integrity checking off ...`, and under `@cache` the result is returned uncached on every call, logged as `Serialization failed with auto: EnvelopeShapeError` then `Failed to store in backend cache for ...: SerializationError`, with no backend write and nothing counted as a decrypt failure. The read-side refusal remains for an entry written before this check or by another writer: its message is `Cache entry failed envelope verification: decoded to a ByteStorage envelope shape that no reader verified ...`, raised as `EnvelopeShapeError` and counted under `cachekit_decrypt_failures_total{reason="envelope_shape"}`, deliberately **not** `corruption`. The entry is evicted as below and the rewrite is refused, so a key that keeps coming back there means another writer, such as an older cachekit version, is still storing it. Nothing else is affected: a `tuple`, a nested or 3-/5-element list, and the default `StandardSerializer` all round-trip. Wrap the value (`{"v": [...]}`) or cache it with the default serializer; caching it as-is needs the writer to mark an envelope as one, which is a wire-format change.

**What it means**: A normal `@cache`-decorated call usually does not surface this to your code — `SerializationError` on a plaintext read is caught internally, cachekit attempts to evict the poisoned entry (best effort), and the function recomputes. You would typically only see it directly by calling a serializer's `deserialize()` method yourself, outside the cache decorator. (A tampered *encrypted* entry is a different code path — see *Decryption failed* above.) One direct-API cause is an `integrity_checking` mismatch between the writer and reader — see [AutoSerializer's cross-config reads](serializers/auto.md#cross-config-reads-integrity_checking-mismatch). The default `StandardSerializer` raises `Cache entry was written with integrity checking on but this reader has integrity checking disabled` for that mismatch — see [its cross-config reads](serializers/default.md#compression-and-integrity).

**Solution**: If the automatic eviction failed, evict the entry yourself. A failed delete logs `Backend error deleting key ...` or `Unexpected error deleting key ...` (or, less often, `Failed to evict poisoned L2 entry ...`), and a delete the backend simply refuses may log nothing, so check that the entry is gone rather than relying on one log line. To evict it, do as in *Option 3: Data corruption* under *Decryption failed* above: call the decorated function's `invalidate_cache(...)` with the failing read's arguments, or delete by the `t:<tenant>:...` prefix. Flush the database (`redis-cli FLUSHDB`) only if it is dedicated to cachekit. The function recomputes and re-caches on the next call.

---

## Circuit Breaker Errors

### Circuit breaker open

**Message** (logged): `Circuit breaker ... transitioned to OPEN`

**Exception**: none: while the breaker is open, a function with an L2 backend still serves L1 hits, skips L2, and runs uncached on an L1 miss, sync or async. An L1 hit is never a probe and records no outcome, so it neither holds the breaker open nor closes it. In L1-only mode (`backend=None`) the breaker is never consulted.

**Cause**: Five failures within a 60-second rolling window (five is the default [`failure_threshold`](features/circuit-breaker.md)) — successes do not reset the count, and a failure older than 60 seconds stops counting. Backend read and write failures (connection errors, timeouts, CachekitIO HTTP errors) do not currently count: they are logged, a failed read is a miss, and a failed write skips only L2. With L1 enabled (the default) a buffered write still stores the result in L1, so later calls in the same process can hit it. A sync function using the plaintext Arrow serializer on the File backend streams its writes instead, and a streamed write never goes to L1, so after a failed one the next call recomputes. What counts is a failure to create the backend client and, for async functions only, a result too large to cache (over `max_value_size`) or a multi-tenant encrypted write whose tenant id cannot be extracted. An exception raised by the decorated function never counts, and neither does an `InteropError` for a return value the interop data model can't represent: both reach the caller unchanged, sync or async. A result that fails to serialize or encrypt for the cache write never counts, sync or async: the write is skipped with an ERROR and a WARNING log line, and the function's result is returned uncached. A keyring configuration fault (`KeyringConfigurationError`) never counts, whether the cache write raised it or the decorated function did (from a nested cached call, for example): it reaches the caller, sync or async. A cached entry that fails to deserialize, decrypt or pass its integrity check, or that an encrypting reader refuses, does not count under either policy (see *Deserialization failed* and *Decryption failed*). Under fail-open, the default, it is a miss: cachekit attempts to evict it (best effort: a failed delete is only logged, and the entry stays) and the function recomputes. With `fail_closed=True`, an authentication failure raises `DecryptionAuthenticationError` and keeps the entry as evidence; any other such failure is a miss, as under fail-open. Each decorated function has its own breaker, and that many counted failures open it even when the backend is healthy.

**What it means**:
- A failure listed under **Cause** has occurred `failure_threshold` times (five by default) within 60 seconds
- Calls to this function that miss L1 run uncached until the breaker recovers; L1 hits are still served. Once the cooldown has passed (`recovery_timeout`, 30 seconds after the breaker opened by default), the next call moves it to HALF_OPEN, which has three probe slots (`half_open_requests`); calls that find none free run uncached. Every admitted probe holds its slot until the cycle ends, cancelled ones included, except a probe whose function raises: it records no outcome and hands its slot to the next call, so while the function keeps raising, more than three calls in one cycle can reach the backend. Three successes (`success_threshold`) close it; a counted failure reopens it for another cooldown. See [What It Does](features/circuit-breaker.md#what-it-does)

**Solutions**:

1. **Check backend health**:
```bash
redis-cli ping
# Output: PONG means Redis is healthy
```

2. **Let the breaker recover** — no restart is needed:
- Once the cause is fixed, the probe calls after the cooldown close the breaker. While the cause persists, each failed probe reopens it for another cooldown

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

# When circuit breaker is open (sync and async functions):
# - A value already in L1 is still served: expensive_operation() does not run
# - On an L1 miss the function still executes: expensive_operation() runs
# - L2 is bypassed: the result is NOT cached
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

### Unsupported keyword argument

**Message**: `The <preset> preset does not accept ...`, `@cache does not accept ...` or `@cache(config=...) does not accept ...`, naming each keyword

**Exception**: `ConfigurationError`, raised when the decorator is applied or a `DecoratorConfig` preset is called, before `@cache.secure` or `@cache.io` looks for its key, so a misspelt keyword is reported even when no key is set. Earlier releases raised the dataclass's `TypeError`. `@cache.local`, which takes only the four [parameters](features/reference-caching.md#parameters) it lists, still raises `TypeError`.

**Cause**: a keyword that names no `DecoratorConfig` field, often a typo such as `tll=`, or one that only another form takes: `api_key=` outside `@cache.io`, or `master_key=`, `tenant_extractor=`, `single_tenant_mode=`, `deployment_uuid=` or `fail_closed=` on any preset but `secure`, or beside `config=`. Bare `@cache` folds those into its own `EncryptionConfig`, `secure` takes them as its own options, and a config already holds its own.

**Solution**: fix the spelling, or set the option where the form takes it, such as `@cache(config=DecoratorConfig.secure(master_key=secret_key, fail_closed=True))`.

---

### Keyword overrides a preset's encryption or backend

**Message**: one of
- `The secure preset sets its own encryption; encryption= cannot override it. ...`
- `encryption= cannot override an encrypted config= ...`
- `@cache.io does not accept backend= — it always caches through CachekitIOBackend. ...`
- `@cache(config=DecoratorConfig.io(...)) does not accept backend= ...`

**Exception**: `ConfigurationError`, raised when the decorator is applied or the preset is called

**Cause**: the keyword names what the preset fixes. The `secure` preset sets its own encryption, so `@cache.secure` and `DecoratorConfig.secure()` refuse `encryption=` (with `TypeError` before 0.23.0), and from 0.23.0 so does any encrypted `config=`: one from `DecoratorConfig.secure()`, or one built with `encryption=EncryptionConfig(enabled=True, ...)`. The `io` preset caches through the `CachekitIOBackend` it builds, so `backend=` is refused by `@cache.io` and, from 0.23.0, beside `config=DecoratorConfig.io(...)`. A `backend=` beside any other config still replaces that config's backend.

**Solution**: set encryption options where the config is built, `DecoratorConfig.secure(master_key=secret_key, fail_closed=True)`. To cache through another backend, use another preset: `@cache(config=DecoratorConfig.production(backend=my_backend))`.

---

## Lock Errors

### Lock acquisition timeout

**Message** (logged): `Failed to acquire lock for ... after 5.0s, checking cache`

**Exception**: none. On a cache miss, an async `@cache` function on CachekitIO, or Redis configured via `CACHEKIT_REDIS_URL`, waits about 5 seconds (plus request time) for the per-key lock. If another process still holds it, cachekit checks the cache once more, then runs the function itself.

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

These errors occur when using `@cache.io()` with the CachekitIO SaaS backend. None raises, for sync or async functions: each HTTP failure becomes a `BackendError` with a `BackendErrorType`, is logged as described under *Connection Errors*, and the function runs; its result is still stored in L1. One case is retried first: a write or delete (a `PUT` or `DELETE`, the lock release included) that the server answers `503` with a `Retry-After` of 2 seconds or less is sent once more, inline, after exactly that delay; see [Server error (5xx)](#server-error-5xx). Nothing else is retried. A failed read is treated as a miss, so the decorator still tries to write the function's result to the cache afterwards, and a failed write is logged the same way. These failures do not count toward the circuit breaker.

On a miss, an async function first requests the per-key lock (see [Distributed Locking](features/distributed-locking.md)). Any HTTP failure of that request ends the lock wait at once: the function runs without the lock, and cachekit logs `Lock operation failed … executing without lock`. Only a lock another caller holds is waited on.

### Authentication failure (401/403)

**Message**: `Authentication failed: HTTP 401` or `Authentication failed: HTTP 403`

**Exception**: none — logged as `BackendError` (`BackendErrorType.AUTHENTICATION`)

**Cause**:
- API key is wrong or revoked, for example a truncated or doubled paste or a stray `.` or `=`. A missing key, or one with a character outside the [bearer-token set](backends/cachekitio.md#convenience-shorthand-via-cacheio), never gets this far: it raises `ConfigurationError` when the backend is built
- API key does not have permission for the requested operation

**Behavior**: Alert ops: every L2 read and write fails until the key is fixed. The function runs and its result is still stored in L1.

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

**Behavior**: TRANSIENT — logged; the function runs and its result is still stored in L1.

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

**Behavior**: TRANSIENT — logged; the function runs and its result is still stored in L1.

**One retry for a shed write**: the server answers `503` with a `Retry-After` header when it sheds a request for a short, transient reason. When that answer is to a cache write or delete and `Retry-After` is a whole number of seconds no greater than 2, cachekit waits exactly that long and sends the request once more, inside the same call; only if the second attempt also fails is the error logged. A longer `Retry-After`, a missing or non-numeric one, any other 5xx, a read, the lock request and a TTL refresh are not retried. The wait adds at most 2 seconds to that call.

**What happens while server errors persist**:
```python notest
@cache.io(ttl=300)
def my_function():
    return expensive_operation()

# While server errors persist:
# - Function still executes: expensive_operation() runs
# - L2 read and write fail: the result is still stored in L1
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

**Behavior**: TIMEOUT — logged; the function runs and its result is still stored in L1.

**Solution**:
```bash
# Increase the request timeout (seconds)
export CACHEKIT_TIMEOUT=10.0
```

---

### Connection error

**Message**: `Connection failed: ...`

**Exception**: none — logged as `BackendError` (`BackendErrorType.TRANSIENT`)

**Cause**: Network-level failure — DNS resolution failed, connection refused, or network unreachable. `Connection failed: certificate verification failed against the system trust store (see SSL_CERT_FILE)` means the host has no CA bundle cachekit can use: install the system bundle (`ca-certificates`), or point `SSL_CERT_FILE` at one

**Behavior**: TRANSIENT — logged; the function runs and its result is still stored in L1.

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
| urllib3 `ConnectTimeoutError`, `ReadTimeoutError` | `TIMEOUT` |
| urllib3 `NewConnectionError`, `ProtocolError`, `SSLError`, `ProxyError` | `TRANSIENT` (a failed certificate verification says so: see `SSL_CERT_FILE` in [CachekitIO](backends/cachekitio.md#characteristics)) |
| Other | `UNKNOWN` |

No type counts toward the circuit breaker. Only a `503` to a write or delete with a `Retry-After` of 2 seconds or less is retried, once, for sync and async functions alike (see [Server error (5xx)](#server-error-5xx)). A failed lock request is not retried: it ends the lock wait, as the note at the top of this section says.

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
