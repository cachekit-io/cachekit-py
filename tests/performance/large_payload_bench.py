"""Thread CPU and minor page faults per op of the encrypted ``@cache.io`` get and set, up to 1 MB.

At 1 MB, page faults on payload-sized buffers are a large share of an encrypted call's CPU under
glibc's default malloc, and the count depends on every large allocation in the process, so removing
one buffer can raise it for another payload shape (docs/performance.md, "Large Payloads and glibc
malloc", has the mechanism and the measured figures). Instruction counts (``ir_budget.py``) cannot
see faults. So this harness reports minor faults (``getrusage`` ``ru_minflt``, this thread only)
beside thread CPU for every figure, runs each measurement in a fresh process, and samples the heap
layouts ``ir_budget.py`` uses (``LAYOUTS``).

The workload is ``@cache.io`` with ``EncryptionConfig(enabled=True, single_tenant_mode=True)``, L1
off, and an in-process backend, so no transport is measured: a real backend also allocates the
response body on every get, which is not in these figures.

  hit    every call is an L2 hit: get, decrypt, deserialize
  miss   every call misses: get, compute, serialize, encrypt, set (the backend discards the value)

Shapes are ``<kind>-<size>``: ``json`` is JSON-like text in a dict (LZ4 compresses it about 2.7x),
``bin`` is random bytes (incompressible), at 64 B, 1 KB, 64 KB and 1 MB (1,000,000 bytes).

Modes:

  run   one interpreter: per shape and path, the mean over fresh processes of CPU and faults per op,
        with the per-process fault range (a bimodal fault count shows as a wide range).
  ab    base against candidate. Each pair runs both arms once, in a random order, at the same heap
        layout; the pair's delta is candidate minus base of the per-process means. Reports the mean
        paired delta with a bootstrap 95% CI over pairs. "lower" or "higher" means the CI excludes 0.

The arms are interpreters (``--base`` / ``--candidate``, each from its own checkout's environment)
and environment settings (``--base-env`` / ``--candidate-env``, repeatable ``KEY=VALUE``), so the same
command compares two builds or one build under two allocator settings. Both arms run this file, so
the harness itself never differs between them. Every measured process starts without the calling
shell's ``MALLOC_*``, ``GLIBC_TUNABLES`` and ``LD_PRELOAD``, so an arm runs glibc malloc's defaults
unless its ``--*-env`` names a setting, and the header prints each arm's settings. With neither
flag, both arms are this interpreter: an A/A run, whose verdicts show the host's noise. A full run
makes 48 comparisons (16 cells, three metrics), so at 95% expect about two false "lower" or
"higher" verdicts in an A/A run; on a shared host the CPU of the small shapes is the noisiest. Repeat a run before trusting a lone verdict.

Run (from a checkout, after ``uv sync``)::

  uv run python tests/performance/large_payload_bench.py run
  uv run python tests/performance/large_payload_bench.py ab --base ../base/.venv/bin/python --pairs 30
  uv run python tests/performance/large_payload_bench.py ab \\
      --candidate-env MALLOC_MMAP_THRESHOLD_=4194304 --candidate-env MALLOC_TRIM_THRESHOLD_=16777216

``--shapes`` and ``--paths`` narrow a run (for example ``--shapes json-1m bin-1m``); ``--json FILE``
writes the figures, every process's included. A full A/B at 30 pairs (8 shapes, 2 paths, 960
processes) took about 14 minutes on a GitHub-hosted ``ubuntu-latest`` runner. Linux only
(``RUSAGE_THREAD``).
"""

from __future__ import annotations

import argparse
import json
import os
import random
import statistics
import subprocess
import sys
from pathlib import Path
from typing import Any

SIZES = {"64b": 64, "1k": 1024, "64k": 65536, "1m": 1_000_000}
KINDS = ("json", "bin")
SHAPES = tuple(f"{kind}-{size}" for kind in KINDS for size in SIZES)
PATHS = ("hit", "miss")
# Ops per process and warmup calls before them: enough that the per-process mean is stable at each size.
OPS = {"64b": (2000, 200), "1k": (2000, 200), "64k": (400, 50), "1m": (40, 10)}
BOOTSTRAP_RESAMPLES = 10_000
CHILD_TIMEOUT_S = 300
WORDS = (
    "randomized controlled trial meditation mindfulness anxiety depression stress cortisol sleep quality "
    "participants intervention control group baseline follow-up weeks significant reduction effect size "
    "cohen confidence interval systematic review meta-analysis heterogeneity bias risk outcome measure "
    "self-reported questionnaire clinical sample adults older students breathing yoga attention wellbeing"
).split()


def payload(shape: str) -> object:
    """The cached value for a shape; seeded, so every process and arm caches the same value.

    >>> len(payload("bin-1k"))
    1024
    >>> value = payload("json-64k")
    >>> 65536 <= sum(len(s) for s in value["reasons"]) + 32 < 65536 + 200
    True
    """
    kind, size = shape.split("-")
    n = SIZES[size]
    rng = random.Random(7)
    if kind == "bin":
        return rng.randbytes(n)
    words: list[str] = []
    length = 32  # the dict, its keys and "doc"
    while length < n:
        words.append(rng.choice(WORDS))
        length += len(words[-1]) + 1
    text = " ".join(words)
    return {"doc": "10.1234/abcd", "reasons": [text[i : i + 160] for i in range(0, len(text), 160)]}


# --- one process: one shape, one path ---------------------------------------------------------------


def worker(shape: str, path: str, layout: int) -> dict[str, Any]:
    """Measure one shape and path in this process; returns per-op means over the measured calls."""
    import gc
    import resource
    import time

    import cachekit
    import cachekit.backends.cachekitio as ckio
    from cachekit import cache
    from cachekit.config import EncryptionConfig

    sys.setswitchinterval(1e6)  # the main thread keeps the GIL unless the code releases it
    _shift = [object() for _ in range(layout)]  # noqa: F841 (held: moves where every later allocation lands)

    class Backend:
        def __init__(self) -> None:
            self.store: dict[str, bytes] = {}

        def get(self, key: str) -> bytes | None:
            return self.store.get(key)

        def set(self, key: str, value: bytes, ttl: int | None = None) -> None:
            if path == "hit":
                self.store[key] = value

        def delete(self, key: str) -> bool:
            return self.store.pop(key, None) is not None

        def exists(self, key: str) -> bool:
            return key in self.store

        def health_check(self) -> tuple[bool, dict[str, Any]]:
            return True, {}

    value = payload(shape)
    computed = [0]

    def body(uid: int, tag: str) -> object:
        computed[0] += 1
        return value

    # A module-level name, as a user's function has: a "<locals>" name sends every key build down the
    # sanitizer's slow path, which no real call takes.
    body.__module__ = "bench"
    body.__qualname__ = "large_payload"
    # @cache.io always builds a CachekitIOBackend; swap the class for the in-process one while it does.
    real, ckio.CachekitIOBackend = ckio.CachekitIOBackend, lambda api_key=None: Backend()
    try:
        fn = cache.io(
            namespace="large-payload-bench",
            ttl=604800,
            stale_ttl=0,
            l1_enabled=False,
            encryption=EncryptionConfig(enabled=True, single_tenant_mode=True, master_key="ab" * 32),
        )(body)
    finally:
        ckio.CachekitIOBackend = real
    ops, warmup = OPS[shape.split("-")[1]]
    for _ in range(warmup):
        if fn(42, "user-profile") != value:
            raise RuntimeError(f"{shape}/{path}: the cached call returned a different value")
    gc.disable()
    before = computed[0]
    faults0, cpu0 = resource.getrusage(resource.RUSAGE_THREAD).ru_minflt, time.thread_time_ns()
    for _ in range(ops):
        fn(42, "user-profile")
    faults1, cpu1 = resource.getrusage(resource.RUSAGE_THREAD).ru_minflt, time.thread_time_ns()
    # A hit that computes, or a miss that does not, measures the wrong path.
    expected = 0 if path == "hit" else ops
    if computed[0] - before != expected:
        raise RuntimeError(f"{shape}/{path}: body ran {computed[0] - before} times in {ops} calls, expected {expected}")
    return {
        "cpu_us": (cpu1 - cpu0) / ops / 1000,
        "faults": (faults1 - faults0) / ops,
        "maxrss_mb": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024,  # Linux reports KiB
        "cachekit": cachekit.__file__,
    }


# --- driver -------------------------------------------------------------------------------------------


def allocator_env(env: dict[str, str]) -> dict[str, str]:
    """This process's environment without ``MALLOC_*``, ``GLIBC_TUNABLES`` (all of it) and ``LD_PRELOAD``, plus ``env``.

    An arm's allocator settings are only the ones it names: a ``MALLOC_*`` or ``GLIBC_TUNABLES``
    exported in the calling shell would otherwise reach both arms and turn the default arm into a copy
    of the tuned one, and an ``LD_PRELOAD``-ed jemalloc or tcmalloc would replace glibc malloc in both.
    """
    dropped = ("GLIBC_TUNABLES", "LD_PRELOAD")
    inherited = {k: v for k, v in os.environ.items() if not k.startswith("MALLOC_") and k not in dropped}
    return {**inherited, **env}


def spawn(python: str, env: dict[str, str], shape: str, path: str, layout: int) -> dict[str, Any]:
    """One measurement in a fresh process (``-I``: no script directory or user site on its path)."""
    cmd = [python, "-I", "-B", str(Path(__file__).resolve()), "worker", shape, path, str(layout)]
    done = subprocess.run(  # noqa: S603 (trusted: interpreter paths the operator passed)
        cmd, env=allocator_env(env), capture_output=True, text=True, timeout=CHILD_TIMEOUT_S, check=False
    )
    if done.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd)} exited {done.returncode}:\n{done.stderr[-2000:]}")
    return json.loads(done.stdout.splitlines()[-1])


def paired_ci(deltas: list[float], seed: int = 0) -> tuple[float, float]:
    """Percentile bootstrap 95% CI of the mean of paired deltas, resampling whole pairs.

    >>> lo, hi = paired_ci([-3.0, -2.5, -3.5, -2.0, -4.0] * 6)
    >>> lo < -3.0 < hi < 0
    True
    """
    rng = random.Random(seed)
    k = len(deltas)
    means = sorted(statistics.fmean(rng.choices(deltas, k=k)) for _ in range(BOOTSTRAP_RESAMPLES))
    return means[int(BOOTSTRAP_RESAMPLES * 0.025)], means[int(BOOTSTRAP_RESAMPLES * 0.975) - 1]


def verdict(ci: tuple[float, float]) -> str:
    """ "lower" or "higher" when the CI excludes 0, else "n.s." (not significant).

    >>> verdict((-2.0, -0.5)), verdict((0.1, 3.0)), verdict((-1.0, 1.0))
    ('lower', 'higher', 'n.s.')
    """
    return "lower" if ci[1] < 0 else "higher" if ci[0] > 0 else "n.s."


def parse_env(pairs: list[str]) -> dict[str, str]:
    """``KEY=VALUE`` strings to a dict.

    >>> parse_env(["MALLOC_TOP_PAD_=67108864"])
    {'MALLOC_TOP_PAD_': '67108864'}
    >>> parse_env(["MALLOC_TOP_PAD_"])
    Traceback (most recent call last):
    SystemExit: --*-env takes KEY=VALUE, got 'MALLOC_TOP_PAD_'
    """
    env = {}
    for pair in pairs:
        key, sep, value = pair.partition("=")
        if not sep or not key:
            raise SystemExit(f"--*-env takes KEY=VALUE, got {pair!r}")
        env[key] = value
    return env


def at_least_two(text: str) -> int:
    """A count of processes or pairs: one gives a bootstrap CI of zero width, so a verdict with no evidence.

    >>> at_least_two("30")
    30
    """
    n = int(text)
    if n < 2:
        raise argparse.ArgumentTypeError(f"needs at least 2, got {n}")
    return n


def cells(args: argparse.Namespace) -> list[tuple[str, str]]:
    return [(shape, path) for shape in args.shapes for path in args.paths]


def layouts() -> tuple[int, ...]:
    """ir_budget.py's heap-layout shifts, imported here so the measured process never loads the gate."""
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from ir_budget import LAYOUTS

    return LAYOUTS


METRICS = (("cpu_us", "CPU us/op"), ("faults", "faults/op"), ("maxrss_mb", "max RSS MB"))


def run(args: argparse.Namespace) -> dict:
    env, lays, out = parse_env(args.candidate_env), layouts(), {}
    print(f"{args.candidate}  allocator: {env or 'glibc defaults'}  {args.procs} processes per cell")
    print("fault range is min-max over processes")
    print(f"{'shape':9} {'path':4} {'CPU us/op':>10} {'faults/op':>10} {'fault range':>12} {'max RSS MB':>11}")
    for shape, path in cells(args):
        rows = [spawn(args.candidate, env, shape, path, lays[i % len(lays)]) for i in range(args.procs)]
        cell = {metric: [r[metric] for r in rows] for metric, _ in METRICS}
        out[f"{shape}/{path}"] = {**cell, "cachekit": rows[0]["cachekit"]}
        spread = f"{min(cell['faults']):.0f}-{max(cell['faults']):.0f}"
        cpu, faults, rss = (statistics.fmean(cell[metric]) for metric, _ in METRICS)
        print(f"{shape:9} {path:4} {cpu:10.1f} {faults:10.1f} {spread:>12} {rss:11.1f}", flush=True)
    return out


def ab(args: argparse.Namespace) -> dict:
    arms = {"base": (args.base, parse_env(args.base_env)), "candidate": (args.candidate, parse_env(args.candidate_env))}
    lays, rng, out = layouts(), random.Random(0), {}
    if args.base != args.candidate and arms["base"][1] == arms["candidate"][1]:
        # Two interpreters that import the same cachekit, in the same environment, make a silent A/A run.
        imported = {arm: spawn(python, env, "json-64b", "hit", 0)["cachekit"] for arm, (python, env) in arms.items()}
        if imported["base"] == imported["candidate"]:
            raise SystemExit(f"--base and --candidate both import {imported['base']}: that compares a build with itself")
    for arm, (python, env) in arms.items():
        print(f"{arm:9} {python}  allocator: {env or 'glibc defaults'}")
    print(f"{args.pairs} pairs per cell")
    print("per metric: base mean, mean paired delta (% of base) [bootstrap 95% CI] verdict")
    for shape, path in cells(args):
        got: dict[str, list[dict]] = {"base": [], "candidate": []}
        for i in range(args.pairs):
            order = ["base", "candidate"]
            rng.shuffle(order)
            for arm in order:
                python, env = arms[arm]
                got[arm].append(spawn(python, env, shape, path, lays[i % len(lays)]))
        cell = {"pairs": args.pairs, "cachekit": {arm: rows[0]["cachekit"] for arm, rows in got.items()}}
        line = f"{shape:9} {path:4}"
        for metric, label in METRICS:
            base = [r[metric] for r in got["base"]]
            cand = [r[metric] for r in got["candidate"]]
            deltas = [c - b for b, c in zip(base, cand, strict=True)]
            mean, ci, base_mean = statistics.fmean(deltas), paired_ci(deltas), statistics.fmean(base)
            pct = f"{100 * mean / base_mean:+6.1f}%" if base_mean else "    n/a"
            cell[metric] = {"base": base, "candidate": cand, "delta": mean, "ci95": ci, "verdict": verdict(ci)}
            line += f" | {label} {base_mean:.1f} {mean:+.1f} ({pct}) [{ci[0]:+.1f}, {ci[1]:+.1f}] {verdict(ci)}"
        out[f"{shape}/{path}"] = cell
        print(line, flush=True)
    return out


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "worker":
        print(json.dumps(worker(sys.argv[2], sys.argv[3], int(sys.argv[4]))))
        return
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="mode", required=True)
    run_p, ab_p = sub.add_parser("run"), sub.add_parser("ab")
    for p in (run_p, ab_p):
        p.add_argument("--shapes", nargs="+", choices=SHAPES, default=list(SHAPES))
        p.add_argument("--paths", nargs="+", choices=PATHS, default=list(PATHS))
        p.add_argument("--candidate", default=sys.executable, help="interpreter of the arm under test (default: this one)")
        p.add_argument("--candidate-env", action="append", default=[], metavar="KEY=VALUE")
        p.add_argument("--json", type=Path, help="write the figures here")
    run_p.add_argument("--procs", type=at_least_two, default=10, help="fresh processes per cell (default: 10)")
    ab_p.add_argument("--base", default=sys.executable, help="interpreter of the base arm (default: this one)")
    ab_p.add_argument("--base-env", action="append", default=[], metavar="KEY=VALUE")
    ab_p.add_argument("--pairs", type=at_least_two, default=30, help="interleaved process pairs per cell (default: 30)")
    args = parser.parse_args()
    out = run(args) if args.mode == "run" else ab(args)
    if args.json:
        args.json.write_text(json.dumps(out, indent=1))


if __name__ == "__main__":
    main()
