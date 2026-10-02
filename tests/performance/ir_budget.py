"""Per-op instruction budgets for the cachekit hot paths (``make perf-ir``).

Wall-clock benchmarks on a shared host cannot gate a 1% regression: run-to-run noise is several
percent. Instruction counts can, with two caveats this harness exists to handle:

- ``import cachekit`` starts background threads (the log writer, the L1 cleanup worker) whose
  counts swing by tens of percent between identical runs. Only the MAIN thread is counted
  (callgrind ``--separate-threads=yes``, file ``-01``).
- Interpreter startup is ~2 billion instructions. Each path runs at two loop sizes and the
  per-op cost is the difference, ``(Ir[N_HI] - Ir[N_LO]) / (N_HI - N_LO)``, so startup and
  warmup cancel.
- Code that observes its own wall-clock duration executes more instructions when it runs
  slower, so the measured process pins its main-thread clocks (see ``_pin_main_thread_clocks``).

With all three, repeat runs agree within 0.03% per op (A/A; the Arrow round trip 0.2%), against a 1% fail threshold.

Budgets are keyed by interpreter (minor version, build flavour, machine): the same code costs a
different number of instructions on 3.12 and 3.14. Instruction counts ignore cache misses and
branch mispredictions, so a claimed wall-clock win still needs an interleaved wall-clock run.

Run ``python tests/performance/ir_budget.py --help`` for the gate and ``--update`` (ratchet-down)
options. Requires valgrind; never imported by the test suite except for its pure helpers.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
import sysconfig
import tempfile
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

N_LO, N_HI = 1000, 3000
FAIL_PCT = 1.0  # regression at or above this fails the gate
WARN_PCT = 0.2  # A/A floor is ~0.13%; above this, report but pass
BASELINES = Path(__file__).with_name("ir_baselines.json")
PATHS = (
    "l1_hit",
    "minimal_l1_hit",
    "l2_hit",
    "miss",
    "secure_l1_hit",
    "serializer_default",
    "serializer_auto",
    "serializer_orjson",
    "serializer_arrow",
    "serializer_encrypted",
)

# ── measured process ────────────────────────────────────────────────────────────────────────


def _build_workloads() -> dict[str, Callable[[], object]]:
    """Every path as a zero-arg callable. Imports happen here, so importing this module stays cheap."""
    import pandas as pd

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

    decorated = {
        "l1_hit": cache(backend=None, ttl=300)(body),
        "minimal_l1_hit": cache.minimal(backend=None, ttl=300)(body),
        "l2_hit": cache(backend=DictBackend(), l1_enabled=False, ttl=300)(body),
        "miss": cache(backend=MissBackend(), l1_enabled=False, ttl=300)(body),
        "secure_l1_hit": cache.secure(master_key=master_key, backend=DictBackend(), ttl=300)(body),
    }
    workloads: dict[str, Callable[[], object]] = {name: (lambda fn=fn: fn(42, "user-profile")) for name, fn in decorated.items()}

    def roundtrip(serializer: Any, obj: object, **key: str) -> Callable[[], object]:
        def op() -> object:
            data, meta = serializer.serialize(obj, **key)
            return serializer.deserialize(data, meta, **key)

        return op

    for name in ("default", "auto", "orjson"):
        workloads[f"serializer_{name}"] = roundtrip(get_serializer(name), value)
    workloads["serializer_arrow"] = roundtrip(get_serializer("arrow"), frame)
    encrypted = EncryptionWrapper(master_key=bytes.fromhex(master_key), previous_master_keys=[])
    workloads["serializer_encrypted"] = roundtrip(encrypted, value, cache_key="ns:bench:func:m.f:args:" + "0" * 64 + ":0")
    return workloads


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
    _pin_main_thread_clocks()
    op = _build_workloads()[path]
    for _ in range(50):  # identical warmup at both N: first-call costs (L1 fill, lazy imports) cancel
        op()
    for _ in range(n):
        op()


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
    which moved a small path by up to 0.4% per op. It also keeps CACHEKIT_* settings out, bar one:
    the L1 cleanup thread reads the real clock against expiries the pinned main thread wrote, so
    each sweep evicts the ``secure_l1_hit`` entry. A one-day interval keeps sweeps out of every run.
    """
    env = {
        "PATH": "/usr/bin:/bin",
        "LC_ALL": "C.UTF-8",
        "PYTHONHASHSEED": "0",
        "PYTHONNOUSERSITE": "1",
        "OPENBLAS_NUM_THREADS": "1",
        "OMP_NUM_THREADS": "1",  # pyarrow converts on a thread pool otherwise: +-0.5% per op
        "CACHEKIT_L1_CLEANUP_INTERVAL_SECONDS": "86400",
    }
    for path in paths:
        _run([], path, 1, env)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    with tempfile.TemporaryDirectory(prefix="cachekit-ir-") as tmp, ThreadPoolExecutor(jobs) as pool:
        futures = {(p, n): pool.submit(_measure_one, p, n, Path(tmp), env) for p in paths for n in (N_LO, N_HI)}
        return {p: per_op(futures[(p, N_LO)].result(), futures[(p, N_HI)].result()) for p in paths}


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
            verdict = "LOWER"  # a real improvement: ratchet the budget down with --update
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
    parser.add_argument("--jobs", type=int, default=min(len(PATHS) * 2, os.cpu_count() or 1))
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
