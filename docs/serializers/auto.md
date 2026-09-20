**[Home](../README.md)** › **[Serializers](README.md)** › **AutoSerializer**

# AutoSerializer

The AutoSerializer (`serializer='auto'`) extends the default MessagePack serialization with **Python-specific type detection and preservation**. Use it when your cache is Python-only and you want types like sets, frozensets, datetime, UUID, and NumPy arrays to survive cache roundtrips intact.

> [!TIP]
> If you're hitting tuple→list or set→list issues with the default serializer, `serializer='auto'` is the fix.

## Quick Start

```python notest
from cachekit import cache

@cache(serializer='auto', ttl=300)
def get_data():
    return {"tags": {"admin", "user"}, "created": datetime.now()}

result = get_data()
# Sets preserved: isinstance(result["tags"], set) → True
# Datetime preserved: isinstance(result["created"], datetime) → True
```

## What It Preserves

Types that the default `StandardSerializer` (MessagePack) loses, but `AutoSerializer` preserves:

| Type | Default (`"default"`) | Auto (`"auto"`) |
|------|:---------------------:|:---------------:|
| `set` | → `list` | Preserved |
| `frozenset` | → `list` | Preserved |
| `datetime` | → string | Preserved (ISO-8601 roundtrip) |
| `date` / `time` | → string | Preserved |
| `UUID` | → string | Preserved |
| `numpy.ndarray` | Not supported | Preserved (zero-copy binary) |
| `pandas.DataFrame` | Not supported | Preserved (columnar format) |
| `pandas.Series` | Not supported | Preserved |

> [!NOTE]
> **Tuples** are not yet preserved by AutoSerializer — they still become lists through MessagePack. This is tracked in [#78](https://github.com/cachekit-io/cachekit-py/issues/78).

## How It Works

AutoSerializer uses **type markers** in the serialized data to preserve Python types:

```python notest
# set {1, 2, 3} is serialized as:
{"__set__": True, "value": [1, 2, 3], "frozen": False}

# frozenset({1, 2, 3}) is serialized as:
{"__set__": True, "value": [1, 2, 3], "frozen": True}

# datetime is serialized as:
{"__datetime__": "2026-03-28T12:00:00+00:00"}
```

These markers are Python-specific — other language SDKs (Rust, TypeScript, PHP) will see them as plain dicts, not as the original types.

## When to Use

**Use `serializer='auto'` when:**
- Your cache is Python-only (no cross-language SDK sharing)
- You need set, frozenset, or datetime type preservation
- You're caching NumPy arrays or pandas DataFrames
- Type fidelity matters more than cross-language compatibility

**Use `serializer='default'` (the default) when:**
- Multiple language SDKs share the same cache (Python + Rust + TypeScript)
- You only cache basic types (dicts, lists, strings, numbers)
- Cross-language interoperability is a requirement

## With Different Backends

AutoSerializer works with all backends — it's a serialization format choice, not a backend choice:

```python notest
from cachekit import cache

# L1-only
@cache(backend=None, serializer='auto', ttl=300)
def fn(): return {1, 2, 3}

# Redis
@cache(serializer='auto', ttl=300)
def fn(): return {1, 2, 3}

# Memcached
@cache(backend=memcached_backend, serializer='auto', ttl=300)
def fn(): return {1, 2, 3}
```

## Cross-Config Reads (integrity_checking Mismatch)

When the reader's `integrity_checking` (see [API Reference](../api-reference.md#core-parameters)) differs from the writer's, every path fails closed except DataFrames handled by pyarrow:

| Written with | Read with | Generic value, Series, or DataFrame without pyarrow | DataFrame with pyarrow |
|---|---|---|---|
| `integrity_checking=True` | `integrity_checking=False` | Raises `SerializationError` (E021) | Decodes normally — always checksum-verified |
| `integrity_checking=False` | `integrity_checking=True` | Raises `SerializationError` (E021) | Decodes normally — always checksum-verified |

**MessagePack paths** — generic values, Series, and DataFrames only when pyarrow is absent — fail closed in both directions. A reader with integrity checking off has no way to verify or unwrap an envelope, and a reader with it on treats bytes that fail envelope verification as corruption rather than guessing they might be plain MessagePack. (Only when no metadata is passed at all does an integrity-on reader fall back to decoding plain MessagePack.)

**DataFrames with pyarrow installed** (`pip install 'cachekit[data]'`) have no mismatch to fail on. AutoSerializer hands every DataFrame to [ArrowSerializer](arrow.md), which always writes and validates an 8-byte xxHash3-64 checksum regardless of `integrity_checking` — the setting changes neither the stored bytes nor the verification, so either reader decodes the entry, and both still raise on genuine corruption. What is *not* interchangeable on this path is pyarrow itself: a DataFrame written with pyarrow installed raises `SerializationError` when read by an install without it.

> [!NOTE]
> This mismatch isn't reachable through `@cache`: `integrity_checking` is part of the cache key, so a reader configured differently from the writer misses the entry and recomputes it, rather than reading it under the wrong config. The table above only applies to direct `AutoSerializer.serialize()` / `.deserialize()` calls with hand-passed metadata.

See [E021](../error-codes.md#e021-deserialization-failed) for the exact error messages.

## Unsupported Types

AutoSerializer explicitly rejects types it can't handle safely:

- **Pydantic models**: Use `.model_dump()` first. See [Pydantic guide](pydantic.md).
- **ORM models** (SQLAlchemy, Django): Convert to dict.
- **Custom classes**: Use `dataclasses.asdict()` or implement a [custom serializer](custom.md).

---

## See Also

- [Default Serializer (StandardSerializer)](default.md) — Cross-language MessagePack
- [ArrowSerializer](arrow.md) — Optimized for large DataFrames
- [Serializer Overview](README.md) — Decision matrix

---

<div align="center">

**[GitHub Issues](https://github.com/cachekit-io/cachekit-py/issues)** · **[Documentation](../README.md)**

</div>
