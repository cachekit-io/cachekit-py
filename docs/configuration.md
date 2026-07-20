**[Home](README.md)** › **Configuration Guide**

# Configuration Guide

> **Configure cachekit through environment variables and decorator parameters**

> [!NOTE]
> This is the canonical reference for all cachekit environment variables.

---

## Backend Options

cachekit supports four backends. Pick the one that fits your infrastructure:

| Backend | Best For | Required Config |
|---------|----------|----------------|
| Redis | Self-hosted, full control | `CACHEKIT_REDIS_URL` |
| CachekitIO | Managed, zero-ops | `CACHEKIT_API_KEY` |
| File | Local dev, testing | None |
| Memcached | High-throughput, existing infra | `CACHEKIT_MEMCACHED_SERVERS` |

By default `@cache` auto-detects your backend from env vars, falling back to Redis at localhost (12-factor convention). CachekitIO is a managed alternative (currently in closed alpha). File backend is intended for local development and testing only. Memcached is an optional backend (`pip install cachekit[memcached]`).

### Redis Backend

For self-hosted Redis infrastructure:

**Usage:**
```python
from cachekit import cache

@cache  # Auto-detects backend (defaults to Redis at localhost)
def fetch_data():
    return expensive_computation()
```

**Configuration:** See [Redis Environment Variables](#redis-connection-for-cachekitconfig) below.

### CachekitIO Backend

> *cachekit.io is in closed alpha — [request access](https://cachekit.io)*

Zero-ops managed caching via the cachekit.io SaaS API. No Redis to provision or maintain.

**Configuration:** See [CachekitIO Configuration](#cachekitio-configuration) below.

### File Backend

File-based cache for local development and testing. No external services required.

**Configuration:** See the [File Backend](#file-backend-environment-variables) section below.

### Memcached Backend

> Requires: `pip install cachekit[memcached]`

High-throughput in-memory caching with consistent hashing across multiple servers.

**Configuration:** See the [Memcached Backend](#memcached-backend-environment-variables) section below.

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
CACHEKIT_MAX_VALUE_SIZE=104857600
CACHEKIT_ARROW_COMPRESSION=zstd

# Encryption (for @cache.secure)
CACHEKIT_MASTER_KEY=<hex-encoded-key-32-bytes-minimum>
# Fail closed on decrypt authentication failures (default: false = fail open/recompute).
# When true, AES-GCM auth failures and key-fingerprint mismatches raise
# DecryptionAuthenticationError to the caller instead of silently recomputing.
# Per-decorator fail_closed=True/False overrides this fleet-wide default.
CACHEKIT_ENCRYPTION_FAIL_CLOSED=false

# Fallback: REDIS_URL also supported (lower priority)
REDIS_URL=redis://localhost:6379/0

# Logging
LOG_LEVEL=INFO

# Performance Testing
REQUESTS_CA_BUNDLE=  # Unset to avoid SSL issues
```

---

## CachekitIO Configuration

> *cachekit.io is in closed alpha — [request access](https://cachekit.io)*

Configure the CachekitIO managed backend through environment variables. All fields use the `CACHEKIT_` prefix.

### CachekitIO Environment Variables

```bash
# Required: API key for authentication (ck_live_... format)
CACHEKIT_API_KEY=ck_live_your_key_here

# Optional: API endpoint (default: https://api.cachekit.io)
CACHEKIT_API_URL=https://api.cachekit.io

# Optional: Request timeout in seconds (default: 5.0, must be > 0)
CACHEKIT_TIMEOUT=5.0

# Optional: Maximum retry attempts for transient errors (default: 3, minimum: 0)
CACHEKIT_MAX_RETRIES=3

# Optional: HTTP connection pool size (default: 10, must be > 0)
CACHEKIT_CONNECTION_POOL_SIZE=10

# Optional: Allow custom API hostname - disables SSRF hostname allowlist (default: false)
# Only set to true when pointing at a private test server
CACHEKIT_ALLOW_CUSTOM_HOST=false
```

### CachekitIO Field Reference

| Variable | Type | Default | Required | Description |
|----------|------|---------|----------|-------------|
| `CACHEKIT_API_KEY` | `SecretStr` | — | Yes | API key (`ck_live_...`) for authentication |
| `CACHEKIT_API_URL` | `str` | `https://api.cachekit.io` | No | API endpoint URL (must use HTTPS) |
| `CACHEKIT_TIMEOUT` | `float` | `5.0` | No | Per-request timeout in seconds |
| `CACHEKIT_MAX_RETRIES` | `int` | `3` | No | Max retry attempts for transient errors |
| `CACHEKIT_CONNECTION_POOL_SIZE` | `int` | `10` | No | Max HTTP connections in pool |
| `CACHEKIT_ALLOW_CUSTOM_HOST` | `bool` | `false` | No | Disable hostname allowlist (testing only) |

**Security notes:**
- `CACHEKIT_API_URL` must use HTTPS. HTTP is rejected at startup.
- Private/internal IP addresses are blocked (SSRF protection). This includes `10.x`, `172.16-31.x`, `192.168.x`, `127.x`, and link-local ranges.
- `CACHEKIT_ALLOW_CUSTOM_HOST=true` disables the hostname allowlist. Only use with trusted configuration (e.g., a local test server running over HTTPS with a self-signed cert).

### Using `@cache.io()`

```python notest
import os
from cachekit import cache

os.environ["CACHEKIT_API_KEY"] = "ck_live_your_key_here"

# All production-grade features enabled: L1, circuit breaker, adaptive timeout, monitoring
@cache.io(ttl=300)
def fetch_data(user_id: int):
    return expensive_api_call(user_id)
```

`@cache.io()` automatically creates a `CachekitIOBackend` from environment variables. It applies production-grade defaults (see the [intent preset table](#intent-presets) below).

### File Backend Environment Variables

```bash
# Directory for cache files (default: system temp dir + /cachekit)
CACHEKIT_FILE_CACHE_DIR=/tmp/cachekit

# Maximum total cache size in MB (default: 1024, range: 1-1,000,000)
CACHEKIT_FILE_MAX_SIZE_MB=1024

# Maximum single value size in MB (default: 100, max: 50% of MAX_SIZE_MB)
CACHEKIT_FILE_MAX_VALUE_MB=100

# Maximum number of cache entries (default: 10000, range: 100-1,000,000)
CACHEKIT_FILE_MAX_ENTRY_COUNT=10000

# Lock acquisition timeout in seconds (default: 5.0, range: 0.5-30.0)
CACHEKIT_FILE_LOCK_TIMEOUT_SECONDS=5.0
```

### Memcached Backend Environment Variables

> Requires: `pip install cachekit[memcached]` or `uv add cachekit[memcached]`

```bash
# Server list (JSON array format, default: ["127.0.0.1:11211"])
CACHEKIT_MEMCACHED_SERVERS='["mc1:11211", "mc2:11211"]'

# Timeouts
CACHEKIT_MEMCACHED_CONNECT_TIMEOUT=2.0    # Default: 2.0 seconds (range: 0.1-30.0)
CACHEKIT_MEMCACHED_TIMEOUT=1.0             # Default: 1.0 seconds (range: 0.1-30.0)

# Connection pool
CACHEKIT_MEMCACHED_MAX_POOL_SIZE=10        # Default: 10 per server (range: 1-100)
CACHEKIT_MEMCACHED_RETRY_ATTEMPTS=2        # Default: 2 (range: 0-10)

# Optional key prefix for namespace isolation
CACHEKIT_MEMCACHED_KEY_PREFIX="myapp:"     # Default: "" (none)
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
| `swr_threshold_ratio` | float | `0.5` | Refresh at X% of TTL, in `(0.0, 1.0]` |
| `invalidation_enabled` | bool | `True` | Enable invalidation event broadcasts |
| `namespace_index` | bool | `True` | Enable fast namespace-based invalidation |

**L1 Cache Concepts:**
- **Freshness**: When to serve stale data + trigger background refresh (SWR)
- **Expiry**: Hard deadline when entry is deleted from cache
- **Namespace**: Logical grouping for bulk invalidation (see [L1 Invalidation Guide](features/l1-invalidation.md))

### L1-Only Mode (`backend=None`)

With `backend=None` the decorator caches raw Python objects in process memory (no
serialization — tuples, sets, and frozensets keep their types). `L1CacheConfig` is
honored as follows:

- **`max_size_mb`** bounds the cache by *estimated bytes*, not entry count. Sizes of
  raw objects are estimated best-effort (builtin containers are walked recursively;
  other objects are counted via `sys.getsizeof`). A single value larger than the whole
  budget is returned to the caller but never cached.
- **SWR requires a `ttl`.** With `swr_enabled=True` and a `ttl` set, a cache hit past
  `ttl * swr_threshold_ratio` (±10% jitter) serves the cached value immediately and
  refreshes it in the background — via `asyncio.create_task` for `async def` functions,
  or a daemon thread for sync functions. A successful refresh restarts both the
  freshness clock and the TTL. With `ttl=None` entries never go stale, so no refresh
  is ever scheduled — they are stored with a one-year (31,536,000&nbsp;s) sentinel
  expiry rather than truly indefinitely, and can still be evicted earlier under
  byte pressure.
- **Refresh failures are non-fatal**: the stale value keeps being served until hard
  expiry, and the next qualifying hit retries the refresh.

```python notest
import asyncio
from cachekit import cache
from cachekit.config import L1CacheConfig

@cache(ttl=60, backend=None, l1=L1CacheConfig(swr_enabled=True, swr_threshold_ratio=0.5))
async def load_dashboard():
    return await fetch_expensive_data()  # illustrative - not defined

# After ~30s (50% of TTL, ±10% jitter), the next call returns the cached value
# instantly and schedules a background refresh — callers never block on revalidation.
```

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

# cachekit.io SaaS - production-grade via managed API (closed alpha)
# Requires: CACHEKIT_API_KEY env var
# @cache.io(ttl=300)
# def io_function():
#     pass
```

**Feature Matrix by Intent:**

| Intent | SWR | Invalidation | Namespace Index | Max Size | Notes |
|--------|-----|--------------|-----------------|----------|-------|
| `minimal()` | ❌ | ❌ | ❌ | 100 MB | Speed-first, no integrity check |
| `test()` | ❌ | ❌ | ❌ | 100 MB | Deterministic, no monitoring |
| `dev()` | ✓ | ❌ | ❌ | 100 MB | Verbose logs, no Prometheus |
| `production()` | ✓ | ✓ | ✓ | 100 MB | Full observability |
| `secure()` | ✓ | ✓ | ✓ | 100 MB | AES-256-GCM encryption required |
| `io()` | ✓ | ✓ | ✓ | 100 MB | Managed SaaS backend (closed alpha — [request access](https://cachekit.io)) |

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

### CachekitIO Config is Separate

`@cache.io()` reads from `CachekitIOBackendConfig` — a completely separate config class from `CachekitConfig`. Redis URL precedence does not apply.

| Decorator | Config Class | Key Variable |
|-----------|-------------|--------------|
| `@cache`, `@cache.production()`, etc. | `CachekitConfig` | `CACHEKIT_REDIS_URL` / `REDIS_URL` |
| `@cache.io()` | `CachekitIOBackendConfig` | `CACHEKIT_API_KEY` |

Setting `REDIS_URL` has no effect on `@cache.io()`, and setting `CACHEKIT_API_KEY` has no effect on Redis-backed decorators.

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
export CACHEKIT_ARROW_COMPRESSION=zstd
```

### Secure Production (Encryption Enabled)

For production with sensitive data:

```bash
export CACHEKIT_REDIS_URL=redis://redis-primary:6379/0
export CACHEKIT_MASTER_KEY=$(openssl rand -hex 32)
export CACHEKIT_ARROW_COMPRESSION=zstd
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

### Value Size Limit

```bash
# Reject values whose serialized envelope exceeds this many bytes (L2 ceiling)
export CACHEKIT_MAX_VALUE_SIZE=104857600  # Default is 100MB

# Tighten for memory-constrained environments
export CACHEKIT_MAX_VALUE_SIZE=10485760   # 10MB
```

### Compression

Arrow payloads are compressed automatically. Select the codec:

```bash
# zstd (default), lz4, or none
export CACHEKIT_ARROW_COMPRESSION=zstd
```

> [!TIP]
> Compression saves network bandwidth for large values. `none` can enable zero-copy mmap reads on the File backend — eligibility also requires a plaintext (unencrypted) Arrow payload read back as pandas (`return_format="pandas"`) and a backend that supports buffer reads.

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

**[GitHub Issues](https://github.com/cachekit-io/cachekit-py/issues)** · **[Documentation](README.md)**

</div>
