# Free-Threaded CPython

Status as of LAB-511 (2026-08): **tested on 3.14t, not yet declared**.
(3.13t is not in the CI matrix — no claim is made for it.)

- The core test suites (`tests/unit/`, `tests/critical/`) run green on
  free-threaded CPython 3.14 with the GIL verified disabled, gated by the
  `test-freethreaded` CI job on every PR and push.
- The Rust extension declares free-threaded safety
  (`#[pymodule(gil_used = false)]`), so importing `cachekit._rust_serializer`
  does not force the GIL back on.
- **No free-threaded (`cp31Nt`) wheels are published yet**, and installing
  cachekit on a free-threaded interpreter is not officially supported. See
  [Deferred: declared support](#deferred-declared-support--free-threaded-wheels).

## What "works only under the GIL" used to mean here

Lock-free fast paths written under the GIL inherit its implicit guarantees:
one bytecode interleaving at a time and, effectively, sequentially consistent
publication of writes. Free-threaded CPython removes both. Its memory model
does not specify cross-variable store visibility order for plain reads, so a
reader may observe a *later* store while an *earlier* one is not yet visible.

Concrete instance (the defect that motivated LAB-511):
`decorators/session.py::_ensure_session_initialized` published three module
globals and relied on assignment order (`_session_start_ms`, then
`_session_id`, then `_session_pid`) to make the lock-free fast path safe. A
GIL-free reader observing `pid`+`id` but not yet `start_ms` sailed past the
fast path into `get_session_start_ms()`'s "should never happen"
`RuntimeError` — which `backends/cachekitio/backend.py` catches and converts
into *silently dropped session headers*, the exact telemetry loss LAB-506
eliminated. The fix gates the fast path (and the in-lock double-check) on
**every** published field; observing a partial publish now falls through to
the lock and waits for the in-flight initializer.

Rule of thumb applied throughout the audit:

- **Single-assignment publication** of a fully-constructed object through one
  reference (e.g. a double-checked module-global singleton) is acceptable.
- **Multi-field publication** that readers expect to be mutually consistent
  must be lock-protected or gated on every field — assignment order proves
  nothing without the GIL.

## Concurrency audit (LAB-511)

Every lock-free fast path and shared mutable module/instance state named by
the ticket, plus what the free-threaded CI lane surfaced and sites added since:

| Site | Mechanism | Verdict |
|:-----|:----------|:--------|
| `decorators/session.py` `_ensure_session_initialized` | Lock-free fast path over three module globals | **Fixed** — fast path and double-check gate on all three fields; regression tests in `tests/unit/test_saas_observability.py::TestMidPublishMemoryOrdering` and `tests/unit/test_free_threading.py` |
| `reliability/metrics_collection.py` `AsyncMetricsCollector.flush` | Polled `Queue.empty()` | **Fixed** — `empty()` flips when the worker *dequeues*, not when it finishes processing; flush now waits on a collector-owned pending-work counter + `Condition` (+1 on enqueue, −1 once the worker has processed or dropped the item, `notify_all()` when it reaches zero). Was a routine flake on the free-threaded lane, invisible under the GIL's coarse scheduling |
| `decorators/wrapper.py` `_FunctionStats` | `RLock` around every counter mutation and `get_info` | Safe. `l1_enabled` is a plain attribute re-set on re-decoration (under the registry lock) and read without the stats lock; a stale read yields a conservative rate-limit classification header, never corruption |
| `decorators/wrapper.py` `_function_stats_registry` | Module-level `Lock` around all access | Safe |
| `os.register_at_fork` handlers (session + stats registry) | Run in the child while single-threaded; replace locks wholesale | Safe — single-threaded by construction at execution time |
| `backends/cachekitio/session.py` header cache | `threading.local` | Safe — per-thread state; its only cross-thread hazard was the session-identity publication above |
| `decorators/stats_context.py` | `contextvars.ContextVar` | Safe by construction |
| `decorators/wrapper.py` L1/SWR + L2/SWR single-flight (`_RefreshPool`: `_l1_swr_pool`, `_l2_swr_pool`) | One `Lock` per pool over its holder map (an L2 holder carries its in-flight key) + PID-owner swap | Safe. The prune, the cap check and L2's per-key check run under the pool lock, so L2 has no check-then-add race; L1's per-key check is `ObjectCache`'s, under its own lock. A pruned refresh is cancelled with `call_soon_threadsafe` on its own loop. The fork-detection swap race is the documented-benign shape: a thread racing it can lose its new holder, whose key then waits for its hard TTL (LAB-7295) |
| `l1_cache.py` `L1CacheManager._take_over_if_forked` + `_empty_caches_after_fork` (LAB-4772) | Owner-PID fast path; on a PID change one thread takes over under a lock fetched by `dict.setdefault(pid, Lock())`. An `os.register_at_fork` hook gives every cache a fresh, empty state before the child's first `get()`, so a forked child starts with an empty L1; fork mechanics and the uWSGI limit are in the `_take_over_if_forked` docstring | Safe. `setdefault` is atomic under per-object locking, so every thread in a new child gets the same fresh lock, which no parent thread can have held at fork; the rest block on it and re-check. The fast path gates on `_owner_pid` alone, so a weak-memory reader may still see the inherited manager lock once: the same benign fork-swap race as the SWR row, since that lock is unheld unless a parent thread held it at fork. The hook runs while the child is single-threaded and replaces every cache's state, held lock or not, so it probes no lock, takes none and starts no thread. It does not free the inherited states, which would copy the parent's L1 pages into every child inside `fork()`: they wait in `_inherited_states` until the child's first put, whose take-over frees them in batches, giving up the GIL between batches, on the cleanup thread it restarts or, when the parent ran none, on a one-shot thread. Each state is taken off that list with one `list.pop()`, atomic under per-object locking, before it is freed, so two releasers running at once (the threads of two managers' take-overs) never take the same state and neither finds the list emptied under it. So a child bound for exec never pays. A child that uses its L1 pays once, up front: each free also writes the allocator's bookkeeping beside the entry, so its first put copies about 2.3 times the inherited L1 (36.5 MiB for a 16 MiB L1 in `tests/performance/test_l1_fork_memory.py`), and its own L1 then reuses that memory. Kept, those pages would cost nothing while the parent left them alone, as an idle preloaded master whose entries outlive its workers does. Freeing is the default anyway because a parent with a cleanup thread rewrites them within one L1 TTL, and from then on a child that kept them would hold a private copy nothing can reach, beside its own L1. A state whose lock is taken is only dropped: one a thread forked inside its critical section holds is freed with that thread's last reference, and one a parent thread held at fork can stay for the child's life, referenced by that thread's frame. Neither the hook nor the take-over clears shared state: each publishes a fresh `_L1State` (entries, byte count and lock) in one store, and a holder that is still running finishes on the state it bound (the forking thread, if it forked inside a critical section; without hooks, a live child thread holding a lock past the take-over's 1 s probe). Invalidations re-apply if a reset replaced the state meanwhile. Without hooks, the cost of a wrong guess is that namespace's L1 entries, once per child; `--py-call-uwsgi-fork-hooks` avoids it. Once it has run, the take-over skips its own timed cache-lock probe, which could otherwise drop a cache's entries under a live child thread. In a child the hook never reached (a fork made from C, as uWSGI does without `--py-call-uwsgi-fork-hooks`; any PID other than the importing process's and the one the hook last ran in), the take-over starts no thread and `start_background_cleanup` refuses to start one, including for a manager first built in that child: such a fork also skips CPython's own after-fork repair, so a thread started there can hang in `Thread.start()` or crash the interpreter. None of that logs, the take-over's lock-reset drop included, because the same fork skips `logging`'s at-fork lock reset, and a handler lock a parent thread held would hang the child. Expired entries in that child are evicted on read or under the memory bound instead. The detection needs cachekit imported before the fork: a process that first imports it after a fork made from C (uWSGI `--lazy-apps` without `--py-call-uwsgi-fork-hooks`) looks like a fresh process, and starts the cleanup thread. No check made at import tells the two apart |
| `backends/cachekitio/client.py` client cache + `CachekitIOBackend._own_sync_lease` | One urllib3 connection pool per config, shared by every thread of a process (urllib3's pool is thread-safe: a `LifoQueue` of connections, each request on its own connection). The process-wide lease cache is a `WeakValueDictionary` under a `Lock`, both held by one `_Leases` object that carries the PID that built it; each lease records its PID too, and the backend re-leases on a mismatch, checked per request. Async methods send on the same client through `asyncio.to_thread` | Safe across threads, with and without the GIL: `tests/unit/backends/test_cachekitio_thread_share.py` runs 8 threads on one backend, sync and through `to_thread`, and expects zero failures on 3.14 and on 3.14t with the GIL off. Safe across fork. A forked child never sends on or reads from its parent's pooled connections, which share the parent's TLS sessions: its first request finds the cache's PID is not its own, replaces the cache and its lock with new ones, and re-leases a new client, whether it was forked by `os.fork()` or from C with no at-fork hooks (uWSGI). An owner-PID check rather than an at-fork hook for that reason. The PID travels with the cache and its lock in one published reference, and with the client in the lease (single-assignment publication), so no thread pairs a new PID with an inherited lock or client, and no child thread takes the parent's lock, which a parent thread may have held at fork. Inherited clients are dropped unclosed: `close()` takes the pool's queue lock, for the same reason, and garbage collection closes the child's copies of the sockets without sending anything. Two child threads racing the first request may each build a cache and a client; the one replaced is simply not shared, and each request holds its lease until it returns, so that replacement never closes a client under an in-flight request. A child runs async calls on an event loop of its own (`asyncio.run`): a child that keeps running its parent's loop object waits forever on that loop's inherited default executor, whose worker threads did not survive the fork, as every async decorator's L2 operation already did. In a child forked from C, any logging call whose level check is not already cached, urllib3's per-request `DEBUG` check included, waits on `logging`'s module lock, and hangs if a parent thread held it at fork; a logging reconfiguration clears those caches. `--py-call-uwsgi-fork-hooks` avoids this. Tests in `tests/unit/backends/test_cachekitio_fork.py` |
| `invalidation.py` dispatch map, start lock and listener thread | `_evictors` (registry id → `WeakSet` of evictors) under a per-PID `Lock` from `dict.setdefault` (`_dispatch_locks`): decoration registers under it, and the listener thread takes a snapshot under it and runs the evictors outside it. The listener is redis-py's own `PubSubWorkerThread`, started behind an owner-PID check under a second per-PID lock (`_start_locks`) taken non-blocking, so a cache operation never waits on another thread's start | Safe. `setdefault` is atomic under per-object locking, so threads racing to create a new process's lock share one, and a child forked while a parent thread holds either lock gets its own instead of waiting on a lock nobody will release. Walking the live `WeakSet` while a decoration adds to it would raise; the snapshot under the lock rules that out. The owner-PID check reads `_listener_pid`, which the starting thread publishes last, so a stale read only skips or retries a start. Evictors call `L1Cache.invalidate` and `invalidate_many`, which lock per state as in the row above. A child forked without at-fork hooks starts no listener and logs nothing (the same detection as the row above). Tests in `tests/unit/test_invalidation_listener.py` |
| `decorators/wrapper.py` `_cached_keys` | Builtin `set`, snapshot-copied before iteration in invalidation | Safe — single ops atomic; a copy racing an add can only miss a concurrently-written key, which invalidate-all semantics tolerate |
| `object_cache.py` `ObjectCache` + `_reset_caches_after_fork` | `RLock` on every public method. The entries, their byte total, their in-flight refresh markers and the lock form one `_ObjectCacheState`, which every critical section binds once. An `os.register_at_fork` hook walks a `WeakSet` of live caches in a forked child | Safe. The hook runs while the child is single-threaded, so it takes no lock that can block and starts no thread. A cache whose lock was free at fork keeps its entries and loses its in-flight refresh markers, since the threads running those refreshes did not survive the fork, so the child's next stale read refreshes again. A cache whose lock was held gets a fresh, empty state in one attribute store: a parent thread that held it is gone, and a forking thread that forked inside a critical section (from a signal handler or a finalizer) may never return there, as in a `multiprocessing` child. A forking thread that does return finishes on the state it bound. The generation counter stays on the cache, so no refresh token is reused across states. A child forked without at-fork hooks (uWSGI without `--py-call-uwsgi-fork-hooks`) gets no repair: its first call hangs if a parent thread held that cache's lock at fork, and a key whose refresh was in flight is served stale until it expires. The check that could catch that, an owner-PID check, would cost a `getpid()` on every cached call. Tests in `tests/unit/test_object_cache.py::TestObjectCacheFork` |
| `reliability/metrics_collection.py` `get_async_metrics_collector` | Double-checked module-global singleton | Safe — single-assignment publication of a fully-constructed object; worst case a benign duplicate worker-restart check |
| `reliability/async_metrics.py` `_metrics_cache` + `_metrics_cache_lock()` | Double-checked module dict of Prometheus metric objects; one `Lock` per PID, created with `dict.setdefault`; held across `os.fork` by at-fork hooks | Safe — the lock-free read sees `None` or a fully constructed metric (single-assignment publication). `setdefault` is atomic under per-object locking, so threads racing to create a new process's lock all get the same one; a child forked without the hooks never takes its parent's lock. Tests in `tests/unit/test_async_metrics_shared_series.py` |
| `backends/memcached/backend.py` `MemcachedBackend` | pymemcache `HashClient(use_pooling=True)`: one `PooledClient` per server, whose `ObjectPool` `threading.Lock` guards only the free/used deque pop and append, never socket I/O | Safe across threads: each operation owns its checked-out connection, so no socket is shared. The pool raises at `max_pool_size` rather than waiting (`tests/unit/backends/test_memcached_pooling.py`). pymemcache's failover state (`HashClient._failed_clients`) is unlocked: while a failed server is retried, concurrent operations can raise a spurious `BackendError` wrapping `KeyError` (documented in `docs/backends/memcached.md#concurrency`). Not fork-safe, by design: a child that reused its parent's backend would share the parent's pooled sockets, and could deadlock on a pool lock a parent thread held at fork. A per-PID lock would cure only the deadlock, not the shared sockets, so the rule is that a forked child builds its own backend (documented in `docs/backends/memcached.md#concurrency`) |
| `reliability/async_metrics.py` `AsyncMetricsCollector._maybe_switch_mode`, `shutdown`, `_take_over_if_forked` | Auto sync/batched switch; `_should_check_mode` is an unlocked check-then-set, so two producers can both run a switch. A forked child inherits the batching state (queue, stop event, worker, pool lock) and prometheus_client's metric locks | **Fixed** — one `Lock` held across the switch, so concurrent switches start one worker. The lock is per PID (a per-instance dict filled with `setdefault`, like `_metrics_locks`), so a child forked while a thread is mid-switch never waits on the copy that thread still holds. `shutdown()` takes the same lock, bars any later switch from starting a worker, and sets sync mode before stopping the worker, so every later record lands synchronously; a record another thread had already chosen to queue can miss the worker's final drain (at most one per such thread). Returning to batched mode starts a worker on the existing queue only once the previous worker has exited (its exit drain assumes a single consumer); while it is alive the collector stays synchronous and retries at the next mode check, never joining on the caller's thread. Within a process the queue is never replaced, so producers always find one. **Fork:** an owner-PID take-over gives an inherited collector fresh batching state and sync mode in a forked child. The at-fork hook (after a fork made from C, the first take-over or collector built in the child instead) gives cachekit's own metrics and the default registry fresh prometheus_client locks; an application's own metrics are not covered, and neither are values in prometheus_client's multiprocess mode (`PROMETHEUS_MULTIPROC_DIR`), which share one lock the reset cannot reach, so a child forked while a parent thread holds it can still hang. No collector, inherited or built there, starts a thread in the child of a fork made from C, where at-fork hooks are skipped and `is_alive()` stays stale, provided cachekit was imported before the fork. If it is first imported in such a child (uWSGI `--lazy-apps`), the child looks like a fresh process: a collector may start a worker there, which is an open case; `--py-call-uwsgi-fork-hooks` avoids it. On free-threaded builds such a fork without `--py-call-uwsgi-fork-hooks` is unsupported: any Python code in that child can hang on a per-object lock a parent thread held, before cachekit can act. The `_take_over_if_forked`, `_reset_metric_locks` and `_reset_metric_locks_once` docstrings give the mechanics and the open cases. There is no per-record `getpid()` on the sync path. Tests in `tests/unit/test_async_metrics_mode_switch.py` |
| Rust extension (`rust/src/`, cachekit-core 0.5.0) | `#[pymodule(gil_used = false)]`; every `#[pyclass]` exposes only `&self` methods; nonce counter is `AtomicU64`, metrics behind `Mutex`; PyO3 enforces `Send + Sync` on pyclasses at compile time | Safe — declared free-threading-ready. (The wasm32 `Cell` nonce variant is single-threaded by target.) |

## The CI safety net

`.github/workflows/ci.yml` job `test-freethreaded`:

1. Installs exactly what the job runs — note the hiredis exclusion, it is
   load-bearing (see below):

   ```bash
   uv sync --python 3.14t --no-default-groups --group test --no-install-package hiredis
   ```

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

## Deferred: declared support + free-threaded wheels

Publishing `cp314t` wheels and declaring official free-threaded support is
**explicitly deferred** (per the LAB-511 acceptance criteria) until the
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
3.14t is untested.

When those clear: add `-i python3.14t` targets to the `build-wheels` matrix in
`.github/workflows/release-please.yml`, revisit `redis[hiredis]` (marker or
documented degradation), and update this page plus the README support
statement. Track upstream — do not fork or vendor (LAB-511 non-goal).
