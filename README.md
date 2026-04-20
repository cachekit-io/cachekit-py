<div align="center">

# cachekit

[![gitcgr](https://gitcgr.com/badge/cachekit-io/cachekit-py.svg)](https://gitcgr.com/cachekit-io/cachekit-py)

> **Python caching, batteries included**

Production-ready caching for Python with intelligent reliability features and Rust-powered performance.

[![PyPI Version][pypi-badge]][pypi-url]
[![Python Versions][python-badge]][pypi-url]
[![codecov][codecov-badge]][codecov-url]
[![License: MIT][license-badge]][license-url]

</div>

---

> [!WARNING]
> **Alpha Software** — cachekit is under active development. While we've been building and testing for ~6 months, the API is not yet stable and **breaking changes may occur** between releases. We're committed to making this library rock-solid, but we need your help!
>
> 🐛 **Found a bug?** Please [open an issue][issues-url] — even small ones help us improve.
>
> 💡 **Something feel off?** We want to hear about rough edges, confusing APIs, or missing features.
>
> Your feedback directly shapes the path to 1.0. Cheers!

---

## Why cachekit?

**Simple to use, production-ready out of the box.**

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
| **Adaptive timeouts** | Auto-tune to system load |

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

### Setup (Redis — recommended default)

```bash
# Run Redis locally or use your existing infrastructure
export REDIS_URL="redis://localhost:6379"
```

```python
from cachekit import cache

@cache  # Uses Redis backend by default
def expensive_api_call(user_id: int):
    return fetch_user_data(user_id)
```

> [!TIP]
> No Redis? No worries! Use `@cache(backend=None)` for L1-only in-memory caching, like `lru_cache`, but with all the bells and whistles.

### More Backends

<details>
<summary><strong>CachekitIO — Managed SaaS (Alpha)</strong></summary>

```python notest
import os
from cachekit import cache

# Set your CachekitIO API key
# export CACHEKIT_API_KEY="your-api-key"  # pragma: allowlist secret

@cache.io()  # Uses CachekitIO SaaS backend — no Redis to manage
def expensive_api_call(user_id: int):
    return fetch_user_data(user_id)
```

*cachekit.io is in closed alpha — [request access](https://cachekit.io) to get started.*

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

> **CachekitIO Cloud (Alpha)**
> Managed caching with zero infrastructure. L1+L2 caching, circuit breaker, and automatic failover — no Redis to manage.
> *cachekit.io is in closed alpha — [request access](https://cachekit.io) to get started.*

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

# Security-critical: PII, medical, financial
@cache.secure
def get_user_profile(user_id: int):
    return db.fetch_user(user_id)
```

| Feature | `@cache.minimal` | `@cache.production` | `@cache.secure` | `@cache.io()` |
|:--------|:----------------:|:-------------------:|:---------------:|:-------------:|
| Circuit Breaker | - | ✅ | ✅ | ✅ |
| Adaptive Timeouts | - | ✅ | ✅ | ✅ |
| Monitoring | - | ✅ Full | ✅ Full | ✅ Full |
| Integrity Checking | - | ✅ Enabled | ✅ Enforced | ✅ Enabled |
| Encryption | - | - | ✅ Required | - |
| Backend | Redis | Redis | Redis | CachekitIO SaaS |
| **Use Case** | High throughput | Production reliability | Compliance/security | Managed cloud |

<details>
<summary><strong>Additional Presets</strong></summary>

```python
# Development: debugging with verbose output
@cache.dev
def debug_expensive_call():
    return complex_computation()

# Testing: deterministic, no randomness
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
│  L1 Cache (In-Memory)  │  L2 Cache (Pluggable Backend)     │
│       ~50ns            │  Redis / CachekitIO / File /      │
│                        │  Memcached    ~2-50ms             │
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

## Features

### Production Hardened

- Circuit breaker with graceful degradation
- Connection pooling with thread affinity (+28% throughput)
- Distributed locking prevents cache stampedes
- Pluggable backend abstraction (Redis, CachekitIO, File, Memcached, custom)

> [!NOTE]
> All reliability features are **enabled by default** with `@cache.production`. Use `@cache.minimal` to disable them for maximum throughput.

### Smart Serialization

| Serializer | Speed | Use Case |
|:-----------|:-----:|:---------|
| **StandardSerializer** | ★★★★☆ | General Python types, NumPy, Pandas |
| **OrjsonSerializer** | ★★★★★ | JSON APIs (2-5x faster than stdlib) |
| **ArrowSerializer** | ★★★★★ | Large DataFrames (6-23x faster for 10K+ rows) |
| **EncryptionWrapper** | ★★★★☆ | Wraps any serializer with AES-256-GCM |

<details>
<summary><strong>Serializer Examples</strong></summary>

```python
from cachekit.serializers import OrjsonSerializer, ArrowSerializer, EncryptionWrapper

# Fast JSON for API responses
@cache.production(serializer=OrjsonSerializer())
def get_api_response(endpoint: str):
    return {"status": "success", "data": fetch_api(endpoint)}

# Zero-copy DataFrames for large datasets
@cache(serializer=ArrowSerializer())
def get_large_dataset(date: str):
    return pd.read_csv(f"data/{date}.csv")

# Encrypted DataFrames for sensitive data
@cache(serializer=EncryptionWrapper(serializer=ArrowSerializer()))
def get_patient_data(hospital_id: int):
    return pd.read_sql("SELECT * FROM patients WHERE hospital_id = ?", conn, params=[hospital_id])
```

</details>

### Integrity Checking

> [!IMPORTANT]
> All serializers support **configurable checksums** for corruption detection using xxHash3-64 (8 bytes). Enabled by default in `@cache.production` and `@cache.secure`.

**Performance Impact** (benchmark-proven):

| Data Type | Latency Reduction (disabled) | Size Overhead |
|:----------|:----------------------------:|:-------------:|
| MessagePack (default) | 60-90% | 8 bytes |
| Arrow DataFrames | 35-49% | 8 bytes |
| JSON (orjson) | 37-68% | 8 bytes |

### Security

> [!CAUTION]
> When handling PII, medical, or financial data, always use `@cache.secure` to enforce encryption.

cachekit employs comprehensive security tooling:

- **Supply Chain Security**: cargo-deny for license compliance + RustSec scanning
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
| Medium | < 15 min | Unsafe code analysis, API stability, Miri subset |
| Deep | < 2 hours | Formal verification, extended fuzzing, full sanitizers |

See [SECURITY.md][security-url] for vulnerability reporting and detailed documentation.

</details>

### Built-in Monitoring

- **Prometheus metrics** - Production-ready observability
- **Structured logging** - Context-aware with correlation IDs
- **Health checks** - Comprehensive status endpoints
- **Performance tracking** - Built-in latency monitoring

<details>
<summary><strong>Thread Safety Details</strong></summary>

**Per-Function Statistics:**
- Statistics tracked per decorated function (shared across all calls)
- Thread-safe via RLock (all methods safe for concurrent access)

```python
from concurrent.futures import ThreadPoolExecutor

@cache()
def expensive_func(x):
    return x ** 2

# All threads share same stats
with ThreadPoolExecutor(max_workers=10) as executor:
    results = list(executor.map(expensive_func, range(100)))

print(expensive_func.cache_info())
# CacheInfo(hits=90, misses=10, maxsize=None, currsize=10)
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

### Feature Deep Dives

| Feature | Description |
|:--------|:------------|
| [Serializer Guide][serializer-guide-url] | ArrowSerializer vs StandardSerializer benchmarks |
| [Circuit Breaker][circuit-breaker-url] | Prevent cascading failures |
| [Distributed Locking][distributed-locking-url] | Cache stampede prevention |
| [Prometheus Metrics][prometheus-url] | Built-in observability |
| [Zero-Knowledge Encryption][encryption-url] | Client-side security |
| [Adaptive Timeouts][adaptive-timeouts-url] | Auto-tune to infrastructure |

---

## Configuration

### Environment Variables

```bash
# Redis Connection (priority: CACHEKIT_REDIS_URL > REDIS_URL)
CACHEKIT_REDIS_URL="redis://localhost:6379"  # Primary (preferred)
REDIS_URL="redis://localhost:6379"           # Fallback

# CachekitIO SaaS Backend (closed alpha — request access at cachekit.io)
CACHEKIT_API_KEY="your-api-key"             # Required for @cache.io()  # pragma: allowlist secret
CACHEKIT_API_URL="https://api.cachekit.io"  # Default SaaS endpoint

# Memcached Backend (optional: pip install cachekit[memcached])
CACHEKIT_MEMCACHED_SERVERS='["mc1:11211", "mc2:11211"]'  # Default: 127.0.0.1:11211
CACHEKIT_MEMCACHED_CONNECT_TIMEOUT=2.0                   # Default: 2.0 seconds
CACHEKIT_MEMCACHED_TIMEOUT=1.0                            # Default: 1.0 seconds
CACHEKIT_MEMCACHED_KEY_PREFIX="myapp:"                    # Default: "" (none)

# Optional Configuration
CACHEKIT_DEFAULT_TTL=3600
CACHEKIT_MAX_CHUNK_SIZE_MB=100
CACHEKIT_ENABLE_COMPRESSION=true
```

> [!NOTE]
> If both `CACHEKIT_REDIS_URL` and `REDIS_URL` are set, `CACHEKIT_REDIS_URL` takes precedence.

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
| Python | 3.9+ |

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
[getting-started-url]: docs/getting-started.md
[api-reference-url]: docs/api-reference.md
[serializer-guide-url]: docs/serializers/index.md
[circuit-breaker-url]: docs/features/circuit-breaker.md
[distributed-locking-url]: docs/features/distributed-locking.md
[prometheus-url]: docs/features/prometheus-metrics.md
[encryption-url]: docs/features/zero-knowledge-encryption.md
[adaptive-timeouts-url]: docs/features/adaptive-timeouts.md
[contributing-url]: CONTRIBUTING.md
[license-file-url]: LICENSE
[github-url]: https://github.com/cachekit-io/cachekit-py
[issues-url]: https://github.com/cachekit-io/cachekit-py/issues
[codecov-badge]: https://codecov.io/github/cachekit-io/cachekit-py/graph/badge.svg
[codecov-url]: https://codecov.io/github/cachekit-io/cachekit-py
