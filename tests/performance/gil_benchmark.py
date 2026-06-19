"""GIL / free-threading thread-scaling benchmark for the cachekit serializer.

Runs an identical CPU-bound serialize workload across 1, 2, 4, 8 threads under the
*current* interpreter and reports speedup + parallel efficiency. Under a stock
(GIL-enabled) build, efficiency reveals how much the Rust ByteStorage path already
runs GIL-free; under a free-threaded build it shows the parallel ceiling.

Free-threaded comparison is interpreter-driven, not code-driven: run this same script
under a free-threaded interpreter and compare the efficiency column. Today that is
blocked for cachekit — PyO3 < 3.14 has no free-threaded support, and orjson /
numpy / pandas / pyarrow lack free-threaded wheels — so the no-GIL arm is reported as
unavailable. Once a free-threaded cachekit installs, no change here is needed: the
same run produces the no-GIL numbers.

Run:  uv run python tests/performance/gil_benchmark.py
"""

from __future__ import annotations

import sys
import time
from concurrent.futures import ThreadPoolExecutor

from cachekit.serializers import StandardSerializer

THREAD_COUNTS = (1, 2, 4, 8)
OPS_PER_THREAD = 2000


def gil_enabled() -> bool:
    """True on a stock GIL build; matches src/cachekit/hiredis_compat.py's check."""
    return bool(getattr(sys, "_is_gil_enabled", lambda: True)())


def _payload() -> dict[str, dict[str, object]]:
    """Medium nested dict — exercises msgpack encode + Rust ByteStorage (LZ4 + checksum)."""
    return {f"key_{i}": {"value": f"data_{i}", "count": i, "vals": list(range(20))} for i in range(200)}


def _work(serializer: StandardSerializer, data: object, ops: int) -> None:
    for _ in range(ops):
        serializer.serialize(data)


def run_threads(n_threads: int, ops_total: int) -> float:
    """Run ops_total serialize calls spread across n_threads; return wall seconds.

    Setup (serializer + payload) happens before the clock starts, so the measurement
    captures only the concurrent serialize work.
    """
    serializer = StandardSerializer()
    data = _payload()
    per = ops_total // n_threads
    start = time.perf_counter()
    with ThreadPoolExecutor(max_workers=n_threads) as pool:
        futures = [pool.submit(_work, serializer, data, per) for _ in range(n_threads)]
        for future in futures:
            future.result()
    return time.perf_counter() - start


def main() -> None:
    gil = gil_enabled()
    label = "GIL ENABLED (stock)" if gil else "FREE-THREADED (no-GIL)"
    ops_total = OPS_PER_THREAD * max(THREAD_COUNTS)  # fixed total work across all thread counts

    print("\nGIL thread-scaling: StandardSerializer.serialize")
    print(f"  interpreter: {sys.version.split()[0]}  |  {label}")
    print(f"  total ops:   {ops_total} (held constant across thread counts)\n")

    run_threads(1, max(THREAD_COUNTS))  # warmup

    single = run_threads(1, ops_total)
    print(f"  {'threads':>7}  {'wall (s)':>9}  {'speedup':>8}  {'efficiency':>11}")
    for n_threads in THREAD_COUNTS:
        wall = run_threads(n_threads, ops_total)
        speedup = single / wall
        efficiency = (speedup / n_threads) * 100
        print(f"  {n_threads:>7}  {wall:>9.4f}  {speedup:>7.2f}x  {efficiency:>10.0f}%")

    if gil:
        print("\n  No-GIL arm: run this under a free-threaded interpreter to compare the")
        print("  efficiency column. Blocked today for cachekit (PyO3 < 3.14; orjson /")
        print("  numpy / pandas / pyarrow lack free-threaded wheels).")


if __name__ == "__main__":
    main()
