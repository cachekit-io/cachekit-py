**[Home](README.md)** › **Getting Started**

# Getting Started with cachekit

> **Smart caching that just works - from simple to advanced**

---

## Table of Contents

- [Quick Start with Redis](#-quick-start-with-redis)
- [Progressive Disclosure](#-progressive-disclosure-choose-your-level)
- [Installation](#-installation)
- [What Makes cachekit Different](#-what-makes-cachekit-different)
- [Common Pitfalls](#-common-pitfalls-to-avoid)
- [Configuration](#-configuration)
- [Testing Your Setup](#-testing-your-setup)
- [Troubleshooting](#-troubleshooting)

---

## Quick Start with Redis

cachekit uses Redis as its backend:

```bash
# 1. Run Redis
docker run -p 6379:6379 redis

# 2. Set Redis URL
export REDIS_URL="redis://localhost:6379"
```

```python
from cachekit import cache

@cache  # Uses Redis by default
def expensive_function():
    return do_work()
```

> [!TIP]
> **What you get out of the box:**
> - Full control over infrastructure
> - On-premise deployment
> - Use existing Redis clusters
> - No external dependencies

---

## Progressive Disclosure: Choose Your Level

### Level 1: "Just Make It Work"

> [!NOTE]
> **90% of users can stop here.** This covers most use cases.

```python
from cachekit import cache

@cache  # Default: 1 hour TTL (3600s)
def expensive_function():
    return do_expensive_computation()

# First call: computes and caches
result = expensive_function()

# Second call: lightning fast from cache
result = expensive_function()
```

**Prerequisites**: Redis running + `REDIS_URL` environment variable set

**Defaults**: 1 hour TTL, no namespace, compression enabled

---

### Level 2: Basic Customization

```python
# Short-lived: API responses, session data
@cache(ttl=300)  # 5 minutes
def get_exchange_rates():
    return fetch_live_rates()

# Long-lived: reference data, configs
@cache(ttl=86400)  # 24 hours
def get_country_list():
    return fetch_countries()

# Namespaced: isolate cache domains, enable bulk invalidation
@cache(ttl=3600, namespace="users")
def get_user_profile(user_id):
    return build_profile(user_id)
```

> [!TIP]
> **Namespace benefits**: Logical grouping in Redis keys, easier debugging with `redis-cli KEYS "users:*"`,
> bulk operations via pattern matching, future cross-pod L1 invalidation support

**New powers unlocked**: TTL tuning for data freshness, namespace isolation

---

### Level 3: Serializer Selection

> [!IMPORTANT]
> cachekit uses **StandardSerializer (MessagePack)** by default for multi-language cache compatibility.

```python
# Default: StandardSerializer - language-agnostic
@cache(ttl=3600)
def compute_results():
    return {"data": [1, 2, 3], "timestamp": datetime.now()}

# For Python-only with NumPy/pandas: AutoSerializer
@cache(ttl=1800, serializer="auto")
def analyze_data():
    import numpy as np
    return np.array([1, 2, 3, 4, 5])

# For JSON APIs: OrjsonSerializer (2-5x faster)
@cache(ttl=900, serializer="orjson")
def get_api_response():
    return {"status": "ok", "data": "response"}

# For large DataFrames (10K+ rows): ArrowSerializer (6-23x faster)
@cache(ttl=7200, serializer="arrow")
def get_large_dataset():
    import pandas as pd
    return pd.read_csv("large_file.csv")
```

<details>
<summary><strong>Serializer Decision Guide</strong></summary>

| Serializer | Language Support | Best For |
|:-----------|:----------------:|:---------|
| **StandardSerializer** | Python, PHP, JS, Java, Go | Multi-language compatible, general-purpose |
| **AutoSerializer** | Python only | NumPy/pandas/UUID/set support |
| **OrjsonSerializer** | Python, PHP, JS, Java, Go | JSON-optimized, fast JSON workloads |
| **ArrowSerializer** | Python, JS, Java, Go | DataFrame-optimized (NOT PHP compatible) |

</details>

---

### Level 4: Advanced Control

```python
from cachekit import cache

# Namespace for cache organization and TTL refresh
@cache(
    ttl=7200,
    namespace="tenant_data",
    refresh_ttl_on_get=True
)
def get_tenant_data(tenant_id, request):
    return process_tenant_request(tenant_id, request)

# TTL refresh for long-lived cache entries
@cache(
    ttl=86400,
    refresh_ttl_on_get=True,
    ttl_refresh_threshold=0.3
)
def get_user(user_id):
    return fetch_user(user_id)
```

**New powers unlocked**: TTL refresh, threshold control, namespace organization

---

### Level 5: Production-Ready Features

```python
from cachekit import cache
from cachekit.config.decorator import CircuitBreakerConfig

@cache(
    ttl=3600,
    namespace="critical_service",
    refresh_ttl_on_get=True,
    ttl_refresh_threshold=0.3,
    circuit_breaker=CircuitBreakerConfig(enabled=True)
)
def critical_business_function(request_id: str):
    """Production-ready function with reliability features."""
    return process_business_logic(request_id)
```

> [!TIP]
> **Or use intent-based presets** for common configurations:
> ```python notest
> @cache.production  # All reliability features enabled
> @cache.secure      # Encryption + reliability
> @cache.minimal     # Maximum speed, minimal overhead
> ```

**New powers unlocked**: Circuit breaker, adaptive timeouts, backpressure control, comprehensive monitoring

---

## Installation

### Quick Install

```bash
# With pip
pip install cachekit

# With uv (recommended - faster)
uv add cachekit

# Start Redis if you haven't
docker run -p 6379:6379 redis
```

### Environment Setup

```bash
# Minimum required configuration
export REDIS_URL="redis://localhost:6379"

# Optional: explicit configuration
export CACHEKIT_REDIS_URL="redis://localhost:6379"
export CACHEKIT_CONNECTION_POOL_SIZE=20
```

---

## What Makes cachekit Different

### It Never Breaks Your App

```python
@cache
def critical_function():
    return important_data()

# Redis is down? No problem!
# Function executes normally, just without caching
result = critical_function()  # Works even if Redis is offline
```

### Multi-Pod Safe by Default

- Distributed locking prevents cache stampedes
- All pods see consistent data
- No local memory cache surprises

### Optimizes What Actually Matters

| Optimization | Impact | Focus |
|:-------------|:------:|:------|
| Connection pooling | **50%** improvement | We focus here |
| Network calls | 1-2ms | We accept this |
| Serialization | 50-200μs | Already fast enough |
| SIMD hashing | 0.077% improvement | We removed this |

---

## Common Pitfalls to Avoid

> [!WARNING]
> **Don't Fight the Network**
>
> Network latency dominates performance (1-2ms typical). The default serializer is already optimized.

```python
@cache  # Default serializer is perfect - optimize elsewhere
def fetch_data(key):
    return important_data()
```

> [!CAUTION]
> **Don't Add Local Caching**
>
> This breaks multi-pod consistency:
> ```python
> # WRONG: Breaks multi-pod consistency
> local_cache = {}
> @cache
> def get_data(key):
>     if key in local_cache:  # NO! Pods will disagree
>         return local_cache[key]
> ```

> [!WARNING]
> **Don't Expect Batching**
>
> Decorators work per-function, not across calls:
> ```python
> # WRONG: Can't optimize across multiple calls
> @cache
> def batch_process(items):
>     return [process(item) for item in items]
> ```

---

## Configuration

### Environment Variables

Create a `.env` file in your project root:

```bash
# Redis Configuration
CACHEKIT_REDIS_URL=redis://localhost:6379/0
CACHEKIT_CONNECTION_POOL_SIZE=20
CACHEKIT_SOCKET_TIMEOUT=1.0
CACHEKIT_SOCKET_CONNECT_TIMEOUT=1.0

# Cache Configuration
CACHEKIT_DEFAULT_TTL=3600
CACHEKIT_MAX_CHUNK_SIZE_MB=50
CACHEKIT_ENABLE_COMPRESSION=true
CACHEKIT_COMPRESSION_LEVEL=6

# Logging
LOG_LEVEL=INFO
CACHE_METRICS_ENABLED=true
```

### Python Configuration

```python
from cachekit import cache

@cache(ttl=3600, namespace="myapp")
def cached_function():
    return expensive_operation()
```

---

## Testing Your Setup

### Basic Functionality Test

```python
import time
from cachekit import cache

@cache(ttl=60)
def test_function(value):
    time.sleep(1)  # Simulate expensive operation
    return f"processed_{value}"

# First call - should take ~1 second
start = time.time()
result1 = test_function("test")
duration1 = time.time() - start
print(f"First call: {duration1:.3f}s, Result: {result1}")

# Second call - should be nearly instant
start = time.time()
result2 = test_function("test")
duration2 = time.time() - start
print(f"Second call: {duration2:.3f}s, Result: {result2}")

assert result1 == result2
assert duration2 < 0.1  # Cache hit should be <100ms (generous for CI)
print("Caching working correctly!")
```

### Performance Test

```bash
# Run the built-in performance tests
pytest tests/performance/ -v

# Or run benchmarks
uv run python -m benchmarks.cli quick --rust-only
```

---

## Troubleshooting

### Redis Connection Issues

```python
import redis

try:
    client = redis.Redis(host='localhost', port=6379, db=0)
    client.ping()
    print("Redis connection successful")
except redis.ConnectionError:
    print("Redis connection failed - check if Redis is running")
```

<details>
<summary><strong>Common Issues & Solutions</strong></summary>

**1. Redis not running**

```bash
# Start Redis (macOS)
brew services start redis

# Start Redis (Linux)
sudo systemctl start redis

# Or run Redis in Docker
docker run -d -p 6379:6379 redis:latest
```

**2. Import errors**

```bash
# Make sure package is installed
uv add cachekit

# Or reinstall
uv remove cachekit && uv add cachekit
```

**3. Performance issues**

```python
from cachekit import cache

@cache(ttl=60)
def test_function():
    return "working"

result = test_function()
print(f"Cache working: {result}")
```

</details>

---

## Production Considerations

<details>
<summary><strong>Connection Pooling</strong></summary>

```python
import redis.connection

redis_client = redis.Redis(
    connection_pool=redis.ConnectionPool(
        host='redis.company.com',
        port=6379,
        db=0,
        max_connections=50,
        socket_keepalive=True,
        health_check_interval=30
    )
)
```

</details>

<details>
<summary><strong>Error Handling</strong></summary>

```python
from cachekit import cache

@cache(ttl=3600)
def robust_function(data):
    try:
        return process_data(data)
    except Exception as e:
        logger.error(f"Processing failed: {e}")
        raise
```

</details>

<details>
<summary><strong>Monitoring</strong></summary>

```python
from cachekit import cache

@cache(ttl=3600, namespace="monitoring_example")
def monitored_function():
    return expensive_operation()

# Use the cached function multiple times to build statistics
for i in range(3):
    result = monitored_function()
    print(f"Call {i+1}: {result}")
```

</details>

---

## Next Steps

| Direction | Resource |
|:----------|:---------|
| **Previous** | [Quick Start](QUICK_START.md) - 5-minute intro |
| **Next** | [API Reference](api-reference.md) - Complete decorator parameters |

### Deep Dives

| Topic | Description |
|:------|:------------|
| [Data Flow Architecture](data-flow-architecture.md) | How cachekit works internally |
| [Performance Guide](performance.md) | Real benchmarks and optimization |
| [Comparison to Alternatives](comparison.md) | How cachekit stacks up |

### Guides & Features

| Guide | Description |
|:------|:------------|
| [Configuration Guide](configuration.md) | Detailed configuration and tuning |
| [Serializer Guide](guides/serializer-guide.md) | Choose the right serializer |
| [Backend Guide](guides/backend-guide.md) | Custom storage backends |
| [Circuit Breaker](features/circuit-breaker.md) | Failure protection |
| [Zero-Knowledge Encryption](features/zero-knowledge-encryption.md) | Client-side encryption |
| [Prometheus Metrics](features/prometheus-metrics.md) | Production observability |

### Troubleshooting

| Resource | Description |
|:---------|:------------|
| [Troubleshooting Guide](troubleshooting.md) | Solutions for common errors |
| [Error Codes](error-codes.md) | Complete error code reference |

---

<div align="center">

**[Documentation Home](README.md)** · **[GitHub Issues](https://github.com/cachekit-io/cachekit-py/issues)** · **[Discussions](https://github.com/cachekit-io/cachekit-py/discussions)**

</div>
