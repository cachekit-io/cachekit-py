"""Performance guards for the LAB-506 session-stats registry + session identity.

The LAB-506 fix (PR #233) moved per-function counters into a process-global,
lock-guarded registry and unified session-ID derivation onto one PID-aware
module. Its correctness *and* its performance both rest on one invariant:

    the registry lock is taken at DECORATION time only, never on the per-call
    path, and session-ID reads are lock-free after first init.

If a later refactor moved ``_get_function_stats`` (or its lock) onto the call
path, or reintroduced per-call locking in ``_ensure_session_initialized``,
every decorated call would serialize on a single process-global lock — a
silent throughput cliff under concurrency. These tests encode that invariant
so the regression fails loudly instead of shipping.

Marker convention (see .github/workflows/ci.yml): deterministic perf
invariants are tagged ``performance and slow`` and run in the CI perf gate
(alongside the #171/#152 memory-invariant tests); flaky wall-clock benchmarks
are tagged ``performance`` only and stay out of CI. The lock-accounting tests
below are deterministic and CI-gating; ``test_session_id_read_overhead_ns`` is
a wall-clock benchmark and is intentionally CI-excluded.
"""

from __future__ import annotations

import statistics
import threading
import time

import pytest

from cachekit.decorators import cache
from cachekit.decorators import session as session_mod
from cachekit.decorators import wrapper as wrapper_mod
from cachekit.decorators.wrapper import _function_stats_registry, _get_function_stats


class _CountingLock:
    """Lock wrapper that counts context-manager entries, delegating to a real lock.

    The registry / session locks are only ever used as ``with lock:`` context
    managers (and reassigned wholesale by the fork handlers), so counting
    ``__enter__`` faithfully counts every acquisition on the guarded path.
    """

    def __init__(self) -> None:
        self._real = threading.Lock()
        self.entries = 0

    def __enter__(self):
        self.entries += 1
        return self._real.__enter__()

    def __exit__(self, *exc):
        return self._real.__exit__(*exc)

    # Delegated for defensiveness if a caller ever switches to acquire/release.
    def acquire(self, *a, **k):
        self.entries += 1
        return self._real.acquire(*a, **k)

    def release(self):
        return self._real.release()


@pytest.mark.performance
@pytest.mark.slow
def test_redecoration_reuses_single_stats_object() -> None:
    """Re-decoration (factory / per-call patterns) reuses ONE stats object.

    This is the LAB-506 fix itself: a fresh counter object per decoration would
    send counters backwards under the process-stable session ID. It must also
    be O(1) in registry size — repeated decoration adds no entries.
    """
    ident = "tests.performance.test_session_stats_overhead:redecoration_target"
    _function_stats_registry.pop(ident, None)
    before = len(_function_stats_registry)

    try:
        first = _get_function_stats(ident, l1_enabled=True)
        seen = {id(_get_function_stats(ident, l1_enabled=True)) for _ in range(2000)}

        assert seen == {id(first)}, "re-decoration must reuse the same _FunctionStats instance"
        assert len(_function_stats_registry) == before + 1, "re-decoration must not grow the registry"

        # l1_enabled is last-decoration-wins (it only feeds the X-CacheKit-L1-Status header).
        latest = _get_function_stats(ident, l1_enabled=False)
        assert latest is first
        assert latest.l1_enabled is False
    finally:
        _function_stats_registry.pop(ident, None)


@pytest.mark.performance
@pytest.mark.slow
def test_registry_lock_not_on_per_call_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """The process-global registry lock is taken at decoration, NEVER per call.

    Regression guard: if the per-call path ever re-derives stats through the
    registry, this fails — that lock on the hot path is the throughput cliff
    the fix is designed to avoid.
    """
    counting = _CountingLock()
    monkeypatch.setattr(wrapper_mod, "_function_stats_registry_lock", counting)

    call_count = 0

    @cache(backend=None)  # L1-only: no backend/network, pure decorator overhead
    def compute(x: int) -> int:
        nonlocal call_count
        call_count += 1
        return x * 2

    ident = f"{compute.__module__}.{compute.__qualname__}"
    try:
        acquisitions_at_decoration = counting.entries
        assert acquisitions_at_decoration >= 1, "decoration should register stats via the registry lock"

        # Exercise the hot path: one miss then many L1 hits.
        assert compute(21) == 42
        for _ in range(5000):
            assert compute(21) == 42

        assert call_count == 1, "sanity: after the first miss, all calls hit L1"
        assert counting.entries == acquisitions_at_decoration, (
            f"per-call path acquired the registry lock "
            f"{counting.entries - acquisitions_at_decoration} time(s); it must be decoration-only"
        )
    finally:
        _function_stats_registry.pop(ident, None)


@pytest.mark.performance
@pytest.mark.slow
def test_session_read_lock_free_after_init(monkeypatch: pytest.MonkeyPatch) -> None:
    """After first init, ``get_session_id()`` reads via the lock-free fast path.

    Guards ``_ensure_session_initialized``'s fast path: the session lock must be
    hit only on the first (initializing) call per process, not on every read.
    """
    # Ensure the process session is initialized using the real lock first.
    session_mod.get_session_id()
    session_mod.get_session_start_ms()

    counting = _CountingLock()
    monkeypatch.setattr(session_mod, "_session_lock", counting)

    for _ in range(10000):
        session_mod.get_session_id()
        session_mod.get_session_start_ms()

    assert counting.entries == 0, (
        f"session reads acquired the lock {counting.entries} time(s) after init; the fast path must be lock-free"
    )


@pytest.mark.performance
def test_session_id_read_overhead_ns() -> None:
    """Wall-clock: per-call ``get_session_id()`` overhead stays cheap.

    CI-excluded (``performance`` without ``slow``) because wall-clock timing is
    machine-dependent; the ceiling is deliberately loose to catch only a gross
    regression (e.g. per-call locking or UUID regeneration reintroduced), not
    to police nanoseconds. Run via ``make benchmark``-style perf sessions.
    """
    iterations = 100_000

    # Warm up (also guarantees the one-time init is done before timing).
    for _ in range(1000):
        session_mod.get_session_id()

    latencies = []
    for _ in range(iterations):
        start = time.perf_counter_ns()
        session_mod.get_session_id()
        latencies.append(time.perf_counter_ns() - start)

    p50 = statistics.median(latencies)
    p95 = statistics.quantiles(latencies, n=20)[18]
    p99 = statistics.quantiles(latencies, n=100)[98]

    print(f"\n{'=' * 60}")
    print("get_session_id() per-call overhead (post-init, lock-free)")
    print(f"{'=' * 60}")
    print(f"Iterations:  {iterations:>10,}")
    print(f"P50:         {p50:>10.1f} ns")
    print(f"P95:         {p95:>10.1f} ns")
    print(f"P99:         {p99:>10.1f} ns")

    ceiling_ns = 20_000
    if p95 >= ceiling_ns:
        raise AssertionError(
            f"get_session_id() p95 {p95:.0f}ns exceeds {ceiling_ns}ns ceiling — "
            f"likely per-call locking or UUID regeneration reintroduced on the hot path"
        )
