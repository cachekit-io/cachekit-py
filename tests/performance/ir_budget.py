"""Per-op instruction budgets for the cachekit hot paths (``make perf-ir``).

Wall-clock benchmarks on a shared host cannot gate a 1% regression: run-to-run noise is several
percent. Instruction counts can, once these sources of run-to-run noise are handled:

- ``import cachekit`` starts background threads (the log writer, the L1 cleanup worker) whose
  counts swing by tens of percent between identical runs. Only the MAIN thread is counted
  (callgrind ``--separate-threads=yes``, file ``-01``).
- Interpreter startup is ~2 billion instructions. Each path runs at two loop sizes and the
  per-op cost is the difference, ``(Ir[N_HI] - Ir[N_LO]) / (N_HI - N_LO)``, so startup and
  warmup cancel.
- Code that observes its own wall-clock duration executes more instructions when it runs
  slower, so the measured process pins its main-thread clocks (see ``_pin_main_thread_clocks``).
  Pinned clocks never let the metrics collector's 5 s mode check fire, so the collector stays
  synchronous; ``l2_hit_async_metrics`` measures the batched mode a busy long-lived process
  switches to (see ``_build_workload``).
- Nothing else that runs on real time reaches the measured loop (no background thread wakes,
  cyclic GC is off, the GIL switch interval is long), the environment is fixed, the bytecode
  cache is warmed first, and the heap layout is sampled (see ``_run_workload``, ``measure`` and
  ``LAYOUTS``).

Two full runs agree within 0.03% per op (0.01% on 3.12), against a 1% fail threshold.

Budgets are keyed by interpreter (minor version, build flavour, machine): the same code costs a
different number of instructions on 3.12 and 3.14. Instruction counts ignore cache misses and
branch mispredictions, so a claimed wall-clock win still needs an interleaved wall-clock run.

Run ``python tests/performance/ir_budget.py --help`` for the gate and ``--update`` (ratchet-down)
options. Measuring requires valgrind; the unit tests import this module's helpers without it.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import statistics
import subprocess
import sys
import sysconfig
import tempfile
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

N_LO, N_HI = 1000, 3000
# Heap layout moves per-op counts: holding 16-1,400 extra objects before the workload moved the
# orjson round trip between 23,404 and 23,827 Ir/op (1.8%), the minimal L1 hit by 0.5%, the L2
# hit by 0.2%. Any code change shifts the layout the same way, so a single run can fail an
# untouched path or hide a real regression. Each path is measured at these layout shifts (extra
# objects held) and the median is its figure.
LAYOUTS = (0, 48, 80, 336, 880)
FAIL_PCT = 1.0  # regression at or above this fails the gate
WARN_PCT = 0.2  # above the A/A floor (0.03%): report but pass
BASELINES = Path(__file__).with_name("ir_baselines.json")
PATHS = (
    "l1_hit",
    "minimal_l1_hit",
    "l2_hit",
    "miss",
    "secure_l1_hit",
    "l2_hit_async_metrics",
    "serializer_default",
    "serializer_auto",
    "serializer_orjson",
    "serializer_arrow",
    "serializer_encrypted",
)

# ── measured process ────────────────────────────────────────────────────────────────────────


def _build_workload(path: str) -> Callable[[], object]:
    """One path as a zero-arg callable. Only that path is built, so no other path's threads run.

    Imports happen here, so importing this module stays cheap.
    """
    import pandas as pd

    import cachekit.decorators.orchestrator as orchestrator
    from cachekit import cache
    from cachekit.serializers import EncryptionWrapper, get_serializer

    value = {"id": 42, "name": "Ada Lovelace", "roles": ["admin", "ops"], "score": 97.5, "tags": list(range(16))}
    frame = pd.DataFrame({"id": range(100), "score": [i * 0.5 for i in range(100)]})
    master_key = "ab" * 32

    class DictBackend:
        """In-process L2 so the L2 path costs instructions only: no sockets, retries or timeouts."""

        def __init__(self) -> None:
            self.store: dict[str, bytes] = {}

        def get(self, key: str) -> bytes | None:
            return self.store.get(key)

        def set(self, key: str, value: bytes, ttl: int | None = None) -> None:
            self.store[key] = value

        def delete(self, key: str) -> bool:
            return self.store.pop(key, None) is not None

        def exists(self, key: str) -> bool:
            return key in self.store

        def health_check(self) -> tuple[bool, dict[str, Any]]:
            return True, {}

    class MissBackend(DictBackend):
        """Never holds a value, so every call takes the full miss path: get, compute, serialize, set."""

        def set(self, key: str, value: bytes, ttl: int | None = None) -> None:
            pass

    def body(uid: int, kind: str) -> dict:
        return value

    def batched_metrics(decorate: Callable[[], Callable[..., dict]]) -> Callable[..., dict]:
        """Decorate with the metrics collector in batched mode, where a busy long-lived process runs.

        A collector starts synchronous. At a mode check (every 5 s) that sees more than 100
        records/s it switches to batched mode: each call puts its record on a queue and a worker
        thread updates Prometheus. Pinned clocks never let 5 s pass, so this variant starts the
        collector batched. It then stops the worker, which drains the queue on real time and would
        otherwise change the main thread's count with host load. The budget covers the caller's
        side, putting the record on the queue; the worker's Prometheus update is not budgeted.
        """
        made: list[Any] = []

        class Batched(orchestrator.AsyncMetricsCollector):
            def __init__(self, **kwargs: Any) -> None:
                super().__init__(sync_mode=False, **kwargs)
                made.append(self)

        real, orchestrator.AsyncMetricsCollector = orchestrator.AsyncMetricsCollector, Batched
        try:
            fn = decorate()
        finally:
            orchestrator.AsyncMetricsCollector = real
        (collector,) = made
        collector._stopped.set()
        collector._worker_thread.join()
        queued = collector._queue.qsize()
        fn(42, "user-profile")
        if collector._sync_mode or collector._queue.qsize() <= queued:
            raise RuntimeError("l2_hit_async_metrics: the call did not queue its metric, so batched mode is not measured")

        def call(*args: Any) -> dict:
            result = fn(*args)
            # Do what the worker does with a record: take it off the queue and return its dict to
            # the pool. Queue and pool then hold one item each, as a keeping-up worker leaves them,
            # so every call takes the pool-hit branch a busy process takes and the heap stops
            # growing (a growing queue tripled the layout spread). Under 1.6k Ir of the figure.
            collector._metric_pool.append(collector._queue.queue.popleft())
            return result

        return call

    l2 = lambda: cache(backend=DictBackend(), l1_enabled=False, ttl=300)(body)  # noqa: E731
    # Every metrics path records through the same AsyncMetricsCollector.record_cache_operation, once
    # per call, so one batched-mode variant covers that mode; the L1-only hits record no metric.
    decorated: dict[str, Callable[[], Callable[..., dict]]] = {
        "l1_hit": lambda: cache(backend=None, ttl=300)(body),
        "minimal_l1_hit": lambda: cache.minimal(backend=None, ttl=300)(body),
        "l2_hit": l2,
        "miss": lambda: cache(backend=MissBackend(), l1_enabled=False, ttl=300)(body),
        "secure_l1_hit": lambda: cache.secure(master_key=master_key, backend=DictBackend(), ttl=300)(body),
        "l2_hit_async_metrics": lambda: batched_metrics(l2),
    }
    if path in decorated:
        fn = decorated[path]()
        return lambda: fn(42, "user-profile")

    def roundtrip(serializer: Any, obj: object, **key: str) -> Callable[[], object]:
        def op() -> object:
            data, meta = serializer.serialize(obj, **key)
            return serializer.deserialize(data, meta, **key)

        return op

    if path == "serializer_arrow":
        return roundtrip(get_serializer("arrow"), frame)
    if path == "serializer_encrypted":
        encrypted = EncryptionWrapper(master_key=bytes.fromhex(master_key), previous_master_keys=[])
        return roundtrip(encrypted, value, cache_key="ns:bench:func:m.f:args:" + "0" * 64 + ":0")
    return roundtrip(get_serializer(path.removeprefix("serializer_")), value)


def _pin_main_thread_clocks() -> None:
    """Make wall-clock-dependent work cost the same instructions in every run.

    The miss and L2 paths observe their real duration into Prometheus histograms, and
    ``Histogram.observe`` walks the bucket list until the value fits, so a slower run (valgrind
    on a loaded host) executes more instructions: +-7% per op on the L2 hit before this. Here
    every main-thread clock read advances one shared counter by 1 us, so durations depend only
    on how many reads the code makes. Other threads keep the real clocks. Log sampling draws
    from ``random``, so it is seeded too. Must run before ``import cachekit``: modules that do
    ``from time import monotonic`` bind at import.
    """
    import itertools
    import random
    import threading
    import time

    random.seed(0)
    main, ticks = threading.get_ident(), itertools.count()

    def pinned(real: Callable[[], Any], origin: float, step: float) -> Callable[[], Any]:
        def read() -> Any:
            return origin + next(ticks) * step if threading.get_ident() == main else real()

        return read

    for name, origin in (("time", 1.7e9), ("monotonic", 1e5), ("perf_counter", 1e5)):
        setattr(time, name, pinned(getattr(time, name), origin, 1e-6))
        setattr(time, f"{name}_ns", pinned(getattr(time, f"{name}_ns"), int(origin * 1e9), 1000))


def _run_workload(path: str, n: int) -> None:
    import gc

    # Background threads' allocations count toward the main thread's GC trigger, so when a
    # collection lands varies run to run (0.3% per op on the orjson round trip), and a full
    # collection landing in one loop size but not the other would swamp the difference. Off from
    # the start: allocation and refcount cost stay in the budget, cyclic-GC cost is outside it.
    gc.disable()
    # A thread waiting on the GIL makes the main thread drop it after every switch interval of
    # REAL time, so a slower (more loaded) run pays for more handoffs. With a long interval the
    # main thread gives up the GIL only where the code releases it itself.
    sys.setswitchinterval(1e6)
    _pin_main_thread_clocks()
    _shift = [object() for _ in range(int(os.environ.get("IR_BUDGET_LAYOUT", "0")))]  # noqa: F841 (held: see LAYOUTS)
    op = _build_workload(path)
    for _ in range(50):  # identical warmup at both N: first-call costs (L1 fill, lazy imports) cancel
        op()
    for _ in range(n):
        op()
    # Skip interpreter teardown: it is not part of a call, and anything it frees that grew with n
    # would leak into the per-op difference.
    os._exit(0)


# ── gate ────────────────────────────────────────────────────────────────────────────────────


def interpreter_key() -> str:
    """Budget key: implementation, minor version, free-threaded flavour, machine."""
    ft = "t" if sysconfig.get_config_var("Py_GIL_DISABLED") else ""
    return f"{sys.implementation.name}-{sys.version_info.major}.{sys.version_info.minor}{ft}-{platform.machine()}"


def main_thread_ir(out_file: Path) -> int:
    """Total Ir of thread 1 from a ``--separate-threads=yes`` callgrind run."""
    thread_one = Path(f"{out_file}-01")
    try:
        text = thread_one.read_text()
    except FileNotFoundError:
        written = sorted(p.name for p in out_file.parent.glob(f"{out_file.name}*")) or "nothing"
        raise RuntimeError(f"no main-thread file {thread_one.name}: callgrind wrote {written}") from None
    for line in text.splitlines():
        if line.startswith("totals:"):
            return int(line.split()[1])
    raise RuntimeError(f"no totals line in {out_file}-01")


def per_op(ir_lo: int, ir_hi: int) -> int:
    return round((ir_hi - ir_lo) / (N_HI - N_LO))


def _run(prefix: list[str], path: str, n: int, env: dict[str, str]) -> None:
    cmd = [*prefix, sys.executable, __file__, "--_workload", path, str(n)]
    proc = subprocess.run(cmd, env=env, capture_output=True, text=True, check=False)  # noqa: S603 (trusted: valgrind + this file)
    if proc.returncode != 0:
        raise RuntimeError(f"{path} n={n} exited {proc.returncode}:\n{proc.stderr[-2000:]}")


def _measure_one(path: str, n: int, workdir: Path, env: dict[str, str]) -> int:
    workdir.mkdir(exist_ok=True)
    out = workdir / f"{path}.{n}.cg"
    valgrind = shutil.which("valgrind") or "valgrind"
    _run([valgrind, "--tool=callgrind", "--separate-threads=yes", f"--callgrind-out-file={out}"], path, n, env)
    return main_thread_ir(out)


def measure(paths: list[str], jobs: int) -> dict[str, int]:
    """Per-op main-thread Ir for each path. Runs are independent processes, so they parallelise.

    A first import compiles and writes ``.pyc`` files, and concurrent runs race to do it, which
    put hundreds of thousands of Ir/op of noise into a fresh venv. So every path first runs once
    natively, serially, to fill the bytecode cache, and the measured runs never write bytecode.

    The process environment is fixed, not inherited: its size moves the stack and heap layout,
    which moved a small path by up to 0.4% per op. No shell setting (CACHEKIT_* included) reaches
    the measured process; the two CACHEKIT_* settings below keep cachekit's background threads asleep.
    """
    env = {
        "PATH": "/usr/bin:/bin",
        "LC_ALL": "C.UTF-8",
        "PYTHONHASHSEED": "0",
        "PYTHONNOUSERSITE": "1",
        "OPENBLAS_NUM_THREADS": "1",
        "OMP_NUM_THREADS": "1",  # pyarrow converts on a thread pool otherwise: +-0.5% per op
        # cachekit's log writer wakes every 1 s and its L1 sweeper every 30 s of real time. A woken
        # thread takes the GIL at a load-dependent moment, and the sweeper compares expiry times
        # stamped by the pinned main-thread clock against the real one, so it evicts live entries
        # mid-run. A day-long interval keeps both asleep for the whole run.
        "CACHEKIT_LOG_FLUSH_INTERVAL": "86400",
        "CACHEKIT_L1_CLEANUP_INTERVAL_SECONDS": "86400",
    }
    for path in paths:
        _run([], path, 1, env)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    runs = [(p, n, shift) for p in paths for shift in LAYOUTS for n in (N_LO, N_HI)]
    with tempfile.TemporaryDirectory(prefix="cachekit-ir-") as tmp, ThreadPoolExecutor(jobs) as pool:
        futures = {
            (p, n, shift): pool.submit(_measure_one, p, n, Path(tmp) / str(shift), env | {"IR_BUDGET_LAYOUT": str(shift)})
            for p, n, shift in runs
        }
        ir = {key: future.result() for key, future in futures.items()}
    return {p: round(statistics.median(per_op(ir[(p, N_LO, s)], ir[(p, N_HI, s)]) for s in LAYOUTS)) for p in paths}


def compare(budgets: dict[str, int], measured: dict[str, int]) -> tuple[list[str], bool]:
    """Report lines and whether the gate passes. A path without a budget fails: record it first."""
    lines, ok = [], True
    for path, ir in measured.items():
        budget = budgets.get(path)
        if budget is None:
            lines.append(f"FAIL  {path:22} {ir:>9,} Ir/op  no budget for this interpreter (run --update)")
            ok = False
            continue
        pct = (ir - budget) / budget * 100
        if pct >= FAIL_PCT:
            verdict, ok = "FAIL", False
        elif pct >= WARN_PCT:
            verdict = "WARN"
        elif pct <= -FAIL_PCT:
            verdict = "LOWER"  # cheaper: ratchet it down with --update if the change touched this path
        else:
            verdict = "ok"
        lines.append(f"{verdict:5} {path:22} {ir:>9,} Ir/op  budget {budget:>9,}  {pct:+.2f}%")
    return lines, ok


def ratchet(budgets: dict[str, int], measured: dict[str, int], allow_increase: bool) -> dict[str, int]:
    """New budgets: only ever lower, unless the increase is deliberate."""
    out = dict(budgets)
    for path, ir in measured.items():
        old = out.get(path)
        out[path] = ir if old is None or allow_increase else min(old, ir)
    return out


def _main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--path", action="append", choices=PATHS, help="measure only this path (repeatable)")
    parser.add_argument("--update", action="store_true", help="write lower measured figures back as budgets")
    parser.add_argument("--allow-increase", action="store_true", help="with --update, also raise budgets")
    parser.add_argument("--jobs", type=int, default=os.cpu_count() or 1)
    args = parser.parse_args()

    if shutil.which("valgrind") is None:
        print("valgrind not found: install it (apt install valgrind) to run the instruction budget", file=sys.stderr)
        return 2
    key = interpreter_key()
    data = json.loads(BASELINES.read_text())
    entry = data["interpreters"].get(key, {"budgets": {}})
    if entry.get("python") not in (None, platform.python_version()):
        print(f"note: budgets for {key} were recorded on {entry['python']}, this is {platform.python_version()}")

    measured = measure(args.path or list(PATHS), args.jobs)
    lines, ok = compare(entry["budgets"], measured)
    print(f"main-thread Ir/op on {key} ({platform.python_version()}), (Ir[{N_HI}] - Ir[{N_LO}]) / {N_HI - N_LO}")
    print("\n".join(lines))

    if args.update:
        entry = {"python": platform.python_version(), "budgets": ratchet(entry["budgets"], measured, args.allow_increase)}
        data["interpreters"][key] = entry
        BASELINES.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
        print(f"budgets written to {BASELINES.name}")
        return 0
    print("PASS" if ok else f"FAIL: a path regressed by {FAIL_PCT}% or more, or has no budget")
    return 0 if ok else 1


if __name__ == "__main__":
    if len(sys.argv) == 4 and sys.argv[1] == "--_workload":
        _run_workload(sys.argv[2], int(sys.argv[3]))
    else:
        sys.exit(_main())
