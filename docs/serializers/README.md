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

### Changing Serializers: Automatic Validation

**Good news:** cachekit automatically detects serializer mismatches and fails fast with a clear error message.

**What happens?** When you change a function's serializer (e.g., `@cache` → `@cache(serializer=ArrowSerializer())`):

1. **Cache hit on old data** → cachekit detects mismatch
2. **Raises SerializationError** with actionable message
3. **Function executes** and caches with new serializer

**Example:**

```python notest
from cachekit import cache
from cachekit.serializers import ArrowSerializer

# BEFORE: Using StandardSerializer (implicit)
@cache(backend=None)
def get_data():
    return large_dataframe()  # illustrative - not defined

# AFTER: Switching to ArrowSerializer
@cache(serializer=ArrowSerializer(), backend=None)
def get_data():
    return large_dataframe()  # illustrative - not defined

# First call after change:
# - Cache hit on old StandardSerializer data
# - Error: "Serializer mismatch: cached data uses 'default', but decorator configured with 'arrow'"
# - Function executes, caches with arrow
# - Subsequent calls work normally
```

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
5. **Flush cache when changing serializers** - Prevents deserialization errors
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
