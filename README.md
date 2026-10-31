<div align="center">

# cachekit

> **Python caching, batteries included**

Backend-agnostic caching with intent-based decorators — circuit breaker, distributed locking, Prometheus metrics, and optional zero-knowledge encryption on a Rust-powered core. Zero config to start, any backend when you scale.

[![PyPI Version][pypi-badge]][pypi-url]
[![Python Versions][python-badge]][pypi-url]
[![codecov][codecov-badge]][codecov-url]
[![License: MIT][license-badge]][license-url]

</div>

---

> [!NOTE]
> **Status: beta** — CacheKit is in closed beta ahead of 1.0. APIs are stabilising; minor breaking changes may still occur between 0.x releases.
>
> 🐛 **Found a bug or a rough edge?** Please [open an issue][issues-url] — your feedback directly shapes the path to 1.0.

---

## Why cachekit?

**Simple to use, works out of the box.**

```python
from cachekit import cache

@cache
def expensive_function():
    return fetch_data()
```

That's it. You get:

| Feature | Description |
|:--------|:------------|
| **Circuit breaker** | Prevents cascading failures |
| **Distributed locking** | Multi-pod safety |
| **Prometheus metrics** | Built-in observability |
| **MessagePack serialization** | Efficient with optional compression |
| **Zero-knowledge encryption** | Client-side AES-256-GCM |

---

## Quick Start

### Installation

```bash
pip install cachekit
```

Or with [uv][uv-url] (recommended):

```bash
uv add cachekit
```

### Setup

With nothing configured, `@cache` caches in L1, in process, like `lru_cache`: no backend,
no connection attempt, no warning.

```python
from cachekit import cache

@cache  # nothing configured: L1-only, per process
def expensive_api_call(user_id: int):
    return fetch_user_data(user_id)
```

An L1-only cache is not shared between processes, holds values by reference, and is chosen
at the function's first call, so configure a backend before then.

### Choose a backend

cachekit exposes one decorator API over a pluggable backend abstraction. Pick the
backend that fits your infrastructure — they're peers behind the same `@cache` API:

| Backend | Best for | Select with |
|---------|----------|-------------|
| Redis | Self-hosted, full control | `REDIS_URL` / `CACHEKIT_REDIS_URL` |
| CachekitIO | Managed, zero-ops (beta) | `CACHEKIT_API_KEY` |
| Memcached | High-throughput, existing infra | `CACHEKIT_MEMCACHED_SERVERS` |
| File / L1-only | Local dev, tests, no external deps | `CACHEKIT_FILE_CACHE_DIR` / nothing (or `backend=None`) |

```bash
# Run Redis locally or use your existing infrastructure
export REDIS_URL="redis://localhost:6379"
```

```python
from cachekit import cache

@cache  # REDIS_URL set: L1 + Redis, shared across processes
def expensive_api_call(user_id: int):
    return fetch_user_data(user_id)
```

> [!WARNING]
> Earlier releases connected to `redis://localhost:6379` when nothing was configured. To keep
> that Redis, set `REDIS_URL=redis://localhost:6379` (or `CACHEKIT_REDIS_URL`). `@cache.secure`,
> `encryption=True` and `interop=` still need a backend and still fall back to localhost Redis;
> see [Backend Resolution Priority](docs/backends/README.md#backend-resolution-priority).

### More Backends

<details>
<summary><strong>CachekitIO — Managed SaaS (Beta)</strong></summary>

```python notest
import os
from cachekit import cache

# Set your CachekitIO API key
# export CACHEKIT_API_KEY="your-api-key"  # pragma: allowlist secret

@cache.io()  # Uses CachekitIO SaaS backend — no Redis to manage
def expensive_api_call(user_id: int):
    return fetch_user_data(user_id)
```

*cachekit.io is in closed beta — [request access](https://cachekit.io) to get started.*

How fresh a cachekit.io read is, and how long a deleted project's data stays readable: [Consistency and Deletion](https://docs.cachekit.io/concepts/consistency/).

</details>

<details>
<summary><strong>Memcached — Optional</strong></summary>

```python notest
from cachekit import cache
from cachekit.backends.memcached import MemcachedBackend

# pip install cachekit[memcached]

backend = MemcachedBackend()  # Defaults to 127.0.0.1:11211

@cache(backend=backend)
def expensive_api_call(user_id: int):
    return fetch_user_data(user_id)
```

*Requires: `pip install cachekit[memcached]` or `uv add cachekit[memcached]`*

</details>

---

> **CachekitIO Cloud (Beta)**
> Managed caching with zero infrastructure. L1+L2 caching, circuit breaker, and automatic failover — no Redis to manage.
> *cachekit.io is in closed beta — [request access](https://cachekit.io) to get started.*

---

## Intent-Based Optimization

cachekit provides **preset configurations** for different use cases:

```python
# Speed-critical: trading, gaming, real-time
@cache.minimal
def get_price(symbol: str):
    return fetch_price(symbol)

# Reliability-critical: payments, APIs
@cache.production
def process_payment(amount):
    return payment_gateway.charge(amount)

# Security-critical: PII, medical, financial (needs a key: master_key= or CACHEKIT_MASTER_KEY)
@cache.secure(master_key=secret_key)
def get_user_profile(user_id: int):
    return db.fetch_user(user_id)
```

Setting `CACHEKIT_MASTER_KEY` instead of passing `master_key=` means every preset except
`@cache.secure` and `@cache.local` must state its intent (`encryption=False` for plaintext), or it raises
`ConfigurationError` at decoration: the key is a key source, never an on switch.

| Feature | `@cache.minimal` | `@cache.dev` | `@cache.test` | `@cache.production` | `@cache.secure` |
|:--------|:----------------:|:------------:|:-------------:|:-------------------:|:---------------:|
| Default TTL | 300 s | 300 s | 300 s | 600 s | 600 s |
| Circuit Breaker | - | ✅ | - | ✅ | ✅ |
| Backpressure | ✅ | ✅ | - | ✅ | ✅ |
| Integrity Checking | - | ✅ | - | ✅ | ✅ 🔒 |
| Encryption | - | - | - | - | ✅ Required |
| L1 SWR (L1-only mode) | - | ✅ | - | ✅ | - |
| Prometheus Metrics¹ | - | ✅ | - | ✅ | ✅ |
| Structured Logging | - | ✅ | - | ✅ | ✅ |
| Backend unreachable² | Function runs, no error | Function runs, no error | Function runs, no error | Function runs, no error | Function runs, no error |
| **Use Case** | High throughput | Local debugging | Deterministic tests | Production reliability | Compliance/security |

> ¹ A preset with a ✅ records `get` and `set` operations only when it has a backend: in L1-only mode it records none. `circuit_breaker_state` is recorded for every function whose circuit breaker is enabled. See [Which Decorators Record](docs/features/prometheus-metrics.md#which-decorators-record).
>
> ² A backend that is down or unreachable never raises to the caller, on any preset (`@cache.io` included): the call logs the failure and runs your function. What gets cached meanwhile is in [Connection Errors](docs/error-codes.md#connection-errors).
>
> 🔒 `@cache.secure` forces `integrity_checking=True` — passing `integrity_checking=False` raises `ConfigurationError` at decoration, including as an override next to `config=DecoratorConfig.secure(...)`. Its encryption is fixed the same way: `encryption=` raises `ConfigurationError` on `@cache.secure` and next to `config=DecoratorConfig.secure(...)`, so pass `fail_closed=` and the other encryption options to the preset itself. `@cache.secure` also rejects `config=`; the RORO form is `@cache(config=DecoratorConfig.secure(...))`.
>
> **Default TTL** follows the cross-SDK [intent-preset spec](https://github.com/cachekit-io/protocol/blob/main/spec/intent-presets.md#default-ttl) (`@cache.io` 3600 s) — the same numbers as cachekit-rs and cachekit-ts. `ttl=` overrides it; `ttl=None` is the explicit never-expire opt-in ([details](docs/configuration.md#intent-presets)).
>
> **Cross-process L1 eviction** is not a preset feature: every preset's `invalidate_cache()` deletes from L1 and L2 alike, and evicting other processes' L1 copies is one process-wide opt-in, `CACHEKIT_INVALIDATION_LISTENER_ENABLED` ([L1 invalidation](docs/features/l1-invalidation.md#cross-process-l1-eviction)).
>
> **L1 SWR** (within-TTL background refresh) runs only in L1-only mode (`backend=None`) — with a backend configured it has no effect. `@cache.io` additionally ships past-TTL SWR via `stale_ttl` ([docs](docs/configuration.md#stale-while-revalidate-stale_ttl)).
>
> **`@cache.io()`** mirrors `@cache.production` (full reliability + observability) but routes to the managed CachekitIO SaaS backend instead of Redis. **`@cache.local()`** is a Python-only, SDK-local preset: a separate in-process path backed by `ObjectCache` (raw object references, entry-count LRU, no serialization) — the reliability and encryption features listed above do not apply to it.

<details>
<summary><strong>Additional Presets: <code>@cache.dev</code> and <code>@cache.test</code></strong></summary>

`@cache.dev` and `@cache.test` are Python-only, SDK-local presets. See the comparison table above for the exact feature set of each preset.

```python
# Development: verbose logging, integrity checks on
@cache.dev
def debug_expensive_call():
    return complex_computation()

# Testing: deterministic, all protections off (no circuit breaker, no backpressure)
@cache.test
def test_cached_function():
    return fixed_test_value()
```

</details>

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                        Application                          │
├─────────────────────────────────────────────────────────────┤
│                     @cache Decorator                        │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────────────┐  │
│  │   Circuit   │  │  Adaptive   │  │    Distributed      │  │
│  │   Breaker   │  │  Timeouts   │  │      Locking        │  │
│  └─────────────┘  └─────────────┘  └─────────────────────┘  │
├─────────────────────────────────────────────────────────────┤
│  L1 Cache (In-Memory)  │  L2 Cache (Pluggable Backend)      │
│       in-process       │  Redis / CachekitIO / File /       │
│                        │  Memcached: one backend round trip │
├─────────────────────────────────────────────────────────────┤
│                    Rust Core (PyO3)                         │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────────────┐  │
│  │    LZ4      │  │  xxHash3    │  │    AES-256-GCM      │  │
│  │ Compression │  │  Checksums  │  │    Encryption       │  │
│  └─────────────┘  └─────────────┘  └─────────────────────┘  │
└─────────────────────────────────────────────────────────────┘
```

> [!TIP]
> **Building in Rust?** The core compression, checksums, and encryption are available as a standalone crate: [`cachekit-core`](https://crates.io/crates/cachekit-core) [![Crates.io](https://img.shields.io/crates/v/cachekit-core.svg)](https://crates.io/crates/cachekit-core)

---

## Performance

What a call costs inside your process, from the wall-clock guards in `tests/performance/` run on a GitHub-hosted `ubuntu-latest` runner (AMD EPYC 7763, 4 vCPUs) with CPython 3.14.7. Each figure is the mean of five run medians with its 95% band.

| Path | Time per call |
|:-----|--------------:|
| `@cache(backend=None)` L1 hit, 100-user nested dict (23.5 KB as MessagePack) | 0.341 ± 0.001 µs |
| `@cache(backend=None)` L1 hit, 10K-row DataFrame | 0.34 ± 0.00 µs |
| L1 hit with an L2 backend configured, same dict | 284 ± 2 µs |
| Redis L2 hit with L1 disabled, same dict, Redis in a container on the same machine | 539 ± 3 µs |

- With no backend, L1 keeps the returned object itself, whatever the payload's size, and a repeat call with a few short scalar arguments is answered by one call into the Rust extension that takes no lock ([which calls](https://github.com/cachekit-io/cachekit-py/blob/main/docs/performance.md#decorator--l1-hit-hot-path)). Any other call builds its key every time, as does every call to a function with a backend.
- With an L2 backend, L1 holds the serialized bytes and every hit deserializes them: for this dict, about 910 times the L1-only hit. This run predates hits skipping the structure walk over bytes they already checked, which took about 20% off this hit. Encryption adds a decrypt to each such hit and keeps the walk a plaintext hit now skips; before that skip, this run measured the difference at 9.3 ± 1.0 µs.
- An L2 hit adds the backend's round trip. A Redis on another machine adds its network latency to the last row.

Hosted runners come with other CPU models too: in a run of an earlier commit, an EPYC 9V45 runner took about half as long on every row, and other machines give other figures. The [Performance Guide](https://github.com/cachekit-io/cachekit-py/blob/main/docs/performance.md#github-hosted-runner) names the guard behind each figure and how to run it, and the repository's `Bench` workflow repeats the run.

---

## Features

### Production Hardened

- Circuit breaker with graceful degradation
- Connection pooling with thread affinity
- CachekitIO keeps idle connections pooled until the server closes them, with TCP keepalive probes, so a request after a pause skips a new TLS handshake
- Distributed locking prevents cache stampedes
- Pluggable backend abstraction (Redis, CachekitIO, File, Memcached, custom)
- Untrusted-decode bounds: nesting depth and header-declared allocation are capped on every cache read (a forged entry is a bounded cache miss), verified against the protocol's shared [`decode-bounds.json`](https://github.com/cachekit-io/protocol/blob/42ea601f3c1656df0a06904afa2406604f43f57d/test-vectors/decode-bounds.json) vectors

> [!NOTE]
> All reliability features are **enabled by default** with `@cache.production`. Use `@cache.minimal` to disable them for maximum throughput.

### Smart Serialization

| Serializer | Speed | Use Case |
|:-----------|:-----:|:---------|
| **StandardSerializer** | ★★★★☆ | General Python types; not NumPy arrays or pandas objects |
| **OrjsonSerializer** | ★★★★★ | JSON APIs — requires `cachekit[json]` |
| **ArrowSerializer** | — | pandas DataFrames (columnar, exact round trip) |
| **+ Encryption** | ★★★★☆ | Any serializer above, AES-256-GCM encrypted: `@cache.secure(master_key=..., serializer=...)` |

<details>
<summary><strong>Serializer Examples</strong></summary>

```python
from cachekit.serializers import OrjsonSerializer, ArrowSerializer

# Fast JSON for API responses
@cache.production(serializer=OrjsonSerializer())
def get_api_response(endpoint: str):
    return {"status": "success", "data": fetch_api(endpoint)}

# Columnar DataFrames (Arrow IPC, zstd-compressed by default)
@cache(serializer=ArrowSerializer())
def get_large_dataset(date: str):
    return pd.read_csv(f"data/{date}.csv")
```

Encrypted DataFrames go through `@cache.secure`, which takes any serializer whose class declares `cross_sdk_compatible = True` ([details](docs/serializers/encryption.md#composability)). A file backend keeps this example self-contained; production uses Redis or cachekit.io:

```python
import tempfile
from cachekit.backends.file import FileBackend, FileBackendConfig
from cachekit.serializers import ArrowSerializer

calls = 0

@cache.secure(
    master_key=secret_key,
    serializer=ArrowSerializer(),
    backend=FileBackend(FileBackendConfig(cache_dir=tempfile.mkdtemp())),
)
def get_patient_data(hospital_id: int):
    global calls
    calls += 1
    return pd.DataFrame({"hospital_id": [hospital_id], "patients": [42]})

get_patient_data(7)
get_patient_data(7)  # second call is served from the encrypted cache
assert calls == 1
```

</details>

### Integrity Checking

> [!IMPORTANT]
> Serializers detect corrupted entries with an xxHash3-64 checksum. It is on by default. `@cache.minimal` and `@cache.test` turn it off where the serializer allows it (`ArrowSerializer` always checks); `@cache.secure` refuses to turn it off.

Each checksum is an 8-byte digest, plus one pass over the payload on each write and read. What `enable_integrity_checking=False` changes depends on the serializer, and for `AutoSerializer` on the value:

| Serializer | Checking on (default) | Checking off |
|:-----------|:----------------------|:-------------|
| `StandardSerializer`, and `AutoSerializer` for values other than the two below | MessagePack in an LZ4 envelope that carries the checksum | Plain MessagePack: no checksum, no LZ4 |
| `AutoSerializer`, NumPy arrays | The checksum, then the raw array bytes (never compressed) | The raw array bytes |
| `AutoSerializer`, pandas DataFrames with pyarrow installed, and `ArrowSerializer` | The checksum, then the Arrow IPC data | No change: Arrow always checks |
| `OrjsonSerializer` | The checksum, then the JSON | Plain JSON |

Where the switch drops the LZ4 envelope, an entry gets larger only when LZ4 saved more than the envelope's overhead of about 25 to 30 bytes; otherwise it stays the same size or gets smaller. Measure on your own payloads before you trade that.

### Security

> [!CAUTION]
> When handling PII, medical, or financial data, always use `@cache.secure` to enforce encryption.

**Key rotation**: keep a retiring key readable with
`CACHEKIT_PREVIOUS_MASTER_KEYS` (comma-separated hex, max 3 decrypt-only keys)
while new writes use `CACHEKIT_MASTER_KEY`. A one-deploy key swap is not
zero-miss; follow the [key rotation
runbook](https://docs.cachekit.io/concepts/key-rotation/), including its Before
You Rotate checks. CK-framed entries are selected by exact key fingerprint —
never trial decryption; Interop-mode entries carry no CK frame and attempt
keyring keys sequentially instead.

cachekit employs comprehensive security tooling:

- **Dependency Security**: cargo-deny for license compliance + cargo-audit for RustSec scanning
- **Formal Verification**: Kani proves correctness of compression, checksums, encryption
- **Runtime Analysis**: Miri + sanitizers for memory safety
- **Fuzzing**: Coverage-guided testing with >80% code coverage
- **Zero CVEs**: Continuous vulnerability scanning

<details>
<summary><strong>Security Commands</strong></summary>

```bash
make security-install  # Install security tools (one-time)
make security-fast     # Run fast checks (< 3 min)
```

**Security Tiers:**

| Tier | Time | Coverage |
|:-----|:----:|:---------|
| Fast | < 3 min | Vulnerability scanning, license checks, linting |
| Medium | < 15 min | Unsafe code analysis (cargo-geiger) |
| Deep | < 2 hours | Formal verification, extended fuzzing, full sanitizers |

See [SECURITY.md][security-url] for vulnerability reporting and detailed documentation.

</details>

### Monitoring & Observability

- **Per-function statistics** - `cache_info()` on every decorated function, modelled on `functools.lru_cache`
- **Prometheus metrics** - Recorded by default by a decorator with a backend (your app owns exposition)
- **Structured logging** - Context-aware, per-operation fields
- **Health checks** - Comprehensive status endpoints

Every decorated function exposes `cache_info()`, returning a `CacheInfo` named tuple with
hit/miss counts, the L1/L2 split, and average backend latency:

```python
@cache()
def get_score(x):
    return x ** 2

get_score(2)
get_score(2)  # served from cache

info = get_score.cache_info()
# CacheInfo has 9 fields: hits, misses, l1_hits, l2_hits, maxsize,
# currsize, l2_avg_latency_ms, last_operation_at, session_id
assert (info.hits, info.misses) == (1, 1)  # the second call was a hit
assert info.l1_hits + info.l2_hits == info.hits  # every hit is L1 or L2
```

`maxsize` and `currsize` are always `None` (the cache lives in an external store, not a
bounded in-process dict); they exist only for `lru_cache` API parity. See the
[API Reference](docs/api-reference.md#per-function-statistics-via-cache_info) for the full
field reference and a sample stats endpoint, and the
[Prometheus Metrics guide](docs/features/prometheus-metrics.md) for metric names and
exposition setup.

<details>
<summary><strong>Thread Safety Details</strong></summary>

**Free-threaded CPython (3.15t, 3.14t):** the core suites run green on
free-threaded 3.14 and 3.15 with the GIL verified disabled (CI job
`test-freethreaded`), and the Rust extension declares free-threaded safety
(`gil_used = false`). **From the release after 0.24.1, 3.15t has a wheel**:
the 3.15+ wheel is `abi3t`, so it installs on 3.15t without a Rust toolchain
(pip 26.1+ or uv 0.11.3+). **3.14t is tested
but has no wheel** (it builds from source). Free-threaded builds are not
officially supported yet — blocked upstream on
orjson (no free-threaded wheels) and hiredis (re-enables the GIL on import;
cachekit loads redis-py only for its Redis backend, and keeps hiredis out there,
so redis-py uses its pure-Python parser). See
[measured performance results](docs/free-threading.md#measured-performance) and the
full concurrency audit: [docs/free-threading.md](docs/free-threading.md).

**Per-Function Statistics:**
- Statistics tracked per function identity (`module.qualname`), shared across all calls and across re-decorations of the same function
- Thread-safe: every count is exact (L1 hits on an atomic counter, everything else under an RLock), and all methods are safe for concurrent access
- Fork-safe: a forked child starts with zeroed counters and its own session ID

```python
from concurrent.futures import ThreadPoolExecutor

@cache()
def expensive_func(x):
    return x ** 2

# All threads share same stats
with ThreadPoolExecutor(max_workers=10) as executor:
    results = list(executor.map(expensive_func, range(100)))

info = expensive_func.cache_info()
# CacheInfo(hits=..., misses=..., l1_hits=..., l2_hits=...,
#           maxsize=None, currsize=None, l2_avg_latency_ms=...,
#           last_operation_at=..., session_id=...)
```

</details>

---

## Documentation

### Start Here

| Guide | Description |
|:------|:------------|
| [Comparison Guide][comparison-url] | How cachekit compares to lru_cache, aiocache, cachetools |
| [Getting Started][getting-started-url] | Progressive tutorial from basics to advanced |
| [API Reference][api-reference-url] | Complete API documentation |
| [Skyline (live example)][skyline-url] | Canonical example project: this SDK ingests the Bluesky firehose and writes the analytics entries a TypeScript edge Worker serves live, on one shared interop namespace |

### Feature Deep Dives

| Feature | Description |
|:--------|:------------|
| [Serializer Guide][serializer-guide-url] | ArrowSerializer vs StandardSerializer benchmarks |
| [Circuit Breaker][circuit-breaker-url] | Prevent cascading failures |
| [Distributed Locking][distributed-locking-url] | Cache stampede prevention |
| [Prometheus Metrics][prometheus-url] | Built-in observability |
| [Zero-Knowledge Encryption][encryption-url] | Client-side security |
| [Interop Mode][interop-url] | Cross-SDK cache sharing with cachekit-ts/rs |
| [L1 Invalidation & SWR][l1-invalidation-url] | Invalidation scope (incl. cross-process whole-function on tenant-scoped Redis from the environment or `RedisBackendProvider`), opt-in cross-process L1 eviction, stale-while-revalidate |
| [Reference Caching][reference-caching-url] | `@cache.local()` for non-serializable objects |
| [Rust Serialization][rust-serialization-url] | ByteStorage layer: LZ4, xxHash3, AES-256-GCM |
| [SSRF Protection][ssrf-url] | URL allowlisting for the CachekitIO backend |
| [Write-Behind][write-behind-url] | Opt-in `write_behind=True`: a miss returns before its L2 write lands |

---

## Configuration

### Environment Variables

```bash
# Backend selection: set exactly ONE of CACHEKIT_REDIS_URL, CACHEKIT_API_KEY,
# CACHEKIT_MEMCACHED_SERVERS, CACHEKIT_FILE_CACHE_DIR. Two or more is a ConfigurationError at
# first call, and decorators relying on env auto-detection run uncached (docs/backends/README.md).
# REDIS_URL is only a fallback and never conflicts. With none of them set, decorators cache
# L1-only, in process (encrypted and interop decorators still use localhost Redis).

# Redis Connection (priority: CACHEKIT_REDIS_URL > REDIS_URL)
CACHEKIT_REDIS_URL="redis://localhost:6379"  # Primary (preferred)
REDIS_URL="redis://localhost:6379"           # Fallback

# CachekitIO SaaS Backend (closed beta — request access at cachekit.io)
# For @cache.io() next to Redis, pass api_key= from your secret store instead.
# CACHEKIT_API_KEY="your-api-key"  # pragma: allowlist secret
CACHEKIT_API_URL="https://api.cachekit.io"  # Default SaaS endpoint

# Memcached Backend (optional: pip install cachekit[memcached])
# CACHEKIT_MEMCACHED_SERVERS='["mc1:11211", "mc2:11211"]'  # Default: 127.0.0.1:11211
CACHEKIT_MEMCACHED_CONNECT_TIMEOUT=2.0                   # Default: 2.0 seconds
CACHEKIT_MEMCACHED_TIMEOUT=1.0                            # Default: 1.0 seconds
CACHEKIT_MEMCACHED_KEY_PREFIX="myapp:"                    # Default: "" (none)

# Optional Configuration
CACHEKIT_MAX_VALUE_SIZE=104857600
CACHEKIT_ARROW_COMPRESSION=zstd
```

---

## Development

```bash
git clone https://github.com/cachekit-io/cachekit-py.git
cd cachekit-py
uv sync && make install
make quick-check  # format + lint + critical tests
```

See [CONTRIBUTING.md][contributing-url] for full development guidelines.

---

## Requirements

| Component | Version |
|:----------|:--------|
| Python | 3.11+ |

---

## License

MIT License - see [LICENSE][license-file-url] for details.

---

<div align="center">

**[PyPI][pypi-url]** · **[GitHub][github-url]** · **[Issues][issues-url]**

</div>

<!-- Reference Links -->
[pypi-badge]: https://img.shields.io/pypi/v/cachekit.svg
[python-badge]: https://img.shields.io/pypi/pyversions/cachekit.svg
[license-badge]: https://img.shields.io/badge/License-MIT-yellow.svg
[pypi-url]: https://pypi.org/project/cachekit/
[license-url]: https://opensource.org/licenses/MIT
[uv-url]: https://github.com/astral-sh/uv
[security-url]: SECURITY.md
[comparison-url]: docs/comparison.md
[skyline-url]: https://github.com/cachekit-io/bluesky-thinking
[getting-started-url]: docs/getting-started.md
[api-reference-url]: docs/api-reference.md
[serializer-guide-url]: docs/serializers/index.md
[circuit-breaker-url]: docs/features/circuit-breaker.md
[distributed-locking-url]: docs/features/distributed-locking.md
[prometheus-url]: docs/features/prometheus-metrics.md
[encryption-url]: docs/features/zero-knowledge-encryption.md
[interop-url]: docs/features/interop-mode.md
[l1-invalidation-url]: docs/features/l1-invalidation.md
[reference-caching-url]: docs/features/reference-caching.md
[rust-serialization-url]: docs/features/rust-serialization.md
[ssrf-url]: docs/features/ssrf-protection.md
[write-behind-url]: docs/features/write-behind.md
[contributing-url]: CONTRIBUTING.md
[license-file-url]: LICENSE
[github-url]: https://github.com/cachekit-io/cachekit-py
[issues-url]: https://github.com/cachekit-io/cachekit-py/issues
[codecov-badge]: https://codecov.io/github/cachekit-io/cachekit-py/graph/badge.svg
[codecov-url]: https://codecov.io/github/cachekit-io/cachekit-py
