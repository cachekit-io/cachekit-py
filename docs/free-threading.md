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
| `decorators/wrapper.py` L1/SWR + L2/SWR single-flight (`_l1_swr_*`, `_l2_swr_*`) | `BoundedSemaphore` slots + in-flight `set` + PID-owner swap | Safe. The check-then-add on the in-flight set was already documented as benign (worst case one duplicate refresh, absorbed by last-write-wins / the backend lease); builtin `set`/`dict` single ops are atomic under free-threading's per-object locking. The fork-detection wholesale swap races are the same documented-benign shape |
| `l1_cache.py` `L1CacheManager._take_over_if_forked` + `_reset_cache_locks_after_fork` (LAB-4772) | Owner-PID fast path; on a PID change one thread takes over under a lock fetched by `dict.setdefault(pid, Lock())`. An `os.register_at_fork` hook repairs cache locks orphaned at fork before the child's first `get()`; fork mechanics and the uWSGI limit are in the `_take_over_if_forked` docstring | Safe. `setdefault` is atomic under per-object locking, so every thread in a new child gets the same fresh lock, which no parent thread can have held at fork; the rest block on it and re-check. The fast path gates on `_owner_pid` alone, so a weak-memory reader may still see the inherited manager lock once: the same benign fork-swap race as the SWR row, since that lock is unheld unless a parent thread held it at fork. The hook runs while the child is single-threaded, so a non-blocking probe (`timeout=0`) suffices, and it starts no thread. Neither probe proves a holder dead (in the hook the forking thread may have forked inside a critical section; without hooks a live child thread may hold a lock past the 1 s probe), so a reset never clears shared state: it publishes a fresh `_L1State` (entries, byte count and lock) in one store, and a holder that is still running finishes on the state it bound. Invalidations re-apply if a reset replaced the state meanwhile. Without hooks, the cost of a wrong guess is that namespace's L1 entries, once per child; `--py-call-osafterfork` avoids it. The hook repairs a cache before logging that cache's drop warning, and carries on past a log call that raises, so every cache is repaired. Once it has run, the take-over skips its own timed cache-lock probe, which could otherwise drop a cache's entries under a live child thread. In a child the hook never reached (a fork made from C, as uWSGI does without `--py-call-osafterfork`; any PID other than the importing process's and the one the hook last ran in), the take-over starts no cleanup thread and `start_background_cleanup` refuses to start one, including for a manager first built in that child: such a fork also skips CPython's own after-fork repair, so a thread started there can hang in `Thread.start()` or crash the interpreter. None of that logs, the take-over's lock-reset drop included, because the same fork skips `logging`'s at-fork lock reset, and a handler lock a parent thread held would hang the child. The hook keeps its drop warning: `logging`'s own at-fork hook has repaired the handler locks before it runs. Expired entries in that child are evicted on read or under the memory bound instead. The detection needs cachekit imported before the fork: a process that first imports it after a fork made from C (uWSGI `--lazy-apps` without `--py-call-osafterfork`) looks like a fresh process, and starts the cleanup thread. No check made at import tells the two apart |
| `decorators/wrapper.py` `_cached_keys` | Builtin `set`, snapshot-copied before iteration in invalidation | Safe — single ops atomic; a copy racing an add can only miss a concurrently-written key, which invalidate-all semantics tolerate |
| `object_cache.py` `ObjectCache` | `RLock` on every public method | Safe |
| `reliability/metrics_collection.py` `get_async_metrics_collector` | Double-checked module-global singleton | Safe — single-assignment publication of a fully-constructed object; worst case a benign duplicate worker-restart check |
| `reliability/async_metrics.py` `_metrics_cache` + `_metrics_cache_lock()` | Double-checked module dict of Prometheus metric objects; one `Lock` per PID, created with `dict.setdefault`; held across `os.fork` by at-fork hooks | Safe — the lock-free read sees `None` or a fully constructed metric (single-assignment publication). `setdefault` is atomic under per-object locking, so threads racing to create a new process's lock all get the same one; a child forked without the hooks never takes its parent's lock. Tests in `tests/unit/test_async_metrics_shared_series.py` |
| `backends/memcached/backend.py` `MemcachedBackend` | pymemcache `HashClient(use_pooling=True)`: one `PooledClient` per server, whose `ObjectPool` `threading.Lock` guards only the free/used deque pop and append, never socket I/O | Safe across threads: each operation owns its checked-out connection, so no socket is shared. The pool raises at `max_pool_size` rather than waiting (`tests/unit/backends/test_memcached_pooling.py`). pymemcache's failover state (`HashClient._failed_clients`) is unlocked: while a failed server is retried, concurrent operations can raise a spurious `BackendError` wrapping `KeyError` (documented in `docs/backends/memcached.md#concurrency`). Not fork-safe, by design: a child that reused its parent's backend would share the parent's pooled sockets, and could deadlock on a pool lock a parent thread held at fork. A per-PID lock would cure only the deadlock, not the shared sockets, so the rule is that a forked child builds its own backend (documented in `docs/backends/memcached.md#concurrency`) |
| `reliability/async_metrics.py` `AsyncMetricsCollector._maybe_switch_mode` | Auto sync/batched switch; `_should_check_mode` is an unlocked check-then-set, so two producers can both run a switch | **Fixed** — one `Lock` held across the switch, so concurrent switches start one worker. The lock is per PID (a per-instance dict filled with `setdefault`, like `_metrics_locks`), so a child forked while a thread is mid-switch never waits on the copy that thread still holds. `shutdown()` takes the same lock and marks the collector shut down, so a switch racing it, or any later switch, cannot restart the worker it stopped. Returning to batched mode starts a worker on the existing queue only once the previous worker has exited (its exit drain assumes a single consumer); while it is alive the collector stays synchronous and retries at the next mode check, never joining on the caller's thread. The queue is never replaced, so producers always find one. Tests in `tests/unit/test_async_metrics_mode_switch.py` |
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

## Measured performance

A post-merge benchmark run (commit `bda770bce822d9a6eff98e555c5f6fd92e509a9c`, CPython 3.14.3 free-threaded build, eight logical CPUs, pinned with `taskset -c 0-7` on a Ryzen 9 5950X) compared no-GIL and GIL cache throughput:

| threads | no-GIL median s (min–max) | GIL median s (min–max) | GIL / no-GIL |
| --: | --: | --: | --: |
| 1 | 3.4422 (2.7822–4.4336) | 3.0566 (2.7979–3.3807) | 0.89x |
| 2 | 1.9956 (1.8117–2.2210) | 3.6367 (3.2625–3.8376) | 1.82x |
| 4 | 1.3402 (1.1175–1.9018) | 3.5293 (3.4629–4.8231) | 2.63x |
| 8 | 1.1550 (0.8962–1.4837) | 3.6749 (3.5853–4.7236) | 3.18x |

**Measurement conditions:** Five isolated repetitions each; 16,000-operation workload; harness built-in warmup; GIL state asserted via `sys._is_gil_enabled()` at runtime. See [verification comment](https://github.com/cachekit-io/cachekit-py/pull/188#issuecomment-5557418229) for full details.

**Key findings:**
- **Threaded throughput confirmed.** no-GIL reaches 2.57x one→four-thread scaling (64.2% efficiency) and is 2.63x faster than the GIL arm at four threads.
- **Single-thread cost confirmed.** no-GIL is 12.6% slower at the single-thread median; however, the ranges overlap (GIL max 3.3807 vs no-GIL min 2.7822).

**Cross-library comparison:** The benchmark measures cachekit operations only. Cross-library throughput (orjson, numpy, pandas, pyarrow) was not run. It waits on orjson, which does not publish free-threaded (`cp314t`) wheels as of 2026-09-29; cross-stack performance will be measured once it does.

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

numpy, pandas and pyarrow (the `[data]` extra) now publish `cp314t` wheels,
but the free-threaded CI lane does not install `[data]` yet, so `[data]` on
3.14t is untested.

When those clear: add `-i python3.14t` targets to the `build-wheels` matrix in
`.github/workflows/release-please.yml`, revisit `redis[hiredis]` (marker or
documented degradation), and update this page plus the README support
statement. Track upstream — do not fork or vendor (LAB-511 non-goal).
