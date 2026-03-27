<div align="center">

# cachekit Documentation

> **Smart caching that just works — from simple to advanced**

</div>

---

## Quick Navigation

| Audience | Guide |
|:---------|:------|
| Just want to cache data | [Getting Started](getting-started.md) |
| Need API reference | [API Reference](api-reference.md) |
| Solving a problem | [Troubleshooting](troubleshooting.md) |
| Setting up infrastructure | [Configuration](configuration.md) |

---

## Backends

Choose your storage backend:

| Guide | Description |
|:------|:------------|
| [Backend Overview](backends/index.md) | Comparison and selection guide |
| [Redis](backends/redis.md) | Production default, connection pooling |
| [File](backends/file.md) | Local filesystem, zero dependencies |
| [Memcached](backends/memcached.md) | High-throughput, consistent hashing |
| [CachekitIO](backends/cachekitio.md) | Managed SaaS, zero infrastructure |
| [L1-Only (None)](backends/none.md) | In-memory only, no external services |
| [Custom](backends/custom.md) | Implement your own backend |

## Serializers

Choose how data is stored:

| Guide | Description |
|:------|:------------|
| [Serializer Overview](serializers/index.md) | Decision matrix |
| [Default (MessagePack)](serializers/default.md) | General-purpose with LZ4 compression |
| [OrjsonSerializer](serializers/orjson.md) | Fast JSON (2-5x faster) |
| [ArrowSerializer](serializers/arrow.md) | DataFrames (6-23x faster) |
| [Encryption](serializers/encryption.md) | AES-256-GCM wrapper |
| [Pydantic Models](serializers/pydantic.md) | Caching Pydantic objects |
| [Custom](serializers/custom.md) | SerializerProtocol |

## Features

| Feature | Description |
|:--------|:------------|
| [Circuit Breaker](features/circuit-breaker.md) | Automatic failure protection |
| [Adaptive Timeouts](features/adaptive-timeouts.md) | Smart timeout management |
| [Distributed Locking](features/distributed-locking.md) | Prevent thundering herd |
| [Zero-Knowledge Encryption](features/zero-knowledge-encryption.md) | Client-side AES-256-GCM |
| [Prometheus Metrics](features/prometheus-metrics.md) | Production observability |

## Architecture & Reference

| Document | Description |
|:---------|:------------|
| [Data Flow Architecture](data-flow-architecture.md) | L1+L2 dual-layer caching internals |
| [Performance](performance.md) | Benchmarks and optimization |
| [Comparison](comparison.md) | vs. lru\_cache, aiocache, cachetools |
| [Error Codes](error-codes.md) | Error reference |

---

## Documentation Map

```
docs/
├── getting-started.md                # Tutorial (start here)
├── api-reference.md                  # Complete API docs
├── configuration.md                  # Environment setup
├── troubleshooting.md                # Error solutions
│
├── backends/
│   ├── index.md                     # Backend overview
│   ├── redis.md, file.md            # Built-in backends
│   ├── memcached.md, cachekitio.md  # Optional backends
│   └── custom.md                    # Custom backend guide
│
├── serializers/
│   ├── index.md                     # Serializer overview
│   ├── default.md, orjson.md        # Built-in serializers
│   ├── arrow.md, encryption.md      # Specialized serializers
│   ├── pydantic.md                  # Pydantic patterns
│   └── custom.md                    # Custom serializer
│
├── features/                         # Feature deep dives
├── data-flow-architecture.md         # How it works
├── performance.md                    # Benchmarks
└── comparison.md                     # vs. alternatives
```
