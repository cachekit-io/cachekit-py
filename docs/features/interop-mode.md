**[Home](../README.md)** › **Features** › **Interop Mode**

# Interop Mode — Cross-SDK Cache Sharing

**Available since v0.12.0** · Implements [interop/v1](https://github.com/cachekit-io/protocol/blob/main/spec/interop-mode.md)

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
