**[Home](../README.md)** › **Features** › **Write-Behind**

# Write-Behind: Return a Miss Before Its L2 Write Lands

> `write_behind=True` is an opt-in, off by default and set by no preset. On a miss, the decorated call returns once the value is serialized and in L1; the L2 write follows on a background writer. The cost is visibility: until that write lands, **another process misses the key** and recomputes it.

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

Every preset takes it: `@cache.io(write_behind=True)`, `@cache.production(write_behind=True)`, `DecoratorConfig(write_behind=True)`. Without a backend (`backend=None`, L1-only) there is no L2 write, and the option does nothing.

## What stays on the caller's path

- **Serialization and encryption.** The value is serialized (and encrypted) on the caller's thread, so `InteropError` and `KeyringConfigurationError` still raise to the caller, and the writer sends exactly the bytes that went into L1.
- **The async distributed-lock path.** An async miss on a lockable backend (CachekitIO, Redis) writes inside its lock, as before: a write deferred past the lock's release would send waiting callers to an empty L2, and each would recompute. Write-behind applies to sync misses and to async misses that run without the lock.
- **The streaming write path.** A plaintext Arrow value written to the File backend streams to disk on the caller's thread, as before.

## The writer

Each decorated function gets one writer, shared by its sync and async callers.

| Property | Behaviour |
|----------|-----------|
| Threads | Up to 4 per function, started when writes are queued and ended when the queue is empty |
| Queue bound | 64 entries and 16 MiB. A miss that finds the queue full writes on the caller's thread, as without the option |
| Coalescing | One pending write per key: a newer value replaces a queued one, and is never sent before an older one still in flight |
| Invalidation | `invalidate_cache()` drops the queued writes it covers and waits for those being sent, so its delete always comes after them |
| Key registry | A key is tracked (whole-function invalidation across processes) only after its write succeeds |
| Failures | A failed write is a WARNING (throttled) and a failed `set` operation in the metrics. The value stays in this process's L1 |

A key queued for a write may also lose that write to another process's invalidation announcement (the [invalidation listener](l1-invalidation.md#cross-process-l1-eviction)): the queued write is dropped, so it cannot land after that process's delete.

## Process lifecycle

> [!WARNING]
> Queued writes live in process memory. A clean interpreter exit flushes them for up to 10 seconds in total, then logs a WARNING naming the writes it dropped. `os._exit()` (multiprocessing and `ProcessPoolExecutor` workers) and `SIGKILL` skip the flush and lose the queue. A lost write is only a later miss: the value is recomputed. But a workflow that writes a "done" marker after a call and expects another process to read the cached entry needs the process to exit cleanly first.

- **Fork:** a forked child starts with an empty queue; the parent's queued writes are the parent's to send. A child forked without at-fork hooks (uWSGI without `--py-call-uwsgi-fork-hooks`) starts no writer thread and writes on the caller.
- **Read-after-write across processes:** another process reading the key before the write lands misses and recomputes. With the queue drained, the lag is one backend write; under a burst it can grow until the queue bound sends callers back to synchronous writes.
