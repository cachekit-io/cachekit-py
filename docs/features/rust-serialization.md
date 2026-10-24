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
Serializer (StandardSerializer / AutoSerializer: MessagePack)
    ↓  [Rust ByteStorage takes over here]
LZ4 compression
    ↓
xxHash3-64 integrity hash  (detects corruption)
    ↓
[Optional] AES-256-GCM encryption  (if @cache.secure; AutoSerializer is refused)
    ↓
Storage backend (Redis / CachekitIO / File)
```

**Retrieval is the exact reverse pipeline.** ArrowSerializer and OrjsonSerializer skip ByteStorage; for them only `@cache.secure`'s AES-256-GCM step runs in cachekit's Rust extension.

The Rust layer is transparent — you configure serializers and encryption at the Python level; ByteStorage handles the rest for the MessagePack serializers automatically.

---

## Why Rust for This Layer?

ByteStorage runs LZ4 compression and xxHash3-64 checksums in Rust over the MessagePack bytes of StandardSerializer and AutoSerializer; ArrowSerializer and OrjsonSerializer do not use it. `@cache.secure` adds a separate Rust AES-256-GCM call for each serializer it wraps. cachekit-core's README publishes [measured figures](https://github.com/cachekit-io/cachekit-core#performance) for those calls on named GitHub-hosted runners; their cost grows with payload size.

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

ByteStorage LZ4 compression of serialized envelopes (MessagePack: StandardSerializer and AutoSerializer) runs automatically whenever integrity checking is on (the default, and always under `@cache.secure`); it has no separate switch. Arrow (DataFrame) payloads use a separate Arrow IPC codec, selectable via the `CACHEKIT_ARROW_COMPRESSION` environment variable (`zstd`, `lz4`, or `none`) — that setting applies only to Arrow payloads and does not disable ByteStorage LZ4 for other serializers.

---

## xxHash3-64 Integrity

With integrity checking on (the default), every value stored in a ByteStorage envelope includes an
xxHash3-64 checksum (8 bytes, big-endian), computed over the original uncompressed payload bytes only.
On retrieval:

1. The payload is decompressed and its checksum is computed
2. Stored checksum is compared
3. Mismatch → `SerializationError` at the serializer layer (corrupted data, never returned to caller); a normal `@cache`-decorated read catches it, evicts the entry, and recomputes rather than propagating it to your code

Not every value travels in an envelope. With integrity checking off, `StandardSerializer` and
`AutoSerializer` write no ByteStorage envelope, so MessagePack, columnar and NumPy values carry no
checksum. Two `AutoSerializer` routes skip ByteStorage even with integrity on: NumPy arrays and Arrow
DataFrames carry their own 8-byte xxHash3-64 prefix over the bytes they store, with no ByteStorage LZ4 step.

This protects the payload against Redis memory corruption, storage bugs, and bit rot. It does not
cover the envelope's `format` field, so a rotted `format` passes it. `AutoSerializer` accepts only a
`format` it writes, and only when a header claim that is present agrees; see *Deserialization failed* in
[error-codes.md](../error-codes.md#deserialization-failed) for what still gets through. A corrupt frame header
(truncated, bad version/length, unparseable JSON) takes the same path: `SerializationError`, evict, recompute.

> **Non-cryptographic — corruption detection only.** xxHash3-64 is not a cryptographic
> hash and the comparison is a plain equality check: an attacker with backend write
> access can forge a valid checksum for arbitrary bytes in microseconds. On the
> plaintext `@cache` path the stored bytes are therefore attacker-forgeable.
> Tamper-resistance comes from AES-256-GCM (`@cache.secure`, or an explicit `encryption=` option),
> never from this checksum. See
> [zero-knowledge-encryption.md](zero-knowledge-encryption.md#corruption-vs-tamper-telemetry-and-fail-closed-mode)
> for the corruption-vs-tamper telemetry split.

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

When using `@cache.secure`, the Rust layer applies AES-256-GCM to the serializer's output. With StandardSerializer (the default), that output is a ByteStorage envelope, so encryption runs after LZ4 compression:

```
StandardSerializer:  LZ4(msgpack) → AES-256-GCM(compressed) → storage
OrjsonSerializer:    JSON bytes   → AES-256-GCM               → storage
ArrowSerializer:     Arrow IPC    → AES-256-GCM               → storage
```

OrjsonSerializer and ArrowSerializer skip ByteStorage, so ByteStorage's LZ4 step does not run before encryption. Arrow IPC bytes carry their own codec instead: zstd by default, set by the serializer's `compression` argument or `CACHEKIT_ARROW_COMPRESSION`. AutoSerializer cannot be encrypted: `@cache.secure` refuses it.

Encryption is authenticated — any tampering with ciphertext raises a decryption error before data reaches your application.

See [Zero-Knowledge Encryption](zero-knowledge-encryption.md) for full details.

---

## Choosing a Serializer

Encryption is orthogonal to the serializer. Mix and match:

| Use Case | Serializer | Encryption |
|----------|-----------|------------|
| General caching | [Default (MessagePack)](../serializers/default.md) | Optional |
| JSON APIs | [Orjson](../serializers/orjson.md) | Optional |
| DataFrames / ML | [Arrow](../serializers/arrow.md) | Optional |
| Typed models | [Pydantic](../serializers/pydantic.md) | Optional |
| Custom types | [Custom](../serializers/custom.md) | Optional |

StandardSerializer, and AutoSerializer for MessagePack values, use ByteStorage (LZ4 + xxHash3-64); OrjsonSerializer and ArrowSerializer write their own xxHash3-64 prefix instead (Orjson only with integrity checking on). `@cache.secure` adds AES-256-GCM over the output of any of Standard, Orjson or Arrow.

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
