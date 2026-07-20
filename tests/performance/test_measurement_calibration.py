"""The measurement instrument must be honest before any benchmark is trusted.

This is the perf suite's self-check: if the timer cannot accurately measure a known
1 ms sleep, every other number it produces is suspect. Marked `performance` (not
`slow`), so it stays out of the CI memory-invariant gate and runs locally / on demand.
"""

from __future__ import annotations

import pytest

from .measurement_env import calibrate_timer


@pytest.mark.performance
def test_timer_is_honest() -> None:
    """A known 1 ms sleep must measure within a sane window.

    Catches timing-scope pollution (setup leaking into the timed region) that would
    inflate a 1 ms operation to 10-30 ms, and under-counting that would report near-zero.
    """
    trustworthy, message = calibrate_timer(sleep_ms=1.0)
    print(f"\n{message}")
    assert trustworthy, message
