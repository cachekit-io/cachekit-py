"""Free-threaded thread scaling of decorated cachekit calls, GIL vs no-GIL, interleaved arms.

``gil_benchmark.py`` times ``StandardSerializer.serialize`` alone. This bench times whole decorated
calls, so it sees the shared state a real call touches: the L1 lock, the per-function stats, the
metrics path, the Rust ByteStorage and the backend client.

Arms. Each process runs one arm once; every rep runs every arm, in a seeded shuffled order.
  ft-nogil     the free-threaded binary with PYTHON_GIL=0
  ft-nogil-aa  the same arm again: an A/A pair, whose difference is this session's noise floor
  ft-gil       the same binary with PYTHON_GIL=1: the clean GIL vs no-GIL comparison
  ft-default   the same binary with PYTHON_GIL unset: the GIL state the imports leave behind
               (with hiredis installed it is back on; without hiredis it matches ft-nogil)
  gil-build    a default GIL build (--gil-python): context only, since it is a different binary

Cells, each at 1, 4 and 16 threads:
  l1hit        @cache over an in-process dict backend, 64 hot keys, every call an L1 hit
  l2stub       the same with L1 off: every call is an L2 hit through cachekit's L2 path, no transport
  cachekitio   L1 off, the shipped CachekitIO backend and client against a loopback TLS fake
               (loopback_saas.py). It measures client-side contention only, never SaaS latency.
               The fake reports its busiest worker's CPU share per cell; above 80% the cell is
               flagged SERVER-BOUND, because then the fake, not the client, sets the ceiling.

Every thread starts at a barrier, warms up, then counts calls for a fixed time. Counts are summed
after the threads join, so no lock sits in the timed loop. The primary metric is scaling: calls/s at
N threads over calls/s at 1 thread, within one process. The summary prints the median and min-max
over reps and each arm's difference from ft-nogil with a 95% bootstrap CI. It drops any process whose
GIL state changed while the cells ran, and flags a cell TAINTED when a timed call was not a hit (a
backend error falls back to computing). A difference counts only if its CI excludes 0 and it is
larger than the ft-nogil-aa difference.

The free-threaded interpreter should come from an environment without the [data] and [json] extras,
for example ``uv sync --python 3.14t --no-default-groups --group test``: some of their builds
re-enable the GIL on import. Needs ``openssl`` on PATH for the loopback certificate.

Run:
  python tests/performance/ft_scaling_bench.py --ft-python <venv-3.14t>/bin/python \\
      --gil-python <venv-3.14>/bin/python --reps 7 --cpus 0-15 --server-cpus 16-23 --out ft-scaling.jsonl
  python tests/performance/ft_scaling_bench.py --summarise ft-scaling.jsonl
"""

from __future__ import annotations

import argparse
import json
import os
import random
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
THREADS = (1, 4, 16)
SCENARIOS = ("l1hit", "l2stub", "cachekitio")
ARMS = {
    "ft-nogil": ("ft", {"PYTHON_GIL": "0"}),
    "ft-nogil-aa": ("ft", {"PYTHON_GIL": "0"}),
    "ft-gil": ("ft", {"PYTHON_GIL": "1"}),
    "ft-default": ("ft", {}),
    "gil-build": ("gil", {}),
}
EXPECTED_GIL = {"ft-nogil": False, "ft-nogil-aa": False, "ft-gil": True, "gil-build": True}
CELL_TIMEOUT_S = 600


def _gil_enabled() -> bool:
    return bool(getattr(sys, "_is_gil_enabled", lambda: True)())


# --- one arm, one process ---------------------------------------------------------------------------


def run_cell(n: int, make_op, dur: float, warm: float) -> dict[str, float | None]:
    """Run make_op(tid)() on n threads for about dur seconds after warm seconds; return aggregate calls/s.

    Each thread's rate is its calls over its own counted interval, so a call that overruns the window
    (a backend timeout) lowers the rate instead of being credited to dur.
    """
    ops = [make_op(tid) for tid in range(n)]
    rates = [0.0] * n
    errors: list[BaseException] = []
    barrier = threading.Barrier(n + 1)
    window: dict[str, float] = {}

    def loop(tid: int) -> None:
        op, clock = ops[tid], time.perf_counter
        barrier.wait()
        try:
            while clock() < window["measure_from"]:
                op()
            calls, started = 0, clock()
            while clock() < window["end"]:
                op()
                op()
                op()
                op()
                calls += 4
            rates[tid] = calls / (clock() - started)
        except BaseException as exc:  # noqa: BLE001 -- re-raised after join: a dead thread must not read as a slow one
            errors.append(exc)

    threads = [threading.Thread(target=loop, args=(tid,)) for tid in range(n)]
    for thread in threads:
        thread.start()
    now = time.perf_counter()
    window["measure_from"] = now + warm
    window["end"] = now + warm + dur
    barrier.wait()
    for thread in threads:
        thread.join()
    if errors:
        raise RuntimeError(f"{len(errors)} of {n} threads raised") from errors[0]
    total = sum(rates)
    return {"calls_per_s": round(total), "us_per_call_per_thread": round(n / total * 1e6, 3) if total else None}


class _DictBackend:
    """In-process backend: cachekit's L2 code path with no transport."""

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

    def health_check(self) -> tuple[bool, dict[str, str]]:
        return True, {"backend_type": "dict"}


def _payload(i: int) -> dict[str, object]:
    return {"id": i, "pad": "y" * 200, "tags": ["a", "b"], "n": 1.5}


def _decorated(scenario: str, port: int, computed: list[int], errors: list[str]):
    """The scenario's decorated function, primed so every later call is a hit.

    Each miss appends to computed; each CachekitIO get/set that raises appends to errors, which also
    catches a failure that a retry then hides.
    """
    from cachekit import cache
    from cachekit.config.nested import L1CacheConfig

    def payload(i: int) -> dict[str, object]:
        computed.append(i)
        return _payload(i)

    if scenario == "l1hit":
        fn = cache(backend=_DictBackend(), ttl=3600, namespace="ftb_l1")(payload)
    elif scenario == "l2stub":
        fn = cache(backend=_DictBackend(), ttl=3600, namespace="ftb_l2", l1=L1CacheConfig(enabled=False))(payload)
    elif scenario == "cachekitio":
        from cachekit.backends.cachekitio import CachekitIOBackend
        from cachekit.backends.cachekitio import config as cachekitio_config

        # The fake listens on 127.0.0.1, which the SSRF guard rejects; lift the guard in this process only.
        cachekitio_config.is_private_ip = lambda hostname: False

        class CountingBackend(CachekitIOBackend):
            pass

        def counting(method):
            def wrapper(self, *args, **kwargs):
                try:
                    return method(self, *args, **kwargs)
                except Exception as exc:
                    errors.append(type(exc).__name__)
                    raise

            return wrapper

        for name in ("get", "get_with_freshness", "set"):  # what the sync decorated path calls
            setattr(CountingBackend, name, counting(getattr(CachekitIOBackend, name)))

        backend = CountingBackend(api_url=f"https://127.0.0.1:{port}", api_key="ck_test_bench")
        fn = cache(backend=backend, ttl=3600, namespace="ftb_io", l1=L1CacheConfig(enabled=False))(payload)
    else:
        raise ValueError(f"unknown scenario {scenario!r}")
    for i in range(64):
        fn(i)  # miss -> compute -> store; every timed call after this is a hit
    return fn


def _fake_cpu(port: int) -> list[float]:
    """CPU seconds used so far by each worker of the loopback fake (on its own HTTP/1.1 connection)."""
    import httpx

    return httpx.get(f"https://127.0.0.1:{port}/__stats").raise_for_status().json()["worker_cpu_s"]


def cell_main(port: int, dur: float, warm: float) -> None:
    import sysconfig

    sys.path.insert(0, str(HERE.parents[1]))
    from tests.performance.measurement_env import fingerprint_hash, system_fingerprint

    gil_at_launch = _gil_enabled()
    load_start = os.getloadavg()
    computed: dict[str, list[int]] = {scenario: [] for scenario in SCENARIOS}
    errors: dict[str, list[str]] = {scenario: [] for scenario in SCENARIOS}
    fns = {scenario: _decorated(scenario, port, computed[scenario], errors[scenario]) for scenario in SCENARIOS}
    gil_before = _gil_enabled()

    def make_op(fn):
        def make(tid: int):
            box = [tid]

            def op():
                box[0] = (box[0] + 1) & 63
                return fn(box[0])

            return op

        return make

    results = []
    for scenario in SCENARIOS:
        for n in THREADS:
            primed, errored = len(computed[scenario]), len(errors[scenario])
            fake_before, wall = (_fake_cpu(port), time.perf_counter()) if scenario == "cachekitio" else (None, 0.0)
            cell = run_cell(n, make_op(fns[scenario]), dur, warm)
            # A timed call that was not a hit, or a backend call that raised, taints the cell.
            cell["misses"] = len(computed[scenario]) - primed
            cell["backend_errors"] = len(errors[scenario]) - errored
            if fake_before is not None:
                wall = time.perf_counter() - wall
                cell["fake_busiest_worker"] = round(
                    max(b - a for a, b in zip(fake_before, _fake_cpu(port), strict=True)) / wall, 3
                )
            results.append({"scenario": scenario, "threads": n, **cell})
    print(
        json.dumps(
            {
                "python": sys.version.split()[0],
                "free_threaded_build": bool(sysconfig.get_config_var("Py_GIL_DISABLED")),
                "gil_at_launch": gil_at_launch,
                "gil_before_cells": gil_before,
                "gil_after_cells": _gil_enabled(),
                "hiredis_loaded": sys.modules.get("hiredis") is not None,
                "cpu_count": os.cpu_count(),
                "cpus_allowed": len(os.sched_getaffinity(0)),
                "load_start": load_start,
                "load_end": os.getloadavg(),
                "fingerprint": fingerprint_hash(system_fingerprint()),
                "results": results,
            }
        )
    )


# --- driver -----------------------------------------------------------------------------------------


def _pinned(cpus: str | None, cmd: list[str]) -> list[str]:
    return ["taskset", "-c", cpus, *cmd] if cpus else cmd


def _start_fake(python: str, tmp: Path, cpus: str | None, workers: int) -> tuple[subprocess.Popen[str], int]:
    cert, key = tmp / "cert.pem", tmp / "key.pem"
    subprocess.run(  # noqa: S603 (trusted: literal openssl argv)
        ["openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1", "-nodes", "-days", "1"]
        + ["-subj", "/CN=127.0.0.1", "-addext", "subjectAltName=IP:127.0.0.1", "-keyout", str(key), "-out", str(cert)],
        check=True,
        capture_output=True,
    )
    env = {**os.environ, "PYTHON_GIL": "0"}  # the fake runs its workers in parallel on a free-threaded build
    cmd = _pinned(cpus, [python, str(HERE / "loopback_saas.py"), "0", str(cert), str(key), str(workers)])
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, text=True, env=env)  # noqa: S603 (trusted: operator-passed interpreter)
    assert proc.stdout is not None
    line = proc.stdout.readline().split()
    if line[:1] != ["ready"]:
        proc.kill()
        raise RuntimeError(f"loopback fake failed to start (exit {proc.wait()})")
    return proc, int(line[1])


def _check_row(arm: str, row: dict) -> None:
    """Fail loudly when an arm did not run under the interpreter state it claims."""
    if ARMS[arm][0] == "ft" and not row["free_threaded_build"]:
        raise RuntimeError(f"--ft-python is not a free-threaded build (arm {arm})")
    if arm in EXPECTED_GIL and row["gil_before_cells"] != EXPECTED_GIL[arm]:
        raise RuntimeError(f"arm {arm} ran with gil_enabled={row['gil_before_cells']}, expected {EXPECTED_GIL[arm]}")


def drive(args: argparse.Namespace) -> None:
    pythons = {"ft": args.ft_python, "gil": args.gil_python}
    wanted = args.arms.split(",") if args.arms else list(ARMS)
    if unknown := set(wanted) - set(ARMS):
        raise SystemExit(f"unknown arms: {sorted(unknown)}")
    arms = [arm for arm in wanted if pythons[ARMS[arm][0]]]
    if not arms:
        raise SystemExit("no arm has an interpreter: pass --ft-python (and --gil-python for gil-build)")
    out = Path(args.out)
    if out.exists():
        raise SystemExit(f"{out} exists; give each session its own --out so sessions are never pooled")
    rng = random.Random(0)
    with tempfile.TemporaryDirectory() as tmp:
        fake, port = _start_fake(args.ft_python, Path(tmp), args.server_cpus, args.server_workers)
        base_env = {k: v for k, v in os.environ.items() if k != "PYTHON_GIL"}
        base_env.update(CACHEKIT_ALLOW_CUSTOM_HOST="true", SSL_CERT_FILE=str(Path(tmp) / "cert.pem"), PYTHONWARNINGS="ignore")
        try:
            for rep in range(args.reps):
                order = rng.sample(arms, len(arms))  # shuffled, so no arm always follows the same neighbour
                for arm in order:
                    build, arm_env = ARMS[arm]
                    cmd = [pythons[build], __file__, "--cell", str(port), "--dur", str(args.dur), "--warm", str(args.warm)]
                    proc = subprocess.run(  # noqa: S603 (trusted: interpreter paths the operator passed)
                        _pinned(args.cpus, cmd),
                        env={**base_env, **arm_env},
                        capture_output=True,
                        text=True,
                        check=False,
                        timeout=CELL_TIMEOUT_S,
                    )
                    if proc.returncode != 0:
                        raise RuntimeError(f"arm {arm} rep {rep} failed:\n{proc.stderr}")
                    row = {"arm": arm, "rep": rep, **json.loads(proc.stdout.strip().splitlines()[-1])}
                    _check_row(arm, row)
                    with out.open("a") as fh:
                        fh.write(json.dumps(row) + "\n")
                    print(f"rep {rep} {arm} done", file=sys.stderr)
        finally:
            fake.terminate()
            fake.wait()
    summarise(out)


def _spread(values: list[float], fmt: str) -> str:
    return f"{statistics.median(values):{fmt}} ({min(values):{fmt}}-{max(values):{fmt}})"


def summarise(path: Path) -> None:
    rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    kept = [r for r in rows if r["gil_before_cells"] == r["gil_after_cells"]]
    print(f"{len(rows)} processes, {len(rows) - len(kept)} dropped (GIL state changed while the cells ran)")
    loads = [r["load_start"][0] for r in kept] + [r["load_end"][0] for r in kept]
    if kept:
        print(f"1-min load average {min(loads):.1f}-{max(loads):.1f}; cpus allowed {sorted({r['cpus_allowed'] for r in kept})}")
    calls: dict[tuple[str, str, int], list[float]] = defaultdict(list)
    scaling: dict[tuple[str, str, int], list[float]] = defaultdict(list)
    gil: dict[str, set[bool]] = defaultdict(set)
    for r in kept:
        gil[r["arm"]].add(r["gil_before_cells"])
        by_cell = {(c["scenario"], c["threads"]): c["calls_per_s"] for c in r["results"]}
        for (scenario, n), value in by_cell.items():
            calls[(r["arm"], scenario, n)].append(value)
            if by_cell[(scenario, 1)]:  # a 1-thread cell with no completed call has no ratio
                scaling[(r["arm"], scenario, n)].append(value / by_cell[(scenario, 1)])
    arms = [arm for arm in ARMS if arm in gil]
    print("arm GIL state during the cells: " + ", ".join(f"{arm}={sorted(gil[arm])}" for arm in arms))
    tainted: dict[tuple[str, str, int], list[int]] = defaultdict(lambda: [0, 0])
    for r in kept:
        for c in r["results"]:
            counts = tainted[(r["arm"], c["scenario"], c["threads"])]
            counts[0] += c["misses"]
            counts[1] += c["backend_errors"]
    for (arm, scenario, n), (misses, errors) in sorted(tainted.items()):
        if misses or errors:
            print(f"TAINTED {arm} {scenario} {n}t: {misses} timed calls were not hits, {errors} backend calls raised")
    for r in kept:
        for c in r["results"]:
            if (c.get("fake_busiest_worker") or 0) > 0.8:
                print(
                    f"SERVER-BOUND {r['arm']} rep {r['rep']} {c['scenario']} {c['threads']}t: a fake worker was "
                    f"{c['fake_busiest_worker']:.0%} busy, so the fake may set this cell's ceiling"
                )
    for title, table, fmt in (("calls/s", calls, ",.0f"), ("scaling vs 1 thread", scaling, ".2f")):
        print(f"\n{title}: median (min-max) over reps")
        print(f"{'scenario':<11}{'thr':>4}  " + "".join(f"{arm:>28}" for arm in arms))
        for scenario in SCENARIOS:
            for n in THREADS:
                cells = [_spread(table[(arm, scenario, n)], fmt) if table[(arm, scenario, n)] else "-" for arm in arms]
                print(f"{scenario:<11}{n:>4}  " + "".join(f"{cell:>28}" for cell in cells))
    if "ft-nogil" not in arms:
        return
    print("\nscaling, arm minus ft-nogil: median difference [95% bootstrap CI over reps]")
    print("the ft-nogil-aa row is the A/A floor: a difference counts only if its CI excludes 0 and it exceeds the floor")
    rng = random.Random(0)
    for arm in [a for a in arms if a != "ft-nogil"]:
        for scenario in SCENARIOS:
            parts = []
            for n in THREADS[1:]:
                ref, other = scaling[("ft-nogil", scenario, n)], scaling[(arm, scenario, n)]
                if not ref or not other:
                    parts.append(f"{n}t -")
                    continue
                diffs = sorted(
                    statistics.median(rng.choices(other, k=len(other))) - statistics.median(rng.choices(ref, k=len(ref)))
                    for _ in range(2000)
                )
                diff = statistics.median(other) - statistics.median(ref)
                parts.append(f"{n}t {diff:+.2f} [{diffs[50]:+.2f}, {diffs[1949]:+.2f}]")
            print(f"  {arm:<12}{scenario:<11}" + "  ".join(parts))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--ft-python", help="free-threaded interpreter with cachekit installed")
    parser.add_argument("--gil-python", help="default (GIL) build with cachekit installed; adds the gil-build arm")
    parser.add_argument("--arms", help=f"comma-separated subset of {','.join(ARMS)} (default: all available)")
    parser.add_argument("--reps", type=int, default=5)
    parser.add_argument("--out", default="ft-scaling.jsonl", help="JSONL results, appended to")
    parser.add_argument("--dur", type=float, default=1.2, help="seconds counted per cell")
    parser.add_argument("--warm", type=float, default=0.25, help="seconds of warm-up per cell")
    parser.add_argument("--cpus", help="taskset CPU list for the arm processes, e.g. 0-15")
    parser.add_argument("--server-cpus", help="taskset CPU list for the loopback fake, e.g. 16-19")
    parser.add_argument("--server-workers", type=int, default=4, help="event-loop threads in the loopback fake")
    parser.add_argument("--summarise", metavar="JSONL", help="print the summary of an existing results file")
    parser.add_argument("--cell", type=int, metavar="PORT", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.cell is not None:
        cell_main(args.cell, args.dur, args.warm)
    elif args.summarise:
        summarise(Path(args.summarise))
    elif args.ft_python:
        drive(args)
    else:
        parser.error("pass --ft-python (to run) or --summarise")


if __name__ == "__main__":
    main()
