**[Home](../README.md)** › **Features** › **Zero-Knowledge Encryption**

# Zero-Knowledge Encryption - Client-Side Security

**Available since v0.3.0**

## TL;DR

Zero-knowledge encryption (AES-256-GCM) encrypts cached data client-side. The backend never sees plaintext values. Perfect for sensitive data (PII, credentials, health info).

```python notest
import os
from cachekit.backends.redis import RedisBackend

@cache.secure(ttl=300, master_key=os.environ["CACHEKIT_MASTER_KEY"], backend=RedisBackend("redis://localhost:6379"))
def get_user_ssn(user_id):
    return db.get_ssn(user_id)  # AES-256-GCM before it leaves the process
```

> [!WARNING]
> **Encryption requires a backend. `backend=None` does not encrypt anything.**
> `backend=None` selects L1-only mode, which stores **live Python object
> references** in process memory — it never serializes, so the encryption layer is
> never reached and the master key is accepted, validated, and then never used.
> `@cache.secure(master_key=..., backend=None)` raises nothing and encrypts nothing:
> the values stay readable in a heap or core dump. The same object is also handed to
> every caller, so mutating a returned value corrupts the cached entry for everyone
> else. Use L1-only mode for non-sensitive data; for encrypted caching pass a real
> backend (`RedisBackend`, `CachekitIOBackend`, …), where L1 then holds ciphertext
> like L2 does.

---

## Quick Start

Enable encryption with single decorator:

> **Configuration is read inline (`os.environ[...]`) in these examples to keep them short.** In an application, load and validate configuration once at startup, so a missing or malformed value fails there — with a clear error — rather than as a `KeyError`/`ValueError` raised at decoration time, when the module is imported (and, on the fail-open `@cache.io()` path, not at all).

```python notest
from cachekit import cache
from cachekit.backends.redis import RedisBackend

import os

# The key comes from the environment, never from source.
# Generate once and store it in your secret manager:
#   export CACHEKIT_MASTER_KEY=$(openssl rand -hex 32)

# A backend is required for encryption — see the warning above on backend=None
@cache.secure(ttl=300, master_key=os.environ["CACHEKIT_MASTER_KEY"], backend=RedisBackend("redis://localhost:6379"))
def get_sensitive_data(user_id):
    return db.query(SensitiveData).filter_by(id=user_id).first()  # illustrative - db not defined

data = get_sensitive_data(123)  # stored encrypted in both L1 and L2
```

---

## Which Path: `@cache.secure` vs `@cache.io` + `CACHEKIT_MASTER_KEY`

There are two real, shipped paths to encrypted caching on the cachekit.io SaaS. Both are
zero-knowledge on the wire **when a master key is present** — the difference is what
happens when it isn't, and which backend you actually reach. "Zero-knowledge" covers cached
**values**: the cache key and frame header stay cleartext on both paths (see
[Accepted Exposure](#cleartext-frame-header-fields-accepted-exposure)).

| | `@cache.secure(backend=CachekitIOBackend())` | `@cache.io()` + `CACHEKIT_MASTER_KEY` env |
|---|---|---|
| Encryption | Forced ON in code (`EncryptionConfig.enabled=True`) | Auto-detected from the env var (tri-state `enabled=None`) |
| **No master key present** | **Fails closed** — raises `ValueError` at decoration time (the `CACHEKIT_MASTER_KEY` fallback is read then, at import) | **Fails open** — silently caches plaintext to the SaaS. The key is read **at decoration time**: one loaded later (dotenv in `main()`, a startup vault hook) is never seen, and every call ships plaintext |
| Integrity checking | Forced `True` on the preset path; **not** re-forced when you pass `integrity_checking=` alongside `@cache(config=DecoratorConfig.secure(...))` | On by preset default |
| Backend | Pinned **only** by the explicit `backend=` shown — omit it and resolution falls to env auto-detect (footgun below) | `CachekitIOBackend` created by the preset — `backend=` raises, see note below; requires an API key (`api_key=` or `CACHEKIT_API_KEY`) at decoration time |
| Tenant mode | `single_tenant_mode` derived from `tenant_extractor`; per-tenant HKDF derivation exists but is **not a tenancy boundary** — see Multi-Tenant Isolation | **Forced single-tenant** — `tenant_extractor` is not accepted; every entry is encrypted under one single-tenant derived key (tenant `"default"` unless a deployment UUID is set), no per-tenant isolation |
| Backend SWR (`stale_ttl`) | Off unless requested | On by default (`stale_ttl` sized from `ttl`); the refresh re-runs the function **concurrently with the remainder of the request** (scheduled before the value is returned) — on a daemon thread for sync functions, as an `asyncio` task on the caller's loop for async ones — so it must not touch request-scoped **or non-thread-safe** resources. Arguments are deep-copied before scheduling, so a session passed *as an argument* is never shared — but a non-copyable argument silently skips the refresh entirely (logged at DEBUG) — on an instance method `self` is that argument, so a service class holding a lock, a client or an open connection disables SWR permanently and quietly. The sharing route that does bite is a `ContextVar`-bound session, which the context snapshot deliberately carries into the refresh. `stale_ttl=0` opts out |

**`@cache.io()` does not take a `backend=` argument.** The preset always
constructs its own `CachekitIOBackend`, so any `backend=` — `None` included —
raises `ConfigurationError` at decoration. To target any other backend, use a
different preset with an explicit `backend=`.

**Rule of thumb**: encryption as a **security requirement** → `@cache.secure` +
explicit backend. The intent is auditable in code. Encryption as a **fleet-wide
opt-in convenience** → set `CACHEKIT_MASTER_KEY` and let auto-detect do it (this
applies to every preset, not just `.io`). Compliance arguments belong on the
fail-closed path only, and even there they are scope-*reduction* arguments, not
guarantees — see [Compliance Implications](#compliance-implications) for the one
canonical statement.

> [!WARNING]
> **`@cache.secure` does NOT pin the SaaS backend.** Backend resolution is the
> same lookup as every preset: explicit `backend=` → `set_default_backend()` (read at
> decoration, and again at first call if still unset) → environment auto-detect at
> **first call**. Auto-detect
> picks whichever **single** prefixed selector is set (`CACHEKIT_API_KEY` → cachekit.io
> SaaS, `CACHEKIT_REDIS_URL` → Redis, `CACHEKIT_MEMCACHED_SERVERS`, `CACHEKIT_FILE_CACHE_DIR`)
> — two or more set at once raises `ConfigurationError`, there is no fallthrough between
> them; none set → `REDIS_URL` / localhost Redis. Only an
> explicit `backend=` is order-independent: the first call pins the backend, so a
> `set_default_backend()` that runs after it never re-points the function. Consequences:
> (1) in a 12-factor environment where `REDIS_URL` is set and `CACHEKIT_API_KEY`
> is not, `@cache.secure` **silently encrypts to Redis instead of the SaaS**;
> (2) for a provider-resolved decorator — `.secure` without `backend=`, `.production`,
> `.minimal`, bare `@cache`; **never `.io`**, which constructs its own backend at
> decoration and never consults the provider — a backend misconfiguration at first
> call (e.g. two auto-detect selectors set at once) is **swallowed** — the `ConfigurationError` is logged at WARNING as a
> `client_creation` failure and the function runs **uncached on every call**, with
> **L1 never populated** — so a cold cache stays cold.
> Alert on `client_creation_failed`. When the SaaS is the requirement, pass
> `backend=CachekitIOBackend()` explicitly — auditable in code and immune to
> environment drift.

```python notest
from cachekit import cache
from cachekit.backends.cachekitio import CachekitIOBackend

# Security requirement: fail-closed, auditable, explicitly targets the SaaS
@cache.secure(backend=CachekitIOBackend(), ttl=3600)
def get_patient_record(patient_id: str):
    return fetch_phi(patient_id)  # illustrative

# Fleet-wide convenience: encrypts iff CACHEKIT_MASTER_KEY is set,
# silently plaintext if it is not
@cache.io(ttl=300)
def get_dashboard_stats(org_id: str):
    return compute_stats(org_id)  # illustrative
```

> [!IMPORTANT]
> **Two separate fail-closed guarantees — don't conflate them.** `.secure` is
> fail-closed on a *missing key* (decoration-time `ValueError`). But `fail_closed`
> on a *decrypt failure* (e.g. an AES-GCM auth-tag mismatch at read time) is a
> separate tri-state setting that defers to `CACHEKIT_ENCRYPTION_FAIL_CLOSED`,
> which **defaults to `False`** — so even `.secure` fails *open* on tampered or
> key-mismatched entries (miss + recompute) unless you opt in. See
> [Corruption vs Tamper: Telemetry and Fail-Closed Mode](#corruption-vs-tamper-telemetry-and-fail-closed-mode).

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
- **Per-tenant key derivation**: Optional, and *not* a tenancy boundary on its own (see Multi-Tenant Isolation)
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
from cachekit.backends.redis import RedisBackend
import os
@cache(ttl=300, backend=None)  # No encryption, faster
def get_public_prices(item_id):
    return db.get_price(item_id)  # illustrative - db not defined

@cache.secure(ttl=300, master_key=os.environ["CACHEKIT_MASTER_KEY"], backend=RedisBackend("redis://localhost:6379"))  # Encryption, slower
def get_user_ssn(user_id):
    return db.get_ssn(user_id)  # illustrative - db not defined
```

---

## What Can Go Wrong

### Missing Master Key
> [!WARNING]
> `cache.secure` requires a master key. Omitting it raises a `ValueError` at decoration time, not at call time — this is the fail-closed guarantee that distinguishes `.secure` from env-var auto-detection (see [Which Path](#which-path-cachesecure-vs-cacheio--cachekit_master_key) above).

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
# Changed CACHEKIT_MASTER_KEY without retaining the old key
# Old encrypted data in Redis → Can't decrypt
# Error: "Decryption failed: authentication tag verification failed"
# Solution: keep the retiring key decrypt-only for the rotation window
export CACHEKIT_MASTER_KEY=new_key                 # encrypts + decrypts
export CACHEKIT_PREVIOUS_MASTER_KEYS=old_key       # decrypt-only (comma-separated, max 3)
# Restart app → old entries stay readable, new writes use the new key.
# Old-key entries age out via TTL; drop the old key from the list once the
# window (≥ longest TTL in use) has passed. Rotation is forward-only: never
# re-promote a retired key to CACHEKIT_MASTER_KEY — a configuration where the
# current key also appears in the previous-keys list is rejected at load.
```

### Enabling Encryption on an Existing (Plaintext) Cache

When you turn encryption on over a cache that already holds plaintext entries, those
entries are **rejected, never read**: the entry raises a `SerializationError`
internally, the caller treats it as a miss, evicts the stale entry, recomputes,
and re-stores the value encrypted. (This rejection is unconditional — it is not
governed by the `fail_closed` setting, which applies only to authenticated-decrypt
failures.) Migration is therefore lazy and self-healing:

```text
read plaintext entry → SerializationError (rejected, never deserialized) → evict → recompute → re-store encrypted
```

There is deliberately **no opt-in flag** to let an encryption-enabled reader accept
plaintext entries. The frame header is not authenticated, so a plaintext entry forged by
an attacker with backend write access is indistinguishable from a legacy one — any
"accept plaintext" escape hatch would reintroduce the encryption-downgrade attack the
downgrade-protected read path exists to prevent. If you need to read plaintext entries, use a
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
from cachekit.backends.redis import RedisBackend
import os
@cache.secure(ttl=300, master_key=os.environ["CACHEKIT_MASTER_KEY"], backend=RedisBackend("redis://localhost:6379"))
def get_sensitive_data():
    # WITH a backend, L1 stores encrypted bytes (~50ns hits vs 2-7ms Redis)
    # Encryption is orthogonal: wraps any serializer, applies to both L1 and L2
    # Both layers store encrypted bytes (encrypt-at-rest everywhere)
    # With backend=None instead, NONE of the above holds — see the warning at the top
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
import os
from cachekit import cache
from cachekit.backends.redis import RedisBackend

@cache.secure(ttl=3600, master_key=os.environ["CACHEKIT_MASTER_KEY"], backend=RedisBackend("redis://localhost:6379"))
def get_user_profile(user_id):
    return db.get_profile(user_id)  # illustrative - db not defined

profile = get_user_profile(123)
# stored encrypted, decrypted in-app
```

### Encrypted JSON (Zero-Knowledge API Caching)
```python notest
import os

from cachekit import cache
from cachekit.backends.redis import RedisBackend
from cachekit.serializers import EncryptionWrapper, OrjsonSerializer

# Encrypt JSON API responses (webhooks, sessions, API keys).
# A real backend is required — backend=None never serializes, so it never encrypts.
@cache(
    serializer=EncryptionWrapper(serializer=OrjsonSerializer()),
    backend=RedisBackend(os.environ["REDIS_URL"]),
)
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
import os

import pandas as pd

from cachekit import cache
from cachekit.backends.redis import RedisBackend
from cachekit.serializers import EncryptionWrapper, ArrowSerializer

# Encrypt DataFrames with patient data, ML features, analytics.
# A real backend is required — backend=None never serializes, so it never encrypts.
@cache(
    serializer=EncryptionWrapper(serializer=ArrowSerializer()),
    backend=RedisBackend(os.environ["REDIS_URL"]),
)
def get_patient_records(hospital_id: int):
    # illustrative - conn not defined
    return pd.read_sql(
        "SELECT patient_id, diagnosis, risk_score FROM patients WHERE hospital_id = ?",
        conn,
        params=[hospital_id]
    )

df = get_patient_records(42)
# DataFrame encrypted client-side — zero-knowledge storage
```

### Multi-Tenant Isolation

> [!CAUTION]
> **The per-tenant example previously shown here did not isolate tenants, and has
> been removed rather than corrected.** Run against this version, the documented
> form returned tenant A's cached value to tenant B. Two causes, and the first is
> enough on its own:
>
> - it passed `backend=None`, so nothing was serialized, no key was derived and no
>   encryption ran at all (see the warning at the top of this page); and
> - the cache key carries **no tenant component** — the key is
>   `ns:{ns}:func:{mod.fn}:args:{hash}:{flags}` — so both tenants address the same
>   entry, and separation depends entirely on the decrypt step failing.
>
> Supplying a real backend is **not** by itself a sufficient correction: with a
> backend and the supported `ContextVarExtractor`, the same call still returned the
> first tenant's value in our check. Until that is root-caused, this page will not
> show a pattern it cannot demonstrate. `tenant_extractor` also requires an object
> implementing `.extract(args, kwargs)` — a bare `lambda` raises `AttributeError` —
> and tenant ids must be valid UUIDs.
>
> **Do not rely on `tenant_extractor` as a tenancy boundary.** Give each tenant its
> own `namespace`, or its own deployment, and treat per-tenant key derivation as
> defence in depth rather than the control that separates them.

### Key Rotation Pattern

Zero-downtime rotation via the keyring: one **current** master key
(`CACHEKIT_MASTER_KEY`, encrypts and decrypts) plus up to **3 decrypt-only**
previous keys (`CACHEKIT_PREVIOUS_MASTER_KEYS`, comma-separated hex, same
per-key requirements as the master key). Entries carry the fingerprint of
their HKDF-derived per-tenant encryption key, so reads select the exact
keyring entry that wrote them — never trial decryption.

```bash
# 1. Promote the new key; retain the old key decrypt-only
export CACHEKIT_MASTER_KEY=<new-key-hex>
export CACHEKIT_PREVIOUS_MASTER_KEYS=<old-key-hex>
# 2. Old entries still decrypt (selected by key fingerprint); new writes use the new key
# 3. Old-key entries age out via TTL (or re-encrypt on the next write)
# 4. After the window (≥ longest TTL in use), drop the old key
unset CACHEKIT_PREVIOUS_MASTER_KEYS
```

Rules enforced at config load — rejected, never truncated or silently fixed:

- **Cap**: at most 3 decrypt-only keys.
- **Per-key validation**: identical to `CACHEKIT_MASTER_KEY` (hex-encoded, ≥32 bytes).
- **Forward-only**: the current master key must not re-appear in the
  decrypt-only list. A key that has ever encrypted is never re-promoted —
  that would resume a used AES-GCM nonce budget and risk catastrophic nonce
  reuse. Backing out a rotation means rotating *forward* to a fresh key.

An empty decrypt-only list is legal — that is the hard cut-over used for
compromise response (old entries become unreadable immediately).

[Interop-mode](../../README.md) entries store no per-entry key fingerprint
(no CK frame), so rotation there attempts keyring keys sequentially — current
key first, identical AAD per attempt — instead of fingerprint selection. Same
environment variables, same rotation window, same fail policy on exhaustion.

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

Single-tenant mode (no tenant_extractor):
  tenant_id = deployment_uuid | CACHEKIT_DEPLOYMENT_UUID | "default"
  "default" is the protocol literal every SDK derives from (intent-presets.md
  § Master Key Input, rule 5), used identically for HKDF and AAD — one master
  key is enough for py, rs and ts to share ciphertext.

Properties of the derivation itself:
- Tenant A's key ≠ Tenant B's key
- Derived keys are unique per tenant

What that does NOT give you: the cache key carries no tenant component, so both
tenants address the same entry and separation rests entirely on the decrypt step
rejecting the other tenant's ciphertext. That is a fail-closed behaviour, not an
isolation boundary, and it does not hold at all when nothing is encrypted
(backend=None). See Multi-Tenant Isolation above before relying on this.
```

### Nonce Generation (Uniqueness)
```
Problem: If same nonce used with same key, encryption breaks
Solution: Counter-based nonce generation

Nonce = [counter_high_64bits][counter_low_32bits][random_32bits]
        └─ Increments per encryption
           Prevents nonce reuse even across reboots
```

### Encryption Downgrade Protection (Read Path)

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
  entry header claims plaintext  → SerializationError (plaintext never returned; miss + evict, independent of `fail_closed`)
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

Beyond the frame header, the **cache key itself is cleartext** — on the CachekitIO backend
it travels percent-encoded in the URL path (`/v1/cache/{key}`). The key carries the
namespace and the function's `module.qualname` plus an unkeyed, unsalted blake2b-256 of
the arguments (`ns:{ns}:func:{mod.fn}:args:{64-hex}:{flags}`), so over a small or known
argument space the hash is offline-enumerable: a backend operator can learn *which* record
was accessed, when, and how often, without decrypting anything. Because the key
travels in the URL path it also lands in every access log on the request path — load
balancer, CDN, TLS terminator — and persists for those retention windows, long after
the cache TTL; ciphertext length leaks approximate plaintext size too. Encryption protects
values, not access patterns — keep secrets out of namespaces and function names, and
count argument-identifiable access as metadata exposure in your threat model.

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
import os
# Per-function (overrides the env setting in either direction)
@cache.secure(master_key=os.environ["CACHEKIT_MASTER_KEY"], fail_closed=True)
def get_payment_token(user_id: int): ...

# Or via explicit EncryptionConfig
from cachekit.config.nested import EncryptionConfig
config = EncryptionConfig(enabled=True, master_key=os.environ["CACHEKIT_MASTER_KEY"],
                          single_tenant_mode=True, fail_closed=True)
```

> **⚠️ Key rotation under fail-closed:** with `fail_closed` enabled there is no
> silent self-heal — rotating `CACHEKIT_MASTER_KEY` **without retaining the old key
> in `CACHEKIT_PREVIOUS_MASTER_KEYS`** makes every pre-rotation entry raise
> `DecryptionAuthenticationError` on read (the fingerprint matches no keyring entry,
> decryption is refused, and the entry is retained, not evicted). Follow the keyring
> rotation pattern above: keep the retiring key decrypt-only for the full window.
> This is the deliberate cost of failing closed; the default fail-open mode treats
> keyless entries as ordinary misses.

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

> [!IMPORTANT]
> The arguments below hold only on the **fail-closed path** (`@cache.secure` + explicit
> backend). On the env auto-detect path one missing `CACHEKIT_MASTER_KEY` silently puts
> plaintext on the backend and none of these checkmarks apply. Even fail-closed,
> client-side encryption may *reduce* HIPAA/PCI DSS scope subject to assessment and your
> surrounding controls — it does not remove regulated data from scope on its own. See
> [Which Path](#which-path-cachesecure-vs-cacheio--cachekit_master_key).
>
> ⚠️ Encryption covers cached **values** only. The cache key travels cleartext in the
> request URL and lands in backend access logs — and it carries the function's
> `module.qualname` plus an enumerable hash of its arguments, so a key like
> `get_patient_record(patient_id)` leaks who was looked up and when, on the log's
> retention window rather than the cache TTL. Assess that alongside the ciphertext.
> See [Accepted Exposure](#cleartext-frame-header-fields-accepted-exposure).

### GDPR
- ✅ Encryption supports the "processing security" requirement
- ✅ Client-side encryption supports the "technical measures" requirement
- ⚠️  Key management still required (rotation, access control)

### HIPAA
- ✅ AES-256-GCM supports the encryption requirement
- ⚠️  Audit logging required (access to decrypted data)
- ⚠️  Key management plan required

### PCI-DSS
- ✅ Encryption supports the "encryption at rest" requirement
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
   - **Zero-knowledge caching overhead is negligible in the benchmarked scenarios** (synthetic API payloads, user profiles, and 1,000-row DataFrames)

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
from cachekit.backends.redis import RedisBackend
import os
@cache.secure(ttl=300, master_key=os.environ["CACHEKIT_MASTER_KEY"], backend=RedisBackend("redis://localhost:6379"))  # Both enabled
def get_data():
    # Decryption error → Circuit breaker catches
    # Encryption happens before circuit breaker (at write time)
    return fetch_data()  # illustrative - fetch_data not defined
```

**Encryption + L1 Cache**:
```python notest
from cachekit.backends.redis import RedisBackend
import os
@cache.secure(ttl=300, master_key=os.environ["CACHEKIT_MASTER_KEY"], backend=RedisBackend("redis://localhost:6379"))
def get_data():
    # L1 cache enabled: stores encrypted bytes (security + performance)
    # No plaintext at rest in L1 or L2 — decryption only at read time (< 1ms exposure).
    # This holds only because a backend is configured; backend=None stores raw objects.
    return fetch_data()  # illustrative - fetch_data not defined
```

---

## Troubleshooting

**Q: "Decryption failed: authentication tag verification failed"**
A: Key mismatch or data corruption. Check CACHEKIT_MASTER_KEY hasn't changed.

**Q: Key rotation failing**
A: Check `CACHEKIT_PREVIOUS_MASTER_KEYS` — comma-separated hex, each key subject to
the same rules as `CACHEKIT_MASTER_KEY` (≥32 bytes), at most 3 entries, and the
current `CACHEKIT_MASTER_KEY` must **not** appear in the list. Follow the keyring
rotation pattern above: keep the retiring key decrypt-only for the full rotation
window before dropping it.

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

    // Backend cannot read user data even if compromised
    return new Response("OK");
  }
}
```

**Benefits**:
- ✅ Backend compromise doesn't expose user data
- ✅ Per-tenant key derivation with fail-closed extraction — a tenant's ciphertext is
  not readable under another tenant's key, and a failed extraction raises rather than
  falling back to a shared key. This is **not** a tenancy boundary on its own: cache
  keys carry no tenant component (see [Multi-Tenant Isolation](#multi-tenant-isolation))
- ✅ Supports GDPR/HIPAA/PCI-DSS arguments on the fail-closed path (`@cache.secure` + explicit backend — see [Compliance Implications](#compliance-implications))
- ✅ Works with any data type (JSON, MessagePack, DataFrames)

---

## See Also

- [Comparison Guide](../comparison.md) - Only cachekit has zero-knowledge encryption
- [Security Policy](../../SECURITY.md)
- [Multi-Tenant Isolation](#multi-tenant-isolation) - why per-tenant keys are not a tenancy boundary
- [Serializer Guide](../serializers/README.md) - Encryption with custom serializers
- [Performance Benchmarks](../../tests/performance/test_encryption_overhead.py) - Evidence-based overhead measurements

---

<div align="center">

**[GitHub Issues](https://github.com/cachekit-io/cachekit-py/issues)** · **[Documentation](../README.md)**

</div>
