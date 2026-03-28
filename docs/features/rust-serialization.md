**[Home](../README.md)** › **Features** › **Rust Serialization**

# Rust Serialization (ByteStorage)

**Available since v0.3.0**

> [!NOTE]
> This page describes the Rust-powered ByteStorage layer. For the pluggable serializer system (MessagePack, Arrow, Orjson, Pydantic), see **[Serializers](../serializers/README.md)**.

---

## What the Rust Layer Does

CacheKit includes a Rust extension (`ByteStorage`) that handles the low-level binary operations between the Python serializer and the storage backend:

```
Python object
    ↓
Serializer (MessagePack / Arrow / Orjson — your choice)
    ↓  [Rust ByteStorage takes over here]
LZ4 compression  (fast, ~500MB/s)
    ↓
Blake3 integrity hash  (~GB/s, detects corruption)
    ↓
[Optional] AES-256-GCM encryption  (if @cache.secure)
    ↓
Storage backend (Redis / CachekitIO / File)
```

**Retrieval is the exact reverse pipeline.**

The Rust layer is transparent — you configure serializers and encryption at the Python level; ByteStorage handles the rest automatically.

---

## Why Rust for This Layer?

| Operation | Python | Rust (ByteStorage) |
|-----------|--------|---------------------|
| LZ4 compression | ~50-100 MB/s | ~500 MB/s |
| Blake3 hashing | ~500 MB/s | ~15 GB/s |
| AES-256-GCM | ~200 MB/s | ~1-4 GB/s (AES-NI) |

For most workloads the bottleneck is Redis RTT (~2-50ms), not serialization. The Rust layer matters for large payloads (DataFrames, bulk data) where serialization time approaches network time.

---

## LZ4 Compression

LZ4 is chosen for its speed/ratio balance:

| Data Type | Compression Ratio |
|-----------|------------------|
| JSON strings | 2-3x |
| Numeric arrays | 3-5x |
| Repeated structures | 5-10x |
| Already-compressed data | ~1x (negligible overhead) |
| Random bytes | ~1x |

Compression runs automatically. It can be toggled via the `CACHEKIT_ENABLE_COMPRESSION` environment variable.

---

## Blake3 Integrity

Every value stored includes a Blake3 hash. On retrieval:

1. Hash of retrieved bytes is computed
2. Stored hash is compared
3. Mismatch → `BackendError` (corrupted data, never returned to caller)

This protects against Redis memory corruption, storage bugs, and bit rot.

---

## AES-256-GCM Encryption

When using `@cache.secure`, the Rust layer applies AES-256-GCM after compression:

```
LZ4(serialized_data) → AES-256-GCM(compressed) → storage
```

Encryption is authenticated — any tampering with ciphertext raises a decryption error before data reaches your application.

See [Zero-Knowledge Encryption](zero-knowledge-encryption.md) for full details.

---

## Choosing a Serializer

The Rust ByteStorage layer is orthogonal to the serializer. Mix and match:

| Use Case | Serializer | Encryption |
|----------|-----------|------------|
| General caching | [Default (MessagePack)](../serializers/default.md) | Optional |
| JSON APIs | [Orjson](../serializers/orjson.md) | Optional |
| DataFrames / ML | [Arrow](../serializers/arrow.md) | Optional |
| Typed models | [Pydantic](../serializers/pydantic.md) | Optional |
| Custom types | [Custom](../serializers/custom.md) | Optional |

All serializers pass through the same ByteStorage pipeline (LZ4 + Blake3 + optional AES-256-GCM).

---

## Troubleshooting

**Q: Size exceeds limit**
A: Don't cache objects >100MB. Break into smaller pieces.

**Q: Type not serializable**
A: Choose a serializer that supports your type. See [Serializers](../serializers/README.md).

**Q: "Decryption failed: authentication tag verification failed"**
A: Key mismatch or data corruption. Check `CACHEKIT_MASTER_KEY` hasn't changed. See [Zero-Knowledge Encryption](zero-knowledge-encryption.md).

**Q: Compression ineffective**
A: Expected for already-compressed or random data. Overhead is negligible.

---

## See Also

- [Serializers](../serializers/README.md) - Choose the right serializer for your data
- [Zero-Knowledge Encryption](zero-knowledge-encryption.md) - AES-256-GCM client-side encryption
- [Performance Guide](../performance.md) - Benchmarks and tuning

---

<div align="center">

**[GitHub Issues](https://github.com/cachekit-io/cachekit-py/issues)** · **[Documentation](../README.md)**

</div>
