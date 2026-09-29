**[Home](../README.md)** › **[Serializers](README.md)** › **StandardSerializer**

# Default Serializer (MessagePack)

The **StandardSerializer** is cachekit's general-purpose serializer. It is used automatically when no serializer is specified on a `@cache` decorator. It combines MessagePack encoding with optional LZ4 compression and xxHash3-64 integrity checksums via cachekit's Rust ByteStorage layer.

The registry aliases for this serializer are `"default"` and `"std"` (`"auto"` is [AutoSerializer](./auto.md)). The class name is `StandardSerializer`.

## Overview

**Best for:**
- General Python objects (dicts, lists, tuples)
- Mixed data types
- Scalar values and nested structures
- Small to medium-sized data
- Binary data (`bytes`)

**Performance characteristics:**
- Serialization: Fast (< 1ms for typical objects)
- Deserialization: Fast (< 1ms for typical objects)
- Memory overhead: Low
- Network overhead: Compact binary format

## Basic Usage

StandardSerializer is used automatically — no configuration needed:

```python
from cachekit import cache

# StandardSerializer is used automatically (no configuration needed)
@cache
def get_user_data(user_id: int):
    return {
        "id": user_id,
        "name": "Alice",
        "scores": [95, 87, 91],
        "metadata": {"tier": "premium"}
    }
```

## Registration Aliases

StandardSerializer can be referenced by alias when configuring serializers:

| Alias | Resolves To |
|-------|-------------|
| `"default"` | StandardSerializer — language-agnostic MessagePack |
| `"std"` | StandardSerializer — explicit alias |
| `"auto"` | AutoSerializer — Python-specific types (NumPy, pandas, datetime) |

> [!NOTE]
> `"auto"` resolves to `AutoSerializer`, which adds Python-specific type detection (NumPy arrays, pandas DataFrames, datetime). `"default"` / `"std"` resolves to `StandardSerializer`, a language-agnostic MessagePack variant designed for cross-language interoperability. Both are based on MessagePack + Rust ByteStorage.

## Type Support Matrix

| Type | Supported | Notes |
|------|-----------|-------|
| `dict` | ✅ | Nested structures |
| `list` | ✅ | Any element types |
| `tuple` | ✅ | Round-trips as list (MessagePack has no tuple type) |
| `str` | ✅ | Unicode |
| `int` | ✅ | Arbitrary precision |
| `float` | ✅ | 64-bit |
| `bool` | ✅ | |
| `None` | ✅ | |
| `bytes` | ✅ | Binary data — only serializer that handles raw bytes |
| `datetime` | ✅ | Via MessagePack extension |
| `numpy.ndarray` | ❌ | Raises `TypeError`; use [AutoSerializer](./auto.md) (`serializer="auto"`) |
| `pandas.DataFrame` | ❌ | Raises `TypeError`; use [ArrowSerializer](./arrow.md) or [AutoSerializer](./auto.md) |
| `pandas.Series` | ❌ | Raises `TypeError`; use [AutoSerializer](./auto.md) |
| Pydantic models | ❌ | See [Caching Pydantic Models](./pydantic.md) |
| `set` / `frozenset` | ❌ | Convert to `list` first |
| Custom classes | ❌ | Implement `__dict__` or use custom serializer |

## Compression and Integrity

StandardSerializer automatically handles:
- **LZ4 compression** — fast compression reducing storage footprint (~30% smaller than raw msgpack)
- **xxHash3-64 checksums** — integrity verification on deserialization

Both are handled by the Rust ByteStorage layer and are on by default. With `integrity_checking=False` (as `@cache.minimal` sets) the serializer writes plain MessagePack instead: no compression, no checksum.

**Cross-config reads.** A reader with integrity checking off does not unwrap envelopes. Handed an entry written with integrity checking on, it raises `SerializationError` (`Cache entry was written with integrity checking on but this reader has integrity checking disabled`) instead of returning the envelope's internal fields as your value, with or without metadata. Under default key generation this never happens through `@cache`, because the integrity flag is part of the key. A custom `key=`, `fast_mode` or the direct serializer API can cross the two configs. The reader decides by verifying the envelope's checksum, not by the value's shape, so a cached value that merely looks like an envelope, such as `[b"\x89PNG", [255, 0, 0, 255, 0, 255, 0, 255], 4096, "rgb"]`, still round-trips.

> **Corruption detection, not tamper resistance.** xxHash3-64 is non-cryptographic: an
> attacker with backend write access can forge a valid checksum for arbitrary bytes. The
> checksum catches bit rot and storage bugs. For tamper resistance, use encryption
> (`@cache.secure` / `CACHEKIT_MASTER_KEY`), which authenticates every byte with AES-256-GCM.

```python
@cache
def get_large_dict():
    return {"large": "data" * 1000}  # Automatically compressed
```

## Performance Optimization Tips

1. **Compression is handled automatically** by the Rust layer (LZ4 + xxHash3-64 checksums) — no action needed.

2. **Use appropriate TTL** to balance freshness vs cache hit rate:
   ```python
   @cache(ttl=3600)  # 1 hour
   def get_cached_data():
       return expensive_computation()
   ```

3. **For DataFrames**, use [ArrowSerializer](./arrow.md): StandardSerializer rejects them.

---

## See Also

- [OrjsonSerializer](orjson.md) — JSON-optimized alternative for API/web data
- [ArrowSerializer](arrow.md) — DataFrame-optimized for large data science workloads
- [Encryption Wrapper](encryption.md) — Add zero-knowledge encryption to StandardSerializer
- [Caching Pydantic Models](pydantic.md) — Patterns for working with Pydantic
- [Performance Guide](../performance.md) — Real serialization benchmarks

---

<div align="center">

**[GitHub Issues](https://github.com/cachekit-io/cachekit-py/issues)** · **[Documentation](../README.md)**

</div>
