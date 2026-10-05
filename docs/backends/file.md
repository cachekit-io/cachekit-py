**[Home](../README.md)** › **[Backends](README.md)** › **File Backend**

# File Backend

Store cache on the local filesystem with automatic oldest-written-first eviction. No infrastructure required — ideal for single-process applications, scripts, and local development.

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

- Latency: `get` and `set` stay flat as the cache grows. `set` costs an fsync, plus a directory scan when eviction is due, before rejecting a new entry at `max_entry_count`, or every 30 seconds (see [Performance Characteristics](#performance-characteristics)). A concurrent `set()` on another thread in the same process blocks `get` for that whole `set()`, because `set()` holds the backend's lock through its fsync.
- Eviction: oldest-written first, by file mtime. Triggered at 90%, evicts to 70% capacity. Reads do not refresh an entry's mtime, so a hot key that is never rewritten is evicted as early as a cold one; `refresh_ttl` and `set` do refresh it
- TTL support: Yes (expiration checking + inspection/refresh via `TTLInspectableBackend`)
- Cross-process: the on-disk format is shared across processes and SDKs (cachekit-rs reads and writes the same files), but concurrent writers in multiple processes are not supported. Within one process, use one `FileBackend` instance per cache directory: each instance tracks only its own writes against the size and entry caps (see [Performance Characteristics](#performance-characteristics))
- Locking: non-blocking. An operation that finds an entry's file lock held fails at once with a `TIMEOUT` `BackendError`; it does not wait
- Platform support: Full on Linux/macOS, limited on Windows (no O_NOFOLLOW)

## Bounded-Memory Large Values (Arrow)

FileBackend is the only backend that implements **both** large-value capability protocols,
making it the right choice for caching DataFrames bigger than your RAM headroom:

- **Streaming writes** (`BufferWritableBackend.set_streaming`): plaintext Arrow values are
  serialized record batch by record batch straight into the cache file (temp file + atomic
  rename), so the full serialized payload never exists in memory. Write peak RSS is ~2.3x the
  DataFrame's logical size, versus ~5.6x on the buffered path every other backend uses.
- **Zero-copy reads** (`BufferReadableBackend.get_buffer`): with `compression="none"`, cached
  entries are served back through a POSIX mmap without materializing the payload on the heap.

Both engage automatically — no API change — whenever the serializer is plaintext Arrow
(`serializer_name="arrow"`, no encryption). **Encrypted values are excluded from both paths**:
AES-256-GCM needs the whole ciphertext to produce/verify its auth tag, so the secure path
stays on buffered `set()`/`get()`. Streamed values also intentionally skip the L1 in-memory
cache — holding a multi-GB envelope in-process would defeat the point.

Other backends (Redis, CachekitIO, Memcached) fall back transparently to buffered
`set(bytes)` with no behaviour change.

## TTL Inspection & Sliding Expiration

FileBackend implements the `TTLInspectableBackend` protocol (`get_ttl` / `refresh_ttl`) by
reading and rewriting the expiry timestamp in each cache file's header. This enables
**sliding expiration**: with `refresh_ttl_on_get=True`, a hot key's TTL is extended on every
hit whose remaining life has dropped below `ttl_refresh_threshold`, so frequently-read keys
don't expire out from under you.

```python notest
from cachekit import cache
from cachekit.backends.file import FileBackend
from cachekit.backends.file.config import FileBackendConfig

backend = FileBackend(FileBackendConfig())

@cache(backend=backend, ttl=3600, refresh_ttl_on_get=True, ttl_refresh_threshold=0.5)
async def get_profile(user_id: str) -> dict:
    return await load_profile(user_id)
```

`refresh_ttl` rewrites only the 8-byte expiry field in place — no on-disk format change, and
the cached payload is left untouched.

## Limitations and Security Notes

1. **One writing process at a time**: FileBackend's file locking does not make concurrent writers in multiple processes safe, and eviction runs per process. Do NOT use with multi-process WSGI servers. Reading or handing over a cache directory between processes, or between cachekit-py and cachekit-rs, is supported: the on-disk format is the same. Two things to know when another process reads: its `get`/`exists` deletes expired and corrupt entries it finds, and a read that meets another process's exclusive file lock raises a `TIMEOUT` `BackendError` rather than returning a miss.

2. **File permissions**: Default permissions (0o600) restrict access to cache files to the owning user. Changing these permissions is a security risk and generates a warning.

3. **Platform differences**: Windows does not support the O_NOFOLLOW flag used to prevent symlink attacks. FileBackend still works but has slightly reduced symlink protection on Windows.

4. **Wall-clock TTL**: Expiration times rely on system time. Changes to system time (NTP, manual adjustments) may affect TTL accuracy. An entry is expired once the clock reaches its stored whole-second expiry, the same boundary cachekit-rs and cachekit-ts apply to a shared cache directory.

5. **Disk space**: FileBackend will evict the oldest-written entries when reaching 90% capacity. Ensure sufficient disk space beyond max_size_mb for temporary writes.

6. **Corruption vs. tampering**: `set()` writes every byte or raises `BackendError` (short `write(2)` calls are resumed until every byte lands, never silently truncated into a "successful" file). On read, an expired entry or a structurally broken file — short header, bad magic or version, or a payload shorter than the file's own `st_size` implies — is deleted and treated as a miss. An entry whose reserved header byte or flags field is nonzero is different: the format reserves those for future payload transforms, so every read path (`get`, `get_buffer`, `exists`, `get_ttl`, `refresh_ttl`) treats it as a miss and leaves the file untouched, expired or not. Payload *content* is not checked here: a same-length modification is served as-is, and the serialization envelope (xxHash3 checksum, or the AES-256-GCM tag for encrypted values) decides whether it is corruption or tampering under your `encryption_fail_closed` policy. Eviction of an expired/corrupt entry is inode-guarded: the delete only fires when the file at that path still has the same `(st_dev, st_ino)` the read decided on. This *narrows*, but does not close, the window in which a `set()` from another writer that renamed a fresh entry into the same path could be deleted by mistake — it shrinks a function-body-wide race to the two syscalls between the guard's own `lstat` and the `unlink` (POSIX has no atomic "unlink iff inode matches"), so a rename landing in that gap can still delete a replacement. That residual window matches cachekit-rs's `unlink_if_same_inode` (relevant because the on-disk format is cross-SDK compatible); a delete lost this way is a rare, self-healing cache miss, not data loss.

## Performance Characteristics

The backend keeps its entry count and total size in memory. It seeds them with one directory
scan when it starts and updates them on every write, delete, eviction and expired-entry
cleanup it makes. So a `set()` checks the entry-count limit and the eviction trigger without
scanning the directory, and its usual cost is one fsync whatever the cache size: about 25
syscalls per `set()` at 0, 1,000 or 5,000 entries.

Three things still scan the directory, and each scan costs time in proportion to the number
of entries. A `set()` that pushes the cache past the eviction trigger rescans first, so it never
evicts on a stale count, and then evicts the oldest-written entries; that happens about once
every `0.2 × max_entry_count` new keys. A `set()` that would be rejected at `max_entry_count`
scans before it rejects; if the directory cannot be read just then, it rejects on the count it
has, keeping the cap rather than guessing. And the first `set()` more than 30 seconds after the last scan
rescans, because the counters cannot see other processes' writes to the same directory. If a scan
cannot read some entry's size, every `set()` rescans until a scan reads them all. A file whose
size stays unreadable is left out of the size total, as it always was. So
the latency tail of `set()` grows with the cache, and the typical `set()` does not.
`get()` and `delete()` do no scan, but a concurrent `set()` on another thread in the same process
blocks them for that whole `set()`, because `set()` holds the backend's lock through its fsync.

If the directory cannot be scanned at all, the counters keep their last values and nothing is
evicted until a scan succeeds. That failure, an entry whose size cannot be read during a scan,
and an eviction that cannot delete an entry for any reason other than the entry already being
gone each log a WARNING on the `cachekit.backends.file.backend` logger, at most once a minute
for each of the three, with the number of failures since the last warning; the failures in
between log at DEBUG.

The counters belong to one `FileBackend` instance, not to the directory. Concurrent writers in
several processes are unsupported (see [Characteristics](#characteristics)). If you run them anyway,
each process sees the others' writes only at its next scan, so the cache can overshoot
`max_size_mb` and `max_entry_count` by up to 30 seconds' worth of their writes. Two instances on
one `cache_dir` in the same process behave the same way, so use one instance per directory.

The fsync cost depends heavily on your disk, filesystem and load, so measure it where you will
run. The harness reports set/get/delete p50 and p99 at 0, 1,000, 5,000 and 9,000 cached
entries (1 KB values, n = 200 per point):

```bash
uv run pytest tests/performance/test_file_backend_perf.py -k scaling -s -m performance --basetemp=<dir on the filesystem to measure>
```

Run it more than once and compare: the spread between runs is your noise floor.

## See Also

- [Backend Guide](README.md) — Backend comparison and resolution priority
- [Redis Backend](redis.md) — Multi-process shared caching
- [Memcached Backend](memcached.md) — Multi-process in-memory caching
- [Configuration Guide](../configuration.md) — Full environment variable reference

---

<div align="center">

**[GitHub Issues](https://github.com/cachekit-io/cachekit-py/issues)** · **[Documentation](../README.md)**

</div>
