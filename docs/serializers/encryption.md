**[Home](../README.md)** › **[Serializers](index.md)** › **Encryption Wrapper**

# Encryption Wrapper

**EncryptionWrapper** adds client-side AES-256-GCM encryption to **any** serializer. It is a composable wrapper — it serializes data using an inner serializer, then encrypts the result before storage.

For comprehensive documentation of cachekit's zero-knowledge encryption architecture, key management, per-tenant isolation, nonce handling, and authentication guarantees, see [Zero-Knowledge Encryption Guide](../features/zero-knowledge-encryption.md).

## Overview

EncryptionWrapper wraps any other serializer:

```
serialize(data) → inner.serialize(data) → encrypt(bytes) → stored bytes
retrieve(bytes) → decrypt(bytes) → inner.deserialize(bytes) → data
```

The backend stores opaque ciphertext only. The master key never leaves the client.

## Basic Usage

```python
from cachekit import cache
from cachekit.serializers import EncryptionWrapper, OrjsonSerializer

# Encrypted JSON (API responses, webhooks, session data)
# Note: EncryptionWrapper requires CACHEKIT_MASTER_KEY env var or master_key param
@cache(serializer=EncryptionWrapper(serializer=OrjsonSerializer(), master_key=bytes.fromhex("a" * 64)), backend=None)
def get_api_keys(tenant_id: str):
    return {
        "api_key": "sk_live_...",
        "webhook_secret": "whsec_...",
        "tenant_id": tenant_id
    }

# Encrypted MessagePack (default - use @cache.secure preset)
@cache.secure(master_key=bytes.fromhex("a" * 64), backend=None)
def get_user_ssn(user_id: int):
    return {"ssn": "123-45-6789", "dob": "1990-01-01"}
```

Encryption works with any serializer — including DataFrames:

```python notest
from cachekit import cache
from cachekit.serializers import EncryptionWrapper, ArrowSerializer

# Encrypted DataFrames (patient data, ML features)
@cache(serializer=EncryptionWrapper(serializer=ArrowSerializer(), master_key=bytes.fromhex("a" * 64)), backend=None)
def get_patient_records(hospital_id: int):
    return pd.read_sql("SELECT * FROM patients WHERE hospital_id = ?", conn, params=[hospital_id])
```

## Composability

EncryptionWrapper works with **any** serializer:

| Inner Serializer | Use Case |
|-----------------|---------|
| DefaultSerializer (default) | Encrypted general-purpose objects |
| OrjsonSerializer | Encrypted API responses, JSON data |
| ArrowSerializer | Encrypted DataFrames (patient data, ML features) |
| Custom serializers | Any data type with encryption |

The `@cache.secure` preset uses EncryptionWrapper with DefaultSerializer automatically.

## Zero-Knowledge Caching

```python notest
from cachekit import cache
from cachekit.serializers import EncryptionWrapper, OrjsonSerializer

# Client-side: Encrypt before sending to remote backend
@cache(
    backend="https://cache.example.com/api",
    serializer=EncryptionWrapper(serializer=OrjsonSerializer(), master_key=bytes.fromhex("a" * 64))
)
def get_secrets(tenant_id: str):
    return {"api_key": "sk_live_...", "secret": "..."}

# Backend receives encrypted blob, never sees plaintext
# GDPR/HIPAA/PCI-DSS compliant out of the box
```

When using `EncryptionWrapper` with a remote backend (e.g., cachekit.io), the SaaS backend stores only opaque ciphertext. It has no access to keys and cannot decrypt data. This makes the backend out-of-scope for HIPAA/PCI-DSS compliance requirements.

## Performance

Encryption adds minimal overhead:

- Small data (< 1KB): **3-5 μs overhead** — negligible vs network latency
- Large DataFrames: **~2.5% overhead**

> [!TIP]
> For detailed encryption performance measurements including overhead vs data size, see [Zero-Knowledge Encryption: Performance Impact](../features/zero-knowledge-encryption.md#performance-impact).

---

## See Also

- [Zero-Knowledge Encryption Guide](../features/zero-knowledge-encryption.md) — Full encryption docs: key management, per-tenant isolation, nonce handling, compliance
- [DefaultSerializer](default.md) — General-purpose inner serializer
- [OrjsonSerializer](orjson.md) — JSON inner serializer
- [ArrowSerializer](arrow.md) — DataFrame inner serializer
- [Configuration Guide](../configuration.md) — CACHEKIT_MASTER_KEY setup

---

<div align="center">

**[GitHub Issues](https://github.com/cachekit-io/cachekit-py/issues)** · **[Documentation](../README.md)**

</div>
