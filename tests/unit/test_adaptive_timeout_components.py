"""Unit tests for adaptive timeout components.

This module tests the AdaptiveTimeout and AdaptiveTimeoutManager classes
that were extracted from reliability_enhanced.py. These tests focus on
the core timeout calculation logic, load factor computation, and thread
safety under concurrent operations.
"""

import time
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from cachekit.reliability.adaptive_timeout import AdaptiveTimeout, AdaptiveTimeoutManager


class TestAdaptiveTimeout:
    """Test the AdaptiveTimeout class."""

    def test_init_default_values(self):
        """Test AdaptiveTimeout initialization with default values."""
        timeout = AdaptiveTimeout()

        assert timeout.window_size == 1000
        assert timeout.percentile == 95.0
        assert timeout.min_timeout == 1.0
        assert timeout.max_timeout == 30.0
        assert len(timeout._durations) == 0

    def test_init_custom_values(self):
        """Test AdaptiveTimeout initialization with custom values."""
        timeout = AdaptiveTimeout(window_size=500, percentile=90.0, min_timeout=0.5, max_timeout=10.0)

        assert timeout.window_size == 500
        assert timeout.percentile == 90.0
        assert timeout.min_timeout == 0.5
        assert timeout.max_timeout == 10.0

    def test_record_duration(self):
        """Test recording operation durations."""
        timeout = AdaptiveTimeout(window_size=10)

        # Record some durations
        durations = [0.1, 0.2, 0.15, 0.3, 0.25]
        for duration in durations:
            timeout.record_duration(duration)

        assert len(timeout._durations) == 5
        assert list(timeout._durations) == durations

    def test_sliding_window_behavior(self):
        """Test that deque maintains sliding window with maxlen."""
        timeout = AdaptiveTimeout(window_size=3)

        # Add more durations than window size
        for i in range(5):
            timeout.record_duration(i * 0.1)

        # Should only keep the last 3
        assert len(timeout._durations) == 3
        # Use pytest.approx for floating point comparison
        import pytest

        assert list(timeout._durations) == pytest.approx([0.2, 0.3, 0.4])

    def test_get_timeout_insufficient_data(self):
        """Test timeout calculation with insufficient data."""
        timeout = AdaptiveTimeout(min_timeout=0.5)

        # With no data
        assert timeout.get_timeout() == 1.0  # 2x min_timeout

        # With some data but less than 10 samples
        for _ in range(5):
            timeout.record_duration(0.1)

        assert timeout.get_timeout() == 1.0  # Still 2x min_timeout

    def test_get_timeout_percentile_calculation(self):
        """Test timeout calculation using percentile method."""
        timeout = AdaptiveTimeout(window_size=100, percentile=95.0, min_timeout=0.1, max_timeout=5.0)

        # Record many fast operations (10ms each)
        for _ in range(20):
            timeout.record_duration(0.01)

        # P95 of 10ms operations = 10ms
        # With 50% buffer = 15ms
        # But min_timeout is 100ms, so should return 100ms
        calculated_timeout = timeout.get_timeout()
        assert calculated_timeout == 0.1  # min_timeout

    def test_get_timeout_with_varied_durations(self):
        """Test timeout calculation with varied operation durations."""
        timeout = AdaptiveTimeout(window_size=100, percentile=95.0, min_timeout=0.05, max_timeout=5.0)

        # Record mix of fast and slow operations
        # 90% fast (10ms), 10% slow (500ms)
        for _ in range(18):
            timeout.record_duration(0.01)  # 10ms
        for _ in range(2):
            timeout.record_duration(0.5)  # 500ms

        # P95 should be around 500ms
        # With 50% buffer = 750ms
        calculated_timeout = timeout.get_timeout()
        assert calculated_timeout > 0.5  # Should be higher than 500ms
        assert calculated_timeout <= 5.0  # But capped at max_timeout

    def test_get_timeout_bounds_enforcement(self):
        """Test that timeout values are properly bounded."""
        timeout = AdaptiveTimeout(window_size=50, percentile=95.0, min_timeout=1.0, max_timeout=3.0)

        # Record very fast operations
        for _ in range(20):
            timeout.record_duration(0.001)  # 1ms

        # Should be clamped to min_timeout
        assert timeout.get_timeout() == 1.0

        # Record very slow operations
        for _ in range(20):
            timeout.record_duration(10.0)  # 10 seconds

        # Should be clamped to max_timeout
        assert timeout.get_timeout() == 3.0

    def test_thread_safety(self):
        """Test thread safety of AdaptiveTimeout operations."""
        timeout = AdaptiveTimeout(window_size=1000)

        def record_durations():
            for i in range(100):
                timeout.record_duration(i * 0.001)

        def get_timeouts():
            timeouts = []
            for _ in range(50):
                timeouts.append(timeout.get_timeout())
            return timeouts

        # Run concurrent operations
        with ThreadPoolExecutor(max_workers=4) as executor:
            # Start recording from multiple threads
            record_futures = [executor.submit(record_durations) for _ in range(2)]

            # Start getting timeouts from multiple threads
            get_futures = [executor.submit(get_timeouts) for _ in range(2)]

            # Wait for all to complete
            for future in record_futures + get_futures:
                future.result()

        # Verify final state is consistent
        assert len(timeout._durations) > 0
        final_timeout = timeout.get_timeout()
        assert final_timeout > 0


class TestAdaptiveTimeoutManager:
    """Test the AdaptiveTimeoutManager class."""

    def test_init_default_values(self):
        """Test AdaptiveTimeoutManager initialization with defaults."""
        manager = AdaptiveTimeoutManager()

        assert manager.base_lock_timeout == 10.0
        assert manager.base_blocking_timeout == 5.0
        assert manager.min_lock_timeout == 2.0
        assert manager.max_lock_timeout == 60.0
        assert manager.min_blocking_timeout == 1.0
        assert manager.max_blocking_timeout == 30.0
        assert manager.adaptation_rate == 0.1
        assert manager._total_operations == 0
        assert manager._successful_operations == 0

    def test_init_custom_values(self):
        """Test AdaptiveTimeoutManager initialization with custom values."""
        manager = AdaptiveTimeoutManager(
            base_lock_timeout=20.0, base_blocking_timeout=10.0, min_lock_timeout=5.0, max_lock_timeout=120.0, adaptation_rate=0.2
        )

        assert manager.base_lock_timeout == 20.0
        assert manager.base_blocking_timeout == 10.0
        assert manager.min_lock_timeout == 5.0
        assert manager.max_lock_timeout == 120.0
        assert manager.adaptation_rate == 0.2

    def test_record_lock_operation_basic(self):
        """Test recording basic lock operation data."""
        manager = AdaptiveTimeoutManager()

        # Record successful operation
        manager.record_lock_operation(duration=0.05, success=True)

        assert manager._total_operations == 1
        assert manager._successful_operations == 1
        assert len(manager._operation_durations) == 1
        assert len(manager._success_rates) == 1
        assert len(manager._contention_factors) == 1

    def test_record_lock_operation_with_contention_factor(self):
        """Test recording lock operation with explicit contention factor."""
        manager = AdaptiveTimeoutManager()

        # Record operation with explicit contention factor
        manager.record_lock_operation(duration=0.1, success=True, contention_factor=0.7)

        assert manager._contention_factors[-1] == 0.7

    def test_record_lock_operation_auto_contention_factor(self):
        """Test automatic contention factor calculation."""
        manager = AdaptiveTimeoutManager(base_blocking_timeout=1.0)

        # Record operation without explicit contention factor
        manager.record_lock_operation(duration=0.5)  # 500ms duration

        # Contention factor should be duration / base_blocking_timeout
        # 0.5 / 1.0 = 0.5
        assert manager._contention_factors[-1] == 0.5

    def test_record_lock_operation_contention_factor_clamping(self):
        """Test contention factor is clamped to 1.0."""
        manager = AdaptiveTimeoutManager(base_blocking_timeout=1.0)

        # Record operation with duration > base_blocking_timeout
        manager.record_lock_operation(duration=2.0)  # 2 seconds

        # Contention factor should be clamped to 1.0
        assert manager._contention_factors[-1] == 1.0

    def test_calculate_load_factor_insufficient_data(self):
        """Test load factor calculation with insufficient data."""
        manager = AdaptiveTimeoutManager()

        # With no data
        load_factor = manager._calculate_load_factor()
        assert load_factor == 1.0

        # With insufficient data (< 5 operations)
        for _ in range(3):
            manager.record_lock_operation(duration=0.1)

        load_factor = manager._calculate_load_factor()
        assert load_factor == 1.0

    def test_calculate_load_factor_with_data(self):
        """Test load factor calculation with sufficient data."""
        manager = AdaptiveTimeoutManager()

        # Record operations with consistent performance
        for _ in range(10):
            manager.record_lock_operation(
                duration=0.1,  # 100ms operations
                success=True,
                contention_factor=0.2,
            )

        load_factor = manager._calculate_load_factor()
        # Should be around 1.0 for normal performance
        assert 0.5 <= load_factor <= 2.0

    def test_calculate_load_factor_high_duration(self):
        """Test load factor calculation with slow operations."""
        manager = AdaptiveTimeoutManager()

        # Record slow operations
        for _ in range(10):
            manager.record_lock_operation(
                duration=1.0,  # 1 second operations (slow)
                success=True,
                contention_factor=0.1,
            )

        load_factor = manager._calculate_load_factor()
        # Should be higher than 1.0 due to slow operations
        assert load_factor > 1.0

    def test_calculate_load_factor_high_contention(self):
        """Test load factor calculation with high contention."""
        manager = AdaptiveTimeoutManager()

        # Record operations with high contention
        for _ in range(10):
            manager.record_lock_operation(
                duration=0.1,
                success=True,
                contention_factor=0.9,  # High contention
            )

        load_factor = manager._calculate_load_factor()
        # Should be higher than 1.0 due to high contention
        assert load_factor > 1.0

    def test_calculate_load_factor_failures(self):
        """Test load factor calculation with operation failures."""
        manager = AdaptiveTimeoutManager()

        # Record mix of successful and failed operations
        for i in range(10):
            success = i < 5  # 50% success rate
            manager.record_lock_operation(duration=0.1, success=success, contention_factor=0.2)

        load_factor = manager._calculate_load_factor()
        # Should be higher than 1.0 due to failures
        assert load_factor > 1.0

    def test_update_adaptive_timeouts(self):
        """Test adaptive timeout updates based on load factor."""
        manager = AdaptiveTimeoutManager(
            base_lock_timeout=10.0,
            base_blocking_timeout=5.0,
            adaptation_rate=1.0,  # Immediate adaptation for testing
        )

        initial_lock, initial_blocking = manager.get_lock_timeouts()
        assert initial_lock == 10.0
        assert initial_blocking == 5.0

        # Record slow operations to increase load factor
        for _ in range(10):
            manager.record_lock_operation(duration=2.0)  # Very slow

        new_lock, new_blocking = manager.get_lock_timeouts()
        # Timeouts should increase due to high load factor
        assert new_lock > initial_lock
        assert new_blocking > initial_blocking

    def test_get_lock_timeouts_bounds_enforcement(self):
        """Test that lock timeouts respect min/max bounds."""
        manager = AdaptiveTimeoutManager(
            base_lock_timeout=10.0,
            base_blocking_timeout=5.0,
            min_lock_timeout=8.0,
            max_lock_timeout=15.0,
            min_blocking_timeout=3.0,
            max_blocking_timeout=8.0,
            adaptation_rate=1.0,
        )

        # Record very fast operations (should lower timeouts)
        for _ in range(10):
            manager.record_lock_operation(duration=0.001)

        lock_timeout, blocking_timeout = manager.get_lock_timeouts()
        # Should be bounded by minimums
        assert lock_timeout >= 8.0
        assert blocking_timeout >= 3.0

        # Record very slow operations (should raise timeouts)
        for _ in range(20):
            manager.record_lock_operation(duration=10.0)

        lock_timeout, blocking_timeout = manager.get_lock_timeouts()
        # Should be bounded by maximums
        assert lock_timeout <= 15.0
        assert blocking_timeout <= 8.0

    def test_get_load_factor(self):
        """Test public load factor getter."""
        manager = AdaptiveTimeoutManager()

        # Initially should return 1.0
        assert manager.get_load_factor() == 1.0

        # Record some operations
        for _ in range(10):
            manager.record_lock_operation(duration=0.1)

        load_factor = manager.get_load_factor()
        assert isinstance(load_factor, float)
        assert load_factor > 0

    def test_get_stats(self):
        """Test statistics collection."""
        manager = AdaptiveTimeoutManager()

        # Get initial stats
        stats = manager.get_stats()

        assert "current_lock_timeout" in stats
        assert "current_blocking_timeout" in stats
        assert "base_lock_timeout" in stats
        assert "base_blocking_timeout" in stats
        assert "load_factor" in stats
        assert "success_rate" in stats
        assert "total_operations" in stats
        assert "data_points" in stats
        assert "avg_duration" in stats
        assert "avg_contention" in stats

        assert stats["total_operations"] == 0
        assert stats["success_rate"] == 1.0  # Default when no operations

        # Record some operations
        for i in range(5):
            manager.record_lock_operation(
                duration=0.1 + i * 0.01,
                success=i % 2 == 0,  # Alternate success/failure
            )

        stats = manager.get_stats()
        assert stats["total_operations"] == 5
        assert stats["data_points"] == 5
        assert 0 < stats["success_rate"] < 1  # Should be between 0 and 1
        assert stats["avg_duration"] > 0
        assert stats["avg_contention"] > 0

    def test_reset(self):
        """Test resetting manager to initial state."""
        manager = AdaptiveTimeoutManager()

        # Record some operations to change state
        for _ in range(10):
            manager.record_lock_operation(duration=1.0)

        # Verify state has changed
        assert manager._total_operations > 0
        assert len(manager._operation_durations) > 0

        # Reset and verify clean state
        with patch("cachekit.reliability.adaptive_timeout.logger") as mock_logger:
            manager.reset()
            mock_logger.info.assert_called_once_with("AdaptiveTimeoutManager reset to base values")

        assert manager._total_operations == 0
        assert manager._successful_operations == 0
        assert len(manager._operation_durations) == 0
        assert len(manager._contention_factors) == 0
        assert len(manager._success_rates) == 0
        assert manager._current_lock_timeout == manager.base_lock_timeout
        assert manager._current_blocking_timeout == manager.base_blocking_timeout

    def test_thread_safety_concurrent_operations(self):
        """Test thread safety under concurrent load."""
        manager = AdaptiveTimeoutManager()

        def record_operations():
            for i in range(50):
                manager.record_lock_operation(
                    duration=0.01 + i * 0.001,
                    success=i % 3 != 0,  # 2/3 success rate
                    contention_factor=i * 0.01,
                )

        def get_stats():
            stats = []
            for _ in range(25):
                stats.append(manager.get_stats())
                time.sleep(0.001)  # Small delay to interleave operations
            return stats

        def get_timeouts():
            timeouts = []
            for _ in range(25):
                timeouts.append(manager.get_lock_timeouts())
                time.sleep(0.001)
            return timeouts

        # Run concurrent operations
        with ThreadPoolExecutor(max_workers=6) as executor:
            record_futures = [executor.submit(record_operations) for _ in range(2)]
            stats_futures = [executor.submit(get_stats) for _ in range(2)]
            timeout_futures = [executor.submit(get_timeouts) for _ in range(2)]

            # Wait for all operations to complete
            for future in record_futures + stats_futures + timeout_futures:
                _result = future.result()

        # Verify final state is consistent
        final_stats = manager.get_stats()
        assert final_stats["total_operations"] == 100  # 2 threads * 50 operations each
        assert len(manager._operation_durations) <= 100  # Respect window size

        # Verify timeouts are reasonable
        lock_timeout, blocking_timeout = manager.get_lock_timeouts()
        assert lock_timeout > 0
        assert blocking_timeout > 0

    def test_exponential_smoothing_adaptation(self):
        """Test exponential smoothing in timeout adaptation."""
        # Use high adaptation rate for faster testing
        manager = AdaptiveTimeoutManager(adaptation_rate=0.5)

        initial_lock, initial_blocking = manager.get_lock_timeouts()

        # Record enough operations to trigger load factor calculation (need >= 5)
        for _ in range(3):
            manager.record_lock_operation(duration=0.1)  # Start with normal operations

        # Record slow operation
        manager.record_lock_operation(duration=2.0)
        manager.record_lock_operation(duration=2.0)

        first_lock, first_blocking = manager.get_lock_timeouts()

        # Record another slow operation
        manager.record_lock_operation(duration=2.0)

        second_lock, second_blocking = manager.get_lock_timeouts()

        # Due to exponential smoothing, changes should be gradual
        # Each step should move closer to target but not reach it immediately
        assert first_lock > initial_lock
        assert second_lock > first_lock
        assert first_blocking > initial_blocking
        assert second_blocking > first_blocking

    @patch("cachekit.reliability.adaptive_timeout.logger")
    def test_significant_change_logging(self, mock_logger):
        """Test that significant timeout changes are logged."""
        manager = AdaptiveTimeoutManager(
            base_lock_timeout=10.0,
            adaptation_rate=1.0,  # Immediate adaptation
        )

        # Record operations that will cause significant change
        for _ in range(10):
            manager.record_lock_operation(duration=5.0)  # Very slow operations

        # Should have logged the change
        mock_logger.debug.assert_called()
        call_args = mock_logger.debug.call_args[0]
        assert "Adaptive timeouts adjusted" in call_args[0]
        assert "load_factor" in call_args[0]
