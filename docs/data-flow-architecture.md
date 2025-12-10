**[Home](README.md)** › **Architecture** › **Data Flow**

# cachekit Data Flow Architecture

> **Complete visualization of the data path from user code to Redis and back**

*Version: v0.2.0 (Backend Abstraction)*

---

## Quick Navigation

- [Overview](#overview) - High-level architecture
- [Complete Flow Diagram](#complete-data-flow-diagram) - ASCII visualization
- [Entry Point](#entry-point-decorator-interface) - Decorator usage
- [Key Generation](#cache-key-generation) - Blake2b hashing
- [L1 Cache](#l1-cache-layer-in-memory) - In-memory layer
- [Serialization](#serialization-path) - Python → bytes
- [Redis Ops](#redis-operations) - Connection & operations
- [Deserialization](#deserialization-path) - bytes → Python
- [Performance](#performance-characteristics) - Latency & optimization
- [Error Handling](#error-handling) - Graceful degradation

---

## Overview

cachekit uses a hybrid Python-Rust architecture to provide enterprise-grade caching with intelligent reliability features. Data flows through multiple layers, each optimized for specific performance and reliability goals.

> [!TIP]
> **Architecture Principles:**
> - **L1 + L2 Caching**: In-memory bytes cache (L1) + pluggable backend storage (L2) for optimal performance
> - **Backend Abstraction**: Protocol-based L2 backend layer (Redis, HTTP, DynamoDB, etc.)
> - **Bytes-Only Storage**: L1 stores serialized bytes (encrypted or plaintext msgpack), not Python objects
> - **Security First**: Decompression bomb protection, checksum validation, size limits, encrypted-at-rest
> - **Performance Optimized**: Thread affinity, connection pooling, pre-cached serializers
> - **Flexible Configuration**: L1+L2, L1-only, L2-only, or observability-only modes

---

## Complete Data Flow Diagram

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                           USER APPLICATION CODE                             │
│                     my_cached_function(arg1, arg2)                          │
└──────────────────────────────────┬──────────────────────────────────────────┘
                                   │
                                   ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│  STEP 1: DECORATOR ENTRY POINT                                              │
│  File: src/cachekit/decorators/intent.py                                    │
├─────────────────────────────────────────────────────────────────────────────┤
│  @cache                          # Zero-config (90% of use cases, L1+L2)    │
│  @cache.minimal                  # Speed-critical (low latency)             │
│  @cache.production               # Reliability-critical (circuit breaker)   │
│  @cache.secure                   # Security-critical (encryption, L2-only)  │
│  @cache(l1=True, l2=None)        # L1-only mode (local dev, no Redis)       │
│                                                                             │
│  Intent Resolution → Config Dictionary (includes l1_enabled, l2_backend)    │
└──────────────────────────────────┬──────────────────────────────────────────┘
                                   │
                                   ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│  STEP 2: CACHE KEY GENERATION                                               │
│  File: src/cachekit/key_generator.py                                        │
├─────────────────────────────────────────────────────────────────────────────┤
│  CacheKeyGenerator.generate_key(func, args, kwargs, namespace)              │
│                                                                             │
│  Key Structure: "ns:{namespace}:func:{module}.{qualname}:args:{blake2b}"    │
│  Example: "ns:users:func:myapp.get_user:args:7a8b9c0d1e2f3a4b"              │
│                                                                             │
│  Blake2b Hashing (16-byte digest = 32 hex chars):                           │
│  • Fast paths for primitives (str, int, float, bool, None)                  │
│  • Inline hashing for small collections (<10 items)                         │
│  • JSON fallback for complex types (security-first)                         │
└──────────────────────────────────┬──────────────────────────────────────────┘
                                   │
                                   ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│  STEP 3: L1 CACHE CHECK (In-Memory Fast Path)                               │
│  File: src/cachekit/l1_cache.py                                             │
├─────────────────────────────────────────────────────────────────────────────┤
│  L1Cache.get(cache_key) → (found: bool, value: bytes)                       │
│                                                                             │
│  Thread-safe OrderedDict lookup with RLock                                  │
│  • Check if key exists                                                      │
│  • Validate TTL (not expired)                                               │
│  • Update LRU (move to end)                                                 │
│  • Return bytes (encrypted or plaintext msgpack)                            │
│                                                                             │
│  ┌──────────────────────────────────────────┐                               │
│  │ L1 HIT → DESERIALIZE & RETURN (~242μs) ✓ │                               │
│  │ Jump to STEP 6 (Deserialization)         │                               │
│  └──────────────────────────────────────────┘                               │
│                                                                             │
│  L1 MISS → Continue to L2 Backend (if enabled)                              │
└──────────────────────────────────┬──────────────────────────────────────────┘
                                   │
                                   ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│  STEP 4: L2 BACKEND CHECK (Pluggable Storage Layer)                         │
│  File: src/cachekit/backends/redis_backend.py                               │
│  File: src/cachekit/cache_handler.py                                        │
├─────────────────────────────────────────────────────────────────────────────┤
│  if l2_backend is None:                                                     │
│      # L1-only mode - skip to STEP 11 (execute function)                    │
│      jump to STEP 11                                                        │
│                                                                             │
│  # L2 backend enabled (default: RedisBackend)                               │
│  l2_backend.get(cache_key) → bytes or None                                  │
│                                                                             │
│  Backend types:                                                             │
│  • RedisBackend: Distributed Redis storage (default)                        │
│  • Future: HTTPBackend, DynamoDBBackend, etc.                               │
│                                                                             │
│  RedisBackend internal flow:                                                │
│  1. Get Redis client from CacheClientProvider                               │
│  2. Thread affinity optimization (95%+ thread-local cache hit rate)         │
│  3. redis_client.get(cache_key) → bytes or None                             │
│                                                                             │
│  Network latency: ~1-2ms (typical for Redis)                                │
│                                                                             │
│  ┌──────────────────────────────┐    ┌─────────────────────────┐            │
│  │ L2 HIT → Deserialize         │    │ L2 MISS → Execute       │            │
│  │ Continue to STEP 6           │    │ Jump to STEP 11         │            │
│  └──────────────────────────────┘    └─────────────────────────┘            │
└──────────────────────────────────┬──────────────────────────────────────────┘
                                   │ (HIT path)
                                   ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│  STEP 6: DESERIALIZATION (Bytes → Python Objects)                           │
│  File: src/cachekit/cache_handler.py (_deserialize_bytes)                   │
│  File: src/cachekit/serializers/default_serializer.py                       │
│  File: rust/src/byte_storage.rs                                             │
├─────────────────────────────────────────────────────────────────────────────┤
│  _deserialize_bytes(data: bytes) → Python object                            │
│                                                                             │
│  Decrypt-at-read-time flow:                                                 │
│  1. If encryption_wrapper set: decrypt bytes → plaintext msgpack bytes      │
│  2. If not set: pass through (already plaintext msgpack bytes)              │
│  3. DefaultSerializer.deserialize(msgpack_bytes, metadata)                  │
│                                                                             │
│  ┌────────────────────────────────────────────────────────────┐             │
│  │ Path 1: NumPy ULTRA-FAST (Direct Binary)                   │             │
│  │   if data.startswith(b"NUMPY_RAW"):                        │             │
│  │     • Parse dtype + shape from header                      │             │
│  │     • np.frombuffer(raw_bytes).reshape(shape)              │             │
│  │     • ~0.5ms for 10KB array                                │             │
│  └────────────────────────────────────────────────────────────┘             │
│                                                                             │
│  ┌────────────────────────────────────────────────────────────┐             │
│  │ Path 2: Rust ByteStorage (Compressed MessagePack)          │             │
│  │   ByteStorage.retrieve(envelope_bytes)                     │             │
│  │     ↓ Rust layer (byte_storage.rs)                         │             │
│  │   1. Deserialize envelope (MessagePack)                    │             │
│  │   2. Security checks (size limits, compression ratio)      │             │
│  │   3. LZ4 decompress                                        │             │
│  │   4. xxHash3-64 checksum validation                        │             │
│  │   5. Size validation                                       │             │
│  │     ↓                                                      │             │
│  │   MessagePack unpack → Python object                       │             │
│  │     • General objects: ~1-3ms                              │             │
│  │     • DataFrames: ~2-5ms (column-wise reconstruction)      │             │
│  └────────────────────────────────────────────────────────────┘             │
│                                                                             │
│  Result: Python object fully reconstructed                                  │
└──────────────────────────────────┬──────────────────────────────────────────┘
                                   │
                                   ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│  STEP 7: UPDATE L1 CACHE & RETURN                                           │
│  File: src/cachekit/cache_handler.py                                        │
│  File: src/cachekit/decorators/wrapper.py                                   │
├─────────────────────────────────────────────────────────────────────────────┤
│  If L2 hit and L1 enabled:                                                  │
│  • Store bytes in L1 cache for future fast access                           │
│  • L1Cache.put(cache_key, encrypted_or_plaintext_bytes, ttl)                │
│                                                                             │
│  Return deserialized Python object to user:                                 │
│  • Record metrics (operation="get", hit=True)                               │
│  • Structured logging (if enabled)                                          │
│  • Return cached value to user ✓                                            │
└─────────────────────────────────────────────────────────────────────────────┘


                            ═══ CACHE MISS PATH ═══

┌─────────────────────────────────────────────────────────────────────────────┐
│  STEP 11: DISTRIBUTED LOCK (Thundering Herd Protection)                     │
│  File: src/cachekit/decorators/wrapper.py                                   │
├─────────────────────────────────────────────────────────────────────────────┤
│  lock_key = f"{cache_key}:lock"                                             │
│  redis_lock = redis_client.lock(lock_key, timeout=30, blocking_timeout=5)   │
│                                                                             │
│  with redis_lock:                                                           │
│      # Only ONE thread executes function                                    │
│      # Others wait for lock or timeout                                      │
└──────────────────────────────────┬──────────────────────────────────────────┘
                                   │
                                   ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│  STEP 12: DOUBLE-CHECK PATTERN                                              │
├─────────────────────────────────────────────────────────────────────────────┤
│  # After acquiring lock, check cache again                                  │
│  # Another thread may have populated it while we waited                     │
│                                                                             │
│  cached_result = get_cached_value(cache_key)                                │
│  if cached_result:                                                          │
│      return cached_result  # Early return ✓                                 │
└──────────────────────────────────┬──────────────────────────────────────────┘
                                   │
                                   ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│  STEP 13: EXECUTE USER FUNCTION                                             │
├─────────────────────────────────────────────────────────────────────────────┤
│  result = func(*args, **kwargs)  # <-- USER FUNCTION RUNS                   │
│                                                                             │
│  Function execution time: Variable (this is what we're caching!)            │
└──────────────────────────────────┬──────────────────────────────────────────┘
                                   │
                                   ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│  STEP 14: SERIALIZATION (Python Objects → Redis)                            │
│  File: src/cachekit/serializers/default_serializer.py                       │
│  File: rust/src/byte_storage.rs                                             │
├─────────────────────────────────────────────────────────────────────────────┤
│  DefaultSerializer.serialize(result) → (bytes, metadata)                    │
│                                                                             │
│  ┌────────────────────────────────────────────────────────────┐             │
│  │ Path 1: NumPy ULTRA-OPTIMIZED (Skip Rust!)                 │             │
│  │   # Direct binary format (no compression overhead)         │             │
│  │   b"NUMPY_RAW" + dtype + shape + arr.tobytes()             │             │
│  │   • ~0.5ms for 10KB array                                  │             │
│  │   • Saves ~8ms vs Rust layer                               │             │
│  └────────────────────────────────────────────────────────────┘             │
│                                                                             │
│  ┌────────────────────────────────────────────────────────────┐             │
│  │ Path 2: DataFrame Column-wise                              │             │
│  │   1. Extract columns + index                               │             │
│  │   2. Numeric columns → bytes (tobytes())                   │             │
│  │   3. Object columns → MessagePack                          │             │
│  │   4. Pack to MessagePack                                   │             │
│  │   5. ByteStorage.store() → LZ4 + xxHash3-64                │             │
│  │   • ~2-5ms for 1K rows                                     │             │
│  └────────────────────────────────────────────────────────────┘             │
│                                                                             │
│  ┌────────────────────────────────────────────────────────────┐             │
│  │ Path 3: General Objects (MessagePack + Rust)               │             │
│  │   1. msgpack.packb(obj)                                    │             │
│  │   2. ByteStorage.store(msgpack_data, "msgpack")            │             │
│  │      ↓ Rust layer                                          │             │
│  │      a. LZ4 compress                                       │             │
│  │      b. xxHash3-64 checksum                                │             │
│  │      c. Create StorageEnvelope {                           │             │
│  │           compressed_data, checksum,                       │             │
│  │           original_size, format                            │             │
│  │         }                                                  │             │
│  │      d. Serialize envelope with MessagePack                │             │
│  │   • ~1-3ms for small dict                                  │             │
│  └────────────────────────────────────────────────────────────┘             │
└──────────────────────────────────┬──────────────────────────────────────────┘
                                   │
                                   ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│  STEP 15: L2 BACKEND SET OPERATION                                          │
│  File: src/cachekit/backends/redis_backend.py                               │
│  File: src/cachekit/cache_handler.py                                        │
├─────────────────────────────────────────────────────────────────────────────┤
│  if l2_backend is not None:                                                 │
│      l2_backend.set(cache_key, serialized_bytes, ttl=ttl)                   │
│                                                                             │
│  RedisBackend internal flow:                                                │
│  • Get Redis client from CacheClientProvider                                │
│  • If ttl: redis_client.setex(cache_key, ttl, serialized_data)              │
│  • If not ttl: redis_client.set(cache_key, serialized_data)                 │
│                                                                             │
│  Network latency: ~1-2ms (typical for Redis)                                │
└──────────────────────────────────┬──────────────────────────────────────────┘
                                   │
                                   ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│  STEP 16: UPDATE L1 CACHE                                                   │
│  File: src/cachekit/l1_cache.py                                             │
│  File: src/cachekit/cache_handler.py                                        │
├─────────────────────────────────────────────────────────────────────────────┤
│  if l1_enabled:                                                             │
│      L1Cache.put(cache_key, serialized_bytes, redis_ttl=ttl)                │
│                                                                             │
│  1. Calculate expiry: time.time() + ttl - buffer (1s)                       │
│  2. Estimate size: len(serialized_bytes)  # O(1), not recursive             │
│  3. Evict LRU entries if needed (max 100MB)                                 │
│  4. Store bytes in OrderedDict with metadata                                │
│  5. Mark as most recently used (move_to_end)                                │
│                                                                             │
│  Note: Stores bytes (encrypted or plaintext msgpack), not Python objects    │
└──────────────────────────────────┬──────────────────────────────────────────┘
                                   │
                                   ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│  STEP 17: RECORD METRICS & RETURN                                           │
│  File: src/cachekit/decorators/wrapper.py                                   │
├─────────────────────────────────────────────────────────────────────────────┤
│  • features.record_success() → Circuit breaker, adaptive timeout            │
│  • features.record_cache_operation() → Prometheus metrics                   │
│  • features.log_cache_operation() → Structured logging                      │
│                                                                             │
│  return result  # Return computed value to user ✓                           │
└─────────────────────────────────────────────────────────────────────────────┘
```

---

## Entry Point: Decorator Interface

**File:** `src/cachekit/decorators/intent.py`

### Usage Examples

```python notest
from cachekit import cache
from cachekit.config.nested import CircuitBreakerConfig

# Zero-config intelligent caching (90% of use cases, L1+L2 enabled)
@cache(backend=None)
def expensive_function():
    return compute_result()  # illustrative - not defined

# Intent-based optimization (9% of use cases)
@cache.minimal(backend=None)      # Speed-critical: minimize latency
def get_price(symbol: str):
    return fetch_price(symbol)  # illustrative - not defined

@cache.production(backend=None)      # Reliability-critical: circuit breaker + backpressure
def process_payment(amount):
    return payment_gateway.charge(amount)  # illustrative - not defined

@cache.secure(master_key="a" * 64, backend=None)    # Security-critical: client-side encryption
def get_user_data(user_id: int):
    return db.fetch_user(user_id)  # illustrative - not defined

# L1-only mode (local development, no Redis required)
@cache(backend=None)  # backend=None means L1-only
def local_dev_function():
    return computation()  # illustrative - not defined

# Manual configuration (1% of use cases)
@cache(ttl=3600, namespace="custom", circuit_breaker=CircuitBreakerConfig(enabled=True), backend=None)
def custom_function():
    return special_computation()  # illustrative - not defined
```

### Configuration Profiles

| Profile | Circuit Breaker | Adaptive Timeout | L1 Cache | L2 Backend | Stats Collection | Use Case |
|---------|----------------|------------------|----------|------------|------------------|----------|
| **default** | ✓ | ✓ | ✓ | RedisBackend | ✓ | General purpose caching (L1+L2) |
| **fast** | ✗ | ✗ | ✓ | RedisBackend | ✗ | Low-latency hot paths |
| **safe** | ✓ | ✓ | ✓ | RedisBackend | ✓ | Mission-critical reliability |
| **secure** | ✓ | ✓ | ✗ | RedisBackend | ✓ | Encrypted sensitive data (L2-only) |
| **l1-only** | ✓ | ✓ | ✓ | None | ✓ | Local development (no Redis) |

---

## Cache Key Generation

**File:** `src/cachekit/key_generator.py`

### Key Structure

```
ns:{namespace}:func:{module}.{qualname}:args:{blake2b_hash}
```

**Example:**
```python
@cache(namespace="users")
def get_user(user_id: int, include_profile: bool = True):
    return db.query(...)

# Call: get_user(123, include_profile=True)
# Key:  "ns:users:func:myapp.get_user:args:7a8b9c0d1e2f3a4b"
```

### Blake2b Hashing Algorithm

**Why Blake2b?**
- **Fast**: 2-3x faster than SHA-256
- **Secure**: Cryptographically secure (unlike xxHash)
- **Compact**: 16-byte digest = 32 hex characters
- **Collision-resistant**: Suitable for cache key deduplication

**Implementation** (lines 71-88):

```python
def _blake2b_pickle_hash(args, kwargs):
    hasher = hashlib.blake2b(digest_size=16)  # 16 bytes = 32 hex chars

    # Fast paths for primitives (no JSON overhead)
    for arg in args:
        if type(arg) is str:
            hasher.update(b"str:" + arg.encode("utf-8"))
        elif type(arg) is int:
            hasher.update(b"int:" + arg.to_bytes(...))
        # ... other primitive types
        else:
            # Complex types → JSON (secure fallback)
            hasher.update(json.dumps(arg, sort_keys=True).encode())

    return hasher.hexdigest()  # 32-char hex string
```

---

## L1 Cache Layer (In-Memory)

**File:** `src/cachekit/l1_cache.py`

### Architecture

```
┌─────────────────────────────────────────┐
│ L1 Cache (In-Memory, Per-Process)       │
│ • Stores bytes (encrypted or plaintext) │
│ • OrderedDict for LRU eviction          │
│ • Thread-safe with RLock                │
│ • TTL-aware (respects Redis TTL)        │
│ • Memory-bounded (100MB default)        │
│ • Namespace-isolated                    │
│ • Size estimation: len(bytes) O(1)      │
└─────────────────────────────────────────┘
            ↓ (on miss)
┌─────────────────────────────────────────┐
│ L2 Backend (Pluggable Storage Layer)    │
│ • RedisBackend (default)                │
│ • HTTPBackend (future)                  │
│ • DynamoDBBackend (future)              │
│ • Network call (~1-2ms latency)         │
│ • Bytes-based protocol (BaseBackend)    │
│ • Persistent across process restarts    │
└─────────────────────────────────────────┘
```

### Storage Architecture

**L1 Cache Storage (Bytes-Only):**
- **Before refactor**: Stored Python objects → recursive `sys.getsizeof()` for size estimation
- **After refactor**: Stores bytes (encrypted or plaintext msgpack) → `len(bytes)` for size estimation
- **Performance improvement**: O(1) size calculation vs recursive traversal
- **Security improvement**: Encrypted bytes at rest (decrypt-at-read-time only)

**Hierarchical Read Path:**
1. Check L1 cache (bytes) → if hit, deserialize and return
2. If L1 miss, check L2 backend (bytes) → if hit, deserialize, populate L1 with bytes, return
3. If L2 miss, execute function → serialize to bytes → store in L2 and L1 → return

### Performance Impact

**Typical Latencies:**
- **L1 Hit (raw dict lookup)**: ~500ns
- **L1 Hit (decorator + 10KB payload)**: ~242μs (0.242ms) including serialization
- **L2 Hit (Redis)**: ~2-5ms (network RTT + deserialization)
- **Speedup: 8-20x** for L1 vs L2 in production scenarios

See [Performance Guide](performance.md) for comprehensive benchmarks and component breakdown.

---

## Backend Abstraction Layer (L2 Storage)

**Files:** `src/cachekit/backends/base.py`, `src/cachekit/backends/redis_backend.py`

### Protocol-Based Design

cachekit v0.2.0 introduces a pluggable backend layer using PEP 544 protocol-based abstraction. This enables custom storage backends beyond Redis.

**BaseBackend Protocol:**
```python
from typing import Optional, Protocol, runtime_checkable

@runtime_checkable
class BaseBackend(Protocol):
    def get(self, key: str) -> Optional[bytes]: ...
    def set(self, key: str, value: bytes, ttl: Optional[int] = None) -> None: ...
    def delete(self, key: str) -> bool: ...
    def exists(self, key: str) -> bool: ...
```

**Design Principles:**
- **Bytes-only interface**: Language-agnostic, no Python-specific types
- **Stateless operations**: No connection management in protocol
- **Simple and focused**: Four operations only (get/set/delete/exists)
- **Works for any backend**: Redis, HTTP, DynamoDB, local file storage, etc.

### RedisBackend (Default Implementation)

**File:** `src/cachekit/backends/redis_backend.py`

**Features:**
- Implements BaseBackend protocol using existing CacheClientProvider
- Reuses connection pooling, thread affinity, retries from existing infrastructure
- Validates REDIS_URL with actionable error messages
- Wraps all Redis exceptions in BackendError with operation context

**Usage:**
```python notest
from cachekit.backends import RedisBackend
from cachekit import cache

# Default: reads REDIS_URL from environment
backend = RedisBackend()

# Or provide URL explicitly
backend = RedisBackend(redis_url="redis://localhost:6379")

# Use with decorator
@cache(backend=backend)
def my_function():
    return expensive_computation()  # illustrative - not defined
```

### Configuration Modes

**L1+L2 (default):** Fast L1 cache + distributed L2 backend
```python
@cache  # L1 enabled, L2 = RedisBackend
def production_function():
    return result
```

**L2-only:** Distributed caching without process memory
```python notest
from cachekit.config.nested import L1CacheConfig

@cache(l1=L1CacheConfig(enabled=False))  # L2 = RedisBackend, no L1
def memory_constrained_function():
    return result  # illustrative - not defined
```

**L1-only:** Local development, no Redis required
```python notest
@cache(backend=None)  # backend=None means L1-only mode
def local_dev_function():
    return result  # illustrative - not defined
```

**Observability-only:** No caching, metrics/circuit breaker only
```python notest
from cachekit.config.nested import L1CacheConfig

@cache(l1=L1CacheConfig(enabled=False), backend=None)  # No caching
def observability_function():
    return result  # illustrative - not defined
```

---

## Serialization Path

**Files:** `src/cachekit/serializers/default_serializer.py`, `rust/src/byte_storage.rs`

cachekit v0.1.0 uses only:

1. **DefaultSerializer** (`"raw"`, default)
   - MessagePack for Python objects
   - ByteStorage (Rust) for compression + checksums
   - Ultra-optimized NumPy path (bypasses Rust)

2. **EncryptionWrapper** (`"encrypted"`)
   - Wraps DefaultSerializer
   - AES-256-GCM client-side encryption

### Serialization Paths

#### Path 1: NumPy ULTRA-OPTIMIZED

**File:** `default_serializer.py`, lines 190-206

```python notest
import numpy as np

def _serialize_numpy(self, arr: np.ndarray) -> bytes:
    # Skip Rust ByteStorage entirely (saves ~8ms)

    # Binary format: [header][dtype][shape][raw_bytes]
    # Illustrative pseudocode showing the conceptual structure
    return (
        b"NUMPY_RAW" +
        dtype_len + dtype_str +  # dtype metadata
        shape_len + shape_data +  # shape metadata
        arr.tobytes()  # Zero-copy raw bytes
    )
```

**Why Skip Rust?**
- NumPy arrays already binary-efficient
- Random data doesn't compress well
- **Result: 8ms → 0.5ms**

#### Path 2: General Objects (MessagePack + Rust)

```python notest
import msgpack

# Python layer
msgpack_data = msgpack.packb(obj)

# Rust layer (byte_storage.rs) - conceptual flow:
# 1. LZ4 compress
# 2. xxHash3-64 checksum
# 3. Create StorageEnvelope {
#      compressed_data,
#      checksum: [u8; 8],
#      original_size: u32,
#      format: "msgpack"
#    }
# 4. Serialize envelope (MessagePack)
```

---

## L2 Backend Operations

**Files:** `src/cachekit/backends/redis_backend.py`, `src/cachekit/connection.py`

### Backend Abstraction Flow

**CacheHandler → L2 Backend → Storage:**
```python notest
from typing import Optional

# CacheHandler delegates to L2 backend
if l2_backend is not None:
    data = l2_backend.get(cache_key)  # Returns bytes or None

# RedisBackend implementation
class RedisBackend:
    def get(self, key: str) -> Optional[bytes]:
        client = self._client_provider.get_sync_client()
        return client.get(key)  # Returns bytes
```

### Thread Affinity Optimization (RedisBackend)

**Implementation:**
- Each thread gets its own Redis connection
- Thread-local storage eliminates pool lock contention
- **Result: +28% throughput, 95%+ reduction in pool overhead**

### Connection Pool Config

```yaml
max_connections: 50
min_idle_connections: 2
connection_timeout: 5.0s
socket_timeout: 5.0s
socket_keepalive: True  # TCP keepalive
```

### Graceful Backend Error Handling

**BackendError Exception:**
- Contains operation context (operation, key)
- Serializable across network boundaries
- Provides actionable error messages

**Example:**
```python notest
from cachekit.backends.base import BackendError

try:
    data = backend.get(cache_key)  # backend = previously defined backend instance
except BackendError as e:
    logger.warning(f"Backend error: {e}")
    # Graceful degradation: execute function without cache
```

---

## Deserialization Path

**Files:** `src/cachekit/serializers/default_serializer.py`, `rust/src/byte_storage.rs`

### Deserialization Flow

```python notest
from typing import Any
import msgpack

def deserialize(data: bytes, metadata) -> Any:
    # ULTRA-FAST: NumPy arrays
    if data.startswith(b"NUMPY_RAW"):
        # Parse header + np.frombuffer()
        return reconstruct_numpy(data)  # illustrative - not defined

    # Rust envelope path
    original_data, format_id = ByteStorage.retrieve(data)  # illustrative - Rust FFI
    # ↓ Rust layer:
    #   1. Deserialize envelope (MessagePack)
    #   2. Security checks (size, compression ratio)
    #   3. LZ4 decompress
    #   4. xxHash3-64 validate
    #   5. Size validate

    # MessagePack unpack
    return msgpack.unpackb(original_data)
```

### Security Protections

**Decompression Bomb Protection:**
- Max uncompressed: 512MB
- Max compressed: 512MB
- Max compression ratio: 100x

**Checksum Validation:**
- xxHash3-64 hash verification
- Detects data corruption

**Test Coverage:**
- ByteStorage module: 82% LLVM coverage (all production paths tested)
- Encryption modules: Full integration test coverage (15 tests)
- Security validation: 16 comprehensive tests for corruption detection, size limits, and bomb protection
- Note: PyO3 cdylib architecture prevents LLVM coverage measurement of encryption modules (known limitation)

---

## Performance Characteristics

### Latency Breakdown

**Production numbers for 10KB payloads (p95 latency):**

| Operation | L1 Hit | L2 Hit (Redis) | Miss (Execute) |
|-----------|--------|----------------|----------------|
| Decorator overhead | ~20μs | ~20μs | ~20μs |
| Key generation | ~2μs | ~2μs | ~2μs |
| L1 lookup (dict) | 500ns | 500ns (miss) | 500ns (miss) |
| Serialization (msgpack) | ~100μs | - | ~100μs |
| Deserialization | ~100μs | ~100μs | - |
| Redis network | - | 2-5ms | 2-5ms (set) |
| Function execution | - | - | Variable |
| **TOTAL** | **~242μs** | **~2-5ms** | **Function + ~250μs** |

**Note:** Raw L1 dict lookup is 500ns, but real user experience includes decorator and serialization overhead.

For comprehensive breakdown, see [Performance Guide](performance.md).

### Compression Ratios

| Data Type | Typical Ratio | Notes |
|-----------|---------------|-------|
| JSON-like dicts | 3-5x | Text compresses well |
| NumPy arrays | 1.0-1.2x | Skip compression |
| DataFrames | 4-8x | Column-wise excellent |
| Time series | 10-20x | Highly compressible |

---

## Error Handling

### Graceful Degradation

> [!IMPORTANT]
> Cache failures should **never** break the application.

### Error Scenarios

1. **Redis Connection Failed** → Execute function without caching
2. **Lock Acquisition Timeout** → Execute without lock (thundering herd risk accepted)
3. **Serialization Failed** → Return result, skip caching
4. **Deserialization Failed** → Execute function as if cache miss

### Circuit Breaker

**States:**
- **CLOSED**: Normal operation (requests allowed)
- **OPEN**: Too many failures (requests blocked, fallback used)
- **HALF_OPEN**: Testing recovery (limited requests)

**Fallback Strategies:**
- `fail_open` (default): Execute function without caching
- `fail_closed`: Raise exception
- `custom`: Call custom_fallback function

---

## File Reference Guide

**Decorator Layer:**
- `src/cachekit/decorators/intent.py` - Entry point, intent resolution
- `src/cachekit/decorators/wrapper.py` - Core sync/async wrapper logic
- `src/cachekit/decorators/orchestrator.py` - Reliability features

**Caching Layer:**
- `src/cachekit/key_generator.py` - Blake2b key generation
- `src/cachekit/l1_cache.py` - In-memory bytes cache (L1)
- `src/cachekit/cache_handler.py` - L1/L2 orchestration, deserialization

**Backend Layer (L2 Storage):**
- `src/cachekit/backends/base.py` - BaseBackend protocol, BackendError exception
- `src/cachekit/backends/redis_backend.py` - RedisBackend implementation
- `src/cachekit/backends/__init__.py` - Backend exports

**Serialization Layer:**
- `src/cachekit/serializers/default_serializer.py` - DefaultSerializer (MessagePack + ByteStorage)
- `src/cachekit/serializers/encryption_wrapper.py` - EncryptionWrapper (AES-256-GCM)
- `rust/src/byte_storage.rs` - Rust ByteStorage (LZ4 + xxHash3-64)
- `rust/src/encryption/` - Rust encryption module

**Connection Layer:**
- `src/cachekit/connection.py` - Redis connection pooling (used by RedisBackend)
- `src/cachekit/reliability/` - Circuit breaker, adaptive timeout

---

## See Also

- [Performance Guide](performance.md) - Real latency measurements and optimization strategies
- [Comparison Guide](comparison.md) - How cachekit's architecture compares to alternatives
- [Backend Guide](guides/backend-guide.md) - Implement custom storage backends
- [Serializer Guide](guides/serializer-guide.md) - Choose the right data format
- [Circuit Breaker](features/circuit-breaker.md) - Failure protection mechanism
- [Distributed Locking](features/distributed-locking.md) - Cache stampede prevention

---

<div align="center">

**[GitHub Issues](https://github.com/cachekit-io/cachekit-py/issues)** · **[Documentation](README.md)** · **[Security](../SECURITY.md)**

*Last Updated: 2025-12-02*

</div>
