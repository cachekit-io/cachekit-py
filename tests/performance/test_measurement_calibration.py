"""The measurement instrument must be honest before any benchmark is trusted.

This is the perf suite's self-check: if the timer cannot accurately measure a known
1 ms sleep, every other number it produces is suspect. Marked `performance` (not
`slow`), so it stays out of the CI memory-invariant gate and runs locally / on demand.

Only the low side fails the test: a sleep never returns early, so a short reading is a
broken timer on any host. The high side (overshoot past 3x) is reported, not asserted: a
loaded host overshoots a 1 ms sleep by itself, which says nothing about the instrument.
"""

from __future__ import annotations

import pytest

from .measurement_env import calibrate_timer


@pytest.mark.performance
def test_timer_is_honest(record_property) -> None:
    """A known 1 ms sleep must never measure short; the full window is reported.

    Under-counting would report near-zero and fails here. Timing-scope pollution (setup
    leaking into the timed region, inflating 1 ms to 10-30 ms) shows as a ✗ in the report.
    """
    trustworthy, message, median_ns = calibrate_timer(sleep_ms=1.0)
    print(f"\n{message}")
    record_property("sleep_1ms_median_ns", round(median_ns))
    record_property("within_window", trustworthy)
    assert median_ns >= 0.8 * 1_000_000, message
