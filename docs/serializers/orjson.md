**[Home](../README.md)** › **[Serializers](./index.md)** › **OrjsonSerializer**

# OrjsonSerializer

**JSON-optimized serializer** — Fast JSON serialization powered by Rust (orjson library). Ideal for JSON-heavy workloads and API response caching.

## Overview

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

## Basic Usage

```python
from cachekit import cache
from cachekit.serializers import OrjsonSerializer

@cache(serializer=OrjsonSerializer())
def get_api_data(endpoint: str):
    return fetch_json_api(endpoint)
```

**Example with datetime handling:**

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

## Configuration Options

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

## Supported Data Types

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

## Performance Comparison

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

**Limitations:**
- JSON-compatible types only (dict, list, str, int, float, bool, None)
- NO binary data (bytes will raise TypeError) → use DefaultSerializer
- NO arbitrary Python objects → use DefaultSerializer
- Output is ~20-50% larger than msgpack+LZ4 (acceptable tradeoff for JSON interop)

---

## See Also

- [DefaultSerializer](./default.md) — General-purpose alternative with binary data support
- [ArrowSerializer](./arrow.md) — DataFrame-optimized serializer
- [Encryption Wrapper](./encryption.md) — Add zero-knowledge encryption to OrjsonSerializer
- [Performance Guide](../performance.md) — Full benchmark comparisons
