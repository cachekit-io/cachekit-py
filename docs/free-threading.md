# Free-Threaded CPython

Status as of 2026-10: **releases after 0.24.1 publish a wheel that installs on
3.15t; 3.14t is tested with no wheel; support is not yet declared**. (3.13t is not in the CI matrix: no claim
is made for it.)

- The core test suites (`tests/unit/`, `tests/critical/`) run green on
  free-threaded CPython 3.14 and 3.15 with the GIL verified disabled, gated by
  the `test-freethreaded` CI job on every PR and push.
- The Rust extension declares free-threaded safety
  (`#[pymodule(gil_used = false)]`), so importing `cachekit._rust_serializer`
  does not force the GIL back on.
- **From the release after 0.24.1, CPython 3.15 and later get one
  free-threaded stable-ABI (`abi3t`) wheel per platform** (`cp315-abi3.abi3t`),
  which loads on both the GIL and the free-threaded build. On 3.15t, `pip install --only-binary :all: cachekit`
  (pip 26.1+ or uv 0.11.3+) installs cachekit and every runtime dependency
  from wheels. See
  [CPython 3.15+: one abi3t wheel](#cpython-315-one-abi3t-wheel).
- **No 3.14t (`cp314t`) wheel is published**, and installing cachekit on a
  free-threaded interpreter is not officially supported. See
  [Deferred: declared support](#deferred-declared-support--free-threaded-wheels).

## What "works only under the GIL" used to mean here

Lock-free fast paths written under the GIL inherit its implicit guarantees:
one bytecode interleaving at a time and, effectively, sequentially consistent
publication of writes. Free-threaded CPython removes both. Its memory model
does not specify cross-variable store visibility order for plain reads, so a
reader may observe a *later* store while an *earlier* one is not yet visible.

Concrete instance (the defect that motivated this audit):
`decorators/session.py::_ensure_session_initialized` published three module
globals and relied on assignment order (`_session_start_ms`, then
`_session_id`, then `_session_pid`) to make the lock-free fast path safe. A
GIL-free reader observing `pid`+`id` but not yet `start_ms` sailed past the
fast path into `get_session_start_ms()`'s "should never happen"
`RuntimeError` — which `backends/cachekitio/backend.py` catches and converts
into *silently dropped session headers*, the exact telemetry loss an earlier
fix eliminated. The fix gates the fast path (and the in-lock double-check) on
**every** published field; observing a partial publish now falls through to
the lock and waits for the in-flight initializer.

Rule of thumb applied throughout the audit:

- **Single-assignment publication** of a fully-constructed object through one
  reference (e.g. a double-checked module-global singleton) is acceptable.
- **Multi-field publication** that readers expect to be mutually consistent
  must be lock-protected or gated on every field — assignment order proves
  nothing without the GIL.

## Concurrency audit

The lock-free fast paths and shared mutable module/instance state the audit
covered, plus what the free-threaded CI lane surfaced and sites added since:

| Site | Mechanism | Verdict |
|:-----|:----------|:--------|
| `decorators/session.py` `_ensure_session_initialized` | Lock-free fast path over three module globals | **Fixed** — fast path and double-check gate on all three fields; regression tests in `tests/unit/test_saas_observability.py::TestMidPublishMemoryOrdering` and `tests/unit/test_free_threading.py` |
| `reliability/metrics_collection.py` `AsyncMetricsCollector.flush` | Polled `Queue.empty()` | **Fixed** — `empty()` flips when the worker *dequeues*, not when it finishes processing; flush now waits on a collector-owned pending-work counter + `Condition` (+1 on enqueue, −1 once the worker has processed or dropped the item, `notify_all()` when it reaches zero). Was a routine flake on the free-threaded lane, invisible under the GIL's coarse scheduling |
| `decorators/wrapper.py` `_FunctionStats` | `RLock` around every counter mutation and `get_info`, except the L1 hit count and latest-hit time, which a native atomic counter (`HitCounter`) keeps without a lock | Safe. Every count is exact; a `get_info` or `clear` that races L1 hits is not a snapshot of one instant, so such a hit can show in `l1_hits` and not in `last_operation_at`, or the other way round. `l1_enabled` is a plain attribute re-set on re-decoration (under the registry lock) and read without the stats lock; a stale read yields a conservative rate-limit classification header, never corruption |
| `decorators/wrapper.py` `_function_stats_registry` | Module-level `Lock` around all access | Safe |
| `os.register_at_fork` handlers (session + stats registry) | Run in the child while single-threaded; replace locks wholesale | Safe — single-threaded by construction at execution time |
| `backends/cachekitio/session.py` header cache | `threading.local` | Safe — per-thread state; its only cross-thread hazard was the session-identity publication above |
| `decorators/stats_context.py` | `contextvars.ContextVar` | Safe by construction |
| `decorators/wrapper.py` L1/SWR + L2/SWR single-flight (`_RefreshPool`: `_l1_swr_pool`, `_l2_swr_pool`) | One `Lock` per pool over its holder map (an L2 holder carries its in-flight key) + PID-owner swap | Safe. The prune, the cap check and L2's per-key check run under the pool lock, so L2 has no check-then-add race; L1's per-key check is `ObjectCache`'s, under its own lock. A pruned refresh is cancelled with `call_soon_threadsafe` on its own loop. The fork-detection swap race is the documented-benign shape: a thread racing it can lose its new holder, whose key then waits for its hard TTL. In a child forked without at-fork hooks (the detection of the `l1_cache.py` row), neither sync path starts its refresh thread (`cachekit-swr-refresh`, `cachekit-swr-revalidate`), because a thread started there can hang in `Thread.start()`. The check is one `getpid()` on a stale hit that would refresh, made before a slot is taken, and it logs nothing. The stale value is served until the entry hard-expires, and the first read after that recomputes it. The async paths schedule a task, not a thread, and are unchanged. Tests in `tests/unit/test_swr_decorator.py::TestSWRForkIsolation` |
| `decorators/single_flight.py` `AsyncFlights`, `ThreadFlights` (in-process single-flight of misses: one map per decorated function, used by `wrapper.py` and `local_wrapper.py`) | One `Lock` per map over its in-flight entries + PID-owner swap, checked by every method that takes the lock (`_FlightMap._owned`: `run`, `forget`, the settle paths). An async entry holds the call's task and its waiter count, which only that task's own loop touches. A sync waiter blocks on a per-call `Lock` its starter holds until the call settles | Safe. Lookup and insert run under the map lock, and an entry is removed only by the call that put it there (identity check), so a settling call never drops a newer one. A sync caller re-checks the cache under the map lock before it starts a call (L1-only: the object cache; backed: L1, through `L1Cache.peek`), so a call that settled between the caller's lookup and its map check is not run twice; a backed caller with L1 off has nothing to re-check and reads L2 on its own trip instead. The lock order is the map lock, then the cache's state lock; nothing that holds a cache lock takes a map lock. A joining caller's `on_join` runs outside the map lock, before it waits. An async caller joins only a task on its own running loop, never another thread's. An invalidation drops the key's entries under the map lock (`forget`); their callers keep waiting on the call they joined. An async starter inserts its call only if no `forget` ran since its lookup, so a call that an eager task factory ran up to its first await before an invalidation never reaches the map after it. A sync starter is settled in a `finally` that covers its insert, so an interrupt cannot leave its waiters blocked. The fork swap is the documented-benign shape: a forked child starts with an empty map, and a thread racing the swap starts its own call. Tests in `tests/unit/test_single_flight.py` |
| `decorators/write_behind.py` `WriteBehind` (opt-in `write_behind=True`: one writer per decorated function, shared by its sync and async callers) | A `_State` holding the PID, a `Condition`, the queued and in-flight jobs, the byte count, the thread count and the closed flag, published as one reference. Every method binds it once (`_current`), and each writer thread is started with the state it serves and touches no other | Safe. Admission, coalescing, the thread count and the queue are read and written under the state's condition. A thread that finds nothing to send gives up its place in the same hold as that check, so a submit never counts a thread that is about to quit. Two sends of one entry never overlap, and `cancel` waits on the jobs in flight at the call, by identity, so a later send of the same entry cannot hold it. Fork: a child's first caller takes over under a lock from `dict.setdefault(pid, Lock())`, re-checks, and publishes a fresh `_State` in one store, so no thread pairs the child's PID with the parent's lock or queue; the PID travels with the state, as in the `client.py` row. A child forked without at-fork hooks (the detection of the `l1_cache.py` row) starts no writer thread, and its misses write on the caller. Tests in `tests/unit/test_write_behind.py` |
| `l1_cache.py` `L1CacheManager._take_over_if_forked` + `_empty_caches_after_fork` | Owner-PID fast path; on a PID change one thread takes over under a lock fetched by `dict.setdefault(pid, Lock())`. An `os.register_at_fork` hook gives every cache a fresh, empty state before the child's first `get()`, so a forked child starts with an empty L1; fork mechanics and the uWSGI limit are in the `_take_over_if_forked` docstring | Safe. `setdefault` is atomic under per-object locking, so every thread in a new child gets the same fresh lock, which no parent thread can have held at fork; the rest block on it and re-check. The fast path gates on `_owner_pid` alone, so a weak-memory reader may still see the inherited manager lock once: the same benign fork-swap race as the SWR row, since that lock is unheld unless a parent thread held it at fork. The hook runs while the child is single-threaded and replaces every cache's state, held lock or not, so it probes no lock, takes none and starts no thread. It does not free the inherited states, which would copy the parent's L1 pages into every child inside `fork()`: they wait in `_inherited_states` until the child's first put, whose take-over frees them in batches, giving up the GIL between batches, on the cleanup thread it restarts or, when the parent ran none, on a one-shot thread. Each state is taken off that list with one `list.pop()`, atomic under per-object locking, before it is freed, so two releasers running at once (the threads of two managers' take-overs) never take the same state and neither finds the list emptied under it. So a child bound for exec never pays. A child that uses its L1 pays once, up front: each free also writes the allocator's bookkeeping beside the entry, so its first put copies about 2.3 times the inherited L1 (36.5 MiB for a 16 MiB L1 in `tests/performance/test_l1_fork_memory.py`), and its own L1 then reuses that memory. Kept, those pages would cost nothing while the parent left them alone, as an idle preloaded master whose entries outlive its workers does. Freeing is the default anyway because a parent with a cleanup thread rewrites them within one L1 TTL, and from then on a child that kept them would hold a private copy nothing can reach, beside its own L1. A state whose lock is taken is only dropped: one a thread forked inside its critical section holds is freed with that thread's last reference, and one a parent thread held at fork can stay for the child's life, referenced by that thread's frame. Neither the hook nor the take-over clears shared state: each publishes a fresh `_L1State` (entries, byte count and lock) in one store, and a holder that is still running finishes on the state it bound (the forking thread, if it forked inside a critical section; without hooks, a live child thread holding a lock past the take-over's 1 s probe). Invalidations re-apply if a reset replaced the state meanwhile. Without hooks, the cost of a wrong guess is that namespace's L1 entries, once per child; `--py-call-uwsgi-fork-hooks` avoids it. Once it has run, the take-over skips its own timed cache-lock probe, which could otherwise drop a cache's entries under a live child thread. In a child the hook never reached (a fork made from C, as uWSGI does without `--py-call-uwsgi-fork-hooks`; any PID other than the importing process's and the one the hook last ran in), the take-over starts no thread and `start_background_cleanup` refuses to start one, including for a manager first built in that child: such a fork also skips CPython's own after-fork repair, so a thread started there can hang in `Thread.start()` or crash the interpreter. None of that logs, the take-over's lock-reset drop included, because the same fork skips `logging`'s at-fork lock reset, and a handler lock a parent thread held would hang the child. Expired entries in that child are evicted on read or under the memory bound instead. A process that first imports cachekit after a fork made from C (uWSGI `--lazy-apps` without `--py-call-uwsgi-fork-hooks`) is detected at that import (`cachekit/_fork.py`) when its parent ran Python threads at the fork: `threading` still lists them, under native thread ids that are no task of this process (`/proc/self/task`), where an `os.fork()` child's after-fork hook would have dropped them. Such a process has no import PID, so every PID of it counts as hookless and every gate above applies; an `os.fork()` child of it runs the hooks and is safe again. When its parent ran no threads it looks like a fresh process and starts the cleanup thread, which is safe: no parent thread held anything at the fork. The check is Linux only and sees only threads `threading` registered. When it cannot tell (no `/proc/self/task`, or the current thread's own id absent from it: another PID namespace, or a patched `get_native_id`) it takes the process for a fresh one. gevent green threads are listed under the native id of the thread they ran on, so a parent's green threads alone do not count. The current and main threads never count, nor any thread under either's native id, because after a fork the forking thread's object may keep the parent's native id: always after a fork made from C, and after `os.fork()` before Python 3.13. So a fork made from C from a thread other than the main one loses the parent's main thread as evidence; uWSGI forks from its main thread. It lists threads under `threading`'s `_active_limbo_lock`, so a parent thread that held that lock at the fork, which a thread does only while it starts, ends or lists threads, hangs the import, as `logging`'s module lock already can. Tests in `tests/unit/test_fork_import_signal.py` |
| `decorators/l1_latency.py` `L1LatencyReport` + `sampling_rng` | One `_State` (lock, PID, buckets, sum) per report, published by one assignment; `add`, `record` and `take` hold its lock. A PID change is taken over under a per-PID lock from `dict.setdefault`; every report construction and use reseeds the private sampling generator once per process under that lock; an `os.register_at_fork` hook reseeds it too | Safe. Racing first uses in a child agree on one new state, and no child thread takes a lock a parent thread may have held at fork. A fork made from C (uWSGI without `--py-call-uwsgi-fork-hooks`) replays the parent's draws until the child builds a backend or makes its first request or timed hit on an inherited one: no per-draw PID check, since `os.getpid()` is a system call costing several draws on the hit path. Tests in `tests/unit/backends/test_cachekitio_telemetry.py` |
| `backends/cachekitio/client.py` client cache + `CachekitIOBackend._own_lease` | One urllib3 connection pool per config, shared by every thread of a process (urllib3's pool is thread-safe: a `LifoQueue` of connections, each request on its own connection). The process-wide lease cache is a `WeakValueDictionary` under a `Lock`, both held by one `_Leases` object that carries the PID that built it; each lease records its PID too, and the backend re-leases on a mismatch, or once `close_http_clients()` has closed its client, checked per request; the new lease is in the cache, so a later close reaches it. Async methods send on the same client through `asyncio.to_thread` | Safe across threads, with and without the GIL: `tests/unit/backends/test_cachekitio_thread_share.py` runs 8 threads on one backend, sync and through `to_thread`, and expects zero failures on 3.14 and on 3.14t with the GIL off. Safe across fork. A forked child never sends on or reads from its parent's pooled connections, which share the parent's TLS sessions: its first request finds the cache's PID is not its own, replaces the cache and its lock with new ones, and re-leases a new client, whether it was forked by `os.fork()` or from C with no at-fork hooks (uWSGI). An owner-PID check rather than an at-fork hook for that reason. The PID travels with the cache and its lock in one published reference, and with the client in the lease (single-assignment publication), so no thread pairs a new PID with an inherited lock or client, and no child thread takes the parent's lock, which a parent thread may have held at fork. Inherited clients are dropped unclosed: `close()` takes the pool's queue lock, for the same reason, and garbage collection closes the child's copies of the sockets without sending anything. Two child threads racing the first request may each build a cache and a client; the one replaced is simply not shared, and each request holds its lease until it returns, so that replacement never closes a client under an in-flight request. A child runs async calls on an event loop of its own (`asyncio.run`): a child that keeps running its parent's loop object waits forever on that loop's inherited default executor, whose worker threads did not survive the fork, as every async decorator's L2 operation already did. In a child forked from C, any logging call whose level check is not already cached, urllib3's per-request `DEBUG` check included, waits on `logging`'s module lock, and hangs if a parent thread held it at fork; a logging reconfiguration clears those caches. `--py-call-uwsgi-fork-hooks` avoids this. Tests in `tests/unit/backends/test_cachekitio_fork.py` |
| `invalidation.py` dispatch map, start lock and listener thread | `_evictors` (registry id → `WeakSet` of evictors) under a per-PID `Lock` from `dict.setdefault` (`_dispatch_locks`): decoration registers under it, and the listener thread takes a snapshot under it and runs the evictors outside it. The listener is redis-py's own `PubSubWorkerThread`, started behind an owner-PID check under a second per-PID lock (`_start_locks`) taken non-blocking, so a cache operation never waits on another thread's start | Safe. `setdefault` is atomic under per-object locking, so threads racing to create a new process's lock share one, and a child forked while a parent thread holds either lock gets its own instead of waiting on a lock nobody will release. Walking the live `WeakSet` while a decoration adds to it would raise; the snapshot under the lock rules that out. The owner-PID check reads `_listener_pid`, which the starting thread publishes last, so a stale read only skips or retries a start. Evictors call `L1Cache.invalidate` and `invalidate_many`, which lock per state as in the row above. A child forked without at-fork hooks starts no listener and logs nothing (the same detection as the row above). Tests in `tests/unit/test_invalidation_listener.py` |
| `backends/redis/provider.py` `with_timeout` windows (`_open_windows`: pool → configured `socket_timeout` and the open windows' timeouts) | Per-PID `Lock` from `_pid_lock` (`_windows_locks`) around each window's open and close and around `listener_pool()`'s read of the pool's kwargs | Safe. Opening and closing each read and write the record and the pool's `socket_timeout` together, and a window can open and close on any thread's event loop over one shared pool, so without the lock a window closing the last record could drop one another thread just joined. No `await` runs under the lock. A child gets its own lock, as in the row above. A child forked while a window is open inherits that window but not the task holding it, so it could never close it: an `os.register_at_fork` hook drops every inherited record and puts each pool's configured timeout back, and each record carries the PID that opened it, so a fork made from C, which runs no hook, drops it on the pool's next window or listener clone instead; until then such a child opens its connections at the inherited window's timeout (no per-operation PID check, as in the object-cache row; `--py-call-uwsgi-fork-hooks` avoids it). A close restores the pool's timeout before it drops the record, so a fork between the two steps still leaves the hook a record to reset from. Close-order and fork tests (single-threaded) in `tests/unit/test_invalidation_listener.py` |
| `decorators/wrapper.py` `_cached_keys` | Builtin `set`, snapshot-copied before iteration in invalidation | Safe — single ops atomic; a copy racing an add can only miss a concurrently-written key, which invalidate-all semantics tolerate |
| `object_cache.py` + `_rust_serializer.HitMemo` lock-free L1-only hit (the [hit path](performance.md#decorator--l1-hit-hot-path) of an L1-only function) | No lock on a hit: `HitMemo.get` looks the call's memo key up in the function's memo, a builtin `dict` of `HitEntry` objects, then loads the entry's `alive` flag (acquire) and compares its deadline, then stores its use time (relaxed) and counts the hit on `HitCounter`. Only the function's `ObjectCache` writes the memo, under its lock: a put, or a read under the lock, memoises its entry, and every removal (expiry, eviction, `delete`, replacement, `clear`, the at-fork reset) kills the entry (release) and then drops its memo slots before it releases the lock. Each `HitEntry` lists the memo keys whose slots hold it, behind a `Mutex` that only those writes take and that is never held across Python code | Safe. A hit that starts after a removal returned, in the order the threads' own synchronisation gives them (a lock, an event, a join), finds no slot or finds the entry dead, so it never serves the removed value; a hit racing the removal may serve the value it found alive, as the removal had not returned. No removed value stays referenced from the memo. A refresh points the slots at the new entry before it kills the stale one. Two hits racing on one entry store either use time, which moves its LRU position by at most the time between them. `tests/unit/test_l1_lock_free_hit.py` pins each removal route and runs hitters, fillers and invalidators on shared keys under a 1 MB budget, so entries are evicted all along: no value served after its removal returned, on 3.14 and on 3.14t with the GIL off |
| `object_cache.py` `ObjectCache` + `_reset_caches_after_fork` | `RLock` on every public method. The entries, their byte total, their expiry and recency records, their in-flight refresh markers and the lock form one `_ObjectCacheState`, which every critical section binds once; the memo of an L1-only function's cache (the row above) is written only under that lock. An `os.register_at_fork` hook walks a `WeakSet` of live caches in a forked child | Safe. The hook runs while the child is single-threaded, so it takes no lock that can block and starts no thread. A cache whose lock was free at fork keeps its entries and loses its in-flight refresh markers, since the threads running those refreshes did not survive the fork, so the child's next stale read refreshes again. A cache whose lock was held kills every entry's lock-free hit, empties its memo and gets a fresh, empty state in one attribute store: a parent thread that held it is gone, and a forking thread that forked inside a critical section (from a signal handler or a finalizer) may never return there, as in a `multiprocessing` child. A forking thread that does return finishes on the state it bound, and takes back any memo slot it then sets for an entry of that state. The generation counter stays on the cache, so no refresh token is reused across states. A child forked without at-fork hooks (uWSGI without `--py-call-uwsgi-fork-hooks`) gets no repair: its first call hangs if a parent thread held that cache's lock at fork, and a key whose refresh was in flight is served stale until it expires. The check that could catch that, an owner-PID check, would cost a `getpid()` on every cached call. A sync function refreshes no key there at all (the SWR row). Tests in `tests/unit/test_object_cache.py::TestObjectCacheFork` |
| `reliability/metrics_collection.py` `get_async_metrics_collector` | Double-checked module-global singleton | Safe — single-assignment publication of a fully-constructed object; worst case a benign duplicate worker-restart check |
| `reliability/metrics_collection.py` `MetricsCollector` + `get_all_metrics` + `_metrics_lock()` | Module dicts of counters and gauges (`cache_operations_total`, `circuit_breaker_state`, ...) behind one `RLock` per PID, created with `dict.setdefault` | **Fixed** — every read and write takes the lock. A child gets its own, so a lock a parent thread held at a fork from C, with no at-fork hook run, is never waited on; before it was one module `RLock`, so a hookless child's first health check or `StructuredLogger.cache_operation()` could hang. The child's counts start from the values it inherited. Tested in `tests/unit/test_structured_logging.py` (`TestEntriesAfterFork`) |
| `reliability/async_metrics.py` `_metrics_cache` + `_metrics_cache_lock()` | Double-checked module dict of Prometheus metric objects; one `Lock` per PID, created with `dict.setdefault`; held across `os.fork` by at-fork hooks | Safe — the lock-free read sees `None` or a fully constructed metric (single-assignment publication). `setdefault` is atomic under per-object locking, so threads racing to create a new process's lock all get the same one; a child forked without the hooks never takes its parent's lock. Tests in `tests/unit/test_async_metrics_shared_series.py` |
| `reliability/async_metrics.py` `AsyncMetricsCollector.record_cache_operation` + `_ThreadState` + `_Shards` + `_ShardView` | Every backed cache operation counts once per collector and updates three Prometheus series. It used to write a shared `_operation_count` (unlocked, so lossy with the GIL off) and either the series' value locks or the batching queue | **Fixed** — each thread writes only its own `_ThreadState`, held in a `threading.local`: one cell per series it records into, and one operation count per collector. The series' values are `_ShardView`s that sum the live cells plus an exact retired total, read under one per-PID lock that no record takes, so a scrape and `get_stats()` count every operation recorded before them. Freed at thread exit, a `_ThreadState` moves its counts into the retired totals and drops its cells in one locked step, so reads never decrease and cells stay bounded to the live threads; sums are retired as exact partials (`math.fsum`), so a retirement never re-rounds a total one ulp lower, which `rate()` would read as a counter reset (an infinite term collapses the sum to `inf`). The lock is reentrant, because a garbage collection under it can run a finalizer on the same thread. A count move such a finalizer starts there is deferred to the next section that is not nested. One example is a cyclic collection freeing a thread's state that a traceback kept alive past the exit, so no read sees a count half moved. The at-fork hooks hold the lock across `os.fork`; a forked child starts from the counts it inherited. In prometheus_client's multiprocess mode cache operations record into the series directly. Tests in `tests/unit/test_async_metrics_per_thread.py` |
| `backends/memcached/backend.py` `MemcachedBackend` | pymemcache `HashClient(use_pooling=True)`: one `PooledClient` per server, whose `ObjectPool` `threading.Lock` guards only the free/used deque pop and append, never socket I/O | Safe across threads: each operation owns its checked-out connection, so no socket is shared. The pool raises at `max_pool_size` rather than waiting (`tests/unit/backends/test_memcached_pooling.py`). pymemcache's failover state (`HashClient._failed_clients`) is unlocked: while a failed server is retried, concurrent operations can raise a spurious `BackendError` wrapping `KeyError` (documented in `docs/backends/memcached.md#concurrency`). Not fork-safe, by design: a child that reused its parent's backend would share the parent's pooled sockets, and could deadlock on a pool lock a parent thread held at fork. A per-PID lock would cure only the deadlock, not the shared sockets, so the rule is that a forked child builds its own backend (documented in `docs/backends/memcached.md#concurrency`) |
| `reliability/async_metrics.py` `AsyncMetricsCollector._maybe_switch_mode`, `shutdown`, `_take_over_if_forked` | Auto sync/batched switch; `_should_check_mode` is an unlocked check-then-set, so two producers can both run a switch. A forked child inherits the batching state (queue, stop event, worker, pool lock) and prometheus_client's metric locks | **Fixed** — one `Lock` held across the switch, so concurrent switches start one worker. The lock is per PID (a per-instance dict filled with `setdefault`, like `_metrics_locks`), so a child forked while a thread is mid-switch never waits on the copy that thread still holds. `shutdown()` takes the same lock, bars any later switch from starting a worker, and sets sync mode before stopping the worker, so every later record lands synchronously; a record another thread had already chosen to queue can miss the worker's final drain (at most one per such thread). Returning to batched mode starts a worker on the existing queue only once the previous worker has exited (its exit drain assumes a single consumer); while it is alive the collector stays synchronous and retries at the next mode check, never joining on the caller's thread. Within a process the queue is never replaced, so producers always find one. **Fork:** an owner-PID take-over gives an inherited collector fresh batching state and sync mode in a forked child. The at-fork hook (after a fork made from C, the first take-over or collector built in the child instead) gives cachekit's own metrics and the default registry fresh prometheus_client locks; an application's own metrics are not covered, and neither are values in prometheus_client's multiprocess mode (`PROMETHEUS_MULTIPROC_DIR`), which share one lock the reset cannot reach, so a child forked while a parent thread holds it can still hang. No collector, inherited or built there, starts a thread in the child of a fork made from C, where at-fork hooks are skipped and `is_alive()` stays stale, nor in a child that first imports cachekit after such a fork of a parent that ran threads (uWSGI `--lazy-apps`; the detection of the `l1_cache.py` row). On free-threaded builds such a fork without `--py-call-uwsgi-fork-hooks` is unsupported: any Python code in that child can hang on a per-object lock a parent thread held, before cachekit can act. The `_take_over_if_forked`, `_reset_metric_locks` and `_reset_metric_locks_once` docstrings give the mechanics and the open cases. Cache operations never queue, so only generic metrics and mode checks touch the batching state. There is no per-record `getpid()` on the sync path. Tests in `tests/unit/test_async_metrics_mode_switch.py` |
| Rust extension (`rust/src/`, cachekit-core 0.7.2) | `#[pymodule(gil_used = false)]`; every `#[pyclass]` exposes only `&self` methods; the nonce counters (the process-wide instance counter and each encryptor's own), core's only shared state in the features cachekit-py enables, are `AtomicU64`; PyO3 enforces `Send + Sync` on pyclasses at compile time | Safe — declared free-threading-ready. (The wasm32 `Cell` nonce variant is single-threaded by target.) |

## The CI safety net

`.github/workflows/ci.yml` job `test-freethreaded`, one leg each for 3.14t and
3.15t:

1. Installs exactly what the job runs — note the hiredis exclusion, it is
   load-bearing (see below):

   ```bash
   uv sync --python 3.14t --no-default-groups --group test --no-install-package hiredis
   ```

   The 3.15t leg builds the extension as the release does, as abi3t, by
   setting `MATURIN_PEP517_ARGS="--features pyo3/abi3t-py315"`, and asserts
   that the module it loaded is the abi3t build:
   `_rust_serializer.abi3t-x86_64-linux-gnu.so` from maturin 1.16 on, or
   `_rust_serializer.abi3t.so` from maturin 1.15. The GIL 3.15 leg of the
   `test` job (push matrix only) builds and asserts abi3t the same way.

   The `test` dependency group is the core test toolchain. orjson, numpy,
   pandas and pyarrow live only in the `dev` group and the `[data]`/`[json]`
   extras, so this lane does not install them and their tests skip via
   `pytest.importorskip`: orjson publishes no free-threaded wheels, and the
   `[data]` extra is not exercised in this lane yet.
2. Asserts the interpreter is a free-threaded build **and** that
   `sys._is_gil_enabled()` is still `False` after importing `cachekit`,
   `cachekit._rust_serializer`, and `redis` — a dependency that fails to
   declare free-threaded support re-enables the GIL for the whole process at
   import time, which would silently turn the lane back into a GIL run.
   `tests/unit/test_free_threading.py` re-asserts this from inside the suite.
3. Runs `tests/unit/` and `tests/critical/`.

hiredis is excluded because it does not declare free-threaded support (no
`Py_mod_gil` slot); redis-py transparently falls back to its pure-Python
parser. On a GIL build nothing changes — hiredis remains the default parser.

The exclusion also means the lane never sees what a default install gets:
`pip install cachekit` pulls in hiredis. A default install keeps the GIL off
anyway, in one of two ways.

A program that uses no Redis backend (L1-only, CachekitIO, File or Memcached)
never loads redis-py: `import cachekit` imports no redis-py, and
`RedisBackend` is loaded only on first use, through the module `__getattr__`
of `cachekit.backends`. With `CACHEKIT_DISABLE_HIREDIS` unset nothing is
blocked, and the program's own `import hiredis` works. (`true` blocks hiredis
at `import cachekit` for every program, Redis or not.)

A program that uses cachekit's Redis backend loads redis-py through
`cachekit.backends.redis`, and that package decides on hiredis before it
imports redis-py. On a free-threaded build whose GIL is still off it sets
`sys.modules["hiredis"] = None`, so redis-py's own `import hiredis` fails and
it binds its pure-Python parser (`_RESP2Parser`). Every cachekit path to
redis-py goes through that package first, `PooledClientProvider` and the
invalidation listener included.
The decision reads `CACHEKIT_DISABLE_HIREDIS` straight from the environment at
`import cachekit`:

| `CACHEKIT_DISABLE_HIREDIS` | GIL build | free-threaded build |
|---|---|---|
| unset | hiredis | pure-Python parser, GIL stays off; blocked when `cachekit.backends.redis` loads (if the GIL is already on, for example `-X gil=1`: hiredis) |
| `true` | pure-Python parser | pure-Python parser, GIL stays off; blocked at `import cachekit` |
| `false` | hiredis | hiredis, GIL re-enabled once redis-py loads |

The block is process-wide: once it is in place, an application's own
`import hiredis` raises `ImportError` unless it sets
`CACHEKIT_DISABLE_HIREDIS=false` (which re-enables the GIL for the whole
process, as importing hiredis there always does). The unset default acts only
when cachekit's Redis backend loads, so an application that imports redis-py
itself before that keeps hiredis, and on a free-threaded build the GIL turns
on. To avoid that, set `CACHEKIT_DISABLE_HIREDIS=true` and import cachekit
before redis-py: `true` blocks at `import cachekit`, and cannot undo a
hiredis that redis-py has already loaded.
When hiredis is already loaded, cachekit logs a warning that redis-py keeps
the hiredis parser and, on a free-threaded build, that the GIL is already on.

`tests/unit/test_free_threading.py` pins this. In every lane,
`test_non_redis_programs_never_load_redis_py` checks that `import cachekit`,
an `@cache(backend=None)` call and a `CachekitIOBackend` leave redis-py
unloaded, and `test_hiredis_settings_are_read_before_backends_load` checks the
setting is read before `cachekit.backends` or redis-py load. In this lane,
`test_redis_backend_blocks_hiredis_before_redis_loads` checks that a
`RedisBackend`, a `PooledClientProvider` and the invalidation listener's
import of redis-py each block hiredis before redis-py is first requested and leave redis-py on `_RESP2Parser`. None of these needs
hiredis installed. `test_default_install_keeps_gil_disabled_after_redis_backend`
checks the GIL in a fresh interpreter with hiredis installed, and
`test_gil_stays_disabled_with_hiredis_blocked` is its control. Both skip where
hiredis is not installed, as in this lane, so run them by hand in a 3.14t
environment that has hiredis:

```bash
uv sync --python 3.14t --no-default-groups --group test
uv run --no-sync pytest tests/unit/test_free_threading.py
```

In that environment `test_gil_stays_disabled_after_importing_cachekit` and the
session-teardown GIL check still report a re-enabled GIL. That is the test
harness, not cachekit: the pytest-redis plugin and `tests/conftest.py` import
redis, and so hiredis, before any test imports cachekit.

## Measured performance

A post-merge benchmark run (commit `bda770bce822d9a6eff98e555c5f6fd92e509a9c`, CPython 3.14.3 free-threaded build, eight logical CPUs, pinned with `taskset -c 0-7` on a Ryzen 9 5950X) compared no-GIL and GIL serializer throughput: `tests/performance/gil_benchmark.py`, which times `StandardSerializer.serialize` alone, not decorated cache calls:

| threads | no-GIL median s (min–max) | GIL median s (min–max) | GIL / no-GIL |
| --: | --: | --: | --: |
| 1 | 3.4422 (2.7822–4.4336) | 3.0566 (2.7979–3.3807) | 0.89x |
| 2 | 1.9956 (1.8117–2.2210) | 3.6367 (3.2625–3.8376) | 1.82x |
| 4 | 1.3402 (1.1175–1.9018) | 3.5293 (3.4629–4.8231) | 2.63x |
| 8 | 1.1550 (0.8962–1.4837) | 3.6749 (3.5853–4.7236) | 3.18x |

**Measurement conditions:** Five isolated repetitions each; 16,000 `serialize` calls (2,000 per thread at eight threads); harness built-in warmup; GIL state asserted via `sys._is_gil_enabled()` at runtime. See [verification comment](https://github.com/cachekit-io/cachekit-py/pull/188#issuecomment-5557418229) for full details.

**Key findings:**
- **Threaded throughput confirmed.** no-GIL reaches 2.57x one→four-thread scaling (64.2% efficiency) and is 2.63x faster than the GIL arm at four threads.
- **Single-thread cost confirmed.** no-GIL is 12.6% slower at the single-thread median; however, the ranges overlap (GIL max 3.3807 vs no-GIL min 2.7822).

**Cross-library comparison:** The benchmark measures cachekit operations only. Cross-library throughput (orjson, numpy, pandas, pyarrow) was not run. It waits on orjson, which does not publish free-threaded (`cp314t`) wheels as of 2026-09-29; cross-stack performance will be measured once it does.

### Decorated-call scaling

`tests/performance/ft_scaling_bench.py` measures whole decorated calls, so it
sees the shared state a real call touches: the L1 lock, per-function stats,
the metrics path, the Rust `ByteStorage` and the backend client. It is a
manual bench and no CI job runs it. It runs three cells at 1, 4 and 16
threads: an L1 hit, an L2 hit over an in-process backend, and an L2 hit
through the shipped CachekitIO backend against a loopback TLS fake
(`tests/performance/loopback_saas.py`). The CachekitIO cell measures
client-side contention only, never service latency. The fake serves each
connection on one worker thread, so the summary flags a cell `SERVER-BOUND`
when a worker was more than 80% busy: there the fake, not the client, sets
the ceiling.

Each process runs one arm. Every repetition runs every arm, in a seeded
shuffled order, so no arm always follows the same neighbour:

| arm | interpreter | what it shows |
| --- | --- | --- |
| `ft-nogil` | free-threaded build, `PYTHON_GIL=0` | the no-GIL result |
| `ft-nogil-aa` | the same again | the A/A noise floor of this session |
| `ft-gil` | the same binary, `PYTHON_GIL=1` | the clean GIL vs no-GIL comparison |
| `ft-default` | the same binary, `PYTHON_GIL` unset | the GIL state the imports leave behind |
| `gil-build` | a default GIL build | context only: a different binary |

```bash
uv sync --python 3.14t --no-default-groups --group test   # FT env (UV_PROJECT_ENVIRONMENT=... to keep it apart)
uv sync --python 3.14 --no-default-groups --group test    # GIL env
python tests/performance/ft_scaling_bench.py --ft-python <ft-env>/bin/python \
    --gil-python <gil-env>/bin/python --reps 7 --cpus 0-15 --server-cpus 16-23 --out ft-scaling.jsonl
```

The primary metric is scaling: calls/s at N threads over calls/s at one
thread, within one process. A cell is `TAINTED` when a timed call was not a
hit or a backend call raised, which usually means the backend failed and the
call fell back to computing; a tainted cell measures that fallback, so it is
left out of every statistic. The summary gives the median and min–max over
clean repetitions. Each repetition is a session block that holds every arm,
so each arm's difference from `ft-nogil` is paired by repetition, with a 95%
bootstrap CI that resamples whole repetitions; with fewer than five clean
pairs it prints "insufficient clean reps" instead of a CI. A difference counts
only if its CI excludes zero and it is larger than the `ft-nogil-aa` floor.
The driver stops if an arm ran under the wrong GIL state, if `--ft-python` is
not a free-threaded build, or if `--gil-python` is one. It refuses an existing
`--out` file so two sessions are never pooled, and the summary refuses a file
that repeats an arm's repetition. The summary also drops a process whose GIL
state changed while the cells ran.

The CachekitIO cell no longer taints under no-GIL. On httpx it did: threads
sharing one HTTP/2 connection raced in httpcore's send path
([encode/httpcore#1118](https://github.com/encode/httpcore/pull/1118)), failing
23–32% of raw requests with 8 threads on 3.14t and 1–4% with the GIL, and its
HTTP/1.1 pool raced too without the GIL (`has_expired()` raised `TypeError` on
0.15–0.23% of requests). urllib3's pool hands each request its own connection
under a lock-protected queue: `tests/unit/backends/test_cachekitio_thread_share.py`
expects zero failures with the GIL and without it, and 10 runs of it on 3.14t with
the GIL off (72,000 shared-backend operations) failed none.

## CPython 3.15+: one abi3t wheel

From 3.15, each `build-wheels` row in `.github/workflows/release-please.yml`
builds per-version wheels for each supported CPython below 3.15 plus one
`cp315-abi3.abi3t-<platform>` wheel, with PyO3's `abi3t-py315` feature on
(PEP 803). One wheel serves the GIL and the free-threaded builds of 3.15 and
of every later CPython, so a new CPython release gets a wheel on its release
day instead of a Rust source build. Building it needs maturin 1.15+
(`[build-system]` in `pyproject.toml`).

Installers read the compressed tag as `cp315-abi3` plus `cp315-abi3t`. On a
GIL 3.15 every installer matches the `abi3` half, older ones included (pip 25.2
installs it). On 3.15t only pip 26.1+ and uv 0.11.3+ match the `abi3t` half; an
older installer there skips the wheel and builds the sdist, which needs a Rust
toolchain, as every 3.15t install did before.

On Windows the abi3t extension links `python3t.dll`, which the python.org
installers ship with both the GIL and the free-threaded build of 3.15. A
distribution or a frozen application (PyInstaller, cx_Freeze) that leaves
that DLL out fails to import cachekit with "DLL load failed" on a GIL 3.15.

The limited API loses version-specific fast paths, so abi3t was adopted only
after a benchmark against per-version `cp315` / `cp315t` builds, with the rule
fixed before measuring: abi3t ships for an interpreter only if every point's
95% upper bound of abi3t ÷ per-version is 1.10 or less (the repo's own 10%
serializer regression bar, `make benchmark-compare`). Otherwise 3.15 keeps
per-version wheels. Both
interpreters passed, so **abi3t replaces the cp315 per-version wheel**. The
largest estimate was GIL 3.15 `verify_checksum` at 64 B: +3.7% (upper bound
+4.7%), about 6 ns per call. The largest upper bound was 3.15t
`verify_checksum` at 1 MB: 1.060.

| interp | point | per-version median ns | abi3t median ns | abi3t ÷ per-version [95% CI] | A/A noise [95% CI] | runs/arm |
| --- | --- | --: | --: | --- | --- | --: |
| 3.15 | compute-64B | 124 | 128 | 1.031 [1.013, 1.044] | 1.012 [0.994, 1.034] | 120 |
| 3.15 | verify-64B | 161 | 167 | 1.037 [1.030, 1.047] | 1.008 [0.998, 1.023] | 120 |
| 3.15 | compute-1KB | 211 | 204 | 0.967 [0.955, 0.982] | 1.014 [0.999, 1.044] | 120 |
| 3.15 | verify-1KB | 252 | 245 | 0.970 [0.953, 0.980] | 1.019 [0.992, 1.058] | 120 |
| 3.15 | compute-64KB | 3130 | 3117 | 0.996 [0.977, 1.011] | 1.006 [0.984, 1.031] | 120 |
| 3.15 | verify-64KB | 3146 | 3147 | 1.000 [0.988, 1.025] | 0.997 [0.986, 1.017] | 120 |
| 3.15 | compute-1MB | 52117 | 52474 | 1.007 [0.970, 1.038] | 1.000 [0.975, 1.039] | 120 |
| 3.15 | verify-1MB | 53453 | 53381 | 0.999 [0.982, 1.031] | 0.997 [0.981, 1.019] | 120 |
| 3.15 | msgpack-plain | 6720 | 6834 | 1.017 [1.010, 1.025] | 1.001 [0.996, 1.012] | 120 |
| 3.15 | msgpack-encrypted | 13795 | 14035 | 1.017 [0.988, 1.033] | 1.008 [0.973, 1.030] | 120 |
| 3.15t | compute-64B | 132 | 128 | 0.969 [0.956, 0.982] | 1.007 [0.985, 1.022] | 60 |
| 3.15t | verify-64B | 170 | 169 | 0.996 [0.979, 1.013] | 1.001 [0.988, 1.021] | 60 |
| 3.15t | compute-1KB | 203 | 205 | 1.005 [0.991, 1.020] | 1.000 [0.984, 1.015] | 60 |
| 3.15t | verify-1KB | 249 | 240 | 0.966 [0.944, 0.976] | 1.009 [0.988, 1.018] | 60 |
| 3.15t | compute-64KB | 3056 | 3095 | 1.013 [1.001, 1.022] | 1.003 [0.990, 1.017] | 60 |
| 3.15t | verify-64KB | 3085 | 3103 | 1.006 [0.997, 1.026] | 1.004 [0.984, 1.014] | 60 |
| 3.15t | compute-1MB | 49874 | 50110 | 1.005 [0.965, 1.031] | 0.996 [0.952, 1.015] | 60 |
| 3.15t | verify-1MB | 51196 | 52680 | 1.029 [0.995, 1.060] | 1.008 [0.965, 1.044] | 60 |
| 3.15t | msgpack-plain | 7735 | 7870 | 1.017 [1.000, 1.029] | 1.005 [0.987, 1.020] | 60 |
| 3.15t | msgpack-encrypted | 15850 | 15970 | 1.008 [0.978, 1.031] | 1.007 [0.973, 1.025] | 60 |

**Measurement conditions:** 2026-10-25, both builds from the same tree in
`quay.io/pypa/manylinux2014_x86_64` (the image maturin-action uses for the
x86_64 Linux row) with maturin 1.15.0, rustc 1.99.1 and the release profile;
CPython 3.15.1 and 3.15.1t from that image; AMD Ryzen 9 5950X, 32 logical
CPUs. Points:
`_rust_serializer.checksum` (compute) and `verify_checksum` at the four sizes
of `tests/performance/benchmark_checksum_ffi.py` (median per call over 201
batches of ~200 µs), and the plain and encrypted `StandardSerializer` round
trips of `test_encryption_overhead_msgpack` (median of 10,000 timed round
trips). One process is one run; per-version (A), a second per-version arm (A'
for the A/A noise check) and abi3t (B) ran in a seeded shuffled order
(`stats_utils.balanced_order`). The estimate is the ratio of the arms' medians
of per-run medians, with a 2,000-resample bootstrap CI over whole runs. The
3.15 arm took 120 runs per arm: at 60 its A/A bounds exceeded 1.10 on 8
points during a load spike, so runs were added until none did, and no point
was excluded. The added 60 runs per arm pass on their own: abi3t ÷
per-version upper bound at most 1.046, A/A at most 1.035.

**Dependencies on 3.15t (PyPI, 2026-10-25):** every runtime dependency ships a
`cp315t` wheel (hiredis 3.5.0, pydantic-core, msgpack, xxhash, blake3). psutil,
which has none, was dropped from the runtime dependencies: cachekit never
imported it. Of the optional extras, orjson 3.13.0 (`[json]`) has no `cp315t`
wheel and builds from source; numpy 2.5.4, pandas 3.1.0 and pyarrow 26.0.0
(`[data]`) all ship them.

## Deferred: declared support + free-threaded wheels

Publishing `cp314t` wheels and declaring official free-threaded support (the
`Programming Language :: Python :: Free Threading` classifier, which would
also claim 3.14t) is **explicitly deferred** until the
dependency chain allows it. Blocking as of 2026-09-29:

- **orjson** — no free-threaded wheels through 3.12.0, and its build script
  rejects free-threaded interpreters ("does not support free-threaded
  Python"). Optional `[json]` extra, but a support declaration that breaks
  the moment a user adds `cachekit[json]` is not a declaration worth making.
- **hiredis** — no `Py_mod_gil` declaration; importing it re-enables the GIL.
  Pulled in unconditionally via the required `redis[hiredis]` dependency.
  A program without cachekit's Redis backend never loads it. One that uses
  the Redis backend has it blocked on free-threaded builds (see
  [The CI safety net](#the-ci-safety-net)), so it runs redis-py's slower
  pure-Python parser.

numpy, pandas and pyarrow (the `[data]` extra) now publish `cp314t` wheels,
but the free-threaded CI lane does not install `[data]` yet, so `[data]` on
3.14t is untested. pandas 2.x does not declare free-threaded support
(`pandas._libs.pandas_parser` re-enables the GIL when it loads). Measured on
CPython 3.14.7t on 2026-10-05, importing each package alone in a fresh
process: pandas 2.3.3 re-enables the GIL; pandas 3.0.6, numpy 2.3.4 and
2.5.3, and pyarrow 24.0.0 and 25.0.1 leave it off. cachekit's lockfile pins
pandas 2.3.3. `import cachekit` never imports numpy, pandas or pyarrow, so
installing `[data]` keeps the GIL off at import. Reading a cached DataFrame or Series in a fresh process
imports pandas, and the GIL turns on then, as it does when your own code
imports pandas.

When those clear: add `-i python3.14t` targets to the `build-wheels` matrix in
`.github/workflows/release-please.yml`, revisit `redis[hiredis]` (marker or
documented degradation), and update this page plus the README support
statement. Track upstream — do not fork or vendor.
