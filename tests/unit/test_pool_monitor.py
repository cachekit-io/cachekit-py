"""Unit tests for PoolMonitor."""

from __future__ import annotations

from unittest.mock import Mock

import pytest

from cachekit.monitoring.pool_monitor import PoolMonitor


@pytest.mark.parametrize(("draw", "sampled"), [(0.004, True), (0.006, False)])
def test_sampling_rate_below_one_percent_is_honoured(monkeypatch, draw, sampled):
    """A rate of 0.005 samples half a percent of operations, not none: the rate is never truncated to a whole percent."""
    monkeypatch.setattr("cachekit.monitoring.pool_monitor.random.random", lambda: draw)

    assert PoolMonitor(Mock(), sampling_rate=0.005)._should_sample() is sampled
