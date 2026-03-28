**[Home](../README.md)** › **[Backends](README.md)** › **File Backend**

# File Backend

Store cache on the local filesystem with automatic LRU eviction. No infrastructure required — ideal for single-process applications, scripts, and local development.

## Basic Usage

```python
from cachekit.backends.file import FileBackend
from cachekit.backends.file.config import FileBackendConfig
from cachekit import cache

# Use default configuration
config = FileBackendConfig()
backend = FileBackend(config)

@cache(backend=backend)
def cached_function():
    return expensive_computation()
```

## Configuration via Environment Variables

```bash
# Directory for cache files
export CACHEKIT_FILE_CACHE_DIR="/var/cache/myapp"

# Size limits
export CACHEKIT_FILE_MAX_SIZE_MB=1024           # Default: 1024 MB
export CACHEKIT_FILE_MAX_VALUE_MB=100           # Default: 100 MB (max single value)
export CACHEKIT_FILE_MAX_ENTRY_COUNT=10000      # Default: 10,000 entries

# Lock configuration
export CACHEKIT_FILE_LOCK_TIMEOUT_SECONDS=5.0   # Default: 5.0 seconds

# File permissions (octal, owner-only by default for security)
export CACHEKIT_FILE_PERMISSIONS=0o600          # Default: 0o600 (owner read/write)
export CACHEKIT_FILE_DIR_PERMISSIONS=0o700      # Default: 0o700 (owner rwx)
```

## Configuration via Python

```python
import tempfile
from pathlib import Path
from cachekit.backends.file import FileBackend
from cachekit.backends.file.config import FileBackendConfig

# Custom configuration
config = FileBackendConfig(
    cache_dir=Path(tempfile.gettempdir()) / "myapp_cache",
    max_size_mb=2048,
    max_value_mb=200,
    max_entry_count=50000,
    lock_timeout_seconds=10.0,
    permissions=0o600,
    dir_permissions=0o700,
)

backend = FileBackend(config)
```

## When to Use

**Use FileBackend when**:
- Single-process applications (scripts, CLI tools, development)
- Local development and testing
- Systems where Redis is unavailable
- Low-traffic applications with modest cache sizes
- Temporary caching needs

**When NOT to use**:
- Multi-process web servers (gunicorn, uWSGI) — use Redis instead
- Distributed systems — use Redis or Memcached
- High-concurrency scenarios — file locking overhead becomes limiting
- Applications requiring sub-1ms latency — use L1-only cache

## Characteristics

- Latency: p50: 100–500μs, p99: 1–5ms
- Throughput: 1000+ operations/second (single-threaded)
- LRU eviction: Triggered at 90%, evicts to 70% capacity
- TTL support: Yes (automatic expiration checking)
- Cross-process: No (single-process only)
- Platform support: Full on Linux/macOS, limited on Windows (no O_NOFOLLOW)

## Limitations and Security Notes

1. **Single-process only**: FileBackend uses file locking that doesn't prevent concurrent access from multiple processes. Do NOT use with multi-process WSGI servers.

2. **File permissions**: Default permissions (0o600) restrict access to cache files to the owning user. Changing these permissions is a security risk and generates a warning.

3. **Platform differences**: Windows does not support the O_NOFOLLOW flag used to prevent symlink attacks. FileBackend still works but has slightly reduced symlink protection on Windows.

4. **Wall-clock TTL**: Expiration times rely on system time. Changes to system time (NTP, manual adjustments) may affect TTL accuracy.

5. **Disk space**: FileBackend will evict least-recently-used entries when reaching 90% capacity. Ensure sufficient disk space beyond max_size_mb for temporary writes.

## Performance Characteristics

```
Sequential operations (single-threaded):
- Write (set):   p50: 120μs, p99: 800μs
- Read (get):    p50: 90μs, p99: 600μs
- Delete:        p50: 70μs, p99: 400μs

Concurrent operations (10 threads):
- Throughput: ~887 ops/sec
- Latency p99: ~30μs per operation

Large values (1MB):
- Write p99: ~15μs per operation
- Read p99: ~13μs per operation
```

## See Also

- [Backend Guide](README.md) — Backend comparison and resolution priority
- [Redis Backend](redis.md) — Multi-process shared caching
- [Memcached Backend](memcached.md) — Multi-process in-memory caching
- [Configuration Guide](../configuration.md) — Full environment variable reference

---

<div align="center">

**[GitHub Issues](https://github.com/cachekit-io/cachekit-py/issues)** · **[Documentation](../README.md)**

</div>
