**[Home](../README.md)** › **[Backends](README.md)** › **CachekitIO Backend**

# CachekitIO Backend

> *cachekit.io is in closed beta — [request access](https://cachekit.io)*

`CachekitIOBackend` connects to the cachekit.io managed cache API over HTTPS (HTTP/1.1, through urllib3). It implements the full `BaseBackend` protocol plus distributed locking (`LockableBackend`) and TTL inspection (`TTLInspectableBackend`).

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

# Every argument is optional; anything left out loads from the environment.
backend = CachekitIOBackend(
    api_key="ck_live_...",              # default: CACHEKIT_API_KEY
    api_url="https://api.cachekit.io",  # default: CACHEKIT_API_URL, then api.cachekit.io
    timeout=5.0,                        # default: CACHEKIT_TIMEOUT, then 5.0 seconds
)
```

## Convenience Shorthand via `@cache.io()`

```python notest
import os

from cachekit import cache

# Equivalent to: @cache(backend=CachekitIOBackend())
# @cache.io() creates its own CachekitIOBackend via DecoratorConfig.io()
# and passes it as an explicit backend kwarg — Tier 1 resolution, not magic.
@cache.io(ttl=300, namespace="my-app")
def cached_function(x):
    return expensive_computation(x)

# The key is CACHEKIT_API_KEY by default; pass it explicitly to hold
# more than one key in a process (multi-tenant services, test suites).
@cache.io(api_key=os.environ["TENANT_B_CACHEKIT_API_KEY"], namespace="tenant-b")
def tenant_b_function(x):
    return expensive_computation(x)
```

`@cache.io()` raises `ConfigurationError` at decoration time if it has no key from either
source, if the key is not an RFC 6750 bearer token, if `CACHEKIT_API_URL` fails validation, or if
you pass `backend=` or `config=` — it always caches through its own `CachekitIOBackend`.
To cache through another backend, use `@cache.production(backend=...)`.

A bearer token carries only `A-Z a-z 0-9 - . _ ~ + /`, then any number of trailing `=`. So cachekit
rejects a key with whitespace (usually a trailing newline from a secrets file), a byte-order mark (from
a file saved on Windows), a control character or a non-ASCII letter. It never strips the key, and the
error never quotes it.

The RORO form `@cache(config=DecoratorConfig.io(api_key=...))` keeps its own key even when
`set_default_backend()` is set: a backend already in `config=` wins over the module default.

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

Direct calls reject six reserved keys, the empty key `""`, `.`, `..`, `health`, `ttl` and
`lock`, with a `PERMANENT` `BackendError` before any request is sent: no URL can carry them
to a stored entry (see [SECURITY.md](../../SECURITY.md#cache-key-path-encoding-cwe-22)). Decorator keys
always contain `:`, so they never hit this.

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

`get_ttl` counts down to eviction, which includes any `stale_ttl` window. A `PATCH` to an entry
already past its freshness returns 409, so `refresh_ttl` returns `False`.

With `refresh_ttl_on_get=True`, an async decorated L2 hit decides from the read's remaining
freshness (`X-CacheKit-Fresh-For`) instead: it sends the refresh when the entry is still fresh
and less than `ttl_refresh_threshold × ttl` of freshness is left. The refresh runs in the
background and adds no round trip to the hit. A stale hit is not refreshed. When the server
sends no usable remaining-freshness value, the decorator falls back to `get_ttl`, also in the
background.

Refreshes are best-effort. On running event loops, at most one refresh per key and 32 per
decorated function run at once; a hit that finds its key's refresh running, or the limit
reached, skips its own, and a later hit retries. A refresh stranded on a stopped event loop
(sync code that calls `loop.run_until_complete` and leaves the loop stopped) gives up its turn
30 seconds after it was scheduled and is cancelled, so a hit on another loop can refresh that
key again. A `PATCH` it had already sent is not recalled: if the server has not answered within
those 30 seconds, the key can get one more.

## Timeout Override

Returns a new instance:

```python notest
backend = CachekitIOBackend()
fast_backend = backend.with_timeout(1.0)  # 1-second timeout variant
```

## Security

The API URL is validated on construction — HTTPS required, credentials in the URL (`user:password@`) rejected, private/internal IP addresses blocked. The default allowlist restricts connections to `api.cachekit.io` and `api.staging.cachekit.io`. Set `CACHEKIT_ALLOW_CUSTOM_HOST=true` to override (testing only).

## Environment Variables

```bash
CACHEKIT_API_KEY=ck_live_...          # API key — or pass api_key= to CachekitIOBackend / @cache.io
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

## Characteristics

- Latency, measured per call as client wall time from a client entering Cloudflare at MEL, against the
  dev environment (2026-10-03, `tests/integration/saas/test_sdk_performance.py`, three runs): an L2
  hit served by the store is p50 42–45ms (n=200 per run), and a miss, GET then SET, is p50 112–116ms
  (n=50 per run). Your numbers depend on where your client enters Cloudflare, the store's region and
  how many reads the edge serves. No p95 is published yet: these samples are too few to claim one.
- Sync and async support on one client: an async method runs its request on a worker thread
  (`asyncio.to_thread`, the event loop's default executor), so it holds no per-loop state, and one
  backend serves `asyncio.run()` per job, Celery tasks, or a loop per thread
- Connection pooling built-in (default: 32 connections): one urllib3 pool per backend configuration and
  process, shared by every thread that calls the backend: a thread pool, every async-decorator L2
  operation and every async backend method, which run on the default executor's threads (at most 32).
  Each request takes its own HTTP/1.1 connection, so threads never share one, and the pool is
  thread-safe with and without the GIL. Each connection costs one TCP and TLS handshake on first use,
  then stays pooled. A request that finds every pooled connection in use opens one more rather than
  waiting for another thread's request to finish; urllib3 closes that connection when it comes back to a
  full pool, and logs a `Connection pool is full, discarding connection` warning. Each such request pays
  a new handshake, so set `connection_pool_size` to at least the number of threads that call one backend
  at once. Backends
  with the same key, URL, timeout and pool size share one pool while any of them is alive, and the pool
  is closed when the last one is released. Create one backend per key and reuse it
- Idle connections stay pooled until the server closes them: urllib3 has no client-side idle expiry.
  Cloudflare documents a 400 s idle close for client connections; on the dev environment, with the
  keepalive probes below running, pooled connections were still reused after idle gaps of 405–600 s
  (2026-10-04, 6 threads, 0 errors and 0 new connections). Either way, urllib3 checks a pooled
  connection for a close before reusing it, so a connection the server closed while it sat idle is
  replaced. A close that races the request itself fails that request, as a miss. Each pooled connection sends TCP keepalive probes after 60 s idle (every 10 s, 3 probes),
  which keeps NAT gateway mappings alive (AWS NAT Gateway drops idle flows at 350 s, Azure at 4 min). If
  a network path does die, the probes find it in about 90 s, and the next request reconnects instead of
  waiting out the timeout. Probes cannot run while a process is suspended (a frozen serverless runtime,
  a stopped container), so the first request after such a pause can still wait out the timeout on a
  dead connection and miss
- Proxy settings in the environment are honoured: `HTTPS_PROXY`, then `ALL_PROXY`, unless `NO_PROXY`
  covers the API host (the macOS and Windows system proxy settings too). A proxy URL with no scheme is
  taken as `http://`, and credentials in it are sent as `Proxy-Authorization` on the `CONNECT`; the
  bearer key only travels inside the TLS tunnel. Proxied connections send keepalive probes too
- Server certificates are verified against the system trust store (OpenSSL's default CA paths, which
  `SSL_CERT_FILE` and `SSL_CERT_DIR` override). cachekit does not ship a CA bundle of its own: on a host
  without one, such as a container image without `ca-certificates`, or a python.org macOS build that has
  not run `Install Certificates.command`, every request fails certificate verification and is a cache
  miss. Install the system bundle, or point `SSL_CERT_FILE` at one (`python -m certifi` prints the
  path of certifi's, if it is installed)
- Fork-safe connections: a forked child (Gunicorn `--preload`, Celery prefork, `multiprocessing` fork, uWSGI)
  opens its own connections on its first request and never reuses its parent's, so a backend built
  before the fork works in every worker. A child runs async calls on an event loop of its own
  (`asyncio.run`), not its parent's loop object. One exception, for a fork made from C that skips Python's
  at-fork hooks (uWSGI without `--py-call-uwsgi-fork-hooks`): if a parent thread was inside `logging`
  at that moment, the child's first request can hang on logging's lock. Run uWSGI with
  `--enable-threads --py-call-uwsgi-fork-hooks` to avoid it, as
  [Forked Processes](../features/l1-invalidation.md#forked-processes) explains;
  [Free-threading](../free-threading.md) gives the detail
- Every request identifies the SDK with a `User-Agent: cachekit-py/<version> urllib3/<version>` header,
  taken from the installed packages, so cachekit.io can attribute traffic to an SDK release
- Distributed locking via server-side Durable Objects
- TTL inspection and in-place refresh supported

---

## Encrypted SaaS Pattern (Zero-Knowledge)

> *cachekit.io is in closed beta — [request access](https://cachekit.io)*

Compose `@cache.secure` with `CachekitIOBackend` for end-to-end zero-knowledge encryption over managed SaaS storage. The backend stores opaque ciphertext values — it never sees plaintext values or your master key. The cache key stays cleartext ([details](../features/zero-knowledge-encryption.md#cleartext-cache-key-accepted-exposure)).

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

    The cachekit.io API sees only encrypted values; the cache key in the URL path is cleartext.
    """
    return fetch_user_from_db(user_id)
```

**Why this matters**:
- `@cache.secure` applies AES-256-GCM client-side encryption before any data leaves the process
- Per-tenant key derivation via HKDF — not a tenancy boundary; see [Multi-Tenant Isolation](../features/zero-knowledge-encryption.md#multi-tenant-isolation)
- The SaaS backend stores whatever bytes arrive and never holds a key to decrypt the values
- With `@cache.secure`: the SaaS holds only ciphertext values, which supports a HIPAA/PCI DSS scope-*reduction* argument — not a guarantee, and the cache key stays cleartext; see [Compliance Implications](../features/zero-knowledge-encryption.md#compliance-implications)
- Without encryption: SaaS stores plaintext, may be in compliance scope
- Pass `backend=` explicitly, as above: `@cache.secure` does not pin the SaaS on its own ([why](../features/zero-knowledge-encryption.md#cachesecure-does-not-pin-a-backend))

**Requirements**:

```bash
CACHEKIT_MASTER_KEY=<hex string, min 32 bytes>  # Never leaves the client
CACHEKIT_API_KEY=ck_live_...
```

See [Zero-Knowledge Encryption](../features/zero-knowledge-encryption.md) for full details on key derivation and serialization format implications.

## See Also

- [Backend Guide](README.md) — Backend comparison and resolution priority
- [Redis Backend](redis.md) — Self-hosted alternative for lower latency
- [Zero-Knowledge Encryption](../features/zero-knowledge-encryption.md) — Client-side encryption details
- [Configuration Guide](../configuration.md) — Full environment variable reference

---

<div align="center">

**[GitHub Issues](https://github.com/cachekit-io/cachekit-py/issues)** · **[Documentation](../README.md)**

</div>
