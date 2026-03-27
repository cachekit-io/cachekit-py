**[Home](README.md)** › **API Reference**

# cachekit - API Reference

> **Complete API documentation for caching with advanced reliability features.**

---

> [!NOTE]
> **Architecture**: cachekit uses a dual-layer L1+L2 caching architecture. See [Data Flow Architecture](data-flow-architecture.md#l1-cache-layer-in-memory) for conceptual overview and [Distributed Locking](features/distributed-locking.md) for multi-pod coordination.

---

## Core Decorators

### `@cache` - Intelligent Cache (Recommended)

**Primary Interface**: The intelligent cache decorator that automatically optimizes based on function analysis and intent. This is the main interface for cachekit.

The `@cache` decorator provides intelligent configuration selection based on function analysis or explicit intent.

```python notest
from cachekit import cache

# Zero-config: automatic optimization (90% of use cases)
@cache(backend=None)
def expensive_function():
    return do_expensive_computation()

# Intent-based optimization (9% of use cases)
# These are decorator syntax examples showing different presets:
@cache.minimal(backend=None)      # Speed-critical: trading, gaming, real-time
@cache.production(backend=None)   # Reliability-critical: payments, APIs
@cache.secure(master_key=secret_key, backend=None)  # Security-critical: PII, medical, financial (requires CACHEKIT_MASTER_KEY env var)

# Manual control when needed (1% of use cases)
@cache(ttl=3600, namespace="custom", backend=None)
def custom_function():
    return do_expensive_computation()
```

**Architecture**: The `@cache` decorator uses intelligent profile selection (fast/safe/secure) or auto-detection to configure caching behavior, then delegates to the wrapper factory for actual caching implementation.

**Intent-Based Profiles:**
- **`@cache.minimal`** - Speed profile: StandardSerializer (default, multi-language compatible), reduced monitoring overhead, optimized for performance
- **`@cache.production`** - Safety profile: StandardSerializer, all enterprise features enabled (circuit breaker, adaptive timeout, backpressure, monitoring)
- **`@cache.secure`** - Security profile: EncryptionWrapper, comprehensive audit logging, zero-knowledge caching
- **`@cache.dev`** - Development profile: Verbose logging, easy debugging, Prometheus disabled for simplicity
- **`@cache.test`** - Testing profile: Deterministic behavior, all protections disabled, no monitoring for reproducible tests
- **`@cache.io`** - cachekit.io SaaS profile: HTTP-based edge caching via api.cachekit.io, zero infrastructure required *(cachekit.io is in closed alpha — [request access](https://cachekit.io))*
- **`@cache`** - Auto-detection: Analyzes function name and signature to select optimal profile

**Implementation Details:**
- Function analysis detects security-sensitive names (`user`, `auth`, `payment`, etc.) → secure profile (EncryptionWrapper)
- High-frequency function patterns (`get`, `calc`, `compute`, etc.) → fast profile (StandardSerializer with optimizations)
- All other functions → default balanced profile (StandardSerializer)
- Manual overrides always take precedence over auto-detection

### `@cache(...)` - Manual Configuration

When you need explicit control over caching parameters, use `@cache()` with manual parameter overrides. All reliability and monitoring features can be configured individually.

> [!TIP]
> This decorator uses dependency injection to get the Redis client. You don't need to pass a `redis_client` parameter - just set the `REDIS_URL` or `CACHEKIT_REDIS_URL` environment variable.

```python
from cachekit import cache
from cachekit.config.nested import L1CacheConfig, CircuitBreakerConfig, TimeoutConfig, BackpressureConfig, MonitoringConfig

@cache(
    ttl=3600,
    namespace=None,
    safe_mode=False,
    backend=None,
    # Performance features
    refresh_ttl_on_get=False,
    ttl_refresh_threshold=0.5,
    # Nested configuration groups (see sections below for details)
    l1=L1CacheConfig(enabled=True),
    circuit_breaker=CircuitBreakerConfig(enabled=True),
    timeout=TimeoutConfig(enabled=True),
    backpressure=BackpressureConfig(max_concurrent_requests=100),
    monitoring=MonitoringConfig(collect_stats=True, enable_tracing=True),
)
def your_function(args):
    return do_expensive_computation()
```

#### Core Parameters

- **`ttl`** (`int | None`, default: `None`) - Cache time-to-live in seconds (`None` = no expiration)
- **`namespace`** (`str | None`, default: `None`) - Cache key prefix for organization
- **`safe_mode`** (`bool`, default: `False`) - Enable fail-open behavior (cache failures return `None` instead of raising)
- **`serializer`** (`str | SerializerProtocol`, default: `"default"`) - Serializer name (`"default"`, `"std"`, `"auto"`, `"arrow"`, `"orjson"`) or `SerializerProtocol` instance
- **`integrity_checking`** (`bool`, default: `True`) - Enable xxHash3-64 checksums for corruption detection
- **`key`** (`Callable[..., str] | None`, default: `None`) - Custom key function for complex types; receives `(*args, **kwargs)` and returns `str`

#### Performance Parameters

- **`refresh_ttl_on_get`** (`bool`, default: `False`) - Refresh TTL on cache hits when below threshold
- **`ttl_refresh_threshold`** (`float`, default: `0.5`) - Minimum remaining TTL fraction (0.0–1.0) to trigger refresh
- **`l1`** (`L1CacheConfig`, default: `L1CacheConfig()`) - L1 in-memory cache configuration

#### Reliability Parameters

- **`circuit_breaker`** (`CircuitBreakerConfig`, default: `CircuitBreakerConfig()`) - Circuit breaker configuration:
  - `enabled` (`bool`, default: `True`) - Enable circuit breaker protection
  - `failure_threshold` (`int`, default: `5`) - Failures before opening circuit
  - `success_threshold` (`int`, default: `3`) - Successes before closing circuit
  - `recovery_timeout` (`int`, default: `30`) - Seconds before attempting recovery
  - `half_open_requests` (`int`, default: `3`) - Test requests allowed in half-open state
  - `excluded_exceptions` (`tuple[type[Exception], ...]`, default: `()`) - Exceptions that don't trigger circuit breaker
- **`timeout`** (`TimeoutConfig`, default: `TimeoutConfig()`) - Adaptive timeout configuration:
  - `enabled` (`bool`, default: `True`) - Enable adaptive timeout
  - `initial` (`float`, default: `1.0`) - Initial timeout in seconds
  - `min` (`float`, default: `0.1`) - Minimum timeout in seconds
  - `max` (`float`, default: `5.0`) - Maximum timeout in seconds
  - `window_size` (`int`, default: `1000`) - Sliding window size for percentile calculation
  - `percentile` (`float`, default: `95.0`) - Target percentile for timeout calculation
- **`backpressure`** (`BackpressureConfig`, default: `BackpressureConfig()`) - Backpressure configuration:
  - `enabled` (`bool`, default: `True`) - Enable backpressure protection
  - `max_concurrent_requests` (`int`, default: `100`) - Maximum concurrent cache requests
  - `queue_size` (`int`, default: `1000`) - Queue size for waiting requests
  - `timeout` (`float`, default: `0.1`) - Seconds to wait in queue before giving up

#### Monitoring Parameters

- **`monitoring`** (`MonitoringConfig`, default: `MonitoringConfig()`) - Observability configuration:
  - `collect_stats` (`bool`, default: `True`) - Collect cache hit/miss statistics
  - `enable_tracing` (`bool`, default: `True`) - Enable distributed tracing
  - `enable_structured_logging` (`bool`, default: `True`) - Enable structured JSON logging
  - `enable_prometheus_metrics` (`bool`, default: `True`) - Export Prometheus metrics

#### Encryption Parameters

- **`encryption`** (`EncryptionConfig`, default: `EncryptionConfig()`) - Client-side encryption configuration (use `@cache.secure` preset instead of configuring directly)

#### Returns
- Cached function result or fresh computation result
- Decorated function includes additional health check methods: `get_health_status()` and `check_health()`

#### Examples

**Modern Intelligent Interface:**
```python
from cachekit import cache

# First, set environment variable:
# export REDIS_URL="redis://localhost:6379"

@cache  # Auto-detects optimal configuration
def analyze_dataset(dataset_id, filters=None):
    """Analyze large dataset with automatic caching."""
    return perform_analysis(dataset_id, filters)

@cache.production  # All reliability features enabled automatically
def critical_business_function():
    return important_computation()

@cache.dev  # Development: verbose logging, no Prometheus
def debug_function():
    return process_data()

@cache.test  # Testing: deterministic, no protections
def test_cacheable_function():
    return compute_value()
```

**Manual Configuration (fully supported):**
```python
from cachekit import cache
from cachekit.config.nested import CircuitBreakerConfig, TimeoutConfig, BackpressureConfig, MonitoringConfig

@cache(ttl=1800, namespace="analytics", backend=None)
def explicit_function(dataset_id, filters=None):
    return process_data(f"dataset_{dataset_id}")

# Manual configuration with nested configs for reliability features
@cache(
    ttl=3600,
    namespace="critical_data",
    backend=None,
    circuit_breaker=CircuitBreakerConfig(enabled=True),
    timeout=TimeoutConfig(enabled=True),
    backpressure=BackpressureConfig(enabled=True),
    monitoring=MonitoringConfig(collect_stats=True, enable_structured_logging=True)
)
def critical_business_logic():
    return do_expensive_computation()

# The decorator automatically connects to Redis using the environment variable
```

### `@cache.io` — cachekit.io SaaS Backend

> *cachekit.io is in closed alpha — [request access](https://cachekit.io)*

**Profile for zero-infrastructure caching** via the cachekit.io edge network. Automatically configures `CachekitIOBackend` (HTTP) as the L2 backend with production-grade reliability settings (circuit breaker, adaptive timeout, L1 in-memory cache).

#### Environment Variables

| Variable | Required | Default | Description |
|---|---|---|---|
| `CACHEKIT_API_KEY` | **Yes** | — | API key (`ck_live_...`) for authentication |
| `CACHEKIT_API_URL` | No | `https://api.cachekit.io` | API endpoint override |
| `CACHEKIT_TIMEOUT` | No | `5.0` | HTTP request timeout in seconds |
| `CACHEKIT_MAX_RETRIES` | No | `3` | Maximum retry attempts for transient errors |

#### Usage

```python notest
import os
from cachekit import cache

# Set your API key (or use environment variable)
# export CACHEKIT_API_KEY=ck_live_your_key_here

@cache.io(ttl=300)
def get_product_catalog(category: str):
    """Cached at the edge — no Redis needed."""
    return fetch_products_from_db(category)

@cache.io(ttl=3600, namespace="reference_data")
def get_exchange_rates():
    """Multi-region edge caching with automatic L1 (in-memory) layer."""
    return fetch_exchange_rates_from_api()
```

#### Raises

- **`ConfigurationError`**: If `CACHEKIT_API_KEY` is not set

#### Notes

- Inherits all production-grade reliability features: circuit breaker, adaptive timeout, backpressure, full monitoring
- L1 in-memory cache is enabled — hot data is served at ~50ns without an HTTP round-trip
- Standard `ttl`, `namespace`, `serializer`, and other `@cache(...)` kwargs are all supported as overrides

### Health Check Methods

All functions decorated with `@cache` automatically include health check methods for monitoring and observability:

**Note**: These methods are added to the decorated function and provide comprehensive health monitoring capabilities for production deployments.

#### `get_health_status()`

Returns current health status including circuit breaker state, backpressure metrics, and adaptive timeout information.

```python
@cache(ttl=300, namespace="api")
def api_function():
    return "data"

# Get health status
health_status = api_function.get_health_status()
print(health_status)
# {
#     "namespace": "api",
#     "features_enabled": {
#         "circuit_breaker": True,
#         "adaptive_timeout": True,
#         "backpressure": True,
#         "statistics": True,
#         "structured_logging": True
#     },
#     "circuit_breaker": {
#         "state": "CLOSED",
#         "failure_count": 0,
#         "success_count": 15,
#         "last_failure_time": null,
#         "next_attempt": null
#     },
#     "backpressure": {
#         "max_concurrent": 100,
#         "current_requests": 2,
#         "rejected_requests": 0
#     },
#     "adaptive_timeout": {
#         "current_timeout": 1.2,
#         "base_timeout": 1.0,
#         "average_duration": 0.85
#     }
# }
```

#### `check_health()`

Performs an active health check and returns comprehensive status including both decorator-specific and system-wide health information.

```python notest
# Perform health check
health_result = api_function.check_health()  # api_function = previously decorated function
print(health_result)
# {
#     "decorator": { ... },  # Same as get_health_status()
#     "system": {
#         "status": "healthy",
#         "redis_connection": "active",
#         "connection_pool": {
#             "created_connections": 2,
#             "available_connections": 8,
#             "in_use_connections": 2
#         }
#     }
# }
```

## Modular Architecture

cachekit uses a modular architecture for better maintainability and testability:

### Decorator Module Structure

```python notest
# New modular structure - internal implementation detail
from cachekit.decorators.orchestrator import FeatureOrchestrator

# Orchestrator manages all reliability features (internal use)
# Users should use @cache decorator with nested configs instead
orchestrator = FeatureOrchestrator(
    namespace="api_service",
    circuit_breaker_enabled=True,
    adaptive_timeout_enabled=True,
    collect_stats=True
)
```

**Key Components:**
- **FeatureOrchestrator**: Manages reliability features (circuit breaker, adaptive timeout, backpressure, statistics, logging)
- **CachedRedisClientProvider**: Thread-local Redis client caching for performance
- **Configuration Caching**: LRU cached configuration objects to eliminate overhead

**Architecture Note**: The implementation uses `FeatureOrchestrator` for better separation of concerns and improved modularity.

### Internal Reliability Components

These components work behind the scenes to provide reliability features:

#### `AsyncMetricsCollector`
Non-blocking metrics collection system that prevents performance degradation:
- Queue-based collection with overflow protection
- Background thread processing
- Self-healing worker thread management
- Zero impact on critical path latency

#### `AdaptiveTimeoutManager`
Dynamic timeout calculation for Redis operations:
- Tracks P95 latency over sliding window (default: 1000 operations)
- Adjusts timeouts between min/max bounds (0.1s - 10s)
- Separate tracking for lock operations vs. data operations
- Provides detailed statistics via `get_stats()` method

#### `RedisErrorClassifier`
Intelligent error categorization for circuit breaker decisions:
- Distinguishes transient vs. permanent failures
- Prevents application errors from triggering circuit breaker
- Enables targeted recovery strategies

#### `CachedRedisClientProvider`
Thread-local Redis client caching:
- Eliminates repeated client creation overhead
- Thread-safe client reuse
- 28% performance improvement in benchmarks

### Async Function Caching

The `@cache` decorator automatically detects and handles async functions without requiring a separate decorator.

```python
from cachekit import cache

@cache(ttl=900)
async def fetch_user_data(user_id):
    """Fetch user data with async caching."""
    async with httpx.AsyncClient() as client:
        response = await client.get(f"/api/users/{user_id}")
        return response.json()
```

> [!TIP]
> The `@cache` decorator automatically detects async functions and uses async Redis operations. No special decorator needed.

---

## Serializers

> [!IMPORTANT]
> cachekit uses **StandardSerializer (language-agnostic MessagePack) by default** to ensure cache data is compatible across Python, PHP, JavaScript, Java, and other languages.

### Serializer Decision Tree

Choose your serializer based on your use case:

<details>
<summary><strong>Expand Decision Tree</strong></summary>

```
Does your app need multi-language cache access (PHP/JS/Java/etc)?
├─ YES → Use StandardSerializer (default)
│   └─ Works with: Python, PHP, JavaScript, Java, R, Go
│   └─ Supports: None, bool, int, float, str, bytes, list, tuple, dict, datetime, date, time
│   └─ Example: @cache(ttl=3600)  # No serializer parameter needed
│
└─ NO → Is your data Python-specific?
    ├─ NumPy arrays / pandas DataFrames / UUID / set / custom classes?
    │   ├─ NumPy/UUID/set → Use AutoSerializer
    │   │   └─ Example: @cache(serializer="auto")
    │   │
    │   └─ Large DataFrames (10K+ rows)?
    │       └─ Use ArrowSerializer (6-23x faster)
    │       └─ Example: @cache(serializer="arrow")
    │
    └─ JSON API responses / JSON-heavy workloads?
        └─ Use OrjsonSerializer (2-5x faster than stdlib json)
        └─ Example: @cache(serializer="orjson")
```

</details>

### Language Compatibility Matrix

| Serializer | Python | PHP | JavaScript | Java/R | Go | Use Case |
|---|---|---|---|---|---|---|
| **StandardSerializer** (default) | ✅ | ✅ | ✅ | ✅ | ✅ | **Multi-language, language-agnostic** |
| AutoSerializer | ✅ | ❌ | ❌ | ❌ | ❌ | Python-only with NumPy/pandas/UUID |
| OrjsonSerializer | ✅ | ✅ | ✅ | ✅ | ✅ | JSON-native data (same as StandardSerializer) |
| ArrowSerializer | ✅ | ❌ | ✅ | ✅ | ✅ | DataFrames (NOT PHP compatible) |

> [!WARNING]
> - **ArrowSerializer is NOT PHP-compatible** - Use StandardSerializer or OrjsonSerializer for PHP
> - Changing serializers requires cache invalidation (see Serializer Switching section below)

> [!TIP]
> **StandardSerializer is the default** - No configuration needed for multi-language compatibility.

### Using StandardSerializer (Default)

StandardSerializer is **automatically used** when you don't specify a serializer:

```python
from cachekit import cache
from datetime import datetime

# StandardSerializer is the default - language-agnostic MessagePack
@cache(ttl=3600)
def compute_results(user_id: int):
    return {
        "id": user_id,
        "timestamp": datetime.now(),
        "data": [1, 2, 3],
        "nested": {"key": "value"}
    }

# Cache is compatible with Python, PHP, JavaScript, Java, etc.
result = compute_results(123)  # Multi-language compatible
```

**Supported Types**:
- Primitives: `None`, `bool`, `int`, `float`, `str`, `bytes`
- Collections: `list`, `tuple`, `dict`
- Dates: `datetime`, `date`, `time` (ISO-8601 format via MessagePack extension)

**Explicitly NOT supported** (raises `TypeError`):
- NumPy arrays → Use `serializer="auto"`
- pandas DataFrames/Series → Use `serializer="arrow"`
- UUID, set, frozenset → Use `serializer="auto"`
- Pydantic models, ORM models → Convert to dict first
- Custom classes → Convert to dict first

### Using AutoSerializer (Python-Only)

Use AutoSerializer when you need Python-specific types but **don't need multi-language compatibility**:

```python
from cachekit import cache
from cachekit.serializers import AutoSerializer
import numpy as np
import uuid

# AutoSerializer for Python-specific types (NumPy, UUID, set, etc.)
@cache(serializer="auto", ttl=3600)
def process_numpy_data():
    return {
        "array": np.array([1, 2, 3, 4, 5]),
        "id": uuid.uuid4(),
        "tags": {"python", "caching", "numpy"}  # set support
    }

# Only accessible from Python - not compatible with PHP/JS/Java
result = process_numpy_data()
```

### Using OrjsonSerializer (JSON-Optimized)

Use OrjsonSerializer for JSON-heavy workloads and APIs:

```python notest
from cachekit import cache
from cachekit.serializers import OrjsonSerializer

# OrjsonSerializer for JSON APIs (2-5x faster than stdlib json)
@cache(serializer="orjson", ttl=900, backend=None)
def fetch_api_response(endpoint: str):
    return {
        "status": "success",
        "data": fetch_external_api(endpoint)  # illustrative - external API call
    }

# Equivalent to StandardSerializer for language compatibility,
# but optimized for JSON serialization speed
response = fetch_api_response("/users/123")
```

**When to use OrjsonSerializer**:
- JSON APIs (already producing JSON)
- Speed matters for JSON serialization
- Still want multi-language compatibility (same as StandardSerializer)

### Using ArrowSerializer (DataFrame-Optimized)

Use ArrowSerializer for large DataFrames (10K+ rows):

```python notest
from cachekit import cache
from cachekit.serializers import ArrowSerializer
import pandas as pd

# ArrowSerializer for DataFrames (6-23x faster for large data)
@cache(serializer="arrow", ttl=7200, backend=None)
def load_large_dataset(date: str):
    return pd.read_csv(f"data/{date}.csv")  # illustrative - file may not exist

# Returns pandas DataFrame directly
df = load_large_dataset("2024-01-01")

# Can also return as polars or pyarrow
@cache(serializer=ArrowSerializer(return_format="polars"), ttl=7200)
def load_polars_data():
    import polars as pl
    return pl.read_csv("data.csv")
```

**Performance**:
- **10K rows**: 5.0x faster than StandardSerializer
- **100K rows**: 9.6x faster than StandardSerializer
- **1M rows**: 20x+ faster than StandardSerializer

> [!CAUTION]
> ArrowSerializer is **NOT PHP-compatible**. Use StandardSerializer or OrjsonSerializer if you need PHP support.

### Serializer Parameter Format

The `serializer` parameter accepts:

```python notest
# By name (string)
@cache(serializer="std", backend=None)          # StandardSerializer (alias)
@cache(serializer="auto", backend=None)         # AutoSerializer
@cache(serializer="orjson", backend=None)       # OrjsonSerializer
@cache(serializer="arrow", backend=None)        # ArrowSerializer

# By instance (for configuration)
@cache(serializer=ArrowSerializer(return_format="polars"), backend=None)
@cache(serializer=OrjsonSerializer(option=orjson.OPT_SORT_KEYS), backend=None)

# No parameter = default StandardSerializer
@cache(ttl=3600, backend=None)  # Uses StandardSerializer automatically
```

### Serializer Switching

When you change a function's serializer, the decorator **automatically detects mismatches**:

```python
# BEFORE: Using StandardSerializer (default)
@cache
def get_data():
    return df

# AFTER: Switching to ArrowSerializer
@cache(serializer="arrow")
def get_data():
    return df

# First call after change:
# 1. Cache hit returns old StandardSerializer data
# 2. Deserializer detects format mismatch
# 3. Error message explains the mismatch
# 4. Function executes, caches with new serializer
# 5. Subsequent calls work normally
```

**Best Practice**: Use namespace versioning for zero-downtime migrations:

```python
# V1: StandardSerializer (existing production)
@cache(namespace="user_data:v1")
def get_user_data_v1(user_id):
    return {"id": user_id, "name": "Alice"}

# V2: ArrowSerializer (new deployment, different namespace)
@cache(serializer="arrow", namespace="user_data:v2")
def get_user_data_v2(user_id):
    return pd.DataFrame({"id": [user_id], "name": ["Alice"]})

# Gradual migration: switch function name in codebase
```

## Configuration Classes

### `CachekitConfig`

Configuration class for backend-agnostic cache settings. Based on `pydantic-settings` for automatic environment variable loading with the `CACHEKIT_` prefix. Redis connection settings (URL, pool size, timeouts) live on `RedisBackendConfig`, not here.

**Key Fields:**
- **`default_ttl`** (`int`, default: `3600`) - Default cache TTL in seconds (env: `CACHEKIT_DEFAULT_TTL`)
- **`enable_compression`** (`bool`, default: `True`) - Enable LZ4 compression (env: `CACHEKIT_ENABLE_COMPRESSION`)
- **`compression_level`** (`int`, default: `6`) - Zlib compression level 1–9 (env: `CACHEKIT_COMPRESSION_LEVEL`)
- **`max_chunk_size_mb`** (`int`, default: `50`) - Maximum cache chunk size in MB (env: `CACHEKIT_MAX_CHUNK_SIZE_MB`)
- **`max_retries`** (`int`, default: `3`) - Maximum retry attempts (env: `CACHEKIT_MAX_RETRIES`)
- **`retry_delay_ms`** (`int`, default: `100`) - Delay between retries in milliseconds (env: `CACHEKIT_RETRY_DELAY_MS`)
- **`l1_enabled`** (`bool`, default: `True`) - Enable L1 in-memory cache (env: `CACHEKIT_L1_ENABLED`)
- **`l1_max_size_mb`** (`int`, default: `100`) - Maximum L1 cache size per namespace in MB (env: `CACHEKIT_L1_MAX_SIZE_MB`)
- **`enable_prometheus_metrics`** (`bool`, default: `True`) - Enable Prometheus metrics collection (env: `CACHEKIT_ENABLE_PROMETHEUS_METRICS`)
- **`master_key`** (`SecretStr | None`, default: `None`) - Master encryption key for `@cache.secure` (env: `CACHEKIT_MASTER_KEY`)

**Environment Variable Priority:** `CACHEKIT_*` variables take precedence over fallback variables (e.g., `CACHEKIT_REDIS_URL` > `REDIS_URL`).

#### Example
```python
from cachekit.config import CachekitConfig

# Load from environment variables (recommended)
config = CachekitConfig()

# Or override specific fields
config = CachekitConfig(
    default_ttl=7200,
    l1_enabled=True,
    l1_max_size_mb=100,
)
```

**Note:** Configuration is typically loaded automatically via environment variables. Explicit configuration is rarely needed.


---

## Error Handling and Classification

The library includes intelligent error classification to distinguish between transient and permanent failures.

> [!NOTE]
> For detailed error information, solutions, and troubleshooting, see:
> - **[Troubleshooting Guide](troubleshooting.md)** - Common errors and solutions
> - **[Error Codes Reference](error-codes.md)** - Complete error code catalog

### Error Categories

1. **Transient Errors** (trigger circuit breaker):
   - `ConnectionError`, `TimeoutError` - Network issues
   - `BusyLoadingError`, `TryAgainError` - Redis temporarily unavailable
   - `ConnectionPoolError` - Pool exhausted

2. **Permanent Errors** (don't trigger circuit breaker):
   - `AuthenticationError` - Wrong credentials
   - `DataError`, `InvalidResponse` - Protocol issues
   - `LockError` - Lock acquisition failures

3. **Application Errors** (ignored by circuit breaker):
   - User code exceptions
   - Business logic errors

### Connection Failures
When Redis is unavailable:
1. Function executes without caching
2. Warning is logged (if logging configured)
3. No exception is raised to the caller

### Serialization Failures
If data cannot be serialized:
1. Function result is returned without caching
2. Warning is logged
3. No exception is raised to the caller

For error examples and handling patterns, see [Troubleshooting Guide](troubleshooting.md).


## Backend Abstraction

cachekit uses a protocol-based backend abstraction (PEP 544) that allows pluggable storage backends for L2 cache. Built-in backends include Redis, CachekitIO, File, and Memcached. You can also implement custom backends for any key-value store.

For comprehensive backend guide with examples and implementation patterns, see **[Backend Guide](backends/index.md)**.

### Backend Resolution Priority

When `@cache` is used without explicit `backend` parameter, resolution follows this 3-tier priority:

1. **Explicit backend parameter** (highest priority)
   ```python notest
   custom_backend = HTTPBackend("https://api.example.com")
   @cache(backend=custom_backend)  # Uses custom backend explicitly
   def my_function():
       return "result"
   ```

2. **Default RedisBackend** (middle priority)
   ```python notest
   @cache  # Uses RedisBackend with CACHEKIT_REDIS_URL or REDIS_URL
   def my_function():
       return "result"
   ```

3. **Environment variable configuration** (lowest priority)
   ```bash
   # Primary: CACHEKIT_REDIS_URL
   CACHEKIT_REDIS_URL=redis://localhost:6379/0

   # Fallback: REDIS_URL
   REDIS_URL=redis://localhost:6379/0
   ```

### L1-Only Mode (No Backend)

For local development or when Redis is unavailable, use L1-only mode:

```python
@cache(backend=None, l1_enabled=True)
def local_only_cache():
    """Cached in process memory only, no Redis required."""
    return computation()
```

**Note**: L1-only mode is process-local and not shared across pods/workers. Use for development or single-process applications only.

For complete backend implementation details, see [Backend Guide - BaseBackend Protocol](backends/index.md#basebackend-protocol) and [Backend Guide - Custom Implementation](backends/custom.md).


## Environment Variables

cachekit is configured through environment variables. For detailed setup and troubleshooting, see **[Configuration Guide](configuration.md)**.

### Standard Configuration

```bash
# Redis Connection (configured on RedisBackend, not CachekitConfig)
CACHEKIT_REDIS_URL=redis://localhost:6379/0

# Cache Behavior
CACHEKIT_DEFAULT_TTL=3600
CACHEKIT_MAX_CHUNK_SIZE_MB=50
CACHEKIT_ENABLE_COMPRESSION=true
CACHEKIT_COMPRESSION_LEVEL=6

# Encryption (for @cache.secure)
CACHEKIT_MASTER_KEY=<hex-encoded-32-bytes-minimum>

# Fallback: REDIS_URL also supported (lower priority)
REDIS_URL=redis://localhost:6379/0

# Logging
LOG_LEVEL=INFO
```

### Variable Precedence

- **CACHEKIT_REDIS_URL** takes precedence over REDIS_URL
- All configuration variables must start with **CACHEKIT_** (not CACHE_)
- Variables must be exported (not just set in shell)

For detailed precedence rules and troubleshooting, see [Configuration Guide - Environment Variable Precedence](configuration.md#environment-variable-precedence) and [Configuration Guide - Troubleshooting Configuration](configuration.md#troubleshooting-configuration).

## Type Hints

cachekit includes comprehensive type hints with full basedpyright type checking (zero errors):

```python
from __future__ import annotations

from typing import Any
from cachekit import cache

@cache(ttl=3600)
def typed_function(data: dict[str, Any]) -> str | int | None:
    return process_data(data)
```

**Type Safety**: The library uses `from __future__ import annotations` for Python 3.9+ compatibility, enabling modern union syntax (`X | Y`) while maintaining backward compatibility.

## Monitoring and Observability

### Prometheus Metrics

The library exposes comprehensive metrics (enabled by default):

- `cachekit_cache_operations_total` - Operation counts by operation, status, serializer, namespace
- `cachekit_cache_operation_duration_seconds` - Latency histograms with optimized buckets
- `cachekit_circuit_breaker_state` - Circuit breaker state per namespace (0=closed, 1=open, 2=half-open)
- `cachekit_connection_pool_utilization` - Pool usage ratio (0.0-1.0)
- `cachekit_connection_pool_usage` - Detailed pool statistics (created, available, in_use)
- `cachekit_serialization_fallbacks_total` - Serializer fallback tracking

### Structured Logging

All operations include structured logging with:
- Correlation IDs for request tracking
- Operation context (namespace, cache key, serializer)
- Performance metrics (duration, cache hit/miss)
- Error classification and recovery actions

### Health Monitoring

Pre-built Grafana dashboards available in `/monitoring/grafana/`:
- Cache Overview Dashboard
- Reliability Metrics Dashboard
- Performance Analysis Dashboard

## Best Practices

### Connection Management
```python
# Connection pooling is automatically enabled on RedisBackend
# Configure pool size via RedisBackendConfig or the backend's environment variables
```

### Namespace Organization
```python
# Organize cache keys with namespaces
@cache(namespace="user_data")
def get_user_profile(user_id): ...

@cache(namespace="analytics")
def get_user_metrics(user_id): ...
```

### TTL Strategy
```python
# Short TTL for frequently changing data
@cache(ttl=300)  # 5 minutes
def get_live_prices(): ...

# Long TTL for stable data
@cache(ttl=86400)  # 24 hours
def get_reference_data(): ...
```

---

## Next Steps

**Previous**: [Getting Started Guide](getting-started.md) - Learn the fundamentals
**Next Feature Deep Dives**:
- [Circuit Breaker](features/circuit-breaker.md) - Failure protection
- [Adaptive Timeouts](features/adaptive-timeouts.md) - Smart timeout management
- [Distributed Locking](features/distributed-locking.md) - Multi-pod safety

## See Also

### Related Guides
- [Serializer Guide](serializers/index.md) - Choose the right serializer for your data types
- [Backend Guide](backends/index.md) - Custom storage backend implementation
- [Configuration Guide](configuration.md) - Environment variable setup and tuning
- [Troubleshooting Guide](troubleshooting.md) - Debugging and error solutions
- [Error Codes](error-codes.md) - Complete error code reference

### Architecture & Performance

| Resource | Description |
|:---------|:------------|
| [Data Flow Architecture](data-flow-architecture.md) | How L1+L2 caching works |
| [Performance Guide](performance.md) | Real benchmarks and latency characteristics |
| [Prometheus Metrics](features/prometheus-metrics.md) | Production observability setup |

---

<div align="center">

**[Getting Started](getting-started.md)** · **[Configuration](configuration.md)** · **[Troubleshooting](troubleshooting.md)**

</div>
