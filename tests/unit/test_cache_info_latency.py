"""Unit tests for cache_info() latency metrics.

This module tests the L2 latency tracking and timestamp features in CacheInfo.
"""

from __future__ import annotations

import threading
import time

import pytest

from cachekit.decorators.wrapper import _FunctionStats


@pytest.mark.unit
def test_cache_info_l2_latency_tracking():
    """Verify L2 latency is accumulated and averaged correctly."""
    stats = _FunctionStats()

    # Record some L2 hits with known latencies
    stats.record_l2_hit(5.0)  # 5ms
    stats.record_l2_hit(3.0)  # 3ms
    stats.record_l2_hit(4.0)  # 4ms

    info = stats.get_info()

    assert info.l2_hits == 3
    assert info.l2_avg_latency_ms == pytest.approx(4.0)  # (5+3+4)/3 = 4.0
    assert info.last_operation_at is not None


@pytest.mark.unit
def test_cache_info_no_l2_hits():
    """Verify L2 average is 0.0 when no L2 hits recorded."""
    stats = _FunctionStats()

    stats.record_l1_hit()
    stats.record_miss()

    info = stats.get_info()

    assert info.l2_hits == 0
    assert info.l2_avg_latency_ms == 0.0


@pytest.mark.unit
def test_cache_info_mixed_hits():
    """Verify L1 and L2 hits are tracked independently."""
    stats = _FunctionStats()

    stats.record_l1_hit()
    stats.record_l1_hit()
    stats.record_l2_hit(10.0)
    stats.record_l2_hit(20.0)
    stats.record_miss()

    info = stats.get_info()

    assert info.hits == 4
    assert info.misses == 1
    assert info.l1_hits == 2
    assert info.l2_hits == 2
    assert info.l2_avg_latency_ms == pytest.approx(15.0)


@pytest.mark.unit
def test_cache_info_last_operation_updated():
    """Verify last_operation_at timestamp is updated on every operation."""
    stats = _FunctionStats()

    t1 = time.time()
    stats.record_l1_hit()
    info1 = stats.get_info()

    time.sleep(0.01)  # Small delay

    stats.record_l2_hit(5.0)
    info2 = stats.get_info()

    assert info1.last_operation_at is not None
    assert info2.last_operation_at is not None
    assert info2.last_operation_at > info1.last_operation_at
    assert info1.last_operation_at >= t1  # Should be after test start


@pytest.mark.unit
def test_cache_info_clear_resets_all():
    """Verify clear() resets all metrics including latency."""
    stats = _FunctionStats()

    stats.record_l2_hit(10.0)
    stats.record_l1_hit()

    info_before = stats.get_info()
    assert info_before.hits > 0
    assert info_before.l2_avg_latency_ms > 0

    stats.clear()

    info_after = stats.get_info()
    assert info_after.hits == 0
    assert info_after.misses == 0
    assert info_after.l2_hits == 0
    assert info_after.l2_avg_latency_ms == 0.0
    assert info_after.last_operation_at is None


@pytest.mark.unit
def test_cache_info_thread_safety():
    """Verify thread-safe concurrent recording."""
    stats = _FunctionStats()

    def record_hits(count: int):
        for i in range(count):
            stats.record_l2_hit(1.0 + i)

    threads = [threading.Thread(target=record_hits, args=(100,)) for _ in range(5)]

    for t in threads:
        t.start()
    for t in threads:
        t.join()

    info = stats.get_info()
    assert info.l2_hits == 500
    # Average should be roughly 1 + (0+1+2+...+99)/100 = 1 + 49.5 = 50.5
    # But across 5 threads, distribution varies. Just check it's reasonable.
    assert 40 < info.l2_avg_latency_ms < 60


@pytest.mark.unit
def test_cache_info_deprecated_record_hit():
    """Verify deprecated record_hit() still works for backward compatibility."""
    stats = _FunctionStats()

    # Test L1 hit via deprecated API
    stats.record_hit("l1")
    info1 = stats.get_info()
    assert info1.l1_hits == 1
    assert info1.l2_hits == 0
    assert info1.last_operation_at is not None

    # Test L2 hit via deprecated API (no latency tracking)
    stats.record_hit("l2")
    info2 = stats.get_info()
    assert info2.l1_hits == 1
    assert info2.l2_hits == 1
    assert info2.l2_avg_latency_ms == 0.0  # No latency tracked via deprecated API


@pytest.mark.unit
def test_cache_info_l2_average_incremental():
    """Verify L2 average updates incrementally with each hit."""
    stats = _FunctionStats()

    # First hit: avg = 10.0
    stats.record_l2_hit(10.0)
    info1 = stats.get_info()
    assert info1.l2_avg_latency_ms == pytest.approx(10.0)

    # Second hit: avg = (10 + 20) / 2 = 15.0
    stats.record_l2_hit(20.0)
    info2 = stats.get_info()
    assert info2.l2_avg_latency_ms == pytest.approx(15.0)

    # Third hit: avg = (10 + 20 + 30) / 3 = 20.0
    stats.record_l2_hit(30.0)
    info3 = stats.get_info()
    assert info3.l2_avg_latency_ms == pytest.approx(20.0)


@pytest.mark.unit
def test_cache_info_miss_updates_timestamp():
    """Verify record_miss() updates last_operation_at."""
    stats = _FunctionStats()

    t1 = time.time()
    stats.record_miss()
    info = stats.get_info()

    assert info.misses == 1
    assert info.last_operation_at is not None
    assert info.last_operation_at >= t1
