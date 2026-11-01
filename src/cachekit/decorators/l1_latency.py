"""The L1 hit-latency report a CachekitIO backend sends to the server (protocol ``spec/saas-api.md#l1-hit-latency``).

A wrapper times a sample of its backed-mode calls, and adds each one that turns out to be an L1 hit to the
report of the backend its cache is configured with. The backend takes the report, whole, into the next request
it sends. Here, not in the backend package, because the wrapper imports it and must not load the HTTP client.
"""

from __future__ import annotations

import os
import threading
import time
import weakref
from collections.abc import Callable

# Bucket i (0-based) holds the durations up to 2**(i + 7) ns, the last up to the 60 s ceiling (spec table).
BUCKETS = 16
MAX_DURATION_NS = 60_000_000_000
# Every integer on the wire is at most 2**53 - 1, the largest a JavaScript number holds exactly.
_MAX_WIRE_INT = 2**53 - 1
# Each call is timed with probability 1/N, the same N for every backend: the wrapper's draw is SAMPLE_BITS random
# bits all zero, so N is derived from it and is always the probability the draw has.
SAMPLE_BITS = 6
ONE_IN = 1 << SAMPLE_BITS
_ONE_IN_VALUE = str(ONE_IN)
# perf_counter qualifies only at 100 ns or finer (spec: Clock).
_MAX_CLOCK_RESOLUTION_S = 1e-7

BUCKETS_HEADER = "X-CacheKit-L1-Latency-Buckets"
SUM_HEADER = "X-CacheKit-L1-Latency-Sum-Ns"
ONE_IN_HEADER = "X-CacheKit-L1-Latency-One-In"


def clock_qualifies() -> bool:
    """Whether ``time.perf_counter_ns`` resolves 100 ns or finer here; without it no latency header is sent (API-82)."""
    return time.get_clock_info("perf_counter").resolution <= _MAX_CLOCK_RESOLUTION_S


def bucket_index(duration_ns: int) -> int:
    """The 0-based bucket a duration falls in: above the previous bound, up to and including its own.

    Examples:
        >>> [bucket_index(d) for d in (0, 128, 129, 256, 2_097_152, 2_097_153, MAX_DURATION_NS)]
        [0, 0, 1, 1, 14, 15, 15]
    """
    return min(max((duration_ns - 1).bit_length() - 7, 0), BUCKETS - 1)


class _State:
    """One process's accumulator. Its ``pid`` never changes: a forked child gets a new ``_State``, never a reset one."""

    __slots__ = ("buckets", "lock", "pid", "sum")

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.pid = os.getpid()
        self.buckets = [0] * BUCKETS
        self.sum = 0


# A forked child empties every report before any thread of its own runs, where the fork runs Python's at-fork
# hooks. The per-call PID check below covers a fork that does not (uWSGI).
_REPORTS: weakref.WeakSet[L1LatencyReport] = weakref.WeakSet()


def _empty_reports_in_child() -> None:
    for report in list(_REPORTS):
        report._empty_in_child()  # pyright: ignore[reportPrivateUsage]


# One lock per process, for replacing an inherited state. dict.setdefault is atomic, so every thread of a process
# gets the same lock, and a child never uses one its parent's threads could hold.
_REPLACEMENT_LOCKS: dict[int, threading.Lock] = {}


def _replacement_lock(pid: int) -> threading.Lock:
    return _REPLACEMENT_LOCKS.setdefault(pid, threading.Lock())


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_empty_reports_in_child)


class L1LatencyReport:
    """One CachekitIO backend's accumulator of timed L1 hits since its last report.

    ``add``, ``record`` and ``take`` hold one lock, so a report's buckets and sum always describe the same hits and
    no two requests carry the same hit (API-85). A forked child starts empty (API-83): an at-fork hook gives every
    report a new state before the child's own threads run. A fork that skips Python's hooks (uWSGI) is caught by
    the PID check on each use instead. The child never touches the parent's state, whose lock a parent thread may
    have held at the fork: it builds a complete new state and publishes it with one assignment, so a thread sees
    either the inherited state, whose PID sends it to build its own, or a new empty one. Without the hook, child
    threads racing their first use build that state once, under a lock of the child's own.

    Examples:
        >>> report = L1LatencyReport()
        >>> for d in (90, 300, 300, 40_000):
        ...     report.add(d)
        >>> report.take()
        {'X-CacheKit-L1-Latency-Buckets': '1,0,2,0,0,0,0,0,0,1,0,0,0,0,0,0', 'X-CacheKit-L1-Latency-Sum-Ns': '40690', 'X-CacheKit-L1-Latency-One-In': '64'}
        >>> report.take() is None
        True
    """

    __slots__ = ("__weakref__", "_state")

    def __init__(self) -> None:
        self._state = _State()
        _REPORTS.add(self)

    def _empty_in_child(self) -> None:
        self._state = _State()

    def _current(self) -> _State:
        state = self._state
        pid = os.getpid()
        if state.pid != pid:
            # A fork that skipped the hook: threads racing their first use agree on one new state, so none
            # discards hits another already counted into its own. The lock is this process's own, never one
            # inherited from the parent, which a parent thread may have held at the fork.
            with _replacement_lock(pid):
                state = self._state
                if state.pid != pid:
                    state = _State()
                    self._state = state
        return state

    @staticmethod
    def _count(state: _State, duration_ns: int) -> None:
        """Count one duration into ``state``; the caller holds its lock. Above 60 s it is discarded (API-87)."""
        if not 0 <= duration_ns <= MAX_DURATION_NS:
            return
        total = state.sum + duration_ns
        if total > _MAX_WIRE_INT:  # never reached in practice: about 150,000 hits of 60 s in one report
            return
        state.sum = total
        state.buckets[bucket_index(duration_ns)] += 1

    def add(self, duration_ns: int) -> None:
        """Count one timed L1 hit of ``duration_ns``."""
        state = self._current()
        with state.lock:
            self._count(state, duration_ns)

    def record(self, start_ns: int, clock: Callable[[], int]) -> None:
        """Count one timed L1 hit that started at ``start_ns`` on ``clock`` and ends now.

        The clock is read once the lock is held, so the time spent waiting for it, which the caller pays before it
        gets its value, is in the duration.
        """
        state = self._current()
        with state.lock:
            self._count(state, clock() - start_ns)

    def take(self) -> dict[str, str] | None:
        """The report's headers, emptying it in the same step; None while its duration sum is 0.

        A report whose sum is 0 stays: all-zero buckets are never attached.
        """
        state = self._current()
        with state.lock:
            if state.sum < 1:
                return None
            buckets, total = state.buckets, state.sum
            state.buckets = [0] * BUCKETS
            state.sum = 0
        return {
            BUCKETS_HEADER: ",".join(map(str, buckets)),
            SUM_HEADER: str(total),
            ONE_IN_HEADER: _ONE_IN_VALUE,
        }


__all__ = ["ONE_IN", "SAMPLE_BITS", "L1LatencyReport", "bucket_index", "clock_qualifies"]
