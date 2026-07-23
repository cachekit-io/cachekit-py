**[Home](../README.md)** › **Features** › **Zero-Knowledge Encryption**

# Zero-Knowledge Encryption - Client-Side Security

**Available since v0.3.0**

## TL;DR

Zero-knowledge encryption (AES-256-GCM) encrypts cached data client-side. Redis never sees plaintext. Perfect for sensitive data (PII, credentials, health info).

```python notest
@cache.secure(ttl=300, master_key="a" * 64, backend=None)  # AES-256-GCM encryption
def get_user_ssn(user_id):
    return db.get_ssn(user_id)  # Encrypted in Redis, decrypted in-app (illustrative)
```

---

## Quick Start

Enable encryption with single decorator:

```python notest
from cachekit import cache

# Set master key (hex-encoded)
import os
os.environ["CACHEKIT_MASTER_KEY"] = "a" * 64  # 32 bytes

@cache.secure(ttl=300, master_key="a" * 64, backend=None)  # AES-256-GCM enabled
def get_sensitive_data(user_id):
    return db.query(SensitiveData).filter_by(id=user_id).first()  # illustrative - db not defined

data = get_sensitive_data(123)  # Encrypted in Redis
```

---

## What It Does

**Encryption pipeline** (works with ANY serializer):
```
Python object (plaintext)
    ↓
Serialize (MessagePack/JSON/Arrow - your choice)
    ↓
AES-256-GCM encryption
    ↓
Derive per-tenant key (optional)
    ↓
Storage backend (ciphertext only - Redis/HTTP/Custom)
    ↓
On cache hit:
    ↓
Decrypt with master key
    ↓
Deserialize (MessagePack/JSON/Arrow)
    ↓
Python object (plaintext, in-app only)
```

**Key insight**: Encryption is **orthogonal to serialization**. You can encrypt MessagePack, JSON (OrjsonSerializer), or DataFrames (ArrowSerializer) for true zero-knowledge caching of any data type.

**Security properties**:
- **AES-256-GCM**: Authenticated encryption, 256-bit key
- **Client-side**: Encryption happens in Python, before Redis
- **Master key**: CACHEKIT_MASTER_KEY environment variable
- **Per-tenant isolation**: Optional key derivation for multi-tenant
- **Nonce uniqueness**: Counter-based, prevents nonce reuse
- **Authentication**: GCM mode prevents tampering

---

## Why You'd Want It

**Compliance scenario**: Caching sensitive data (PII, health info, credentials).

**Regulations**:
- **GDPR**: Requires encryption of personal data in transit and at rest
- **HIPAA**: Requires encryption of health information
- **PCI-DSS**: Requires encryption of payment card data

**Benefits**:
```python
# Without encryption:
# Redis memory dump → attacker reads plaintext SSNs
# Redis backup → attacker reads plaintext emails
# Network intercept → attacker reads plaintext credentials

# With @cache.secure:
# Redis memory dump → attacker sees ciphertext only
# Redis backup → attacker sees ciphertext only
# Network intercept → attacker sees ciphertext only
# Encryption key in environment → separate from data
```

---

## Why You Might Not Want It

**Scenarios where encryption overhead matters**:

1. **No sensitive data**: Public caching (prices, menus)
2. **High-volume, low-margin**: Encryption adds 100-500μs
3. **Already encrypted at transport**: TLS + encryption is redundant

**Mitigation**: Use standard @cache for non-sensitive data:
```python notest
@cache(ttl=300, backend=None)  # No encryption, faster
def get_public_prices(item_id):
    return db.get_price(item_id)  # illustrative - db not defined

@cache.secure(ttl=300, master_key="a" * 64, backend=None)  # Encryption, slower, for sensitive data
def get_user_ssn(user_id):
    return db.get_ssn(user_id)  # illustrative - db not defined
```

---

## What Can Go Wrong

### Missing Master Key
> [!WARNING]
> `cache.secure` requires a master key. Omitting it raises a `ConfigurationError` at decoration time, not at call time.

```python notest
# Forget to set master_key parameter
@cache.secure(ttl=300)  # Missing master_key!
def operation(x):
    return sensitive_data(x)  # illustrative - sensitive_data not defined

# Error: "cache.secure requires master_key parameter or CACHEKIT_MASTER_KEY environment variable"
# Solution: Set CACHEKIT_MASTER_KEY env var, or pass master_key= explicitly
```

### Invalid Key Format
```bash
export CACHEKIT_MASTER_KEY="not_hex"  # Invalid
# Error: "CACHEKIT_MASTER_KEY must be hex-encoded, minimum 32 bytes"
# Solution: Use 64-char hex string
export CACHEKIT_MASTER_KEY=$(openssl rand -hex 32)
```

### Key Rotation

```bash
# Changed CACHEKIT_MASTER_KEY
# Old encrypted data in Redis → Can't decrypt
# Error: "Decryption failed: authentication tag verification failed"
# Solution: Clear cache before rotating keys
redis-cli FLUSHDB  # Clear Redis
export CACHEKIT_MASTER_KEY=new_key
# Restart app → re-populates cache with new key
```

### Enabling Encryption on an Existing (Plaintext) Cache

When you turn encryption on over a cache that already holds plaintext entries, those
entries are **rejected, never read**. The read path fails closed: the entry raises a
`SerializationError`, the caller treats it as a miss, evicts the stale entry, recomputes,
and re-stores the value encrypted. Migration is therefore lazy and self-healing:

```text
read plaintext entry → SerializationError (fail closed) → evict → recompute → re-store encrypted
```

There is deliberately **no opt-in flag** to let an encryption-enabled reader accept
plaintext entries. The frame header is not authenticated, so a plaintext entry forged by
an attacker with backend write access is indistinguishable from a legacy one — any
"accept plaintext" escape hatch would reintroduce the encryption-downgrade attack the
fail-closed read path exists to prevent. If you need to read plaintext entries, use a
handler with `encryption=False` (which never had keys to protect).

For large caches, choose between lazy migration and eager eviction based on your
workload: lazy migration spreads recomputation over reads (each legacy entry pays one
recompute on first access), while an eager flush concentrates it into a cold-start miss
wave — throttle or batch the eviction if the recompute cost is high. Either way, scope
eviction to cachekit's keys so unrelated data in the same Redis database survives:

```bash
# Evict only this namespace's cachekit entries (keys are prefixed ns:<namespace>:)
redis-cli --scan --pattern 'ns:<your-namespace>:*' | xargs -r redis-cli DEL

# FLUSHDB is only safe when the database is dedicated to cachekit
# then deploy with CACHEKIT_MASTER_KEY set
```

### L1 Cache Conflict
```python notest
@cache.secure(ttl=300, master_key="a" * 64, backend=None)  # Encryption + L1 cache (stores encrypted bytes)
def get_sensitive_data():
    # L1 cache enabled: stores encrypted bytes (~50ns hits vs 2-7ms Redis)
    # Encryption is orthogonal: wraps any serializer, applies to both L1 and L2
    # Both layers store encrypted bytes (encrypt-at-rest everywhere)
    return fetch_sensitive_data()  # illustrative - fetch_sensitive_data not defined
```

---

## How to Use It

### Basic Usage (Default: MessagePack)
```bash
# Generate secure master key
export CACHEKIT_MASTER_KEY=$(openssl rand -hex 32)
```

```python notest
from cachekit import cache

@cache.secure(ttl=3600, master_key="a" * 64, backend=None)  # AES-256-GCM with MessagePack
def get_user_profile(user_id):
    return db.get_profile(user_id)  # illustrative - db not defined

profile = get_user_profile(123)
# Data encrypted in Redis, decrypted in-app
```

### Encrypted JSON (Zero-Knowledge API Caching)
```python notest
from cachekit import cache
from cachekit.serializers import EncryptionWrapper, OrjsonSerializer

# Encrypt JSON API responses (webhooks, sessions, API keys)
@cache(serializer=EncryptionWrapper(serializer=OrjsonSerializer()), backend=None)
def get_api_keys(tenant_id: str):
    return {
        "api_key": "sk_live_abcdef123456",
        "webhook_secret": "whsec_xyz789",
        "tenant_id": tenant_id
    }

keys = get_api_keys("customer-123")
# JSON encrypted client-side, backend never sees plaintext (illustrative)
```

### Encrypted DataFrames (Zero-Knowledge ML Caching)
```python notest
from cachekit import cache
from cachekit.serializers import EncryptionWrapper, ArrowSerializer
import pandas as pd

# Encrypt DataFrames with patient data, ML features, analytics
@cache(serializer=EncryptionWrapper(serializer=ArrowSerializer()), backend=None)
def get_patient_records(hospital_id: int):
    # illustrative - conn not defined
    return pd.read_sql(
        "SELECT patient_id, diagnosis, risk_score FROM patients WHERE hospital_id = ?",
        conn,
        params=[hospital_id]
    )

df = get_patient_records(42)
# DataFrame encrypted client-side, HIPAA-compliant zero-knowledge storage
```

### Multi-Tenant Isolation
```python notest
from cachekit import cache
from contextvars import ContextVar

tenant_context = ContextVar("tenant_id")

@cache.secure(
    ttl=3600,
    master_key="a" * 64,
    tenant_extractor=lambda user_id: tenant_context.get(),
    backend=None
)
def get_user_data(user_id):
    tenant_id = tenant_context.get()
    return db.get_user_data(tenant_id, user_id)  # illustrative - db not defined

# Each tenant gets separate encryption key
# Tenant A can't decrypt Tenant B's data
tenant_context.set("tenant_1")
data_a = get_user_data(123)

tenant_context.set("tenant_2")
data_b = get_user_data(123)  # Same user_id, different tenant, different encryption
```

### Key Rotation Pattern
```python notest
# Gradual key rotation (for zero-downtime)
@cache.secure(ttl=3600, master_key="a" * 64, backend=None)
def get_data(x):
    return sensitive_data(x)  # illustrative - sensitive_data not defined

# 1. Add new key to CACHEKIT_MASTER_KEY_ROTATION
# 2. Old key still decrypts old data
# 3. New data encrypted with new key
# 4. Eventually old data expires from cache
# 5. Remove old key from rotation list
```

---

## Technical Deep Dive

### AES-256-GCM Details
```
Key size: 256 bits (32 bytes)
Nonce size: 96 bits (12 bytes, randomly generated)
Authentication: 128 bits (16 bytes, computed by GCM)

Encryption:
  Plaintext + Additional Authenticated Data (AAD) → Ciphertext + AuthTag
  AuthTag protects against tampering (any bit change fails)

Decryption:
  Ciphertext + AuthTag + AAD → Plaintext or ERROR
  If AuthTag doesn't match → raise error (don't return plaintext)
```

### Per-Tenant Key Derivation
```
Master key: CACHEKIT_MASTER_KEY
Tenant ID: tenant_context.get()

Per-tenant key = HKDF(master_key, tenant_id)
                 [Key Derivation Function, cryptographically secure]

Properties:
- Tenant A's key ≠ Tenant B's key
- Derived keys are unique per tenant
- Tenant A can't decrypt Tenant B's data
- Enables secure multi-tenant with single master key
```

### Nonce Generation (Uniqueness)
```
Problem: If same nonce used with same key, encryption breaks
Solution: Counter-based nonce generation

Nonce = [counter_high_64bits][counter_low_32bits][random_32bits]
        └─ Increments per encryption
           Prevents nonce reuse even across reboots
```

### Fail-Closed Read Path (Encryption Downgrade Protection)

The CK frame header — the JSON envelope carrying `encrypted`, `tenant_id`, `format`,
and the serializer name — is plaintext and is **not** covered by the AES-GCM
authentication tag. AAD v0x03 binds tenant, cache key, wire format, and compression
into the tag, but the header itself stays outside that boundary so a reader can parse
it before it has a key.

An attacker with backend write access (the threat actor in the protocol's threat
model) could exploit that gap by planting a frame whose header claims
`encrypted: false` plus an arbitrary plaintext payload — a classic encryption
downgrade (CWE-757). cachekit therefore never lets header metadata select the read
path when encryption is configured:

```text
Handler configured with encryption:
  entry header claims encrypted  → authenticated decrypt (AAD + GCM tag verified)
  entry header claims plaintext  → SerializationError (fail closed, entry evicted)
```

The plaintext deserializer is unreachable on an encryption-enabled handler, regardless
of what the stored frame claims. Configuration decides the read path; stored (i.e.
attacker-writable) data never does.

### Cleartext Frame Header Fields (Accepted Exposure)

Encrypted entries expose three fields in the plaintext header: `tenant_id`,
`encryption_algorithm`, and `key_fingerprint`. This exposure is deliberate and
accepted:

- **`tenant_id`** — required *before* decryption to derive the per-tenant key
  (HKDF); moving it inside the ciphertext is a chicken-and-egg problem. It is an
  opaque identifier, not secret material, and it *is* tamper-protected: AAD v0x03
  binds it into the GCM tag, so a modified header fails authentication.
- **`key_fingerprint`** — a one-way fingerprint of the derived key, used only for
  clearer diagnostics during key rotation. It reveals nothing about key material.
- **`encryption_algorithm`** — public information (`AES-256-GCM`); hiding the
  algorithm adds no security (Kerckhoffs's principle).

Relocating these fields would be a cross-SDK wire-format change owned by the
[protocol spec](https://github.com/cachekit-io/protocol); the Python SDK documents the
exposure rather than diverging from the shared frame format.

### Corruption vs Tamper: Telemetry and Fail-Closed Mode

Three failure classes surface on the decrypt read path, and cachekit distinguishes
them (cachekit-py#170):

- **`auth_tamper`** — cryptographic authentication failed: the ciphertext was modified,
  the key is wrong (rotation/misconfiguration), the AAD didn't match (ciphertext moved
  between cache keys), or the entry claims a different tenant. Raised as
  `DecryptionAuthenticationError`. This is the signal an active attack would produce.
- **`suspicious_envelope`** — the unauthenticated envelope is inconsistent with the
  handler's configuration: a plaintext claim under an encryption-enabled handler (the
  CWE-757 downgrade guard) or a missing `tenant_id`. Benign during a lazy
  plaintext→encrypted migration; a spike outside a migration window is suspect. Always
  fails open (miss + evict) so migration keeps working — even in fail-closed mode.
- **`corruption`** — everything else: checksum mismatch, truncated/malformed frame,
  serializer mismatch, or a deserialize failure on *already-authenticated* plaintext.
  Storage rot and bugs, not evidence of tampering.

All are counted on the Prometheus counter
`cachekit_decrypt_failures_total{reason, tier="l1"|"l2"}` — alert on
`reason="auth_tamper"` specifically; a nonzero rate there is a security event, not
noise. Baseline `suspicious_envelope` around migration windows.

**Default (fail open):** a decrypt failure of any class logs a warning, evicts the
poisoned entry, and recomputes the value. Availability-first — a tampered cache entry
degrades to a cache miss. The tampering is visible only in logs and the metric.

**Fail closed (opt-in):** `auth_tamper` failures raise
`DecryptionAuthenticationError` to *your* caller instead of silently recomputing, and
a key-fingerprint mismatch refuses to even attempt decryption. The poisoned **L2**
entry is deliberately **not** evicted (it is evidence); a poisoned L1 copy *is*
invalidated so remediating L2 immediately clears every process. Other classes still
fail open — only authentication failures escalate. Enable it fleet-wide or
per-function:

```bash
# Fleet-wide (all decorators, overridable per-function)
export CACHEKIT_ENCRYPTION_FAIL_CLOSED=1
```

```python notest
# Per-function (overrides the env setting in either direction)
@cache.secure(master_key="a" * 64, fail_closed=True)
def get_payment_token(user_id: int): ...

# Or via explicit EncryptionConfig
from cachekit.config.nested import EncryptionConfig
config = EncryptionConfig(enabled=True, master_key="a" * 64,
                          single_tenant_mode=True, fail_closed=True)
```

> **⚠️ Key rotation under fail-closed:** with `fail_closed` enabled there is no
> silent self-heal — rotating `CACHEKIT_MASTER_KEY` without clearing the cache makes
> **every** pre-rotation entry raise `DecryptionAuthenticationError` on read (the
> fingerprint mismatch refuses decryption, and the entry is retained, not evicted).
> Follow the documented rotation procedure: flush (or namespace-version) the cache
> *before* rotating. This is the deliberate cost of failing closed; the default
> fail-open mode self-heals rotations as ordinary misses.

Note the boundary with the integrity checksum: the ByteStorage **xxHash3-64 checksum
is corruption detection only** — it is not cryptographic and an attacker who can write
to the backend can trivially forge a valid checksum for arbitrary bytes. On the
plaintext `@cache` path the stored bytes are therefore attacker-forgeable; tamper
resistance exists **only** under encryption, where AES-256-GCM authenticates every
byte. `fail_closed` governs the authenticated path — it cannot add tamper resistance
to plaintext caching.

**Config-drift reads:** if a handler has encryption *disabled* but reads a stale
*encrypted* entry (e.g. encryption was recently turned off), cachekit decrypts it via
the globally configured master key — the same signature appears under
misconfiguration or a planted entry, so every occurrence increments
`cachekit_config_drift_reads_total{reason="encryption_disabled"}` and the first read
of each key logs a warning (once per key, so a hot key can't flood the logs). If you
didn't recently disable encryption for that function, investigate.

---

## Compliance Implications

### GDPR
- ✅ Encryption satisfies "processing security" requirement
- ✅ Client-side encryption satisfies "technical measures"
- ⚠️  Key management still required (rotation, access control)

### HIPAA
- ✅ AES-256-GCM satisfies encryption requirement
- ⚠️  Audit logging required (access to decrypted data)
- ⚠️  Key management plan required

### PCI-DSS
- ✅ Encryption satisfies "encryption at rest" requirement
- ⚠️  Key management plan required
- ⚠️  Regular key rotation required

> [!CAUTION]
> NOT legal advice. Consult your compliance team before making claims about regulatory compliance.

---

## Performance Impact

### Encryption Overhead (Measured)

**Evidence-based benchmarks** (P95 latency, roundtrip serialize + deserialize):

| Serializer | Plain | Encrypted | Overhead | Relative |
|------------|-------|-----------|----------|----------|
| **JSON** (OrjsonSerializer) | 0.75 μs | 4.25 μs | +3.50 μs | +467% |
| **MessagePack** (StandardSerializer) | 3.21 μs | 6.54 μs | +3.33 μs | +104% |
| **DataFrames** (ArrowSerializer, 1000 rows) | 731.67 μs | 749.75 μs | +18.08 μs | **+2.5%** |

**Key insights**:
1. **Small data** (JSON/MessagePack): Encryption adds 3-5 μs absolute
   - Relative overhead looks high because baseline is fast
   - Absolute cost <10 μs is negligible vs network latency (1-10 ms)

2. **Large data** (DataFrames): Encryption overhead **virtually disappears**
   - Serialization dominates (731 μs for 1000-row DataFrame)
   - Encryption only 18 μs = 2.5% overhead
   - **Zero-knowledge DataFrame caching is 97.5% free**

3. **Production implications**:
   - API caching: 5 μs encryption < network jitter
   - ML features: 2.5% overhead = rounding error
   - **Zero-knowledge caching is production-ready**

Run benchmarks: `pytest tests/performance/test_encryption_overhead.py -v -s`

### Key Derivation (Per-Tenant)
```
Per-tenant key derivation: 50-100μs (HKDF operation)
Cached after first use: No additional overhead
```

---

## Interaction with Other Features

**Encryption + Circuit Breaker**:
```python notest
@cache.secure(ttl=300, master_key="a" * 64, backend=None)  # Both enabled
def get_data():
    # Decryption error → Circuit breaker catches
    # Encryption happens before circuit breaker (at write time)
    return fetch_data()  # illustrative - fetch_data not defined
```

**Encryption + L1 Cache**:
```python notest
@cache.secure(ttl=300, master_key="a" * 64, backend=None)
def get_data():
    # L1 cache enabled: stores encrypted bytes (security + performance)
    # No plaintext in memory: encryption at rest in both L1 and L2
    # Decryption only at read time (< 1ms exposure)
    return fetch_data()  # illustrative - fetch_data not defined
```

---

## Troubleshooting

**Q: "Decryption failed: authentication tag verification failed"**
A: Key mismatch or data corruption. Check CACHEKIT_MASTER_KEY hasn't changed.

**Q: Key rotation failing**
A: Ensure CACHEKIT_MASTER_KEY_ROTATION is formatted correctly.

**Q: Performance degraded after enabling encryption**
A: Expected 100-500μs overhead. Profile to confirm acceptable.

---

## Zero-Knowledge Architecture

**Use case**: Building a caching system where the backend never sees user data.

### Client-Side Encryption Flow
```python notest
# Client application (user's infrastructure)
from cachekit import cache
from cachekit.serializers import EncryptionWrapper, OrjsonSerializer

# Configure for HTTP API backend
@cache(
    backend="https://cache.example.com/api",
    serializer=EncryptionWrapper(serializer=OrjsonSerializer())
)
def get_api_secrets(tenant_id: str):
    return {"api_key": "sk_live_...", "secret": "..."}  # illustrative

# Data flow:
# 1. Function executes (cache miss)
# 2. Serialize to JSON (OrjsonSerializer)
# 3. Encrypt with client's master key (AES-256-GCM)
# 4. Send encrypted blob to backend
# 5. Backend stores opaque ciphertext (zero knowledge)
# 6. Client retrieves and decrypts locally
```

### HTTP Backend Example (Zero-Knowledge Storage)
```typescript
// Example HTTP API backend
export default {
  async fetch(request: Request) {
    const { key, value } = await request.json();

    // Backend receives encrypted blob
    // NEVER sees plaintext (no decryption key)
    await KV.put(key, value);

    // Compliance: GDPR, HIPAA, PCI-DSS satisfied
    // Backend cannot read user data even if compromised
    return new Response("OK");
  }
}
```

**Benefits**:
- ✅ Backend compromise doesn't expose user data
- ✅ Multi-tenant isolation (per-tenant encryption keys)
- ✅ GDPR/HIPAA/PCI-DSS compliance out of the box
- ✅ Works with any data type (JSON, MessagePack, DataFrames)

---

## See Also

- [Comparison Guide](../comparison.md) - Only cachekit has zero-knowledge encryption
- [Security Policy](../../SECURITY.md)
- [Multi-Tenant Encryption](../getting-started.md#multi-tenant)
- [Serializer Guide](../serializers/README.md) - Encryption with custom serializers
- [Performance Benchmarks](../../tests/performance/test_encryption_overhead.py) - Evidence-based overhead measurements

---

<div align="center">

**[GitHub Issues](https://github.com/cachekit-io/cachekit-py/issues)** · **[Documentation](../README.md)**

</div>
