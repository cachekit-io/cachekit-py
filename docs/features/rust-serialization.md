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
xxHash3-64 integrity hash  (~GB/s, detects corruption)
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
| xxHash3-64 hashing | ~35 GB/s (`xxhash` C ext) | ~35 GB/s |
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

ByteStorage LZ4 compression of serialized envelopes (MessagePack, orjson) runs automatically and is not configurable. Arrow (DataFrame) payloads use a separate Arrow IPC codec, selectable via the `CACHEKIT_ARROW_COMPRESSION` environment variable (`zstd`, `lz4`, or `none`) — that setting applies only to Arrow payloads and does not disable ByteStorage LZ4 for other serializers.

---

## xxHash3-64 Integrity

Every value stored includes an xxHash3-64 checksum (8 bytes, big-endian). On retrieval:

1. Checksum of retrieved bytes is computed
2. Stored checksum is compared
3. Mismatch → `BackendError` (corrupted data, never returned to caller)

This protects against Redis memory corruption, storage bugs, and bit rot.

> **Non-cryptographic.** The checksum detects corruption, not tampering.
> Tamper-resistance comes from AES-256-GCM (`@cache.secure`), never from this checksum.

### Standalone checksum API

> **Available since v0.12.0.** On earlier releases,
> `from cachekit._rust_serializer import checksum` raises `ImportError`.

The same primitive is exposed directly — decoupled from LZ4 compression — for
serializers where compression is ineffective (Arrow IPC, compact JSON):

```python
from cachekit._rust_serializer import checksum, verify_checksum

digest = checksum(b"payload")  # 8 bytes, big-endian
assert len(digest) == 8
assert verify_checksum(b"payload", digest) is True
assert verify_checksum(b"tampered", digest) is False
```

`verify_checksum` raises `ValueError` unless the expected checksum is exactly
8 bytes. Both functions accept any buffer-protocol object (`bytes`, `bytearray`,
`memoryview`), so a serializer holding its payload as a `memoryview` can hash it
without a `bytes` copy. The output is byte-identical to the checksum embedded in
every ByteStorage envelope and to `xxhash.xxh3_64_digest` from the `xxhash` package.

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

All serializers pass through the same ByteStorage pipeline (LZ4 + xxHash3-64 + optional AES-256-GCM).

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
