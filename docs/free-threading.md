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
the ticket, plus what the free-threaded CI lane surfaced:

| Site | Mechanism | Verdict |
|:-----|:----------|:--------|
| `decorators/session.py` `_ensure_session_initialized` | Lock-free fast path over three module globals | **Fixed** — fast path and double-check gate on all three fields; regression tests in `tests/unit/test_saas_observability.py::TestMidPublishMemoryOrdering` and `tests/unit/test_free_threading.py` |
| `reliability/metrics_collection.py` `AsyncMetricsCollector.flush` | Polled `Queue.empty()` | **Fixed** — `empty()` flips when the worker *dequeues*, not when it finishes processing; flush now waits on its own pending-work condition, signaled after `task_done()`. Was a routine flake on the free-threaded lane, invisible under the GIL's coarse scheduling |
| `decorators/wrapper.py` `_FunctionStats` | `RLock` around every counter mutation and `get_info` | Safe. `l1_enabled` is a plain attribute re-set on re-decoration (under the registry lock) and read without the stats lock; a stale read yields a conservative rate-limit classification header, never corruption |
| `decorators/wrapper.py` `_function_stats_registry` | Module-level `Lock` around all access | Safe |
| `os.register_at_fork` handlers (session + stats registry) | Run in the child while single-threaded; replace locks wholesale | Safe — single-threaded by construction at execution time |
| `backends/cachekitio/session.py` header cache | `threading.local` | Safe — per-thread state; its only cross-thread hazard was the session-identity publication above |
| `decorators/stats_context.py` | `contextvars.ContextVar` | Safe by construction |
| `decorators/wrapper.py` L1/SWR + L2/SWR single-flight (`_l1_swr_*`, `_l2_swr_*`) | `BoundedSemaphore` slots + in-flight `set` + PID-owner swap | Safe. The check-then-add on the in-flight set was already documented as benign (worst case one duplicate refresh, absorbed by last-write-wins / the backend lease); builtin `set`/`dict` single ops are atomic under free-threading's per-object locking. The fork-detection wholesale swap races are the same documented-benign shape |
| `decorators/wrapper.py` `_cached_keys` | Builtin `set`, snapshot-copied before iteration in invalidation | Safe — single ops atomic; a copy racing an add can only miss a concurrently-written key, which invalidate-all semantics tolerate |
| `object_cache.py` `ObjectCache` | `RLock` on every public method | Safe |
| `reliability/metrics_collection.py` `get_async_metrics_collector` | Double-checked module-global singleton | Safe — single-assignment publication of a fully-constructed object; worst case a benign duplicate worker-restart check |
| Rust extension (`rust/src/`, cachekit-core 0.5.0) | `#[pymodule(gil_used = false)]`; every `#[pyclass]` exposes only `&self` methods; nonce counter is `AtomicU64`, metrics behind `Mutex`; PyO3 enforces `Send + Sync` on pyclasses at compile time | Safe — declared free-threading-ready. (The wasm32 `Cell` nonce variant is single-threaded by target.) |

## The CI safety net

`.github/workflows/ci.yml` job `test-freethreaded`:

1. Installs with `uv sync --python 3.14t --no-default-groups --group test
   --no-install-package hiredis`. The `test` dependency group is the core
   test toolchain; the extras that lack free-threaded wheels (orjson, numpy,
   pandas, pyarrow) live only in the `dev` group and the `[data]`/`[json]`
   extras, and their tests skip via `pytest.importorskip`.
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

## Deferred: declared support + free-threaded wheels

Publishing `cp314t` wheels and declaring official free-threaded support is
**explicitly deferred** (per the LAB-511 acceptance criteria) until the
dependency chain allows it. Blocking as of 2026-08:

- **orjson** — no free-threaded wheels through 3.12.0, and its build script
  rejects free-threaded interpreters ("does not support free-threaded
  Python"). Optional `[json]` extra, but a support declaration that breaks
  the moment a user adds `cachekit[json]` is not a declaration worth making.
- **hiredis** — no `Py_mod_gil` declaration; importing it re-enables the GIL.
  Pulled in unconditionally via the required `redis[hiredis]` dependency.
- **numpy / pandas / pyarrow** — the `[data]` extra; free-threaded wheel
  coverage across all three is not yet complete enough to declare.

When those clear: add `-i python3.14t` targets to the `build-wheels` matrix in
`.github/workflows/release-please.yml`, revisit `redis[hiredis]` (marker or
documented degradation), and update this page plus the README support
statement. Track upstream — do not fork or vendor (LAB-511 non-goal).
