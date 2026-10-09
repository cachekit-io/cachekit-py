**[Home](../README.md)** › **Features** › **Zero-Knowledge Encryption**

# Zero-Knowledge Encryption - Client-Side Security

**Available since v0.3.0**

## TL;DR

Zero-knowledge encryption (AES-256-GCM) encrypts cached data client-side. The backend never sees plaintext values. Perfect for sensitive data (PII, credentials, health info).

```python notest
@cache.secure(ttl=300, master_key=secret_key)  # AES-256-GCM encryption
def get_user_ssn(user_id):
    return db.get_ssn(user_id)  # Encrypted in Redis, decrypted in-app (illustrative)
```

---

## Quick Start

Enable encryption with single decorator:

```python notest
from cachekit import cache

# Key comes from CACHEKIT_MASTER_KEY (64 hex chars) when master_key is omitted.
@cache.secure(ttl=300)  # AES-256-GCM enabled
def get_sensitive_data(user_id):
    return db.query(SensitiveData).filter_by(id=user_id).first()  # illustrative - db not defined

data = get_sensitive_data(123)  # Encrypted in Redis
```

Later examples pass the key explicitly as `master_key=secret_key`: your 64-hex-char key string,
loaded from a secret store. Never a literal in source. A bytes key raises `TypeError`: pass
`key.hex()` ([details](../error-codes.md#bytes-key-where-a-hex-string-is-taken)).

> **`@cache.secure` needs a backend.** `backend=None` (L1-only) stores raw Python objects,
> which cannot be ciphertext, so the combination is refused at decoration time with a
> `ConfigurationError`. With a backend configured, L1 *does* stay on — it holds the same
> ciphertext L2 does.

---

## What It Does

**Encryption pipeline** (works with any allowed serializer, see [Serializer requirement](#serializer-requirement)):
```
Python object (plaintext)
    ↓
Serialize (MessagePack/JSON/Arrow - your choice)
    ↓
AES-256-GCM encryption
    ↓
Derive per-tenant key (optional)
    ↓
Storage backend (ciphertext values; the key stays cleartext - Redis/HTTP/Custom)
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
# Redis memory dump → attacker sees ciphertext values
# Redis backup → attacker sees ciphertext values
# Network intercept → attacker sees ciphertext values
# (cache keys stay cleartext in all three)
# Encryption key in environment → separate from data
```

---

## Why You Might Not Want It

**Scenarios where encryption overhead matters**:

1. **No sensitive data**: Public caching (prices, menus)
2. **High-volume, low-margin**: every cache read and write pays a decrypt or encrypt step (see [Performance Impact](#performance-impact))
3. **Already encrypted at transport**: TLS + encryption is redundant

**Mitigation**: state `encryption=False` for non-sensitive data:

```python notest
@cache(ttl=300, encryption=False, backend=None)  # Explicit plaintext, faster
def get_public_prices(item_id):
    return db.get_price(item_id)  # illustrative - db not defined

@cache.secure(ttl=300, master_key=secret_key)  # Encryption, slower, for sensitive data
def get_user_ssn(user_id):
    return db.get_ssn(user_id)  # illustrative - db not defined
```

---

## Activation: the Master Key Is a Source, Not a Switch

Encryption turns on only where the code says so — `@cache.secure(...)`, or an explicit
encryption option on another preset (exact spellings below). `CACHEKIT_MASTER_KEY` supplies
the key for those spellings and decrypts stale ciphertext on read (not in an interop cache —
see the `encryption=False` row). A master key present with no stated intent is an error at
construction, not a default. Contract: [`protocol/spec/intent-presets.md` § Encryption Activation](https://github.com/cachekit-io/protocol/blob/main/spec/intent-presets.md#encryption-activation).

| Call site | No key (neither `master_key=` nor `CACHEKIT_MASTER_KEY`) | Key available (`master_key=` or `CACHEKIT_MASTER_KEY`) |
|---|---|---|
| `@cache.secure(...)` | **Fails closed** — `ValueError` at decoration | Encrypts |
| `@cache(encryption=True, single_tenant_mode=True)`; on a preset `encryption=EncryptionConfig(enabled=True, single_tenant_mode=True)` | **Fails closed** — `ConfigurationError` at decoration | Encrypts |
| `encryption=False` | Plaintext | Plaintext; stale ciphertext is still decrypted on read (each stale key logs one config-drift warning and counts on `cachekit_config_drift_reads_total` until it expires — expected after switching to plaintext). Not in an [interop cache](#turning-encryption-off-in-an-interop-cache): its stale entries are never decrypted |
| No encryption intent — no `encryption=`, or an `EncryptionConfig` without `enabled=` — on `@cache`, `.minimal`, `.production`, `.io`, `.dev`, `.test`, including `backend=None` and caches with a tenant extractor (flat `tenant_extractor=` on bare `@cache`, inside `EncryptionConfig` on a preset) | Plaintext | **Raises** `ConfigurationError` at decoration, naming the explicit spellings. `@cache.local` never encrypts and never raises. |

The raising row replaces the earlier "fleet-wide convenience" rule, under which the key's
presence auto-enabled encryption wherever no `master_key=` or `tenant_extractor=` was passed
(deprecated with a warning in 0.20.0; those two stayed plaintext, and `backend=None` caches
stored raw objects). It went because a
call site's encryption state was unreadable from the code — it depended on which pod
carried which variable — and a pod *missing* the variable wrote plaintext to the backend
with no error (issue #128). Migrate by writing the intent. Both explicit spellings fail closed
on a missing key (`.secure` → `ValueError`, the encryption option → `ConfigurationError`);
every other row can store plaintext, and the compliance argument below holds only on an
explicit path.

**The key is read when the decorator is applied** — at import, for a module-level function —
not at call time. A key that arrives later (`load_dotenv()` in `main()`, a startup hook that
fetches it from a vault) is never seen by a function decorated before it: `@cache.secure`
without `master_key=` has already raised `ValueError`, and a cache with no `encryption=` stays
plaintext. It works the other way too: unsetting the variable later does not turn a
`@cache.secure` cache off, because it keeps the key it read. Load the key before the modules
that define cached functions are imported.

> [!IMPORTANT]
> **Failing closed on a missing key is not failing closed on a bad entry.** A decrypt
> failure at read time — an AES-GCM tag mismatch from a tampered entry or the wrong key — is
> governed by a separate setting, `fail_closed`. It defers to `CACHEKIT_ENCRYPTION_FAIL_CLOSED`,
> which defaults to off, so even under `@cache.secure` such an entry is evicted and the function
> recomputes unless you opt in. See
> [Corruption vs Tamper](#corruption-vs-tamper-telemetry-and-fail-closed-mode).

### Turning Encryption Off in an Interop Cache

An interop cache (`interop=`) never decrypts stale ciphertext after `encryption=False`. Its entries
carry no header, so the reader decodes the ciphertext as MessagePack. Most stale entries fail to
decode and are recomputed, but a rare small one decodes cleanly and is served as a wrong value.
Encryption is also part of the [shared-entry contract](interop-mode.md#operation-names-are-a-contract-shared-entries):
every SDK that binds the operation must agree on it.

So move the operation to a new `namespace` in the same change, in every SDK that binds it. Old and
new writers then use different keys, so no plaintext reader decodes the old ciphertext — on any
backend, in L1, or mid-rollout. Old-version processes keep reading and writing the old namespace,
encrypted, until the rollout drains them.

Once the last old-version process is gone, the old keys are never read again, but they are not deleted: they retire only by TTL (never, if none
was set) and stay decryptable while the key is. If they hold personal data, delete them once the last
old writer is gone — on Redis, `SCAN` for `<old-namespace>:<operation>:*` and `UNLINK` the matches;
the File backend can only clear its whole `cache_dir`; Memcached and CachekitIO retire entries only by
TTL. Never `FLUSHDB` a shared database.

## What Can Go Wrong

### Missing Master Key
> [!WARNING]
> `cache.secure` requires a master key: `master_key=` or `CACHEKIT_MASTER_KEY`. With neither, it raises a `ValueError` at decoration time, not at call time.

```python notest
# No master_key= and CACHEKIT_MASTER_KEY unset
@cache.secure(ttl=300)
def operation(x):
    return sensitive_data(x)  # illustrative - sensitive_data not defined

# Error: "cache.secure requires master_key parameter or CACHEKIT_MASTER_KEY environment variable"
# Solution: Set CACHEKIT_MASTER_KEY env var, or pass master_key= explicitly
```

### Invalid Key Format
```bash
export CACHEKIT_MASTER_KEY="not_hex"  # Invalid
# Error: "CACHEKIT_MASTER_KEY must be hex-encoded: ..."
# Solution: Use 64-char hex string
export CACHEKIT_MASTER_KEY=$(openssl rand -hex 32)
```

### `@cache.secure` Does Not Pin a Backend

`@cache.secure` resolves its backend the way every preset does ([Backend Resolution
Priority](../backends/README.md#backend-resolution-priority)), so without an explicit backend or a
`set_default_backend()` default, the environment decides where the ciphertext goes. With `REDIS_URL` set and `CACHEKIT_API_KEY` unset,
`@cache.secure` encrypts to Redis, not to the SaaS. The values are still ciphertext; what changes
is which system holds them.

When a particular backend is a requirement, pass it explicitly:

```python notest
# notest: CachekitIOBackend needs the network and CACHEKIT_API_KEY
from cachekit import cache
from cachekit.backends.cachekitio import CachekitIOBackend

@cache.secure(master_key=secret_key, backend=CachekitIOBackend(), ttl=3600)
def get_patient_record(patient_id: str):
    return fetch_phi(patient_id)  # illustrative - fetch_phi not defined
```

### Key Rotation

Keeping a retiring key decrypt-only makes its entries readable; it does **not**
make a one-deploy key swap zero-miss. See [Key Rotation Pattern](#key-rotation-pattern)
for the keyring configuration, and follow the [key rotation
runbook](https://docs.cachekit.io/concepts/key-rotation/) — including its
[Before You Rotate](https://docs.cachekit.io/concepts/key-rotation/#before-you-rotate)
checks — for the rotation itself.

### Enabling Encryption on an Existing (Plaintext) Cache

When you turn encryption on over a cache that already holds plaintext entries, those
entries are **rejected, never read**: the entry raises a `SerializationError` internally, the
caller treats it as a miss, evicts the stale entry, recomputes, and re-stores the value
encrypted, whatever `fail_closed` says. Migration is therefore lazy and self-healing:

```text
read plaintext entry → SerializationError (rejected, never deserialized) → evict → recompute → re-store encrypted
```

That holds for CK-framed entries. An [interop cache](interop-mode.md) stores no header, so a
plaintext entry there reaches the decrypt step and fails authentication: a miss that recomputes
by default, but with `fail_closed=True` every read of it raises `DecryptionAuthenticationError`
until it expires. Turn encryption on in an interop cache by moving the operation to a new
`namespace`, as in [Turning Encryption Off in an Interop Cache](#turning-encryption-off-in-an-interop-cache).

There is deliberately **no opt-in flag** to let an encryption-enabled reader accept
plaintext entries. The frame header's `encrypted` flag is not authenticated, so a
plaintext entry forged by an attacker with backend write access is indistinguishable
from a legacy one — any "accept plaintext" escape hatch would reintroduce the
encryption-downgrade attack the downgrade-protected read path exists to prevent. If you need to
read plaintext entries, use a handler with `encryption=False` (which never had keys to
protect).

For large caches, choose between lazy migration and eager eviction based on your
workload: lazy migration spreads recomputation over reads (each legacy entry pays one
recompute on first access), while an eager flush concentrates it into a cold-start miss
wave — throttle or batch the eviction if the recompute cost is high. Either way, scope
eviction to cachekit's keys so unrelated data in the same Redis database survives: run the
`scan_iter` + `unlink` script under *Option 3: Data corruption* in
[Decryption failed](../error-codes.md#decryption-failed---authentication-tag-mismatch), once per
tenant and per namespace or function, and evict functions with a custom `key=` as it describes.
`FLUSHDB` is only safe when the database is dedicated to cachekit. Then deploy the encrypting
decorator (`@cache.secure` or an explicit `encryption=` option), with `CACHEKIT_MASTER_KEY` set
if it supplies the key.

### L1 Cache Conflict
```python notest
@cache.secure(ttl=300, master_key=secret_key)  # Encryption + L1 cache (stores encrypted bytes)
def get_sensitive_data():
    # L1 cache enabled: stores encrypted bytes (~50ns hits vs 2-7ms Redis)
    # Encryption is orthogonal: wraps any allowed serializer, applies to both L1 and L2
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

@cache.secure(ttl=3600)  # AES-256-GCM with MessagePack, key from CACHEKIT_MASTER_KEY
def get_user_profile(user_id):
    return db.get_profile(user_id)  # illustrative - db not defined

profile = get_user_profile(123)
# Data encrypted in Redis, decrypted in-app
```

### Encrypted JSON (Zero-Knowledge API Caching)
```python notest
from cachekit import cache
from cachekit.serializers import OrjsonSerializer

# Encrypt JSON API responses (webhooks, sessions, API keys)
@cache.secure(master_key=secret_key, serializer=OrjsonSerializer())
def get_api_keys(tenant_id: str):
    return {
        "api_key": "sk_live_abcdef123456",
        "webhook_secret": "whsec_xyz789",
        "tenant_id": tenant_id
    }

keys = get_api_keys("customer-123")
# JSON encrypted client-side, backend never sees plaintext values (illustrative)
```

### Encrypted DataFrames (Zero-Knowledge ML Caching)
```python notest
from cachekit import cache
from cachekit.serializers import ArrowSerializer
import pandas as pd

# Encrypt DataFrames with patient data, ML features, analytics
@cache.secure(master_key=secret_key, serializer=ArrowSerializer())
def get_patient_records(hospital_id: int):
    # illustrative - conn not defined
    return pd.read_sql(
        "SELECT patient_id, diagnosis, risk_score FROM patients WHERE hospital_id = ?",
        conn,
        params=[hospital_id]
    )

df = get_patient_records(42)
# DataFrame encrypted client-side, zero-knowledge storage
```

### Serializer Requirement
Encryption takes only a serializer whose class declares `cross_sdk_compatible = True`: `StandardSerializer`
(the default), `OrjsonSerializer`, `ArrowSerializer`, or a custom serializer that sets the flag. Anything
else, `AutoSerializer` included, raises `ConfigurationError` in the decorators and in a directly built
`EncryptionWrapper` ([details](../error-codes.md#serializer-refused-under-encryption)). The reason is the
protocol's rule that the step after decryption is chosen by the reader's configured serializer, never by
inspecting the decrypted bytes, on every backend. `AutoSerializer` picks its decoder by inspecting them.

### Multi-Tenant Isolation

> [!CAUTION]
> **`tenant_extractor` is not a tenancy boundary.** Cache keys carry no tenant
> component — the key is `ns:{ns}:func:{mod.fn}:args:{hash}:{flags}` — so tenants
> calling with identical arguments address the same entry. Give each tenant its own
> `namespace` or its own deployment, or make the tenant id a keyword argument of the
> cached function so it is part of the args hash.
>
> `tenant_extractor` requires an object implementing `.extract(args, kwargs)`, such as
> `ArgumentNameExtractor` or `ContextVarExtractor`, and tenant ids must be valid UUIDs.
> `ContextVarExtractor.set_tenant_id()` rejects a non-UUID id with `ValueError`. With
> `ArgumentNameExtractor` a non-UUID id fails at store time, and a bare `lambda` fails on
> every call; either way the failure is logged and the result is not written to the cache.

On an encrypted cache, a shared entry is never decrypted for the wrong tenant. A read
decrypts as the caller's tenant, resolved by `tenant_extractor` exactly as on the write.
It refuses a backend entry encrypted for any other tenant as `auth_tamper` (see Corruption
vs Tamper below). By default that read is a miss: the function runs and its result
overwrites the entry, so tenants that share a key keep evicting each other. With
`fail_closed=True` the read raises `DecryptionAuthenticationError` (its subclass
`TenantMismatchError`) instead, until the entry expires or is invalidated. That lets
whichever tenant calls a shared-key function first block every other tenant for the
entry's TTL, and seed it again once it expires. In L1 such an entry is only a miss (see
[Cache Key Binding](#cache-key-binding)). A read whose tenant cannot be resolved decrypts nothing and is a plain miss
that keeps the entry. Either way, keep tenants on separate keys as the caution above says.

### Key Rotation Pattern

The keyring has one **current** master key
(`CACHEKIT_MASTER_KEY`, encrypts and decrypts) plus up to **3 decrypt-only**
previous keys (`CACHEKIT_PREVIOUS_MASTER_KEYS`, comma-separated hex, same
per-key requirements as the master key). CK-framed entries carry the
fingerprint of their HKDF-derived per-tenant encryption key, so reads select
the exact keyring entry that wrote them — never trial decryption. The keyring
alone does not make a single-deploy swap zero-miss: use the [three-phase key
rotation runbook](https://docs.cachekit.io/concepts/key-rotation/) for scheduled
rotation, including its [Before You
Rotate](https://docs.cachekit.io/concepts/key-rotation/#before-you-rotate)
checks. Entries without a TTL, and entries whose expiry reads extend
(`refresh_ttl_on_get=True` or `refresh_ttl`), keep the retiring key in use
indefinitely; the runbook's Phase 3 drain window, not a fixed TTL, decides when
the old key can be removed.

```bash
# Phase 2 state only — <new-key-hex> is the key phase 1 distributed
# decrypt-only fleet-wide; <old-key-hex> is the master key it replaces.
# Pseudocode — both placeholders are 64-character hex (32-byte) values.
# Non-hex or short values are rejected at load.
export CACHEKIT_MASTER_KEY=<new-key-hex>
export CACHEKIT_PREVIOUS_MASTER_KEYS=<old-key-hex>
```

Rules enforced at config load — rejected, never truncated or silently fixed:

- **Cap**: at most 3 decrypt-only keys.
- **Per-key validation**: identical to `CACHEKIT_MASTER_KEY` (hex-encoded, at least 32 bytes; use exactly 32).
- **Current key not in the list**: the current master key must not re-appear in
  the decrypt-only list — the detectable signature of re-promoting a retired key.
  Keys compare as decoded bytes, so hex case makes no difference. A
  `CACHEKIT_MASTER_KEY` is checked when settings load (`pydantic.ValidationError`).
  A `master_key=` given to `@cache.secure`, to an `EncryptionConfig` or to an
  encrypting `@cache` or `CacheSerializationHandler` is checked when the cache is
  built, against the previous keys as they stand then, before any backend call
  (`ConfigurationError`).

Operator rule, which no SDK detects: **never re-promote a key that has
encrypted**, including by rolling back a Phase 2 deploy. Rolling back restores
the old key as current with the new key decrypt-only — a legal configuration
that passes load and silently resumes the old key's used AES-GCM nonce budget,
risking catastrophic nonce reuse. Back out a rotation by rotating *forward* to a
fresh key.

For a suspected key compromise, do not use the scheduled rotation. Follow the
runbook's [Compromise
Response](https://docs.cachekit.io/concepts/key-rotation/#compromise-response):
deploy a fresh key and unset `CACHEKIT_PREVIOUS_MASTER_KEYS` (code that builds
an `EncryptionWrapper` directly must pass an explicit `previous_master_keys=[]` —
omitting it falls back to the environment variable), flush encrypted namespaces at cut-over, and flush again once the last
instance writing under the old key has stopped. Ciphertext under the compromised
key stays readable to whoever holds that key until it is deleted.

[Interop-mode](interop-mode.md) entries store no per-entry key fingerprint
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
Tenant ID: tenant_extractor.extract(args, kwargs)  (or the single-tenant id below)

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

This is not a tenancy boundary: the cache key carries no tenant component, so
tenants calling with identical arguments address the same entry. See Multi-Tenant
Isolation above.
```

### Nonce Generation (Uniqueness)
```
Problem: If same nonce used with same key, encryption breaks
Solution: Counter-based nonce generation

Nonce = [counter_high_64bits][counter_low_32bits][random_32bits]
        └─ Increments per encryption
           Prevents nonce reuse even across reboots
```

### Cache Key Binding

The AAD binds the cache key, so ciphertext moved to another key fails authentication: a
backend-write attacker cannot serve one entry's value at another entry's key. The key bound is
the one the backend is handed, with the backend's `key_prefix` in front of it: the namespace is
already part of the key, a `MemcachedBackend` reports its configured `key_prefix`, and the
tenant-scoped Redis backend (env auto-detection, `RedisBackendProvider`; `t:default:` with no
tenant set) reports the calling tenant's `t:{tenant}:`. An entry copied from `app-a:` to `app-b:`, or from `t:acme:` to
`t:globex:`, is refused. A custom backend that prefixes keys must expose that prefix as
`key_prefix` for it to be bound. Backend encodings of the key (the File backend's hashed file
name, CachekitIO's percent-encoded URL path) are not part of it, and neither is the tenant
scoping the CachekitIO server applies.

There is one AAD per read. A read never retries with another form of the key, such as the key
without its prefix, so a failed authentication is final.

L1 is shared by every function in a namespace and keyed by the bare cache key, so it can hold
an entry bound to another prefix: another tenant's behind the tenant-scoped Redis backend, or
another function's behind a different Memcached `key_prefix`. On a
backend with a key prefix that read is an L1 miss and goes on to the backend, never an
`auth_tamper`. An L1 entry encrypted for another tenant (`tenant_extractor`) is a miss on any
backend. Two encrypted functions that share a namespace and a cache key but not a backend
prefix (one on a prefixing backend, one on an unprefixed one) read each other's L1 entries as
failed authentication on the unprefixed side; give them separate namespaces.

**Upgrading.** Releases before this binding left the backend's prefix out of the AAD. Their
encrypted entries written through a prefixing backend fail authentication after the upgrade:
`MemcachedBackend` with a `key_prefix`, the tenant-scoped Redis backend, which includes the
default Redis backend env auto-detection builds (`t:default:` with no tenant set), and any custom
backend with a non-empty `key_prefix`. By default each such
entry is read once as a miss, counted as `auth_tamper` with a WARNING, recomputed and
overwritten. With `fail_closed=True`, every read of one raises `DecryptionAuthenticationError`
until it expires or is deleted. So delete those entries, or move the cache to a fresh key space
(a new `namespace`, or a new Memcached `key_prefix`), rather than wait out the TTL. During a
rolling upgrade, processes on the earlier release and on this one each read the other's writes
as failed authentication, and deleting entries does not help while both run. Give the upgraded
release a fresh key space, or stop every earlier-release process first. Unaffected: a
`RedisBackend` you construct yourself, `FileBackend`, `CachekitIOBackend`, and interop mode.

### Encryption Downgrade Protection (Read Path)

The CK frame header — the JSON envelope carrying `encrypted`, `tenant_id`, `format`,
and the serializer name — is plaintext, so a reader can parse it before it has a key.
Its JSON bytes are not what the AES-GCM tag covers; the tag covers the ciphertext and
the AAD. AAD v0x03 is built from the tenant, the cache key (with any
[key prefix the backend adds](#cache-key-binding)), and the header's wire format,
compression flag and (when set) original type, so a change to one of those header
values that alters the AAD fails authentication. The `encrypted` flag is **not** an
AAD input: nothing authenticates it.

An attacker with backend write access (the threat actor in the protocol's threat
model) could exploit that unauthenticated flag by planting a frame whose header claims
`encrypted: false` plus an arbitrary plaintext payload — a classic encryption
downgrade (CWE-757). cachekit therefore never lets header metadata select the read
path when encryption is configured:

```text
Handler configured with encryption:
  entry header claims encrypted  → authenticated decrypt (AAD + GCM tag verified)
  entry header claims plaintext  → SerializationError (plaintext never returned; miss + evict, whatever `fail_closed` says)
```

The plaintext deserializer is unreachable on an encryption-enabled handler, regardless
of what the stored frame claims. Configuration decides the read path; stored (i.e.
attacker-writable) data never does.

### Cleartext Frame Header Fields (Accepted Exposure)

Encrypted entries expose three fields in the plaintext header: `tenant_id`,
`encryption_algorithm`, and `key_fingerprint`. This exposure is deliberate and
accepted:

- **`tenant_id`** — the tenant the entry was encrypted for, needed *before*
  decryption; moving it inside the ciphertext is a chicken-and-egg problem. It is an
  opaque identifier, not secret material, and it *is* tamper-protected. A cache with a
  `tenant_extractor` derives the per-tenant key (HKDF) from the caller's tenant and
  refuses an entry whose `tenant_id` differs (`auth_tamper`) before attempting to
  decrypt. A cache without one derives the key from the header's `tenant_id`, so a
  modified value selects a different key and the read fails authentication
  (`auth_tamper`) — a key-fingerprint mismatch under fail-closed, a GCM tag failure
  otherwise.
- **`key_fingerprint`** — a one-way fingerprint of the derived key, used only for
  clearer diagnostics during key rotation. It reveals nothing about key material.
- **`encryption_algorithm`** — public information (`AES-256-GCM`); hiding the
  algorithm adds no security (Kerckhoffs's principle).

Relocating these fields would be a cross-SDK wire-format change owned by the
[protocol spec](https://github.com/cachekit-io/protocol); the Python SDK documents the
exposure rather than diverging from the shared frame format.

### Cleartext Cache Key (Accepted Exposure)

The cache key is cleartext too. By default it carries the namespace (when set), the function's
`module.qualname` and an unkeyed, unsalted blake2b-256 hash of the arguments
(`[ns:{ns}:]func:{mod.fn}:args:{64-hex}:{flags}`), so over a small or guessable argument space the
hash can be enumerated offline. A custom `key=` function is not hashed: its return value becomes
the key verbatim, after the namespace (`{namespace}:{value}`, with `default` when none is set), so
never return raw identifiers or personal data from it — derive them with an HMAC whose key never
reaches the backend.

Whoever operates the backend can therefore learn which record was read or written, when and how
often, without decrypting anything. On the CachekitIO backend the key travels percent-encoded in
the URL path (`/v1/cache/{key}`), so it also lands in access logs along the request path and stays
there for their retention period, not the cache TTL. Ciphertext length reveals the approximate
plaintext size, and because the default serializer compresses before encrypting, it also tracks
how compressible the content is. Encryption protects values, not access patterns: keep secrets
out of namespaces, function names and `key=` return values, and count argument-identifiable
access as metadata exposure in your threat model.

### Corruption vs Tamper: Telemetry and Fail-Closed Mode

Four failure classes surface on the decrypt read path, and cachekit distinguishes
them (cachekit-py#170):

- **`auth_tamper`** — the entry failed authentication: the ciphertext was modified,
  the key is wrong (rotation/misconfiguration), the AAD didn't match (ciphertext moved
  between cache keys or [key prefixes](#cache-key-binding)), or, on a cache with a
  `tenant_extractor`, a backend entry was encrypted for a tenant other than the caller's
  (`TenantMismatchError`, refused before any decrypt attempt). In L1, another tenant's entry,
  or behind a prefixing backend one bound to another prefix, is a plain miss instead
  ([Cache Key Binding](#cache-key-binding)). The
  plaintext frame header fields built into the AAD
  (`format`, `compressed`, `original_type`) are unencrypted, but the AAD built from
  them is authenticated by the tag: a header change that produces different AAD bytes
  also fails here. (The tag authenticates the constructed AAD, not the header's JSON
  bytes.) Raised as
  `DecryptionAuthenticationError`. This is the signal an active attack would produce.
- **`suspicious_envelope`** — the unauthenticated envelope is inconsistent with the
  handler's configuration: a plaintext claim under an encryption-enabled handler (the
  CWE-757 downgrade guard) or a missing `tenant_id`. Benign during a lazy
  plaintext→encrypted migration; a spike outside a migration window is suspect. Always
  fails open (miss + evict) so migration keeps working — even in fail-closed mode.
- **`envelope_shape`** — an entry nothing verified decoded to the *shape* of a ByteStorage
  envelope and was refused. Either a rotted integrity-on envelope or a legitimate top-level
  4-element list that merely looks like one; the read path cannot tell them apart, so this
  is not reliable corruption evidence and is kept out of `corruption`. Always fails open.
  An integrity-off `AutoSerializer` refuses to write the second case, so the same redacted key repeating in the
  WARNING log is another writer still storing it (see *Deserialization failed* in [error-codes.md](../error-codes.md)).
- **`corruption`** — everything else: checksum mismatch, truncated/malformed frame,
  serializer mismatch, a deserialize failure on *already-authenticated* plaintext (including
  plaintext in a container other than the one the serializer writes, which is refused, never
  decoded: Arrow IPC without its checksum prefix, or, for an integrity-off `StandardSerializer`,
  a legacy-layout envelope like those `AutoSerializer` sealed under `compressed=False` before
  0.12.0, when it declares at most 256 KiB; a larger one comes back as its fields), or a
  non-string or non-UTF-8-encodable `original_type`, or a non-UTF-8-encodable `compressed`,
  in the frame header. Such a value
  cannot be built into the AAD at all, so no tag check runs — the read is
  corruption-class, and the entry is evicted and recomputed even in fail-closed mode.
  Storage rot and bugs, not evidence of tampering.

All are counted on the Prometheus counter
`cachekit_decrypt_failures_total{reason, tier="l1"|"l2"}` — alert on
`reason="auth_tamper"` specifically; a nonzero rate there is a security event, not
noise. Baseline `suspicious_envelope` around migration windows; a flat, steady
`envelope_shape` rate is a writer still storing a value the shape rule refuses, not rot.

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
@cache.secure(master_key=secret_key, fail_closed=True)
def get_payment_token(user_id: int): ...

# Or via explicit EncryptionConfig
from cachekit.config.nested import EncryptionConfig
config = EncryptionConfig(enabled=True, master_key=secret_key,
                          single_tenant_mode=True, fail_closed=True)
```

**Keyring configuration faults are not a decrypt-failure class.** `EncryptionWrapper`
raises `KeyringConfigurationError` (a `ValueError` subclass, exported from
`cachekit.serializers`) when the decrypt-only keyring is unusable: a previous master key
passed directly that is not exactly 32 bytes, more than three previous keys, or the current
key repeated among them. Settings check all three at load, and an encrypting cache checks its
current key against the previous keys when it is built, so behind the decorators this surfaces
only when settings are assigned after that, or on a config-drift read (below). Outside config-drift reads, the fault never
evicts and is not counted on `cachekit_decrypt_failures_total`. Direct `EncryptionWrapper` users
and callers of the `CacheOperationHandler` read and write methods receive it in both fail modes. Behind
the `@cache` decorators, a read of an existing encrypted entry raises it too, from L1 or L2 and
from the re-read after a distributed-lock wait, so the function does not run and no
circuit-breaker failure is counted. A key with no entry yet reads as a miss before the keyring
is built, so the fault surfaces at the write instead, and the write raises it too, sync or async:
the function has already run, but its result is neither cached nor returned, and no
circuit-breaker failure is counted. Two cases take other paths: a missing *current* master key,
or one of the wrong length, raises `EncryptionError`, and an encryption-disabled handler reading an entry that
claims encryption treats the fault as corruption (miss + evict), because only the
unauthenticated header sent it down the decrypt path.

> **⚠️ Key rotation under fail-closed:** with `fail_closed` enabled there is no
> silent self-heal — rotating `CACHEKIT_MASTER_KEY` **without retaining the old key
> in `CACHEKIT_PREVIOUS_MASTER_KEYS`** makes every pre-rotation entry raise
> `DecryptionAuthenticationError` on read (the fingerprint matches no keyring entry,
> decryption is refused, and the entry is retained, not evicted). Follow the keyring
> rotation pattern above: keep the retiring key decrypt-only until the runbook's
> [Phase 3](https://docs.cachekit.io/concepts/key-rotation/#scheduled-rotation-three-phases)
> drain window has closed.
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
> Client-side encryption may *reduce* GDPR, HIPAA or PCI DSS scope, subject to assessment and
> your other controls, and only on an explicit path (see
> [Activation](#activation-the-master-key-is-a-source-not-a-switch)); it is not a compliance
> guarantee. Encryption covers values only: the cache key is cleartext (and, on CachekitIO, lands
> in access logs; see [Cleartext Cache Key](#cleartext-cache-key-accepted-exposure)).

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

Through the decorator, a wall-clock comparison of an L1 hit of a 23.5KB dict with and without encryption is inconclusive: the difference stayed inside run-to-run noise. See [Not Measured Here](../performance.md#not-measured-here) in the Performance Guide.

### Key Derivation (Per-Tenant)
```
Per-tenant key derivation: 50-100μs (HKDF operation)
Cached after first use: No additional overhead
```

---

## Interaction with Other Features

**Encryption + Circuit Breaker**:
```python notest
@cache.secure(ttl=300, master_key=secret_key)  # Both enabled
def get_data():
    # Decrypt or integrity failure on read → cache miss, entry evicted, function runs.
    # It does NOT count toward the circuit breaker.
    return fetch_data()  # illustrative - fetch_data not defined
```

A decrypt or integrity failure says nothing about backend health, so the breaker ignores it. For
fail-open reads, the warning log and the failure metric,
`cache_operations_total{operation="cache_get_deserialize", success="False"}`, still fire. This keeps a
lazy plaintext→encrypted migration, where every pre-encryption entry is refused once, from opening
the breaker. With `fail_closed=True`, an authentication failure raises `DecryptionAuthenticationError`
to the caller instead of recomputing. It emits `cachekit_decrypt_failures_total` and the
authentication error log, but not the `operation="cache_get_deserialize"` sample or warning log. It does not
count toward the breaker either.

**Encryption + L1 Cache**:
```python notest
@cache.secure(ttl=300, master_key=secret_key)
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
A: Check `CACHEKIT_PREVIOUS_MASTER_KEYS` — comma-separated hex, each key subject to
the same rules as `CACHEKIT_MASTER_KEY` (at least 32 bytes; use exactly 32), at most 3 entries, and the
current `CACHEKIT_MASTER_KEY` must **not** appear in the list. Follow the keyring
rotation pattern above: keep the retiring key decrypt-only until the runbook's
[Phase 3](https://docs.cachekit.io/concepts/key-rotation/#scheduled-rotation-three-phases)
drain window has closed.

**Q: Performance degraded after enabling encryption**
A: Expected 100-500μs overhead. Profile to confirm acceptable.

---

## Zero-Knowledge Architecture

**Use case**: Building a caching system where the backend never sees plaintext values.

### Client-Side Encryption Flow
```python notest
# Client application (user's infrastructure)
from cachekit import cache
from cachekit.backends.cachekitio import CachekitIOBackend
from cachekit.serializers import OrjsonSerializer

# An HTTP API backend: CachekitIOBackend talks to cachekit.io over HTTPS
@cache.secure(master_key=secret_key, serializer=OrjsonSerializer(), backend=CachekitIOBackend())
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
    // NEVER sees plaintext values (no decryption key); the key is cleartext
    await KV.put(key, value);

    return new Response("OK");
  }
}
```

**Benefits**:
- ✅ Backend compromise doesn't expose cached values
- ✅ Supports a compliance scope-reduction argument on an explicit path (see [Compliance Implications](#compliance-implications))
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
