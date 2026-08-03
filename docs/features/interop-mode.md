**[Home](../README.md)** › **Features** › **Interop Mode**

# Interop Mode — Cross-SDK Cache Sharing

**Released** — see the [changelog](../../CHANGELOG.md) for the interop entry · Implements [interop/v1](https://github.com/cachekit-io/protocol/blob/main/spec/interop-mode.md)

## TL;DR

Interop mode is the opt-in path that lets cachekit-py share cache entries **byte-identically** with cachekit-rs and cachekit-ts. Keys become `{namespace}:{operation}:{args_hash}` and values become one plain MessagePack document — no Python-internal framing, readable by any language with a MessagePack library.

```python notest
from cachekit import cache

@cache(interop="get_user", namespace="users", ttl=300)
def get_user(user_id: int, include_profile: bool = False):
    return db.fetch(user_id)  # illustrative - db not defined
```

A Rust service using `#[cachekit(interop = "get_user", namespace = "users")]` or a TypeScript service using `wrap(fetchUser, { interop: "get_user", namespace: "users" })` reads and writes the **same entries**.

Default behavior is completely unchanged: functions that don't pass `interop=` keep auto-mode keys and the Python CK v3 frame, byte-for-byte.

---

## Two Modes

| | Auto mode (default) | Interop mode (opt-in) |
| :--- | :--- | :--- |
| Key format | `ns:{ns}:func:{module.qualname}:args:{hash}:{flags}` | `{namespace}:{operation}:{args_hash}` |
| Operation identity | Derived from the Python function path | **Explicit, user-supplied** |
| Value format | CK v3 frame + ByteStorage envelope (LZ4 + xxHash3-64) | **Plain MessagePack, no envelope** |
| Cross-SDK reads | ❌ Python-only | ✅ py / rs / ts |

## Choosing a Decorator Form

`interop=` is an ordinary [`DecoratorConfig`](../api-reference.md) field, so it rides **every** entry point — bare `@cache`, any intent preset, or the RORO `config=` form. Presets change the *runtime* profile (circuit breaker, monitoring, L1 tuning), never the wire bytes: in interop mode the value encoder is always the canonical interop MessagePack encoder, whatever the preset says.

| Form | Bytes on the wire | Why |
| :--- | :--- | :--- |
| `@cache(interop=..., namespace=...)` | ✅ Spec-identical | Baseline — key and value bytes come only from the interop/v1 spec |
| `@cache.production(interop=..., ...)` | ✅ Spec-identical | **Recommended.** Reliability profile (circuit breaker, monitoring) affects runtime only, never bytes |
| `@cache.minimal(interop=..., ...)` | ✅ Spec-identical | Its `integrity_checking=False` is a no-op here — see [Encryption](#encryption) |
| `@cache.secure(interop=..., ...)` | ✅ Spec-identical ciphertext | Encrypted interop bytes; cross-SDK readable with the same master key + deployment UUID |
| `@cache.io(interop=..., ...)` | ✅ Composes in code | ⚠️ Don't run against CachekitIO until the saas#91 validator deploy is live — see the note at the bottom |
| `@cache.local(...)` / `@cache(backend=None)` | ❌ Rejected loudly | No shared medium: `.local` raises `TypeError` (it accepts no `interop=`), `backend=None` raises `ConfigurationError` at decoration time |

**Recommended form** — spec-identical bytes plus the production reliability profile:

```python notest
from cachekit import cache

@cache.production(interop="get_user", namespace="users", ttl=300)
def get_user(user_id: int):
    return db.fetch(user_id)  # illustrative
```

All three configuration styles accept it — bare `@cache(interop=..., ...)` (see the TL;DR), an intent preset (above), or RORO:

```python
from cachekit import DecoratorConfig

# RORO — interop= is a plain DecoratorConfig field
config = DecoratorConfig.production(interop="get_user", namespace="users", ttl=300)
assert config.interop == "get_user"
assert config.circuit_breaker.enabled  # production profile intact
```

## Operation Names Are a Contract (Shared Entries)

Interop keys are **deliberately function-identity-free**: the key is built from `(namespace, operation, args)` and nothing else — no module path, no function name, no decorator settings. That is the property that makes cross-SDK sharing work at all: a Rust service can't know your Python module path, so the key must not contain one.

The flip side: two *differently decorated* Python functions that declare the same `(namespace, operation)` and receive matching arguments read and write **the same entry — including L1** (same key, same process-wide per-namespace cache). There is nothing function-shaped to include: `generate_interop_key(namespace, operation, args)` takes no function at all — see [Manual Key/Value Helpers](#manual-keyvalue-helpers) for the byte-pinned demonstration.

Treat operation names like queue names or topic names: a **cross-team contract**, not a local variable. Two teams binding `users:get_user` had better agree on the argument list and the meaning of the cached value — the cache will not referee. If two functions must not share entries, give them different operation names.

**Encryption settings are part of that contract.** Every function — and every SDK — binding one `(namespace, operation)` must agree on encryption on/off, master key, and deployment UUID. The failure mode is quiet: an encrypted-config reader treats a plaintext entry as an authentication failure — a miss, unless `fail_closed=True` — and overwrites it with ciphertext; a plaintext-config reader can't decode the ciphertext, recomputes, and **re-stores the value unencrypted at the same shared key**, silently defeating the zero-knowledge guarantee while both sides evict each other's entries on every read.

## The Cross-SDK Contract

The contract for one operation is the operation name **plus** the effective argument list (arity, order, types):

- `namespace` and `operation` must match `^[a-z0-9][a-z0-9._-]{0,63}$` (lowercase only — enforced loudly at decoration time, never silently normalized).
- Named arguments bind to their declared positions and **introspectable defaults are applied**: `get_user(42)`, `get_user(user_id=42)` and `get_user(42, include_profile=False)` all produce the same key.
- Arguments must fit the closed interop data model (int in `[-2^63, 2^64-1]`, float, str, bytes, bool, None, list/tuple, dict with str keys, set, tz-aware datetime, UUID; Python conveniences: Enum → value, Path → POSIX string, Decimal → string). Anything else raises `InteropError` **at call time** — interop mode never silently degrades to uncached execution.
- Values are plain MessagePack: None, bool, int, float, str, bytes, list/tuple, dict with str keys, plus datetime/date/time as portable sentinel maps. Python-specific values (sets, custom classes, NumPy/pandas) raise `InteropError` at store time — they would not round-trip cross-SDK.

## Encryption

Encryption works unchanged — and cross-SDK. The AES-256-GCM plaintext is the plain MessagePack bytes (no ByteStorage step), the AAD is always exactly four components (`tenant_id`, `cache_key`, `"msgpack"`, `"False"`), and the ciphertext layout is `nonce(12) ‖ ciphertext ‖ tag(16)`.

```python notest
@cache(
    interop="get_user",
    namespace="users",
    encryption=True,
    master_key="a" * 64,
    single_tenant_mode=True,
    deployment_uuid="00000000-0000-0000-0000-000000000001",  # share across SDKs
)
def get_user(user_id: int):
    return db.fetch(user_id)  # illustrative
```

Three constraints, all fail-closed:

- **Single-tenant only.** Interop entries carry no metadata header, so the read path cannot recover a per-call tenant; `tenant_extractor` is rejected at decoration time. To share encrypted entries across SDKs, configure the same master key **and** the same `deployment_uuid` (or `CACHEKIT_DEPLOYMENT_UUID`) everywhere.
- **The shared tenant must be explicit and canonical.** The machine-local auto-generated deployment UUID is rejected (it differs per host — nothing else could ever decrypt), and the configured value must already be in canonical lowercase-hyphenated form (Python would otherwise normalize it before key derivation while other SDKs use the raw string — silently different keys).
- **Config decides, bytes never do.** With encryption enabled, stored bytes are always treated as ciphertext and authenticated before any decode. There is no header to forge, so the CWE-757 downgrade class (see the auto-mode fail-closed read path in [zero-knowledge-encryption.md](zero-knowledge-encryption.md)) cannot exist here.

One thing no guardrail can catch: two *binders* of the same `(namespace, operation)` with different encryption configs. That mismatch is silent — see [Operation Names Are a Contract](#operation-names-are-a-contract-shared-entries).

**Integrity checking is a no-op in interop mode.** Interop values bypass the ByteStorage envelope entirely (no envelope, hence no metadata header and no xxHash3-64 checksum), so `.minimal`'s `integrity_checking=False` and `.production`'s `True` have no effect on interop entries. Without encryption, that means interop entries have **no corruption detection at all**: corrupted bytes that still parse as valid MessagePack are returned as valid values — under `.production` just as under `.minimal`. Tamper and corruption protection, when you need it, is AES-256-GCM: enable encryption, and every read is authenticated before decode.

## Guardrails (all loud, none silent)

| Situation | Behavior |
| :--- | :--- |
| Missing/invalid `namespace` or `operation` | `ConfigurationError` at decoration time |
| `interop=` combined with `key=`, `fast_mode`, `backend=None` (L1-only), or a non-default serializer | `ConfigurationError` at decoration time |
| Encryption without an explicit, canonical shared deployment UUID | `ConfigurationError` at decoration time |
| Backend with a wire-level key prefix (e.g. Memcached `key_prefix`) | `ConfigurationError` — checked at decoration **and re-checked per call** (a prefixed key is invisible to other SDKs and would escape the encryption AAD binding) |
| Out-of-model argument | `InteropError` at call time (function does **not** run) |
| Out-of-model return value | `InteropError` at store time (never "computed but silently never cached") |
| CK v3 frame found at an interop key | Diagnostic error, treated as a miss, entry overwritten (self-healing) |

## Manual Key/Value Helpers

For debugging, migrations, or out-of-band writers:

```python
from cachekit import generate_interop_key, encode_interop_value, decode_interop_value

# Byte-pinned by the protocol vectors (single_int / issue_example_object):
key = generate_interop_key("users", "get_user", [42])
assert key == "users:get_user:61598716255080080f6456eb065c2e51badfaa4320b0efe97469c29cffee8875"

data = encode_interop_value({"name": "alice", "age": 30})
assert data.hex() == "82a36167651ea46e616d65a5616c696365"  # canonical: sorted keys
assert decode_interop_value(data) == {"age": 30, "name": "alice"}
```

## Conformance

Every build byte-verifies the implementation against the shared protocol vectors (`tests/unit/protocol/`): 33 key vectors, 4 value vectors, 9 must-error vectors, the interop AAD vector, and a full HKDF-SHA256 → AES-256-GCM decrypt of the published cross-SDK ciphertext through the production Rust stack.

> **CachekitIO note**: the deployed api.cachekit.io cache-key validator predates interop keys and rejects them until the saas#91 validator shrink is live in production. Redis and other self-hosted backends are unaffected.
