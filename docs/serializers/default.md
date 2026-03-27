**[Home](../README.md)** › **[Serializers](./index.md)** › **DefaultSerializer**

# Default Serializer (MessagePack)

The **DefaultSerializer** is cachekit's general-purpose serializer. It is used automatically when no serializer is specified on a `@cache` decorator. It combines MessagePack encoding with optional LZ4 compression and xxHash3-64 integrity checksums via cachekit's Rust ByteStorage layer.

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

DefaultSerializer is used automatically — no configuration needed:

```python
from cachekit import cache

# DefaultSerializer is used automatically (no configuration needed)
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

DefaultSerializer can be referenced by multiple aliases when configuring serializers:

| Alias | Resolves To |
|-------|-------------|
| `"auto"` | DefaultSerializer |
| `"default"` | DefaultSerializer |
| `"std"` | StandardSerializer (language-agnostic MessagePack variant) |

> [!NOTE]
> `StandardSerializer` (`"std"`) is a language-agnostic variant of the default serializer designed for cross-language interoperability (Python/PHP/JavaScript). It omits NumPy and DataFrame auto-detection in favor of strict MessagePack compatibility.

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
| `numpy.ndarray` | ✅ | Auto-detected, binary format |
| `pandas.DataFrame` | ✅ | Auto-detected, column-wise |
| `pandas.Series` | ✅ | Auto-detected |
| Pydantic models | ❌ | See [Caching Pydantic Models](./pydantic.md) |
| `set` / `frozenset` | ❌ | Convert to `list` first |
| Custom classes | ❌ | Implement `__dict__` or use custom serializer |

## Compression and Integrity

DefaultSerializer automatically handles:
- **LZ4 compression** — fast compression reducing storage footprint (~30% smaller than raw msgpack)
- **xxHash3-64 checksums** — integrity verification on deserialization

Both are handled by the Rust ByteStorage layer. No configuration required — it's always on.

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

3. **For DataFrames with 10K+ rows**, consider switching to [ArrowSerializer](./arrow.md) for significant speedups.

---

## See Also

- [OrjsonSerializer](./orjson.md) — JSON-optimized alternative for API/web data
- [ArrowSerializer](./arrow.md) — DataFrame-optimized for large data science workloads
- [Encryption Wrapper](./encryption.md) — Add zero-knowledge encryption to DefaultSerializer
- [Caching Pydantic Models](./pydantic.md) — Patterns for working with Pydantic
- [Performance Guide](../performance.md) — Real serialization benchmarks
