**[Home](../README.md)** › **Features** › **Rust Serialization**

# Rust Serialization - Performance & Safety

**Version**: cachekit v1.0+

## TL;DR

MessagePack serialization (Python) with optional compression and integrity checking. Pluggable serializer system supports optimized formats for specific use cases.

```python
@cache(ttl=300)
def get_data(x):
    # Automatically serialized with MessagePack
    # Optional: LZ4 compression, xxHash3-64 checksums
    return {"result": x * 2}  # Serialized safely
```

---

## Quick Start

Serialization is automatic:

```python
from cachekit import cache

@cache(ttl=300)  # MessagePack serialization by default
def expensive_operation(x):
    return {
        "result": x * 2,
        "nested": {"data": [1, 2, 3]},
    }

data = expensive_operation(42)
# Returned data structure is serialized and cached
```

---

## What It Does

**Serialization pipeline**:
```
Python object (dict, list, tuple, etc)
    ↓
MessagePack encoding (binary format)
    ↓
LZ4 compression (fast compression)
    ↓
xxHash3-64 checksum (integrity protection)
    ↓
Redis storage (L2 cache)
    ↓
Deserialization (reverse pipeline)
    ↓
Python object (reconstructed exactly)
```

**Features**:
- **MessagePack**: Binary format, faster than JSON
- **Optional Compression**: LZ4 compression (typical 3-5x reduction)
- **Integrity Checking**: xxHash3-64 checksums detect corruption
- **Decompression bomb protection**: Size limits enforced
- **Pluggable Serializers**: Use optimized formats for specific workloads (planned for v1.0+)

---

## Why You'd Want It

**Scenario**: Caching complex data structures across pods with efficiency.

**Without serialization** (raw Python objects):
```python
# Can't store in Redis - must serialize first
# JSON would convert tuples to lists, lose type information
```

**With MessagePack serialization**:
```python
# Store complex data in cache
data = {"metrics": [1, 2, 3], "summary": "data"}
# MessagePack: fast binary serialization
# Redis stores bytes, L1 cache stores compressed bytes
```

**Note on type preservation**: MessagePack is capable of preserving some type information (unlike JSON), but current cachekit implementation doesn't fully leverage this. Planned pluggable serializers (v1.0+) will support optimized formats for specific use cases.

**Performance**:
- Compression reduces Redis memory 3-5x
- Fast serialization (<1ms for most objects)
- Pure Python alternative would be 10-100x slower

---

## Why You Might Not Want It

**Scenarios where serialization overhead doesn't matter**:

1. **Primitive types only** (strings, ints): Compression doesn't help
2. **Tiny objects** (<100 bytes): Overhead > savings
3. **Already-compressed formats** (images, PDFs): Re-compression wasteful

**Mitigation**: Serialization is required for L2 cache (can't avoid)

---

## What Can Go Wrong

### Size Limit Exceeded (Decompression Bomb)
```python
# Object too large
huge_list = list(range(1_000_000_000))

@cache(ttl=300)
def process(x):
    return {"data": huge_list}  # Will fail
# Error: "Serialized size exceeds 100MB limit"
# Solution: Don't cache massive objects
```

### Unsupported Type
```python
# Custom class instance
class CustomClass:
    def __init__(self, x):
        self.x = x

@cache(ttl=300)
def process():
    return CustomClass(42)  # MessagePack can't serialize
# Error: "Type CustomClass not serializable"
# Solution: Return dict or convert to JSON-serializable format
```

### Compression Ineffective
```python
@cache(ttl=300)
def process():
    # Return already-compressed data
    return {"image": base64.b64encode(png_bytes)}
# LZ4 compression won't help, actually adds overhead
# Solution: Cache decompressed data or skip caching
```

---

## How to Use It

### Basic Usage (Automatic)
```python
@cache(ttl=3600)
def get_report(date):
    return {
        "date": date,
        "metrics": [1, 2, 3],
        "summary": "Report data",
    }

# Automatically serialized with MessagePack + LZ4
report = get_report("2025-01-15")
```

### With Custom Compression Settings
```python notest
@cache(
    ttl=3600,
    compression_level=6,  # 0-9, default 6
    compression_enabled=True,  # Default: True
)
def get_data(x):
    return compute(x)
```

### Disabling Compression (Not Recommended)
```python notest
@cache(
    ttl=3600,
    compression_enabled=False,  # Uses MessagePack without compression
)
def cheap_operation(x):
    return simple_result(x)
```

---

## Technical Deep Dive

### MessagePack Format
```
Python dict: {"key": "value", "num": 42}
    ↓
MessagePack binary: Efficient binary encoding
    ↓
Optional: LZ4 compression (typical 3-5x reduction)
    ↓
Optional: xxHash3-64 checksum (8 bytes for integrity)
    ↓
Redis L2 or L1 cache: Bytes storage
```

### LZ4 Compression Ratios
```
Type of Data          Compression Ratio
─────────────────────────────────────
JSON strings          2-3x
Numeric arrays        3-5x
Repeated data         5-10x
Already compressed    ~1x (no benefit)
Random data           ~1x (no benefit)
```

### xxHash3-64 Integrity
```
Deserialization flow:
1. Read data from Redis
2. Extract xxHash3-64 checksum
3. Compute checksum of data
4. Compare: if mismatch → corrupted data
5. Decompress only if checksums match
6. Deserialize MessagePack
```

### Type Preservation & Pluggable Serializers (Planned for v1.0+)

**Current state**: Default MessagePack serializer handles most types effectively.

**Planned enhancement**: Pluggable serializer system (v1.0+) will support:
- **ArrowSerializer**: Zero-copy DataFrames (100,000x faster deserialization)
- **OrjsonSerializer**: JSON-only APIs (10-50x faster serialization)
- **MsgspecSerializer**: Typed APIs with validation (2-5x faster)

**Note**: Some type information (like tuple vs list distinction) may be lost during serialization depending on serializer choice. Future serializers will support better type fidelity for specific use cases.

---

## Performance Impact

### Compression Overhead
```
Small objects (<1KB):
  - Compression adds 10-50μs
  - Decompression adds 10-50μs
  - Savings from smaller Redis storage: varies

Large objects (>100KB):
  - Compression adds 100-500μs
  - Decompression adds 100-500μs
  - Savings: typically 3-5x storage reduction
```

### Throughput

**MessagePack encoding** (Python via msgpack library):
- Serialization: ~100-200MB/s typical
- For 1KB object: <10μs serialization time

**Optional ByteStorage layer** (Rust - compression + checksums):
- LZ4 compression: ~500MB/s
- xxHash3-64 checksums: ~36GB/s

Total pipeline with compression: ~100-200MB/s throughput

---

## Integration with Other Features

**Serialization + Encryption**:
```python notest
@cache.secure(ttl=300)  # Both enabled
def fetch_sensitive(x):
    # Order: MessagePack → optional LZ4 → AES-256-GCM → Redis
    # Encryption is applied after serialization
    return sensitive_data(x)
```

**Serialization + Circuit Breaker**:
```python
@cache(ttl=300)  # Both enabled
def operation(x):
    # Serialization before L2 write
    # If deserialization fails → circuit breaker catches error
    return data(x)
```

---

## Troubleshooting

**Q: Size exceeds limit**
A: Don't cache objects >100MB. Break into smaller pieces.

**Q: Type not serializable**
A: Convert to dict/list. MessagePack only supports standard JSON types (dict, list, str, int, float, bool, None)

**Q: Compression ineffective**
A: Expected for already-compressed or random data. Overhead minimal.

**Q: Tuples returning as lists**
A: MessagePack doesn't preserve tuple type in current cachekit implementation. Planned pluggable serializers (v1.0+) will provide options with better type fidelity for specific use cases.

---

## See Also

- [Pluggable Serializers Strategy](../../strategy/2025-11-12/serializer-abstraction/) - Planned v1.0+ serializer abstraction
- [API Reference](../api-reference.md) - `@cache` decorator configuration
- [Encryption](zero-knowledge-encryption.md) - Works with serialization layer

---

<div align="center">

*Last Updated: 2025-12-02 · ✅ MessagePack serialization implemented*

</div>
