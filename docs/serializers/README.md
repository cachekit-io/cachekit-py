**[Home](../README.md)** › **Serializers**

# Serializer Guide

## Overview

cachekit uses a pluggable serializer architecture that allows you to choose the optimal serialization strategy for your use case. Serializers are responsible for converting Python objects to bytes for storage and back again — they sit between your function's return value and the cache backend.

Each serializer integrates transparently with the `@cache` decorator. You can configure one per decorated function, or rely on the default.

## Available Serializers

| Serializer | Speed | Best For |
|-----------|-------|----------|
| [StandardSerializer](default.md) | Fast | General Python objects, cross-language SDK interop |
| [AutoSerializer](auto.md) | Fast | Python-only — preserves sets, frozensets, datetime, UUID, NumPy, pandas |
| [OrjsonSerializer](orjson.md) | Very Fast (JSON) | JSON-heavy APIs, cross-language interop, human-readable |
| [ArrowSerializer](arrow.md) | Very Fast (DataFrames) | Large pandas/polars DataFrames (10K+ rows) |
| [EncryptionWrapper](encryption.md) | Adds ~3-5 μs | Zero-knowledge caching, GDPR/HIPAA/PCI-DSS compliance |
| [Custom Serializers](custom.md) | Varies | Specialized data types not covered above |

> **OrjsonSerializer** requires the `[json]` extra: `pip install 'cachekit[json]'` (or `uv add 'cachekit[json]'`).

For caching Pydantic models, see [Caching Pydantic Models](pydantic.md).

## Decision Matrix

| Use Case | Recommended Serializer | Reason |
|----------|----------------------|--------|
| General Python objects | StandardSerializer | Broad type support, cross-language safe |
| Python-only with type preservation | AutoSerializer | Preserves sets, frozensets, datetime, UUID, NumPy |
| JSON-heavy data | OrjsonSerializer | 2-5x faster than stdlib json |
| API response caching | OrjsonSerializer | JSON-native, human-readable |
| Web session data | OrjsonSerializer | Fast JSON, cross-language |
| Small DataFrames (< 1K rows) | StandardSerializer | Lower overhead for small data |
| Large DataFrames (10K+ rows) | ArrowSerializer | Significant speedup (6-23x) |
| Mixed object types | StandardSerializer | Broad type support |
| Real-time data pipelines | ArrowSerializer | Zero-copy deserialization |
| Time-series analytics | ArrowSerializer | Optimized for columnar data |
| Binary data | StandardSerializer | Only serializer supporting bytes |

## Migration Guide

### Breaking change in v0.20.0: the key carries the real serializer

Before v0.20.0 the serializer half of the key suffix was a constant: **every key ended `:1s`
whatever serializer was configured** (`:0s` with `integrity_checking=False`). From v0.20.0 the
code reflects the serializer in use — the "Suffix" column in the table below — so keys
change identity on upgrade, with no change on your side, for:

- `serializer="auto"` / `"pythonic"`, `"orjson"`, `"arrow"` → now `:1a`, `:1o`, `:1w`;
- any serializer passed as an **instance**, built-in or custom → now `:1x` + 4 hex.

Not affected: the default serializer (no `serializer=`, `"std"`, `"default"`, `"standard"` —
`:1s` before and after), `@cache.local()` (always `:0l`), a custom `key=` function (no
suffix), and `interop_mode=True` (separate key format).

Each affected function on a shared backend gets a one-time cold cache at cut-over, and its
pre-upgrade entries are never read again. Two decorators over one function that differed only
in serializer used to evict each other on every call through the `Serializer mismatch` path;
they now coexist.

**What invalidation still reaches.** Upgraded code never *reads* a pre-upgrade entry, but
single-key invalidation — `fn.invalidate_cache(*args)` / `await fn.ainvalidate_cache(*args)`
on a decorator with a generated key — deletes both the current key and the pre-v0.20.0
`:{integrity_flag}s` key for the same arguments. An erasure through the SDK therefore removes
the pre-upgrade copy too, including one an old replica wrote during a rolling deploy. The
cost: a default-serializer decorator over the same function, namespace and arguments loses
that entry and recomputes once.

The reverse direction is not covered. An erasure served by a v0.19 replica during the rollout,
or by any replica after a rollback to v0.19, deletes only the `:{integrity_flag}s` key, so a
copy that a v0.20.0 replica wrote under the new key survives. Re-issue any erasure made during
the rollout once the last v0.19 replica is retired, or cover it with the flush below.

**What still needs a backend flush.** The SDK cannot reach a pre-upgrade entry whose
arguments you never invalidate, and no-argument `invalidate_cache()` / `cache_clear()` only
deletes the keys the current process wrote — in a freshly deployed process that is none of the
old ones. So if you cache personal data under `ttl=None`, or otherwise need every pre-upgrade
entry gone rather than aging out, follow the flush procedure in the retention warning
[below](#changing-serializers-separate-keyspaces) — **after the last v0.19 replica is
retired**, not at the start of a rolling deploy, or replicas still on the old release keep
writing `:{integrity_flag}s` entries behind your flush. A `namespace=` bump gives an explicit cut-over but
orphans the old keyspace rather than deleting it; the retention step still applies.

### Changing Serializers: Separate Keyspaces

The serializer is part of the cache key. The key's trailing metadata suffix is
`{integrity_flag}{serializer_code}` — `1`/`0` for integrity checking, then one character
for the serializer:

| Configured as | Code | Suffix | Before v0.20.0 |
| :--- | :---: | :--- | :--- |
| `serializer="std"` / `"default"` / `"standard"` (the default) | `s` | `:1s` | `:1s` |
| `serializer="auto"` / `"pythonic"` | `a` | `:1a` | `:1s` |
| `serializer="orjson"` | `o` | `:1o` | `:1s` |
| `serializer="arrow"` | `w` | `:1w` | `:1s` |
| `@cache.local()` reference caching | `l` | `:0l` | `:0l` |
| **any serializer instance**, built-in or custom | `x` + 4 hex | `:1x4874` | `:1s` |

That last row is not a typo: `serializer="arrow"` and `serializer=ArrowSerializer()` are
*different* cache identities. The cached envelope records the serializer by name — `"arrow"`
for the string form, `"ArrowSerializer"` for the instance — and has done so since long before
the key carried the code. The key follows the envelope, because a key that claimed two
configurations were the same when the envelope says otherwise is exactly what this suffix
exists to prevent. Prefer the string names; they are the documented, stable identities.

The code is derived from the serializer's **class** name, so two *different* classes over
the same function stay in separate keyspaces rather than evicting each other on every read.
Two instances of the *same* class share one code — see the caution below. The four hex
digits are a two-byte digest, so two different class names can collide (65,536 codes); the
envelope's serializer name still stops a mis-deserialization, but the colliding pair shares
a key and misses on each other's entries.

> [!CAUTION]
> **Two instances of the same class with different constructor arguments are not
> distinguished** — `ArrowSerializer(return_format="arrow")` and
> `ArrowSerializer(return_format="pandas")` produce the same code *and* the same envelope
> name, so neither the key nor the mismatch guard separates them and one can deserialize
> the other's bytes. The same applies to a configurable custom serializer
> (`PydanticSerializer(model=User)` vs `(model=Order)`). Give them distinct `namespace=`
> values.

So changing a function's serializer does not collide with its old entries: the new
serializer reads and writes its own keys, and the old ones age out on their TTL.

1. **New key** (different serializer code) → cache miss
2. **Function executes** and caches under the new serializer's key
3. **Old entries** are never read again and expire on their own TTL

```python notest
from cachekit import cache
from cachekit.serializers import ArrowSerializer

# BEFORE: Using StandardSerializer (implicit) -> keys end in ":1s"
@cache(backend=None)
def get_data():
    return large_dataframe()  # illustrative - not defined

# AFTER: Switching to ArrowSerializer -> keys end in ":1w"
@cache(serializer="arrow", backend=None)
def get_data():
    return large_dataframe()  # illustrative - not defined
```

> [!IMPORTANT]
> **Plan for a cold cache on the first deploy after a serializer change.** Every key for
> that function changes identity at once, so the whole function's working set recomputes.
> On a hot path, roll it out behind your usual warm-up or stampede controls.

> [!WARNING]
> **Orphaned entries are a data-retention question, not just a hit-rate one.** When you
> change a function's serializer, `invalidate_cache()` computes the *new* key and cannot
> reach the old copy — a deletion for erasure, consent withdrawal or permission revocation
> will report success while the previous entry survives until its TTL expires, or
> indefinitely if no TTL is set. The one old key it does reach is the default serializer's
> `:{integrity_flag}s` key, kept for the v0.20.0 upgrade (see
> [above](#breaking-change-in-v0200-the-key-carries-the-real-serializer)). Any move *away*
> from a non-default serializer is not covered — to another one (`"auto"` to `"arrow"`) or
> to the default (`"auto"` to `"default"`, or to `@cache.secure`, which requires it).
> If you cache personal data, **flush the affected namespace** when you change a serializer
> rather than relying on expiry, and after upgrading to v0.20.0 for any entries that
> single-key invalidation will not reach. The SDK has no
> bulk delete — `cache_clear()` only knows the keys the current process wrote — so flush on
> the backend: on Redis, `SCAN` for the key prefix (`ns:<namespace>:*`) and `UNLINK` the
> matches; the File backend stores one file per hashed key in `cache_dir`, so the only flush
> is the whole directory. Memcached and CachekitIO offer no pattern delete, so old entries
> there retire only by TTL. During a rolling deploy, flush **after the last replica still
> writing the old keys is gone** — anything written behind the flush is orphaned.

**The serializer-mismatch guard still exists**, and still raises
`SerializationError: Serializer mismatch: cached data uses 'X', but decorator configured with 'Y'`.
It is the fallback for the cases where the key cannot separate the serializers:

- a custom `key=` function (custom keys carry no serializer code).

And it cannot help at all where the envelope name is identical too — two instances of one
class, as in the caution above.

**For zero-downtime migrations**, use namespace versioning:

```python notest
from cachekit import cache
from cachekit.serializers import ArrowSerializer

# V1: StandardSerializer (existing production)
@cache(namespace="user_data:v1", backend=None)
def get_user_data_v1(user_id):
    return df  # illustrative - df not defined

# V2: ArrowSerializer (new deployment, different namespace)
@cache(serializer=ArrowSerializer(), namespace="user_data:v2", backend=None)
def get_user_data_v2(user_id):
    return df  # illustrative - df not defined

# Gradual migration: switch function name in codebase, both caches coexist
```

## Best Practices

1. **Use ArrowSerializer for large DataFrames (10K+ rows)** - Significant performance gains
2. **Use StandardSerializer for mixed types** - Broader type support
3. **Benchmark your specific workload** - Performance varies by data characteristics
4. **Version your cache namespaces** - Makes serializer migrations safer
5. **Expect a cold cache when changing serializers** - Keys change identity, so the function's working set recomputes once
6. **Monitor serialization metrics** - Track time spent in serialize/deserialize
7. **Consider network latency** - Even 20x speedup is small compared to 100ms network RTT

---

## Serializer Pages

- [StandardSerializer (MessagePack)](default.md) — General-purpose, handles all Python types
- [OrjsonSerializer](orjson.md) — JSON-optimized, 2-5x faster than stdlib json
- [ArrowSerializer](arrow.md) — DataFrame-optimized, 6-23x faster for large DataFrames
- [Encryption Wrapper](encryption.md) — Wraps any serializer for zero-knowledge caching
- [Caching Pydantic Models](pydantic.md) — Patterns and pitfalls for Pydantic model caching
- [Custom Serializers](custom.md) — Implement your own via SerializerProtocol

## See Also

- [API Reference](../api-reference.md) - Serializer parameters and options
- [Zero-Knowledge Encryption](../features/zero-knowledge-encryption.md) - Encryption with serializers
- [Configuration Guide](../configuration.md) - Environment variable setup
- [Performance Guide](../performance.md) - Real serialization benchmarks
- [Troubleshooting Guide](../troubleshooting.md) - Serialization error solutions

---

<div align="center">

**[GitHub Issues](https://github.com/cachekit-io/cachekit-py/issues)** · **[Documentation](../README.md)**

</div>
