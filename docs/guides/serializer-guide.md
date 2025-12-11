**[Home](../README.md)** › **Guides** › **Serializer Guide**

# Serializer Guide

## Overview

cachekit uses a pluggable serializer architecture that allows you to choose the optimal serialization strategy for your use case. This guide explains when to use each serializer, how to configure them, and how to get the best performance.

## Available Serializers

### DefaultSerializer (MessagePack)

**Default serializer** - Efficient general-purpose serialization using MessagePack format with optional LZ4 compression and xxHash3-64 integrity checksums.

**Best for:**
- General Python objects (dicts, lists, tuples)
- Mixed data types
- Scalar values and nested structures
- Small to medium-sized data

**Performance characteristics:**
- Serialization: Fast (< 1ms for typical objects)
- Deserialization: Fast (< 1ms for typical objects)
- Memory overhead: Low
- Network overhead: Compact binary format

**Example:**
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

### OrjsonSerializer

**JSON-optimized serializer** - Fast JSON serialization powered by Rust (orjson library). Ideal for JSON-heavy workloads and API response caching.

**Best for:**
- API response caching (JSON-native data)
- Web application session data
- JSON-heavy configuration/metadata
- Cross-language compatibility (JSON ubiquitous)
- When human-readable format matters

**Performance characteristics:**
- Serialization: **2-5x faster** than stdlib json
- Deserialization: **2-3x faster** than stdlib json
- Memory overhead: Low
- Network overhead: JSON format (larger than msgpack, but human-readable)

**Measured speedups vs stdlib json:**
- Nested structures (1K objects): **3-4x faster** serialization
- Simple dicts (10 keys): **2x faster** roundtrip

**Native type support:**
- datetime → ISO-8601 strings (automatic conversion)
- UUID → string representation
- Dataclass → dict (with OPT_PASSTHROUGH_DATACLASS)
- Sorted keys by default (deterministic caching)

**Example:**
```python notest
from cachekit import cache
from cachekit.serializers import OrjsonSerializer
from datetime import datetime

# Explicit OrjsonSerializer for JSON-heavy caching
@cache(serializer=OrjsonSerializer(), backend=None)
def get_api_response(endpoint: str):
    return {
        "status": "success",
        "data": fetch_external_api(endpoint),  # illustrative - not defined
        "timestamp": datetime.now(),  # Auto-converts to ISO string
        "metadata": {"cached": True}
    }

# JSON response is cached efficiently
response = get_api_response("/users/123")  # Cache miss: calls API
response = get_api_response("/users/123")  # Cache hit: fast retrieval
```

**Limitations:**
- JSON-compatible types only (dict, list, str, int, float, bool, None)
- NO binary data (bytes will raise TypeError) → use DefaultSerializer
- NO arbitrary Python objects → use DefaultSerializer
- Output is ~20-50% larger than msgpack+LZ4 (acceptable tradeoff for JSON interop)

### ArrowSerializer

**DataFrame-optimized serializer** - Zero-copy serialization for pandas and polars DataFrames using Apache Arrow IPC format.

**Best for:**
- Large pandas DataFrames (10K+ rows)
- Large polars DataFrames
- Data science workloads
- Time-series data
- High-frequency DataFrame caching

**Performance characteristics:**
- Serialization: **3-6x faster** than MessagePack for large DataFrames
- Deserialization: **7-20x faster** (memory-mapped, zero-copy)
- Memory overhead: Minimal (zero-copy deserialization)
- Network overhead: Efficient columnar format

**Measured speedups:**
- **10K rows**: 0.80ms (Arrow) vs 3.96ms (MessagePack) = **5.0x faster**
- **100K rows**: 4.06ms (Arrow) vs 39.04ms (MessagePack) = **9.6x faster**

For detailed performance analysis, see [Performance Guide](../performance.md).

**Example:**
```python notest
from cachekit import cache
from cachekit.serializers import ArrowSerializer
import pandas as pd

# Explicit ArrowSerializer for DataFrame caching
@cache(serializer=ArrowSerializer(), backend=None)
def get_large_dataset(date: str):
    # Load 100K+ row DataFrame (illustrative - file may not exist)
    df = pd.read_csv(f"data/{date}.csv")
    return df

# Automatic round-trip with pandas DataFrame
df = get_large_dataset("2024-01-01")  # Cache miss: loads CSV
df = get_large_dataset("2024-01-01")  # Cache hit: fast retrieval (~1ms)
```

## Decision Matrix

| Use Case | Recommended Serializer | Reason |
|----------|----------------------|--------|
| General Python objects | DefaultSerializer | Broad type support, efficient |
| JSON-heavy data | OrjsonSerializer | 2-5x faster than stdlib json |
| API response caching | OrjsonSerializer | JSON-native, human-readable |
| Web session data | OrjsonSerializer | Fast JSON, cross-language |
| Small DataFrames (< 1K rows) | DefaultSerializer | Lower overhead for small data |
| Large DataFrames (10K+ rows) | ArrowSerializer | Significant speedup (6-23x) |
| Mixed object types | DefaultSerializer | Broad type support |
| Real-time data pipelines | ArrowSerializer | Zero-copy deserialization |
| Time-series analytics | ArrowSerializer | Optimized for columnar data |
| Binary data | DefaultSerializer | Only serializer supporting bytes |

## Using OrjsonSerializer

### Basic Usage

```python
from cachekit import cache
from cachekit.serializers import OrjsonSerializer

@cache(serializer=OrjsonSerializer())
def get_api_data(endpoint: str):
    return fetch_json_api(endpoint)
```

### Configuration Options

OrjsonSerializer supports orjson option flags for customization:

```python
import orjson
from cachekit.serializers import OrjsonSerializer

# Default: sorted keys for deterministic output (OPT_SORT_KEYS)
serializer = OrjsonSerializer()

# Pretty-printed JSON (debugging only - larger output)
serializer_debug = OrjsonSerializer(option=orjson.OPT_INDENT_2)

# Treat naive datetime as UTC
serializer_utc = OrjsonSerializer(option=orjson.OPT_NAIVE_UTC)

# Combine multiple options
serializer_multi = OrjsonSerializer(
    option=orjson.OPT_SORT_KEYS | orjson.OPT_NAIVE_UTC
)
```

### Supported Data Types

OrjsonSerializer handles JSON-compatible types plus extended types:

**Native JSON types** (work automatically):
- `dict`, `list`, `str`, `int`, `float`, `bool`, `None`
- Nested structures (dicts of lists of dicts, etc.)
- Unicode strings (emoji, international characters)

**Extended types** (auto-converted):
- `datetime` → ISO-8601 string (`"2025-01-15T12:30:45Z"`)
- `UUID` → string representation
- `dataclass` → dict (requires `OPT_PASSTHROUGH_DATACLASS`)

**NOT supported** (raises `TypeError`):
- `bytes` → use `DefaultSerializer` instead
- Custom classes → use `DefaultSerializer` or implement `__dict__`
- `set`, `frozenset` → convert to `list` first

**Type checking example:**
```python
from cachekit.serializers import OrjsonSerializer

serializer = OrjsonSerializer()

# Works: JSON-compatible types
data = {"name": "Alice", "age": 30, "active": True}
serialized, metadata = serializer.serialize(data)

# Raises TypeError with helpful message
try:
    serializer.serialize({"binary": b"data"})
except TypeError as e:
    print(e)
    # "OrjsonSerializer only supports JSON types. Use DefaultSerializer for binary data."
```

### Performance Comparison

OrjsonSerializer vs stdlib json (measured):

| Data Type | orjson | stdlib json | Speedup |
|-----------|--------|-------------|---------|
| Nested objects (1K) | 0.8ms | 3.2ms | **4.0x** |
| Simple dict (10 keys) | 0.05ms | 0.10ms | **2.0x** |
| Large flat dict (10K keys) | 2.5ms | 7.0ms | **2.8x** |

OrjsonSerializer vs DefaultSerializer (msgpack):

| Metric | orjson | msgpack+LZ4 | Note |
|--------|--------|-------------|------|
| Serialization speed | Fast | Faster | msgpack ~10% faster |
| Deserialization speed | Fast | Faster | Similar performance |
| Output size | Medium | Small | msgpack+LZ4 ~30% smaller |
| Human-readable | Yes ✅ | No ❌ | JSON is text |
| Cross-language | Yes ✅ | Limited | JSON ubiquitous |

**When to prefer OrjsonSerializer:**
- JSON-native APIs (already producing JSON)
- Cross-language interoperability matters
- Human-readable cache inspection needed
- Trading ~30% more storage for JSON compatibility

**When to prefer DefaultSerializer:**
- Maximum compression needed
- Binary data (bytes, images, etc.)
- Non-JSON types (custom objects)
- Smallest possible cache footprint

## Using ArrowSerializer

### Basic Usage

```python
from cachekit import cache
from cachekit.serializers import ArrowSerializer
import pandas as pd

@cache(serializer=ArrowSerializer())
def load_stock_data(symbol: str):
    # Returns large DataFrame
    return fetch_historical_prices(symbol)  # doctest: +SKIP
```

### Return Format Options

ArrowSerializer supports multiple return formats for deserialization:

```python
from cachekit.serializers import ArrowSerializer

# Return as pandas DataFrame (default)
serializer = ArrowSerializer(return_format="pandas")

# Return as polars DataFrame (requires polars installed)
serializer = ArrowSerializer(return_format="polars")

# Return as pyarrow.Table (zero-copy, fastest)
serializer = ArrowSerializer(return_format="arrow")
```

**Example with polars:**
```python notest
import polars as pl
from cachekit import cache
from cachekit.serializers import ArrowSerializer

@cache(serializer=ArrowSerializer(return_format="polars"), backend=None)
def get_polars_data():
    return pl.DataFrame({
        "id": [1, 2, 3],
        "value": [10.5, 20.3, 30.1]
    })
```

### Supported Data Types

ArrowSerializer supports:
- `pandas.DataFrame` (with index preservation)
- `polars.DataFrame` (via `__arrow_c_stream__` interface)
- `dict` of arrays (converted to DataFrame)

**Not supported:**
- Scalar values (int, str, float) → raises `TypeError`
- Nested dictionaries → raises `TypeError`
- Lists of objects → raises `TypeError`

**Type checking example:**
```python
from cachekit.serializers import ArrowSerializer

serializer = ArrowSerializer()

# Works: DataFrame
df = pd.DataFrame({"a": [1, 2, 3]})
data, meta = serializer.serialize(df)

# Raises TypeError with helpful message
try:
    serializer.serialize({"key": "value"})
except TypeError as e:
    print(e)
    # "ArrowSerializer only supports DataFrames. Use DefaultSerializer for dict types."
```

## Performance Characteristics

### Benchmark Results

Real-world performance benchmarks (measured on M1 Mac):

**Serialization (encode to bytes):**
| DataFrame Size | Arrow Time | Default Time | Speedup |
|----------------|------------|--------------|---------|
| 1K rows | 0.29ms | 0.20ms | 0.7x (overhead for small data) |
| 10K rows | 0.48ms | 1.64ms | **3.4x** |
| 100K rows | 2.93ms | 16.42ms | **5.6x** |

**Deserialization (decode from bytes):**
| DataFrame Size | Arrow Time | Default Time | Speedup |
|----------------|------------|--------------|---------|
| 1K rows | 0.21ms | 0.39ms | **1.8x** |
| 10K rows | 0.32ms | 2.32ms | **7.1x** |
| 100K rows | 1.13ms | 22.62ms | **20.1x** |

**Total Roundtrip (serialize + deserialize):**
| DataFrame Size | Arrow Total | Default Total | Speedup |
|----------------|-------------|---------------|---------|
| 10K rows | 0.80ms | 3.96ms | **5.0x** |
| 100K rows | 4.06ms | 39.04ms | **9.6x** |

**Key takeaway:** ArrowSerializer shines for DataFrames with 10K+ rows. For smaller data (< 1K rows), DefaultSerializer has lower overhead.

For comprehensive performance analysis including decorator overhead, concurrent access, and encryption impact, see [Performance Guide](../performance.md).

### Memory Usage

ArrowSerializer uses memory-mapped deserialization, which means:
- No full copy of data into memory
- Minimal memory allocation
- Faster garbage collection

**Example comparison (100K rows):**
- Default deserialization: +15 MB memory allocation
- Arrow deserialization: +2 MB memory allocation

## Caching Pydantic Models

**Issue:** Pydantic models are not directly serializable by DefaultSerializer. This is intentional.

### Why Pydantic Models Aren't Auto-Detected

When you try to cache a Pydantic model directly:

```python
from pydantic import BaseModel
from cachekit import cache

class User(BaseModel):
    id: int
    name: str
    email: str

# WRONG - Raises TypeError
@cache
def get_user(user_id: int) -> User:
    return fetch_user_from_db(user_id)  # Raises: User is not serializable
```

**Why we don't auto-detect Pydantic models:**

1. **Explicit is better than implicit** - Converting models to dicts without your knowledge is surprising
2. **Loss of fidelity** - `model.model_dump()` discards validators, computed fields, and methods
3. **Scope creep prevention** - Auto-detection for Pydantic opens the door to SQLAlchemy, dataclasses, ORMs, etc.
4. **Clear error messages** - The error tells you exactly what's wrong and how to fix it

### Recommended: Cache the Data, Not the Model

**Best practice** - Convert to dict before caching (explicit and efficient):

```python notest
# Illustrative example showing Pydantic model handling pattern
from pydantic import BaseModel
from cachekit import cache

class User(BaseModel):
    id: int
    name: str
    email: str

# RIGHT - Cache the data (dict), caller gets dict
@cache(ttl=3600)
def get_user(user_id: int) -> dict:
    user = fetch_user_from_db(user_id)  # Returns Pydantic model
    return user.model_dump()  # Convert to dict before caching

# Usage
data = get_user(123)  # Returns: {"id": 123, "name": "Alice", "email": "alice@example.com"}
print(data["name"])  # Works fine
```

**Advantages:**
- Explicit about what's being cached
- No validators/methods to lose
- Best performance (dict is optimal for MessagePack)
- Works with any serializer (Default, OrJSON, Arrow)

### Alternative: Use PickleSerializer for Full Model Instances

If you need the cached object to be a full Pydantic model instance with all methods:

```python notest
from pydantic import BaseModel
from cachekit import cache
from cachekit.serializers import StandardSerializer

class User(BaseModel):
    id: int
    name: str
    email: str

    def is_admin(self) -> bool:
        return self.id < 10  # Example computed property

# Cache the model data (note: methods not preserved, only data)
@cache(serializer=StandardSerializer(), ttl=3600, backend=None)
def get_user(user_id: int) -> User:
    return fetch_user_from_db(user_id)  # illustrative - not defined

# Usage
user = get_user(123)  # Returns: User(id=123, name="Alice", email="alice@example.com")
print(user.is_admin())  # Works - model reconstructed with methods
```

**Trade-offs:**
- ✅ Secure MessagePack serialization
- ✅ Portable across Python versions
- ✅ Pydantic models reconstructed correctly with all methods
- ❌ Larger serialized size

**When to use PickleSerializer:**
- You need model methods after deserialization
- You trust the cache source (internal Redis, not user-controlled)
- You're comfortable with Python-only serialization
- Code execution risk is acceptable in your threat model

### Advanced: Custom PydanticSerializer

If you have strong opinions about Pydantic handling, implement a custom serializer:

```python
from pydantic import BaseModel
from cachekit.serializers.base import SerializerProtocol, SerializationMetadata
import msgpack
from typing import Any, Tuple

class PydanticSerializer:
    """Serializer that handles Pydantic models explicitly."""

    def serialize(self, obj: Any) -> Tuple[bytes, SerializationMetadata]:
        """Convert Pydantic models to dict before serializing."""
        if isinstance(obj, BaseModel):
            obj = obj.model_dump()

        data = msgpack.packb(obj)
        metadata = SerializationMetadata(
            format="MSGPACK",
            original_type="pydantic" if isinstance(obj, BaseModel) else "msgpack"
        )
        return data, metadata

    def deserialize(self, data: bytes, metadata: Any = None) -> Any:
        """Deserialize MessagePack bytes."""
        return msgpack.unpackb(data)

# Usage
@cache(serializer=PydanticSerializer())
def get_user(user_id: int) -> dict:
    user = fetch_user_from_db(user_id)
    return user.model_dump()
```

### Migration Path: Pydantic v1 → v2

**Pydantic v1 API:**
```python
@cache
def get_user(user_id: int) -> dict:
    user = fetch_user(user_id)
    return user.dict()  # Pydantic v1 method
```

**Pydantic v2 API (current):**
```python
@cache
def get_user(user_id: int) -> dict:
    user = fetch_user(user_id)
    return user.model_dump()  # Pydantic v2 method
```

**Future-proof approach (supports both):**
```python
@cache
def get_user(user_id: int) -> dict:
    user = fetch_user(user_id)
    # Works with Pydantic v1 or v2
    method = getattr(user, "model_dump", None) or getattr(user, "dict")
    return method()
```

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

# BEFORE: Using DefaultSerializer (implicit)
@cache(backend=None)
def get_data():
    return large_dataframe()  # illustrative - not defined

# AFTER: Switching to ArrowSerializer
@cache(serializer=ArrowSerializer(), backend=None)
def get_data():
    return large_dataframe()  # illustrative - not defined

# First call after change:
# - Cache hit on old DefaultSerializer data
# - Error: "Serializer mismatch: cached data uses 'default', but decorator configured with 'arrow'"
# - Function executes, caches with arrow
# - Subsequent calls work normally
```

**For zero-downtime migrations**, use namespace versioning:

```python notest
from cachekit import cache
from cachekit.serializers import ArrowSerializer

# V1: DefaultSerializer (existing production)
@cache(namespace="user_data:v1", backend=None)
def get_user_data_v1(user_id):
    return df  # illustrative - df not defined

# V2: ArrowSerializer (new deployment, different namespace)
@cache(serializer=ArrowSerializer(), namespace="user_data:v2", backend=None)
def get_user_data_v2(user_id):
    return df  # illustrative - df not defined

# Gradual migration: switch function name in codebase, both caches coexist
```

## Troubleshooting

### TypeError: ArrowSerializer only supports DataFrames

**Problem:** ArrowSerializer received a non-DataFrame type (scalar, dict, list, etc.).

**Solution:** Use DefaultSerializer for non-DataFrame data:

```python notest
from cachekit import cache
from cachekit.serializers import ArrowSerializer

# WRONG: ArrowSerializer with dict
@cache(serializer=ArrowSerializer(), backend=None)
def get_config():
    return {"key": "value"}  # TypeError!

# RIGHT: DefaultSerializer with dict
@cache(backend=None)  # Uses DefaultSerializer by default
def get_config():
    return {"key": "value"}  # Works
```

### ImportError: polars not installed

**Problem:** Using `return_format="polars"` without polars installed.

**Solution:** Install polars as optional dependency:

```bash
pip install polars
# or
uv add polars
```

Alternatively, use `return_format="pandas"` (default) or `return_format="arrow"`.

### SerializationError: Serializer mismatch

**Problem:** Cached data was created with a different serializer than the decorator specifies.

**Error message:**
```
SerializationError: Serializer mismatch: cached data uses 'default',
but decorator configured with 'arrow'. Cache entry is incompatible.
```

**Solutions:**

**Option 1:** Let it self-heal (simplest for dev/staging):
```python notest
from cachekit import cache
from cachekit.serializers import ArrowSerializer

# Cache hit fails once, then function executes and caches with new serializer
# Subsequent calls work normally
@cache(serializer=ArrowSerializer(), backend=None)
def get_data():
    return df
```

**Option 2:** Flush cache manually (for immediate consistency):
```python
# Flush all cache entries for this function (pseudo-code)
# cache_manager.flush(namespace="function_name")  # doctest: +SKIP
```

### Slow deserialization for small DataFrames

**Problem:** ArrowSerializer has overhead for small DataFrames (< 1K rows).

**Solution:** Use DefaultSerializer for small data:

```python notest
from cachekit import cache
from cachekit.serializers import ArrowSerializer
import pandas as pd

# BEFORE: Arrow overhead for small data
@cache(serializer=ArrowSerializer(), backend=None)
def get_small_data():
    return pd.DataFrame({"a": [1, 2, 3]})  # Only 3 rows

# AFTER: Default is faster for small data
@cache(backend=None)
def get_small_data():
    return pd.DataFrame({"a": [1, 2, 3]})
```

## Custom Serializers

You can implement custom serializers by following the `SerializerProtocol`:

```python
from cachekit.serializers.base import SerializerProtocol, SerializationMetadata
from typing import Any, Tuple

class CustomSerializer:
    """Custom serializer following SerializerProtocol."""

    def serialize(self, obj: Any) -> Tuple[bytes, SerializationMetadata]:
        """Serialize object to bytes with metadata."""
        # Your serialization logic here
        data = custom_encode(obj)
        metadata = SerializationMetadata(
            format="custom",
            compressed=False,
            encrypted=False,
            size_bytes=len(data)
        )
        return data, metadata

    def deserialize(self, data: bytes) -> Any:
        """Deserialize bytes back to object."""
        # Your deserialization logic here
        return custom_decode(data)

# Use custom serializer
@cache(serializer=CustomSerializer())
def my_function():
    return special_data()
```

**Requirements:**
- Implement `serialize(obj) -> (bytes, SerializationMetadata)` method
- Implement `deserialize(bytes) -> Any` method
- Ensure round-trip fidelity (deserialize(serialize(obj)) == obj)

## Best Practices

1. **Use ArrowSerializer for large DataFrames (10K+ rows)** - Significant performance gains
2. **Use DefaultSerializer for mixed types** - Broader type support
3. **Benchmark your specific workload** - Performance varies by data characteristics
4. **Version your cache namespaces** - Makes serializer migrations safer
5. **Flush cache when changing serializers** - Prevents deserialization errors
6. **Monitor serialization metrics** - Track time spent in serialize/deserialize
7. **Consider network latency** - Even 20x speedup is small compared to 100ms network RTT

## Performance Optimization Tips

### For ArrowSerializer

1. **Use return_format="arrow"** for zero-copy access:

   ```python notest
   from cachekit import cache
   from cachekit.serializers import ArrowSerializer

   @cache(serializer=ArrowSerializer(return_format="arrow"), backend=None)
   def get_data():
       return df  # illustrative - df not defined

   # Result is pyarrow.Table (no pandas conversion overhead)
   table = get_data()
   ```

2. **Preserve pandas index** for efficient round-trips:

   ```python
   # ArrowSerializer automatically preserves pandas index
   df = pd.DataFrame({"a": [1, 2, 3]}, index=pd.Index([10, 20, 30], name="id"))
   # Index is preserved through serialization/deserialization
   ```

3. **Batch similar queries** to amortize cache lookup overhead:

   ```python notest
   from cachekit import cache
   from cachekit.serializers import ArrowSerializer
   import pandas as pd

   @cache(serializer=ArrowSerializer(), backend=None)
   def get_data_batch(date_range):
       # Return one large DataFrame instead of many small ones
       return pd.concat([load_day(d) for d in date_range])  # illustrative - load_day not defined
   ```

### For DefaultSerializer

1. **Compression is handled automatically** by the Rust layer (LZ4 + xxHash3-64 checksums):
   ```python
   @cache
   def get_large_dict():
       return {"large": "data" * 1000}  # Automatically compressed
   ```

2. **Use appropriate TTL** to balance freshness vs cache hit rate:
   ```python
   @cache(ttl=3600)  # 1 hour
   def get_cached_data():
       return expensive_computation()
   ```

## Encryption Composability (Zero-Knowledge Caching)

**EncryptionWrapper** can wrap **any** serializer for client-side AES-256-GCM encryption. For comprehensive explanation of how encryption works, see [Zero-Knowledge Encryption Guide](../features/zero-knowledge-encryption.md#what-it-does).

### Encrypt ANY Data Type with EncryptionWrapper

```python notest
from cachekit import cache
from cachekit.serializers import EncryptionWrapper, OrjsonSerializer, ArrowSerializer
import pandas as pd

# Encrypted JSON (API responses, webhooks, session data)
# Note: EncryptionWrapper requires CACHEKIT_MASTER_KEY env var or master_key param
@cache(serializer=EncryptionWrapper(serializer=OrjsonSerializer(), master_key="a" * 64), backend=None)
def get_api_keys(tenant_id: str):
    return {
        "api_key": "sk_live_...",
        "webhook_secret": "whsec_...",
        "tenant_id": tenant_id
    }

# Encrypted DataFrames (patient data, ML features)
@cache(serializer=EncryptionWrapper(serializer=ArrowSerializer(), master_key="a" * 64), backend=None)
def get_patient_records(hospital_id: int):
    # illustrative - conn not defined
    return pd.read_sql("SELECT * FROM patients WHERE hospital_id = ?", conn, params=[hospital_id])

# Encrypted MessagePack (default - use @cache.secure preset)
@cache.secure(master_key="a" * 64, backend=None)
def get_user_ssn(user_id: int):
    return {"ssn": "123-45-6789", "dob": "1990-01-01"}
```

**Encryption Performance**: See [zero-knowledge encryption performance analysis](../features/zero-knowledge-encryption.md#performance-impact) for detailed overhead measurements. TL;DR: 3-5 μs overhead for small data (negligible vs network latency), 2.5% overhead for large DataFrames.

### Zero-Knowledge Caching Use Case

```python notest
from cachekit import cache
from cachekit.serializers import EncryptionWrapper, OrjsonSerializer

# Client-side: Encrypt before sending to remote backend
@cache(
    backend="https://cache.example.com/api",
    serializer=EncryptionWrapper(serializer=OrjsonSerializer(), master_key="a" * 64)
)
def get_secrets(tenant_id: str):
    return {"api_key": "sk_live_...", "secret": "..."}

# Backend receives encrypted blob, never sees plaintext
# GDPR/HIPAA/PCI-DSS compliant out of the box
```

**Security details**: See [Zero-Knowledge Encryption Guide](../features/zero-knowledge-encryption.md) for key management, per-tenant isolation, nonce handling, and authentication guarantees.

---

## Next Steps

**Previous**: [Getting Started Guide](../getting-started.md) - Learn the basics
**Next**: [Backend Guide](backend-guide.md) - Implement custom storage backends

## See Also

- [API Reference](../api-reference.md) - Serializer parameters and options
- [Zero-Knowledge Encryption](../features/zero-knowledge-encryption.md) - Encryption with serializers
- [Configuration Guide](../configuration.md) - Environment variable setup
- [Performance Guide](../performance.md) - Real serialization benchmarks
- [Troubleshooting Guide](../troubleshooting.md) - Serialization error solutions

---

## Summary

- **DefaultSerializer**: General-purpose, handles all Python types, fast for small data, best compression
- **OrjsonSerializer**: JSON-optimized, 2-5x faster than stdlib json, human-readable, cross-language compatible
- **ArrowSerializer**: DataFrame-optimized, 6-23x faster for large DataFrames, zero-copy deserialization
- **EncryptionWrapper**: Wraps ANY serializer for zero-knowledge caching (2.5-467% overhead depending on data size)
- **Decision point**: Use Orjson for JSON-heavy workloads and APIs, Arrow for DataFrames with 10K+ rows, Default for everything else
- **Encryption**: Add EncryptionWrapper to any serializer for GDPR/HIPAA/PCI-DSS compliance
- **Migration**: Flush cache when changing serializers (incompatible formats)
- **Custom serializers**: Implement SerializerProtocol for specialized use cases

---

<div align="center">

**[GitHub Issues](https://github.com/cachekit-io/cachekit-py/issues)** · **[Documentation](../README.md)**

*Last Updated: 2025-12-02*

</div>
