**[Home](../README.md)** › **[Serializers](README.md)** › **OrjsonSerializer**

# OrjsonSerializer

**JSON-optimized serializer** — Fast JSON serialization powered by Rust (orjson library). Ideal for JSON-heavy workloads and API response caching.

**Requires the `[json]` extra** — orjson is an optional dependency:

```bash
pip install 'cachekit[json]'
# or
uv add 'cachekit[json]'
```

Without orjson installed, `get_serializer("orjson")` raises `ImportError: orjson is not installed. OrjsonSerializer requires the [json] extra: pip install 'cachekit[json]'`.

## Overview

**Best for:**
- API response caching (JSON-native data)
- Web application session data
- JSON-heavy configuration/metadata
- Cross-language compatibility (JSON ubiquitous)
- When human-readable format matters

**Performance characteristics:**
- Encoding: orjson, a Rust JSON library. No cachekit guard compares its speed with stdlib json or MessagePack, so the [Performance Guide](../performance.md#not-measured-here) publishes no figure for it
- Memory overhead: Low
- Network overhead: JSON format (larger than msgpack, but human-readable)

**Native type support:**
- datetime → ISO-8601 strings (automatic conversion)
- UUID → string representation
- Dataclass → dict (encoded by default; a hit returns the dict, not the dataclass)
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
- `dataclass` → dict (a hit returns the dict, not the dataclass)

**NOT supported.** Calling `serialize()` directly raises `TypeError`. A `@cache`-decorated call with a backend does not raise: it returns the value, caches nothing, and logs the failure ([Troubleshooting → Serialization Failures](../troubleshooting.md#common-errors)).
- `bytes` → use `StandardSerializer` instead
- Custom classes → convert to a dict (`dataclasses.asdict`, `model_dump`) or use a [custom serializer](./custom.md)
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
    # "OrjsonSerializer only supports JSON types. Use StandardSerializer for binary data."
```

## Comparison with StandardSerializer

No cachekit guard times OrjsonSerializer against stdlib json or StandardSerializer, so this table compares formats, not speed (see the [Performance Guide](../performance.md#not-measured-here)):

| Metric | orjson | msgpack+LZ4 | Note |
|--------|--------|-------------|------|
| Output size | Larger | Smaller | JSON is text |
| Human-readable | Yes ✅ | No ❌ | JSON is text |
| Cross-language | Yes ✅ | Limited | JSON ubiquitous |

**When to prefer OrjsonSerializer:**
- JSON-native APIs (already producing JSON)
- Cross-language interoperability matters
- Human-readable cache inspection needed
- Trading larger cache entries for JSON compatibility

**When to prefer StandardSerializer:**
- Maximum compression needed
- Binary data (bytes, images, etc.)
- Smallest possible cache footprint

**Limitations:**
- JSON-compatible types only (dict, list, str, int, float, bool, None)
- NO binary data (`bytes`) → use StandardSerializer
- NO arbitrary Python objects → convert to a dict first
- Output is ~20-50% larger than msgpack+LZ4 (acceptable tradeoff for JSON interop)

---

## See Also

- [StandardSerializer](default.md) — General-purpose alternative with binary data support
- [ArrowSerializer](arrow.md) — DataFrame-optimized serializer
- [Encryption Wrapper](encryption.md) — Add zero-knowledge encryption to OrjsonSerializer
- [Performance Guide](../performance.md) — Full benchmark comparisons

---

<div align="center">

**[GitHub Issues](https://github.com/cachekit-io/cachekit-py/issues)** · **[Documentation](../README.md)**

</div>
