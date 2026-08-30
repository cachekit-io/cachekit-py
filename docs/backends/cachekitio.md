**[Home](../README.md)** › **[Backends](README.md)** › **CachekitIO Backend**

# CachekitIO Backend

> *cachekit.io is in closed beta — [request access](https://cachekit.io)*

`CachekitIOBackend` connects to the cachekit.io managed cache API over HTTP/2. It implements the full `BaseBackend` protocol plus distributed locking (`LockableBackend`) and TTL inspection (`TTLInspectableBackend`).

## Setup

```bash
export CACHEKIT_API_KEY="ck_live_..."
```

## Basic Usage

Loads config from environment:

```python notest
from cachekit import cache
from cachekit.backends.cachekitio import CachekitIOBackend

backend = CachekitIOBackend()

@cache(backend=backend)
def cached_function(x):
    return expensive_computation(x)
```

## Explicit Configuration

```python notest
from cachekit.backends.cachekitio import CachekitIOBackend

backend = CachekitIOBackend(
    api_url="https://api.cachekit.io",  # required if not using env
    api_key="ck_live_...",              # required if not using env
    timeout=5.0,                        # optional, default: 5.0 seconds
)
```

## Convenience Shorthand via `@cache.io()`

```python notest
from cachekit import cache

# Equivalent to: @cache(backend=CachekitIOBackend())
# @cache.io() creates its own CachekitIOBackend via DecoratorConfig.io()
# and passes it as an explicit backend kwarg — Tier 1 resolution, not magic.
@cache.io(ttl=300, namespace="my-app")
def cached_function(x):
    return expensive_computation(x)
```

## Health Check

```python notest
backend = CachekitIOBackend()
is_healthy, details = backend.health_check()
# details: {"backend_type": "saas", "latency_ms": 12.4, "api_url": "...", "version": "..."}
```

## Async Support

All protocol methods have async counterparts:

```python notest
from cachekit import cache
from cachekit.backends.cachekitio import CachekitIOBackend

backend = CachekitIOBackend()

@cache(backend=backend)
async def async_cached_function(x):
    return await fetch_data(x)

# Direct async calls also available:
# await backend.get_async(key)
# await backend.set_async(key, value, ttl=60)
# await backend.delete_async(key)
# await backend.exists_async(key)
# is_healthy, details = await backend.health_check_async()
```

## Distributed Locking (async only)

`acquire_lock` is an async context manager (LockableBackend protocol). It yields
`True` if the lock was acquired, `False` if `blocking_timeout` elapsed first, and
releases the lock automatically on context exit. This is a **best-effort lease**,
not mutual exclusion: it expires server-side at `timeout` even if the holder is
still working (no notification, no fencing token) — do not rely on it for
correctness of non-idempotent operations.

```python notest
backend = CachekitIOBackend()

async with backend.acquire_lock("my-key", timeout=30.0, blocking_timeout=5.0) as acquired:
    if acquired:
        pass  # do work; lock auto-releases on context exit
```

The `@cache` decorators use this automatically on cache miss for **async**
functions — see [Distributed Locking](../features/distributed-locking.md).

## TTL Inspection (async only)

```python notest
backend = CachekitIOBackend()

remaining = await backend.get_ttl("my-key")      # seconds remaining, or None
refreshed = await backend.refresh_ttl("my-key", ttl=300)  # update TTL in place
```

## Timeout Override

Returns a new instance:

```python notest
backend = CachekitIOBackend()
fast_backend = backend.with_timeout(1.0)  # 1-second timeout variant
```

## Security

The API URL is validated on construction — HTTPS required, private/internal IP addresses blocked. The default allowlist restricts connections to `api.cachekit.io` and `api.staging.cachekit.io`. Set `CACHEKIT_ALLOW_CUSTOM_HOST=true` to override (testing only).

## Environment Variables

```bash
CACHEKIT_API_KEY=ck_live_...          # Required — API key for authentication
CACHEKIT_API_URL=https://api.cachekit.io  # Optional — defaults to api.cachekit.io
CACHEKIT_TIMEOUT=5.0                  # Optional — request timeout in seconds
```

## When to Use

**Use CachekitIOBackend when**:
- Managed, zero-infrastructure caching
- Multi-region distributed caching without operating Redis
- Teams that want caching without DevOps overhead
- Zero-knowledge architecture (compose with `@cache.secure` — see below)

**When NOT to use**:
- Sub-millisecond latency requirements — use Redis or L1 cache
- Fully offline/air-gapped environments
- Applications that cannot tolerate HTTP/2 dependency

## Characteristics

- Latency: ~10–50ms L2 (HTTP/2, region-dependent)
- Sync and async support (hybrid client architecture)
- Connection pooling built-in (default: 10 connections)
- Automatic retries on transient errors (default: 3)
- Distributed locking via server-side Durable Objects
- TTL inspection and in-place refresh supported

---

## Encrypted SaaS Pattern (Zero-Knowledge)

> *cachekit.io is in closed beta — [request access](https://cachekit.io)*

Compose `@cache.secure` with `CachekitIOBackend` for end-to-end zero-knowledge encryption over managed SaaS storage. The backend stores opaque ciphertext — it never sees plaintext data or your master key.

```python notest
from cachekit import cache
from cachekit.backends.cachekitio import CachekitIOBackend

# Required env: CACHEKIT_MASTER_KEY (hex, min 32 bytes) + CACHEKIT_API_KEY
backend = CachekitIOBackend()

@cache.secure(backend=backend, ttl=3600, namespace="sensitive-data")
def get_user_profile(user_id: str) -> dict:
    """Result is AES-256-GCM encrypted before storage.

    Data flow:
      serialize(result) -> encrypt(HKDF-derived key) -> PUT /v1/cache/{key}
      GET /v1/cache/{key} -> decrypt() -> deserialize() -> return result

    The cachekit.io API sees only encrypted bytes. Zero-knowledge.
    """
    return fetch_user_from_db(user_id)
```

**Why this matters**:
- `@cache.secure` applies AES-256-GCM client-side encryption before any data leaves the process
- Per-tenant key derivation via HKDF — cryptographic isolation between namespaces
- The SaaS backend is a zero-knowledge conduit: it stores whatever bytes arrive
- With `@cache.secure`: SaaS is out of scope for HIPAA/PCI (stores only ciphertext)
- Without `@cache.secure`: SaaS stores plaintext, may be in compliance scope

**Requirements**:

```bash
CACHEKIT_MASTER_KEY=<hex string, min 32 bytes>  # Never leaves the client
CACHEKIT_API_KEY=ck_live_...
```

See [Zero-Knowledge Encryption](../features/zero-knowledge-encryption.md) for full details on key derivation and serialization format implications.

### `.secure` + explicit backend vs `.io()` + env var — which one?

There is a second path to encrypted SaaS caching: `@cache.io()` with
`CACHEKIT_MASTER_KEY` set. Encryption is then auto-detected downstream — the same
zero-knowledge bytes on the wire — **but the failure mode is inverted**:

- `@cache.secure(backend=CachekitIOBackend())` — **fails closed.** Encryption is
  forced on in code; a missing master key (param or `CACHEKIT_MASTER_KEY`) raises
  `ValueError` at decoration time. No plaintext **values** can ever reach the
  backend (cache keys and the frame header stay plaintext by design).
- `@cache.io()` + `CACHEKIT_MASTER_KEY` — **fails open.** If the env var is absent,
  the same code silently caches **plaintext** to the SaaS.

Use `.secure` + explicit backend when encryption is a security requirement (PII,
PHI, compliance claims — the "SaaS out of HIPAA/PCI scope" argument only holds on
this path). Use `.io()` + env when encryption is a fleet-wide opt-in convenience
and plaintext caching is an acceptable state.

Two caveats, covered in depth in
[Which Path](../features/zero-knowledge-encryption.md#which-path-cachesecure-vs-cacheio--cachekit_master_key):
`.secure` does **not** pin the SaaS backend (env auto-detect can silently route
encrypted values to Redis — pass `backend=` explicitly, as above), and fail-closed
on a *missing key* is separate from `fail_closed` on a *decrypt failure*, which
defaults to off.

## See Also

- [Backend Guide](README.md) — Backend comparison and resolution priority
- [Redis Backend](redis.md) — Self-hosted alternative for lower latency
- [Zero-Knowledge Encryption](../features/zero-knowledge-encryption.md) — Client-side encryption details
- [Configuration Guide](../configuration.md) — Full environment variable reference

---

<div align="center">

**[GitHub Issues](https://github.com/cachekit-io/cachekit-py/issues)** · **[Documentation](../README.md)**

</div>
