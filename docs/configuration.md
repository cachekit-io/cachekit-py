**[Home](README.md)** › **Configuration Guide**

# Configuration Guide

> **Configure cachekit through environment variables and decorator parameters**

---

## Backend Options

cachekit supports multiple backend options:

### Redis Backend

For self-hosted Redis infrastructure:

**Usage:**
```python
from cachekit import cache

@cache  # Uses Redis backend by default
def fetch_data():
    return expensive_computation()
```

**Configuration:** See [Redis Environment Variables](#redis-connection-for-cachekitconfig) below.

---

## Environment Variables

### Redis Connection (for CachekitConfig)

Configure Redis backend through environment variables:

```bash
# Redis Connection
CACHEKIT_REDIS_URL=redis://localhost:6379/0
CACHEKIT_CONNECTION_POOL_SIZE=10
CACHEKIT_SOCKET_TIMEOUT=1.0
CACHEKIT_SOCKET_CONNECT_TIMEOUT=1.0

# Cache Behavior
CACHEKIT_DEFAULT_TTL=3600
CACHEKIT_MAX_CHUNK_SIZE_MB=50
CACHEKIT_ENABLE_COMPRESSION=true
CACHEKIT_COMPRESSION_LEVEL=6

# Encryption (for @cache.secure)
CACHEKIT_MASTER_KEY=<hex-encoded-key-32-bytes-minimum>

# Fallback: REDIS_URL also supported (lower priority)
REDIS_URL=redis://localhost:6379/0

# Logging
LOG_LEVEL=INFO

# Performance Testing
REQUESTS_CA_BUNDLE=  # Unset to avoid SSL issues
```

---

## L1 Cache Configuration

Configure L1 (in-memory) cache behavior through `L1CacheConfig`:

```python notest
from cachekit import cache
from cachekit.config import L1CacheConfig

# Enable SWR (stale-while-revalidate) with custom threshold
@cache(
    l1=L1CacheConfig(  # Correct parameter name is 'l1', not 'l1_config'
        enabled=True,
        max_size_mb=100,
        swr_enabled=True,
        swr_threshold_ratio=0.5,  # Refresh at 50% of TTL
        invalidation_enabled=True,
        namespace_index=True,
    ),
    backend=None
)
def my_function():
    return expensive_computation()  # illustrative - not defined
```

### L1CacheConfig Fields

| Field | Type | Default | Purpose |
|-------|------|---------|---------|
| `enabled` | bool | `True` | Enable L1 in-memory cache |
| `max_size_mb` | int | `100` | Maximum L1 cache size in MB |
| `swr_enabled` | bool | `True` | Enable stale-while-revalidate (SWR) |
| `swr_threshold_ratio` | float | `0.5` | Refresh at X% of TTL (0.1-1.0) |
| `invalidation_enabled` | bool | `True` | Enable invalidation event broadcasts |
| `namespace_index` | bool | `True` | Enable fast namespace-based invalidation |

**L1 Cache Concepts:**
- **Freshness**: When to serve stale data + trigger background refresh (SWR)
- **Expiry**: Hard deadline when entry is deleted from cache
- **Namespace**: Logical grouping for bulk invalidation (see [L1 Invalidation Guide](features/l1-invalidation.md))

### Intent Presets

Use intent presets to configure L1 and other features for different use cases:

```python notest
from cachekit import cache

# Zero overhead - all features disabled
@cache.minimal(backend=None)
def minimal_function():
    pass

# Development - SWR enabled, invalidation disabled
@cache.dev(backend=None)
def dev_function():
    pass

# Production - all features enabled
@cache.production(backend=None)
def prod_function():
    pass

# Testing - deterministic behavior
@cache.test(backend=None)
def test_function():
    pass

# Secure - encryption + all features
@cache.secure(master_key="a" * 64, backend=None)
def secure_function():
    pass
```

**Feature Matrix by Intent:**

| Intent | SWR | Invalidation | Namespace Index | Max Size |
|--------|-----|--------------|-----------------|----------|
| `minimal()` | ❌ | ❌ | ❌ | 100 MB |
| `test()` | ❌ | ❌ | ❌ | 100 MB |
| `dev()` | ✓ | ❌ | ❌ | 100 MB |
| `production()` | ✓ | ✓ | ✓ | 100 MB |
| `secure()` | ✓ | ✓ | ✓ | 100 MB |

---

## Environment Variable Precedence

> [!IMPORTANT]
> When multiple environment variables could apply, cachekit follows this priority order.

### Redis URL Priority

| Priority | Variable | Description |
|:--------:|:---------|:------------|
| 1 | `CACHEKIT_REDIS_URL` | Explicit cachekit-specific connection |
| 2 | `REDIS_URL` | Fallback (only if CACHEKIT_REDIS_URL not set) |

**Example**:
```bash
# This configuration uses CACHEKIT_REDIS_URL (REDIS_URL is ignored)
export CACHEKIT_REDIS_URL=redis://prod.example.com:6379/0
export REDIS_URL=redis://localhost:6379/0  # Ignored - won't be used
```

## Common Configuration Patterns

### Development (In-Memory L1 Cache Only)

For local development without Redis:

```bash
# Disable Redis backend, use L1 only
# Set decorator with backend=None
```

```python
@cache(backend=None, l1_enabled=True)
def local_only_cache():
    """Cached in process memory only, no Redis required."""
    return computation()
```

**Note**: L1-only mode is process-local and not shared across pods/workers. Use for development or single-process applications only.

### Production (Redis + L1 Cache)

For production with Redis:

```bash
export CACHEKIT_REDIS_URL=redis://redis-primary:6379/0
export CACHEKIT_CONNECTION_POOL_SIZE=20
export CACHEKIT_DEFAULT_TTL=3600
export CACHEKIT_ENABLE_COMPRESSION=true
```

### Secure Production (Encryption Enabled)

For production with sensitive data:

```bash
export CACHEKIT_REDIS_URL=redis://redis-primary:6379/0
export CACHEKIT_MASTER_KEY=$(openssl rand -hex 32)
export CACHEKIT_ENABLE_COMPRESSION=true
export LOG_LEVEL=WARNING
```

---

## Troubleshooting Configuration

<details>
<summary><strong>Issue: "Redis connection error"</strong></summary>

**Causes**:
1. Redis service not running
2. Wrong Redis URL format
3. Firewall blocking connection
4. Authentication failed

**Solutions**:
```bash
# Check Redis is running
redis-cli ping
# Output: PONG

# Verify connection string format
export CACHEKIT_REDIS_URL=redis://localhost:6379/0
# NOT: redis:localhost:6379 (wrong)
# NOT: redis/localhost:6379 (wrong)

# Test connection from Python
python -c "
import redis
r = redis.from_url('redis://localhost:6379/0')
print(r.ping())  # Should print True
"
```

</details>

<details>
<summary><strong>Issue: Both CACHEKIT_REDIS_URL and REDIS_URL set - which is used?</strong></summary>

Priority order: `CACHEKIT_REDIS_URL` > `REDIS_URL`

If you have both set and want to verify which is being used:

```python
import os
from cachekit import cache

# Check which URL was loaded
print(f"CACHEKIT_REDIS_URL: {os.getenv('CACHEKIT_REDIS_URL')}")
print(f"REDIS_URL: {os.getenv('REDIS_URL')}")

# CACHEKIT_REDIS_URL takes precedence if both are set
@cache()
def test_function():
    return "cached"
```

</details>

<details>
<summary><strong>Issue: Wrong prefix used (CACHE_ instead of CACHEKIT_)</strong></summary>

> [!CAUTION]
> All cachekit environment variables start with **CACHEKIT_** (not CACHE_).

```bash
# WRONG - won't be read:
export CACHE_REDIS_URL=redis://localhost:6379

# CORRECT - will be read:
export CACHEKIT_REDIS_URL=redis://localhost:6379
```

</details>

<details>
<summary><strong>Issue: Variable not being read</strong></summary>

Check if variable is properly exported:

```bash
# Print all CACHEKIT variables
env | grep CACHEKIT

# Ensure variable is exported (not just set in shell)
export CACHEKIT_REDIS_URL=redis://localhost:6379  # Correct - variable is exported
CACHEKIT_REDIS_URL=redis://localhost:6379  # Wrong - not exported to child processes
```

</details>

<details>
<summary><strong>Issue: "CACHEKIT_MASTER_KEY not set" when using encryption</strong></summary>

When using `@cache.secure()`, the master key is required:

```bash
# Generate secure master key
export CACHEKIT_MASTER_KEY=$(openssl rand -hex 32)

# Verify key is set
python -c "import os; print(f'Key length: {len(os.getenv(\"CACHEKIT_MASTER_KEY\", \"\"))}')"
# Output: Key length: 64 (hex-encoded 32 bytes)
```

**Valid key formats**:
- 64 character hex string (32 bytes)
- Minimum requirement: 32 bytes (64 hex characters)

**Invalid key formats**:
```bash
# WRONG - not hex
export CACHEKIT_MASTER_KEY="my-secret-key"

# WRONG - too short
export CACHEKIT_MASTER_KEY="abcd1234"

# CORRECT - 64 hex characters = 32 bytes
export CACHEKIT_MASTER_KEY="a1b2c3d4e5f6789012345678901234567890abcdef12345678901234567890"
```

</details>

<details>
<summary><strong>Issue: Connection timeout errors</strong></summary>

Adjust timeout settings:

```bash
# Increase timeout values (in seconds)
export CACHEKIT_SOCKET_TIMEOUT=5.0
export CACHEKIT_SOCKET_CONNECT_TIMEOUT=5.0

# These are often needed for:
# - High-latency networks
# - Slow Redis instance
# - High load scenarios
```

</details>

---

## Performance Configuration

### Memory Usage

```bash
# Limit cache chunk size (affects L2 large value handling)
export CACHEKIT_MAX_CHUNK_SIZE_MB=50  # Default is 50MB

# Reduce for memory-constrained environments
export CACHEKIT_MAX_CHUNK_SIZE_MB=10
```

### Compression

Enable compression for large values:

```bash
# Enable compression (recommended for >1KB values)
export CACHEKIT_ENABLE_COMPRESSION=true

# Compression level (1-9, higher = more compression)
export CACHEKIT_COMPRESSION_LEVEL=6  # Default
```

> [!TIP]
> Compression has 100-500μs overhead but saves network bandwidth for large values (>1KB).

### Connection Pooling

```bash
# Tune connection pool size based on concurrency
export CACHEKIT_CONNECTION_POOL_SIZE=20  # Default is 10

# Higher for:
# - Many concurrent requests
# - High-concurrency workloads
#
# Lower for:
# - Memory-constrained environments
# - Single-threaded applications
```

## See Also

- [API Reference](api-reference.md) - All decorator parameters
- [Troubleshooting Guide](troubleshooting.md) - Common errors and solutions
- [Zero-Knowledge Encryption](features/zero-knowledge-encryption.md) - Encryption setup
- [Getting Started](getting-started.md) - Quick start guide

---

<div align="center">

*Last Updated: 2025-12-02*

</div>
