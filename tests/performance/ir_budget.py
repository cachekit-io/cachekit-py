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
  slower, so the measured process pins its main-thread clocks (``ir_workload.pin_main_thread_clocks``).
  Pinned clocks never let the metrics collector's 5 s mode check fire, so the collector stays
  synchronous; ``l2_hit_async_metrics`` measures the batched mode a busy long-lived process
  switches to (``ir_workload.build_workload``).
- Nothing else that runs on real time reaches the measured loop (no background thread wakes,
  cyclic GC is off, the GIL switch interval is long), the environment is fixed, the bytecode
  cache is warmed first, and the heap layout is sampled (``ir_workload.run_workload``, ``measure``
  and ``LAYOUTS``).

Two full runs agree within 0.08% per op (0.01% on 3.12), against a 1% fail threshold.

The measured process is ``ir_workload.py``. Its text is compiled into every measured run, so
editing it moves the heap layout the budgets were recorded at; this gate is kept out of that
process, so editing the gate does not.

Budgets are keyed by interpreter (minor version, build flavour, machine): the same code costs a
different number of instructions on 3.12 and 3.14. Instruction counts ignore cache misses and
branch mispredictions, so a claimed wall-clock win still needs an interleaved wall-clock run.

Run ``python tests/performance/ir_budget.py --help`` for the gate and ``--update`` (ratchet-down)
options. Measuring requires valgrind; the unit tests import this module's helpers without it.

Callgrind runs need a bound. Each path here peaks at 0.5 to 1.0 GiB per run, but a workload that
grows under callgrind can use many GiB: a FileBackend ``set()`` workload passed 9 GiB in under four
minutes. Each run is killed after ``--child-timeout`` minutes, and every live run is killed when the
gate exits, fails, or gets SIGINT or SIGTERM. On Linux with systemd, also cap the whole gate's
memory and run time from outside::

    systemd-run --user --scope -p MemoryMax=10G -p MemorySwapMax=0 -p RuntimeMaxSec=90min -- \
        uv run python tests/performance/ir_budget.py

The gate does not call ``systemd-run`` itself: CI runners and macOS lack it.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import signal
import statistics
import subprocess
import sys
import sysconfig
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

N_LO, N_HI = 1000, 3000
# Heap layout moves per-op counts: the allocators take shorter or longer paths in some heap
# states. Any code change shifts the layout, even an edit to ir_workload.py's comments, so a single
# run can fail an untouched path or hide a real regression. Each path runs at these layout shifts
# (extra objects held) and its figure is the median, which one or two odd layouts cannot move.
# The cheapest layout is not used: a lucky one sits up to 1% below the rest, and a budget recorded
# there makes every later run look like a regression. docs/performance.md has the measured ranges.
LAYOUTS = (0, 48, 80, 336, 880)
FAIL_PCT = 1.0  # regression at or above this fails the gate
WARN_PCT = 0.2  # above the A/A floor: report but pass
# The exception: the orjson round trip's 1 KB output buffer goes to glibc malloc, whose path length
# depends on heap state that no layout sample pins (longer warmups did not settle it). Its figure
# moves by more than the 1% threshold between unrelated changes, so it is gated at what this
# harness resolves for it.
TOLERANCE_PCT = {"serializer_orjson": (2.0, 1.0)}  # path: (fail, warn)
# Callgrind runs at a time by default. Each path peaks at 0.5 to 1.0 GiB per run (the L2, miss and
# secure paths at the top), so the default stays small enough to share the machine with other work
# rather than growing with its core count; --jobs overrides it. A workload that grows under callgrind
# can hold many GiB per run, so each run also has a time limit and the module docstring gives a
# memory cap to run the gate under.
JOBS = min(8, os.cpu_count() or 1)
CHILD_TIMEOUT_MIN = 15.0  # wall-clock limit per callgrind run; --child-timeout overrides it
BASELINES = Path(__file__).with_name("ir_baselines.json")
WORKLOAD = Path(__file__).with_name("ir_workload.py")  # the measured process
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


# Live child processes, so a failing or interrupted gate can kill every one of them. Once _stop is set,
# a run that starts is killed at once: a pool thread can start one after the others were killed.
_children: set[subprocess.Popen[str]] = set()
_children_lock = threading.Lock()
_stop = threading.Event()


def _kill_children() -> None:
    with _children_lock:
        _stop.set()
        for proc in _children:
            proc.kill()


def _exit_on_sigterm(signum: int, _frame: object) -> None:
    raise SystemExit(128 + signum)  # unwinds measure(), which kills the live runs


def _run(prefix: list[str], path: str, n: int, env: dict[str, str], timeout_s: float) -> None:
    cmd = [*prefix, sys.executable, str(WORKLOAD), path, str(n)]
    run = f"{path} n={n} layout={env.get('IR_BUDGET_LAYOUT', '-')}"
    proc = subprocess.Popen(cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)  # noqa: S603 (trusted: valgrind + this file)
    with _children_lock:
        _children.add(proc)
        if _stop.is_set():
            proc.kill()
    try:
        _, stderr = proc.communicate(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"{run} ran past --child-timeout ({timeout_s / 60:g} min) and was killed") from None
    finally:
        if proc.returncode is None:  # timed out or interrupted: never leave it running
            proc.kill()
            proc.communicate()
        with _children_lock:
            _children.discard(proc)
    if proc.returncode != 0:
        raise RuntimeError(f"{run} exited {proc.returncode}:\n{stderr[-2000:]}")


def _measure_one(path: str, n: int, workdir: Path, env: dict[str, str], timeout_s: float) -> int:
    workdir.mkdir(exist_ok=True)
    out = workdir / f"{path}.{n}.cg"
    valgrind = shutil.which("valgrind") or "valgrind"
    _run([valgrind, "--tool=callgrind", "--separate-threads=yes", f"--callgrind-out-file={out}"], path, n, env, timeout_s)
    return main_thread_ir(out)


def measure(paths: list[str], jobs: int, child_timeout_s: float = CHILD_TIMEOUT_MIN * 60) -> dict[str, int]:
    """Per-op main-thread Ir for each path. Runs are independent processes, so they parallelise.

    A first import compiles and writes ``.pyc`` files, and concurrent runs race to do it, which
    put hundreds of thousands of Ir/op of noise into a fresh venv. So every path first runs once
    natively, serially, to fill the bytecode cache, and the measured runs never write bytecode.

    The process environment is fixed, not inherited: its size moves the stack and heap layout,
    which moved a small path by up to 0.4% per op. No shell setting (CACHEKIT_* included) reaches
    the measured process; the two CACHEKIT_* settings below keep cachekit's background threads asleep.

    Each run is killed after ``child_timeout_s``. A failed run, SIGINT or SIGTERM kills every live run
    before the error propagates, so no callgrind process outlives this call. Call it from the main
    thread: it handles SIGTERM while it runs.
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
    _stop.clear()
    previous = signal.signal(signal.SIGTERM, _exit_on_sigterm)
    try:
        for path in paths:
            _run([], path, 1, env, child_timeout_s)
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        runs = [(p, n, shift) for p in paths for shift in LAYOUTS for n in (N_LO, N_HI)]
        with tempfile.TemporaryDirectory(prefix="cachekit-ir-") as tmp, ThreadPoolExecutor(jobs) as pool:
            futures = {
                (p, n, shift): pool.submit(
                    _measure_one, p, n, Path(tmp) / str(shift), env | {"IR_BUDGET_LAYOUT": str(shift)}, child_timeout_s
                )
                for p, n, shift in runs
            }
            try:
                ir = {key: future.result() for key, future in futures.items()}
            except BaseException:
                pool.shutdown(wait=False, cancel_futures=True)
                _kill_children()
                raise
    finally:
        signal.signal(signal.SIGTERM, previous)
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
        fail_pct, warn_pct = TOLERANCE_PCT.get(path, (FAIL_PCT, WARN_PCT))
        if pct >= fail_pct:
            verdict, ok = "FAIL", False
        elif pct >= warn_pct:
            verdict = "WARN"
        elif pct <= -fail_pct:
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
    parser.add_argument(
        "--jobs",
        type=int,
        default=JOBS,
        help=f"callgrind runs at a time (default {JOBS}); each path peaks at 0.5-1.0 GiB per run, but a FileBackend set() "
        "workload passed 9 GiB in four minutes, so cap memory from outside (see the module docstring)",
    )
    parser.add_argument(
        "--child-timeout",
        type=float,
        default=CHILD_TIMEOUT_MIN,
        metavar="MIN",
        help=f"kill a callgrind run after this many minutes and fail the gate (default {CHILD_TIMEOUT_MIN:g})",
    )
    args = parser.parse_args()

    if shutil.which("valgrind") is None:
        print("valgrind not found: install it (apt install valgrind) to run the instruction budget", file=sys.stderr)
        return 2
    key = interpreter_key()
    data = json.loads(BASELINES.read_text())
    entry = data["interpreters"].get(key, {"budgets": {}})
    if entry.get("python") not in (None, platform.python_version()):
        print(f"note: budgets for {key} were recorded on {entry['python']}, this is {platform.python_version()}")

    measured = measure(args.path or list(PATHS), args.jobs, args.child_timeout * 60)
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
    sys.exit(_main())
