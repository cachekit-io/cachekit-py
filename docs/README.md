<div align="center">

# cachekit Documentation

> **Smart caching that just works - from simple to advanced**

**Documentation Hub** - All learning paths begin here

</div>

---

## Quick Navigation

| Audience | Time | Guide |
|:---------|:----:|:------|
| Just want to cache data | 5 min | [Quick Start](QUICK_START.md) |
| Want to understand caching | 30 min | [Getting Started](getting-started.md) |
| Need API reference | - | [API Reference](api-reference.md) |
| Solving a problem | - | [Troubleshooting](troubleshooting.md) |
| Setting up infrastructure | - | [Configuration](configuration.md) |

---

## Just Want to Cache Data?

```python
from cachekit import cache

@cache
def expensive_function():
    return compute()
```

> [!TIP]
> That's it! See [Quick Start](QUICK_START.md) for the full 5-minute guide.

---

## Learning Paths

### Getting Started Guide

Progressive disclosure of cachekit features:

| Level | Topic | What You Learn |
|:-----:|:------|:---------------|
| 1 | Zero-config caching | Basic `@cache` decorator |
| 2 | TTL and namespaces | Cache organization |
| 3 | Serializers | Choosing the right format |
| 4 | Advanced control | Full configuration |

**[Start the Guide](getting-started.md)**

---

## Feature Guides

### Caching Strategies

| Guide | Description |
|:------|:------------|
| [Serializer Guide](guides/serializer-guide.md) | Choose the right serializer for your data |
| [Backend Guide](guides/backend-guide.md) | Custom storage backends (Redis, HTTP, DynamoDB) |

<details>
<summary><strong>Serializer Options</strong></summary>

| Serializer | Best For |
|:-----------|:---------|
| **StandardSerializer** (default) | Multi-language compatible (Python/PHP/JS/Java) |
| **AutoSerializer** | Python-only with NumPy/pandas support |
| **OrjsonSerializer** | JSON APIs (2-5x faster) |
| **ArrowSerializer** | DataFrames (6-23x faster for 10K+ rows) |

</details>

### Reliability Features

| Feature | Description |
|:--------|:------------|
| [Circuit Breaker](features/circuit-breaker.md) | Automatic failure protection |
| [Adaptive Timeouts](features/adaptive-timeouts.md) | Smart timeout management |
| [Distributed Locking](features/distributed-locking.md) | Prevent thundering herd |

### Security & Privacy

| Feature | Description |
|:--------|:------------|
| [Zero-Knowledge Encryption](features/zero-knowledge-encryption.md) | Client-side AES-256-GCM |
| [Security Policy](../SECURITY.md) | Vulnerability reporting |

> [!CAUTION]
> For PII, health info, or financial data, use `@cache.secure` to enforce encryption. See [Zero-Knowledge Encryption](features/zero-knowledge-encryption.md).

### Monitoring

| Feature | Description |
|:--------|:------------|
| [Prometheus Metrics](features/prometheus-metrics.md) | Production observability |

---

## Architecture & Design

| Document | Description |
|:---------|:------------|
| [Data Flow Architecture](data-flow-architecture.md) | L1+L2 dual-layer caching internals |
| [Performance](performance.md) | Benchmarks and optimization |
| [Comparison](comparison.md) | vs. lru_cache, aiocache, etc. |

---

## Common Tasks

<details>
<summary><strong>Cache JSON API responses</strong></summary>

1. Read [Quick Start](QUICK_START.md) - basic usage
2. Check [Serializer Guide](guides/serializer-guide.md) - OrjsonSerializer for JSON

</details>

<details>
<summary><strong>Encrypt sensitive data (PII, health info)</strong></summary>

1. Read [Configuration Guide](configuration.md) - set `CACHEKIT_MASTER_KEY`
2. Read [Zero-Knowledge Encryption](features/zero-knowledge-encryption.md) - `@cache.secure`

</details>

<details>
<summary><strong>Cache DataFrames or ML features</strong></summary>

1. Read [Quick Start](QUICK_START.md) - basic setup
2. Check [Serializer Guide](guides/serializer-guide.md) - ArrowSerializer

</details>

<details>
<summary><strong>Debug caching issues</strong></summary>

1. Check [Troubleshooting Guide](troubleshooting.md) - find your error
2. See [Configuration Guide](configuration.md) - verify environment setup
3. Review [API Reference](api-reference.md) - check decorator parameters

</details>

<details>
<summary><strong>Monitor cache in production</strong></summary>

1. Set up [Prometheus Metrics](features/prometheus-metrics.md) - hit/miss rates
2. Use [Troubleshooting Guide](troubleshooting.md) - health check patterns

</details>

<details>
<summary><strong>Build a custom backend</strong></summary>

1. Read [Backend Guide](guides/backend-guide.md) - protocol and examples
2. Review [Data Flow Architecture](data-flow-architecture.md) - understand integration points

</details>

---

## Quick Reference

### Installation & Setup

```bash
pip install cachekit
export REDIS_URL=redis://localhost:6379/0
```

### Basic Usage

```python
from cachekit import cache

@cache(ttl=3600)
def my_function():
    return expensive_operation()
```

### With Encryption

```python notest
@cache.secure(ttl=3600, master_key="a" * 64, backend=None)
def sensitive_operation():
    return sensitive_data()  # illustrative - not defined
```

### Custom Backend

```python notest
from cachekit import cache

backend = CustomBackend()  # illustrative - CustomBackend not defined

@cache(backend=backend)
def cached_function():
    return data()  # illustrative - not defined
```

---

## Documentation Map

```
docs/
├── README.md                         # You are here
├── QUICK_START.md                    # 5-minute start
├── getting-started.md                # 30-minute tutorial
├── api-reference.md                  # Complete API docs
├── configuration.md                  # Environment setup
├── troubleshooting.md                # Error solutions
├── data-flow-architecture.md         # How it works
├── performance.md                    # Benchmarks
├── comparison.md                     # vs. alternatives
│
├── features/
│   ├── circuit-breaker.md            # Failure protection
│   ├── adaptive-timeouts.md          # Timeout tuning
│   ├── distributed-locking.md        # Thundering herd
│   ├── zero-knowledge-encryption.md  # Client encryption
│   ├── prometheus-metrics.md         # Monitoring
│   └── rust-serialization.md         # Rust integration
│
└── guides/
    ├── serializer-guide.md           # Choose serializer
    └── backend-guide.md              # Custom backends
```

---

<div align="center">

**[GitHub Issues](https://github.com/cachekit-io/cachekit-py/issues)** · **[Discussions](https://github.com/cachekit-io/cachekit-py/discussions)** · **[Security](../SECURITY.md)**

*Last Updated: 2025-12-02*

</div>
