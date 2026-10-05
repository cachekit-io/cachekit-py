**[Home](../README.md)** › **[Serializers](README.md)** › **Encryption Wrapper**

# Encryption Wrapper

**EncryptionWrapper** adds client-side AES-256-GCM encryption to any serializer whose class declares `cross_sdk_compatible = True` ([Composability](#composability)). It is a composable wrapper — it serializes data using an inner serializer, then encrypts the result before storage.

For comprehensive documentation of cachekit's zero-knowledge encryption architecture, key management, multi-tenant limits, nonce handling, and authentication guarantees, see [Zero-Knowledge Encryption Guide](../features/zero-knowledge-encryption.md).

## Overview

EncryptionWrapper wraps a cross-SDK serializer:

```
serialize(data) → inner.serialize(data) → encrypt(bytes) → stored bytes
retrieve(bytes) → decrypt(bytes) → inner.deserialize(bytes) → data
```

The backend stores opaque ciphertext values only; the cache key stays cleartext (see [Cleartext Cache Key](../features/zero-knowledge-encryption.md#cleartext-cache-key-accepted-exposure)). The master key never leaves the client.

## Basic Usage

On a decorated function, `@cache.secure` applies `EncryptionWrapper` for you: pass the inner
serializer as `serializer=`. Passing an `EncryptionWrapper` instance, or the `"encrypted"` serializer
name, to `@cache(serializer=...)` or to any preset that takes `serializer=` raises `ConfigurationError`
when the decorator is applied: the decorator never gives it the cache key each ciphertext is bound to,
so it could not store an entry. With `backend=None` you get a different error instead
([details](../error-codes.md#encrypting-serializer-on-a-decorator)). `EncryptionWrapper` stays
available for direct use outside a decorator, with the same serializer rule as the decorator
([Composability](#composability)).

```python fixture:master_key_env
import os
import tempfile

from cachekit import cache
from cachekit.backends.file import FileBackend, FileBackendConfig
from cachekit.serializers import OrjsonSerializer

# 64 hex chars from your secret store, e.g. generated once with: openssl rand -hex 32
secret_key = os.environ["CACHEKIT_MASTER_KEY"]

# A file backend keeps this example self-contained; production uses Redis or cachekit.io.
# Encryption needs a backend: backend=None (L1-only) stores raw objects and is refused.
backend = FileBackend(FileBackendConfig(cache_dir=tempfile.mkdtemp()))

calls = 0

# Encrypted JSON (API responses, webhooks, session data)
@cache.secure(master_key=secret_key, serializer=OrjsonSerializer(), backend=backend)
def get_api_keys(tenant_id: str):
    global calls
    calls += 1
    return {
        "api_key": "sk_live_...",
        "webhook_secret": "whsec_...",
        "tenant_id": tenant_id
    }

get_api_keys("acme")
get_api_keys("acme")  # second call is served from the encrypted cache
assert calls == 1

# Encrypted MessagePack (the @cache.secure default serializer)
@cache.secure(master_key=secret_key, backend=backend)
def get_user_ssn(user_id: int):
    return {"ssn": "123-45-6789", "dob": "1990-01-01"}
```

Encryption works with any cross-SDK serializer — including DataFrames:

```python notest
from cachekit import cache
from cachekit.serializers import ArrowSerializer

# Encrypted DataFrames (patient data, ML features)
@cache.secure(master_key=secret_key, serializer=ArrowSerializer())
def get_patient_records(hospital_id: int):
    return pd.read_sql("SELECT * FROM patients WHERE hospital_id = ?", conn, params=[hospital_id])
```

## Composability

EncryptionWrapper works with any serializer whose class declares `cross_sdk_compatible = True`:

| Inner Serializer | Use Case |
|-----------------|---------|
| StandardSerializer (default) | Encrypted cross-language MessagePack data |
| OrjsonSerializer | Encrypted API responses, JSON data |
| ArrowSerializer | Encrypted DataFrames (patient data, ML features) |
| [Custom serializers](custom.md#under-encryption) that set `cross_sdk_compatible = True` | Any data type with encryption |

Any other serializer, `AutoSerializer` included, raises `ConfigurationError` when the wrapper is built,
as the decorators already do ([details](../error-codes.md#single-sdk-serializer-under-encryption)).

```python
from cachekit.config import ConfigurationError
from cachekit.serializers import AutoSerializer, EncryptionWrapper, StandardSerializer

key = bytes(range(32))  # 32 bytes from your secret store

try:
    EncryptionWrapper(serializer=AutoSerializer(), master_key=key, previous_master_keys=[])
except ConfigurationError:
    pass  # AutoSerializer sniffs the decrypted bytes, so it is refused
else:
    raise AssertionError("AutoSerializer must be refused under encryption")

wrapper = EncryptionWrapper(serializer=StandardSerializer(), master_key=key, previous_master_keys=[])
data, meta = wrapper.serialize({"ssn": "123-45-6789"}, cache_key="users:1:ssn")
assert wrapper.deserialize(data, meta, cache_key="users:1:ssn") == {"ssn": "123-45-6789"}
```

EncryptionWrapper defaults to StandardSerializer, which uses MessagePack for cross-language compatibility. The `@cache.secure` preset uses this default.

## Zero-Knowledge Caching

```python notest
# notest: CachekitIOBackend needs the network and CACHEKIT_API_KEY
from cachekit import cache
from cachekit.backends.cachekitio import CachekitIOBackend
from cachekit.serializers import OrjsonSerializer

# Client-side: encrypted before it is sent to the remote backend
@cache.secure(master_key=secret_key, serializer=OrjsonSerializer(), backend=CachekitIOBackend())
def get_secrets(tenant_id: str):
    return {"api_key": "sk_live_...", "secret": "..."}

# Backend receives encrypted blob, never sees plaintext values
```

With a remote backend such as cachekit.io, the backend stores only opaque ciphertext. It has no access to keys and cannot decrypt values. That supports a HIPAA/PCI DSS scope-*reduction* argument, subject to assessment and your other controls. It does not take regulated data out of scope on its own, and the cache key still travels in cleartext (see [Compliance Implications](../features/zero-knowledge-encryption.md#compliance-implications)).

## Performance

Encryption adds minimal overhead:

- Small data (< 1KB): **3-5 μs overhead** — negligible vs network latency
- Large DataFrames: **~2.5% overhead**

> [!TIP]
> For detailed encryption performance measurements including overhead vs data size, see [Zero-Knowledge Encryption: Performance Impact](../features/zero-knowledge-encryption.md#performance-impact).

---

## See Also

- [Zero-Knowledge Encryption Guide](../features/zero-knowledge-encryption.md) — Full encryption docs: key management, multi-tenant limits, nonce handling, compliance
- [StandardSerializer](default.md) — General-purpose inner serializer
- [OrjsonSerializer](orjson.md) — JSON inner serializer
- [ArrowSerializer](arrow.md) — DataFrame inner serializer
- [Configuration Guide](../configuration.md) — CACHEKIT_MASTER_KEY setup

---

<div align="center">

**[GitHub Issues](https://github.com/cachekit-io/cachekit-py/issues)** · **[Documentation](../README.md)**

</div>
