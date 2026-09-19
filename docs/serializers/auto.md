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

`integrity_checking` (see [API Reference](../api-reference.md#core-parameters)) must match between the writer and the reader of a given entry. If it doesn't, AutoSerializer fails closed rather than returning wrong data:

| Written with | Read with | Generic value (dict, list, etc.) | DataFrame / Series |
|---|---|---|---|
| `integrity_checking=True` | `integrity_checking=False` | Raises `SerializationError` (E021) | Raises `SerializationError` (E021) |
| `integrity_checking=False` | `integrity_checking=True` | Decodes normally — there's no envelope to verify | Raises `SerializationError` (E021) |

DataFrame/Series always fail closed on a mismatch, in both directions — a same-shaped DataFrame with silently wrong values is far more dangerous than an explicit error. A generic value written with integrity checking off has no envelope at all, so a reader with it on just decodes the plain MessagePack.

> [!NOTE]
> This mismatch isn't reachable through `@cache`: `integrity_checking` is part of the cache key, so a reader configured differently from the writer misses the entry and recomputes it, rather than reading it under the wrong config. The table above only applies to direct `AutoSerializer.serialize()` / `.deserialize()` calls with hand-passed metadata.

See [E021](../error-codes.md#e021-deserialization-failed) for the exact error message.

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
