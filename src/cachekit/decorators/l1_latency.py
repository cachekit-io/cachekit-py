"""The L1 hit-latency report a CachekitIO backend sends to the server (protocol ``spec/saas-api.md#l1-hit-latency``).

A wrapper times a sample of its backed-mode calls, and adds each one that turns out to be an L1 hit to the
report of the backend its cache is configured with. The backend takes the report, whole, into the next request
it sends. Here, not in the backend package, because the wrapper imports it and must not load the HTTP client.
"""

from __future__ import annotations

import os
import threading
import time

# Bucket i (0-based) holds the durations up to 2**(i + 7) ns, the last up to the 60 s ceiling (spec table).
BUCKETS = 16
MAX_DURATION_NS = 60_000_000_000
# Every integer on the wire is at most 2**53 - 1, the largest a JavaScript number holds exactly.
_MAX_WIRE_INT = 2**53 - 1
# Each call is timed with probability 1/N, the same N for every backend. A power of two, so the draw is k random bits.
ONE_IN = 64
_ONE_IN_VALUE = str(ONE_IN)
# perf_counter qualifies only at 100 ns or finer (spec: Clock).
_MAX_CLOCK_RESOLUTION_S = 1e-7

BUCKETS_HEADER = "X-CacheKit-L1-Latency-Buckets"
SUM_HEADER = "X-CacheKit-L1-Latency-Sum-Ns"
ONE_IN_HEADER = "X-CacheKit-L1-Latency-One-In"
HEADERS = (BUCKETS_HEADER, SUM_HEADER, ONE_IN_HEADER)


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


class L1LatencyReport:
    """One CachekitIO backend's accumulator of timed L1 hits since its last report.

    ``add`` and ``take`` hold one lock, so a report's buckets and sum always describe the same hits and no two
    requests carry the same hit (API-85). A forked child starts empty (API-83): checked against the PID rather
    than by an at-fork hook, because uWSGI forks without running Python's hooks. The lock is replaced with the
    state, as a thread of the parent may have held it at the fork.

    Examples:
        >>> report = L1LatencyReport()
        >>> for d in (90, 300, 300, 40_000):
        ...     report.add(d)
        >>> report.take()
        {'X-CacheKit-L1-Latency-Buckets': '1,0,2,0,0,0,0,0,0,1,0,0,0,0,0,0', 'X-CacheKit-L1-Latency-Sum-Ns': '40690', 'X-CacheKit-L1-Latency-One-In': '64'}
        >>> report.take() is None
        True
    """

    __slots__ = ("_buckets", "_lock", "_pid", "_sum")

    def __init__(self) -> None:
        self._reset()

    def _reset(self) -> None:
        self._lock = threading.Lock()
        self._pid = os.getpid()
        self._buckets = [0] * BUCKETS
        self._sum = 0

    def add(self, duration_ns: int) -> None:
        """Count one timed L1 hit. A duration above 60 s is discarded (API-87)."""
        if duration_ns > MAX_DURATION_NS or duration_ns < 0:
            return
        if self._pid != os.getpid():
            self._reset()
        index = bucket_index(duration_ns)
        with self._lock:
            total = self._sum + duration_ns
            if total > _MAX_WIRE_INT:  # never reached in practice: about 150,000 hits of 60 s in one report
                return
            self._sum = total
            self._buckets[index] += 1

    def take(self) -> dict[str, str] | None:
        """The report's headers, emptying it in the same step; None while its duration sum is 0.

        A report whose sum is 0 stays: all-zero buckets are never attached.
        """
        if self._pid != os.getpid():
            self._reset()
        with self._lock:
            if self._sum < 1:
                return None
            buckets, total = self._buckets, self._sum
            self._buckets = [0] * BUCKETS
            self._sum = 0
        return {
            BUCKETS_HEADER: ",".join(map(str, buckets)),
            SUM_HEADER: str(total),
            ONE_IN_HEADER: _ONE_IN_VALUE,
        }


__all__ = ["HEADERS", "ONE_IN", "L1LatencyReport", "bucket_index", "clock_qualifies"]
