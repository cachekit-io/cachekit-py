"""The process ``ir_budget.py`` measures: one hot path, called ``n`` times under callgrind.

This file is kept apart from the gate because its text is compiled into every measured process,
so editing it moves the heap layout the budgets were recorded at: after any change here, re-record
the budgets on every interpreter (``ir_budget.py --update --allow-increase``). Editing the gate
itself (thresholds, report, options, its docs) does not touch the measured process.

Usage, as ``ir_budget.py`` runs it: ``python ir_workload.py <path> <n> <warmup>``.
"""

from __future__ import annotations

import os
import shutil
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

FILE_SET_ENTRIES = 1000
_scratch_dirs: list[str] = []  # removed before exit; the removal costs the same at both loop sizes


def build_workload(path: str) -> Callable[[], object]:
    """One path as a zero-arg callable. Only that path is built, so no other path's threads run.

    Imports happen here, so importing this module stays cheap.
    """
    import pandas as pd

    import cachekit.decorators.orchestrator as orchestrator
    from cachekit import cache
    from cachekit.serializers import EncryptionWrapper, get_serializer

    value = {"id": 42, "name": "Ada Lovelace", "roles": ["admin", "ops"], "score": 97.5, "tags": list(range(16))}
    # Dict-heavy value (about 6 KB): the decoder's object_hook runs once per record.
    records = [
        {"id": i, "name": f"user-{i}", "email": f"user-{i}@example.com", "active": i % 2 == 0, "score": i * 0.5, "team": "ops"}
        for i in range(100)
    ]
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
            # growing (a growing queue widened the layout spread). Under 1.6k Ir of the figure.
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

    if path == "file_set":
        # FileBackend.set() overwriting one key in a cache of FILE_SET_ENTRIES entries: the entry
        # count stays flat, so no eviction runs, and anything set() does per cached entry shows.
        import tempfile

        from cachekit.backends.file import FileBackend
        from cachekit.backends.file.config import FileBackendConfig

        cache_dir = tempfile.mkdtemp(prefix="cachekit-ir-file-")
        _scratch_dirs.append(cache_dir)
        backend = FileBackend(FileBackendConfig(cache_dir=cache_dir))
        payload = b"x" * 512
        for i in range(FILE_SET_ENTRIES):
            backend.set(f"entry:{i}", payload)
        return lambda: backend.set("entry:0", payload)

    def roundtrip(serializer: Any, obj: object, **key: str) -> Callable[[], object]:
        def op() -> object:
            data, meta = serializer.serialize(obj, **key)
            return serializer.deserialize(data, meta, **key)

        return op

    if path == "serializer_arrow":
        return roundtrip(get_serializer("arrow"), frame)
    if path.endswith("_usgs"):
        # A real public dataset, 12,535 rows of mixed dtypes (tests/data/README.md).
        usgs = pd.read_parquet(Path(__file__).parents[1] / "data" / "usgs_earthquakes_2024-01.parquet")
        return roundtrip(get_serializer(path.removeprefix("serializer_").removesuffix("_usgs")), usgs)
    if path == "serializer_default_records":
        return roundtrip(get_serializer("default"), records)
    if path == "serializer_encrypted":
        encrypted = EncryptionWrapper(master_key=bytes.fromhex(master_key), previous_master_keys=[])
        return roundtrip(encrypted, value, cache_key="ns:bench:func:m.f:args:" + "0" * 64 + ":0")
    return roundtrip(get_serializer(path.removeprefix("serializer_")), value)


def pin_main_thread_clocks() -> None:
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


def run_workload(path: str, n: int, warmup: int) -> None:
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
    pin_main_thread_clocks()
    _shift = [object() for _ in range(int(os.environ.get("IR_BUDGET_LAYOUT", "0")))]  # noqa: F841 (held: see LAYOUTS in ir_budget.py)
    op = build_workload(path)
    for _ in range(warmup):  # identical warmup at both N: first-call costs (L1 fill, lazy imports) cancel
        op()
    for _ in range(n):
        op()
    for scratch in _scratch_dirs:
        shutil.rmtree(scratch, ignore_errors=True)
    # Skip interpreter teardown: it is not part of a call, and anything it frees that grew with n
    # would leak into the per-op difference.
    os._exit(0)


if __name__ == "__main__":
    run_workload(sys.argv[1], int(sys.argv[2]), int(sys.argv[3]))
