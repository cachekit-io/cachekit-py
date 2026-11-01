**[Home](../README.md)** › **Features** › **Write-Behind**

# Write-Behind: Return a Miss Before Its L2 Write Lands

> `write_behind=True` is an opt-in, off by default and set by no preset. On a miss, the decorated call returns once the value is serialized and in L1; the L2 write follows on a background writer. The cost is visibility: until that write lands, **another process misses the key** and recomputes it, and a write still queued when **another process invalidates the key can land after that invalidation**: that invalidation is not guaranteed to reach this process's queue.

> [!CAUTION]
> Do not enable it for values that must not outlive an invalidation in another process, such as authorization decisions or entitlements. Another process's `invalidate_cache()` is not guaranteed to reach this process's queue, so a value computed before the invalidation can be written to L2 after it, with its full TTL (see [Invalidation](#invalidation)).

## Why

On a miss, the caller computes the value, then waits for the backend write before it gets the value back. That wait buys the caller nothing: a failed write is logged and the value is returned anyway. It only buys "L2 holds the value when the call returns". On a remote backend the write is a full network round trip, so on a miss-heavy workload it can be a large share of each call.

## Usage

```python
import tempfile

from cachekit import cache
from cachekit.backends.file import FileBackend
from cachekit.backends.file.config import FileBackendConfig

backend = FileBackend(FileBackendConfig(cache_dir=tempfile.mkdtemp()))

@cache(backend=backend, ttl=600, write_behind=True)
def lookup(term: str) -> dict:
    return {"term": term, "hits": 3}

assert lookup("sleep") == {"term": "sleep", "hits": 3}  # returns before the L2 write
assert lookup("sleep") == {"term": "sleep", "hits": 3}  # an L1 hit: the value was in L1 at return
```

Every serialized preset takes it: `@cache.io(write_behind=True)`, `@cache.production(write_behind=True)`, `DecoratorConfig(write_behind=True)`. `@cache.local()` stores no L2 entry and does not accept it. Without a backend (`backend=None`, L1-only) there is no L2 write, and the option does nothing.

## What stays on the caller's path

- **Serialization and encryption.** The value is serialized (and encrypted) on the caller's thread, so `InteropError` and `KeyringConfigurationError` still raise to the caller, and the writer sends exactly the bytes that went into L1.
- **The async distributed-lock path.** An async miss on a backend with a distributed lock (CachekitIO, and the tenant-scoped Redis backend that environment auto-detection or `RedisBackendProvider` builds) writes inside its lock, as before: a write deferred past the lock's release would send waiting callers to an empty L2, and each would recompute. Write-behind applies to sync misses and to async misses that run without the lock: every async miss on a plain `RedisBackend`, Memcached or File, which have no lock, and every async miss of a function decorated with `distributed_lock=False` ([Turning It Off](distributed-locking.md#turning-it-off-distributed_lockfalse)), on any backend.
- **The sync streaming write.** On a sync miss, a plaintext Arrow value written to the File backend streams to disk on the caller's thread, as before. An async miss never streams: it serializes the value in full, so its File write is deferred like any other.

## The writer

Each decorated function gets one writer, shared by its sync and async callers.

| Property | Behaviour |
|----------|-----------|
| Threads | Up to 4 per function, started when writes are queued. A thread whose queue is empty waits up to 1 second for the next write before it ends, so a miss that finds one waiting starts no thread. At most 8 threads wait per process, across every function; past that a thread ends as soon as its queue is empty |
| Queue bound | 64 entries and 16 MiB, newer values for queued keys included. A miss whose value would take the queue past either bound writes on the caller's thread, as without the option, and drops its key's older queued value. The exception is a value for a key whose older value is being sent right then: it always waits behind that send, so the older value can never land last. Those values can take the queue past the bounds by at most one value per send in flight, 4 at most |
| Coalescing | One pending write per key: a newer value replaces a queued one, and is never sent before an older one still in flight |
| Invalidation | In this process, `invalidate_cache()` drops the queued writes it covers and waits for those being sent, so its delete comes after every write this process made before the call. That covers every decoration of the same function and namespace in the process, whichever one invalidates; an exact-key invalidation also covers another function writing the same key (interop), and a whole-function one does not. The wait has no timeout of its own, so on a backend with no operation timeout it lasts as long as the slowest of those writes |
| Key registry | A key is tracked (whole-function invalidation across processes) only after its write succeeds |
| Failures | A failed write is a WARNING (throttled) and a failed `set` operation in the metrics. The value stays in this process's L1 |
| Metrics | A deferred write records its `set` sample when it finishes, on the writer thread. The sample times the backend write alone (serialization ran on the caller) and reports `success=False` when the backend refused or failed it. Without the option, or for a write that stays on the caller's path or that the full queue refused, the sample covers serialization plus the write, is recorded before the call returns and reports `success=True` even when the write failed |

### Invalidation

The ordering above holds within one process. Across processes it does not: process P returns a value, its write still queued; process Q then calls `invalidate_cache()` for the same key and returns; then P's write lands, and the value Q invalidated is back in L2 for its full TTL. Without the option, P's write would have landed before P returned.

The [invalidation listener](l1-invalidation.md#cross-process-l1-eviction) narrows this but does not close it. When it delivers another process's announcement, this process drops its queued writes of that key (of every key, for a whole-function announcement, and for every tenant, since the event carries no tenant). It does not wait for a write already being sent. The announcement is published after the other process's delete and delivered at most once, the listener runs only on the tenant-scoped Redis backend, and a function with L1 off does not register for it.

## Process lifecycle

> [!WARNING]
> Queued writes live in process memory. A clean interpreter exit flushes them for up to 10 seconds in total, then logs a WARNING naming the writes it dropped. `SIGKILL` skips the flush and loses the queue, and so does `os._exit()`, which is how a multiprocessing or `ProcessPoolExecutor` worker started with the `fork` or `forkserver` method ends. A `spawn` worker ends with `sys.exit()`, so its queue is flushed. A lost write is only a later miss: the value is recomputed. But a workflow that writes a "done" marker after a call and expects another process to read the cached entry needs the process to exit cleanly first.

- **Fork:** a forked child starts with an empty queue; the parent's queued writes are the parent's to send. A child forked without at-fork hooks (uWSGI without `--py-call-uwsgi-fork-hooks`) starts no writer thread and writes on the caller. A waiting writer thread holds no lock, so it cannot deadlock a child, but it keeps the process multi-threaded for up to 1 second after the last write, so Python 3.12+ `os.fork()` can warn that the process is multi-threaded. A process with an L1 cache (the default) already runs L1's sweeper thread for good, so it warns with or without write-behind.
- **Read-after-write across processes:** another process reading the key before the write lands misses and recomputes. With the queue drained, the lag is one backend write; under a burst it can grow until the queue bound sends callers back to synchronous writes.
- **CachekitIO read-back:** the API promises no read-your-writes across connections, and a read the server answers from an edge copy taken before the write can still miss for that copy's coherence window. The writer may send on a different connection from the caller's, so a read-back in the same process (L1 off, or the entry evicted from L1) is subject to that window too, which a synchronous write on the caller's connection usually avoids.
