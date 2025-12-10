"""
CRITICAL PATH TEST: Adaptive Timeout Learning

This test MUST pass for adaptive timeout functionality to work correctly.
Tests P95 percentile calculation and timeout adaptation based on actual latencies.

The adaptive timeout system learns from Redis performance and adjusts timeouts
dynamically to prevent both premature timeouts (when Redis is slow but responsive)
and excessive waiting (when Redis is truly unresponsive).
"""

import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from cachekit.reliability.adaptive_timeout import (
    MIN_SAMPLES_FOR_LOAD_FACTOR,
    AdaptiveTimeout,
    AdaptiveTimeoutManager,
)

pytestmark = pytest.mark.critical


class TestAdaptiveTimeoutLearning:
    """Critical tests for adaptive timeout P95 calculation and learning."""

    def test_insufficient_data_returns_conservative_default(self):
        """CRITICAL: With <10 samples, return conservative default (2x min_timeout)."""
        timeout = AdaptiveTimeout(min_timeout=1.0, max_timeout=30.0)

        # No data yet
        result = timeout.get_timeout()
        assert result == 2.0, "Should return 2x min_timeout with no data"

        # Record 5 samples (< 10 minimum)
        for _ in range(5):
            timeout.record_duration(0.1)

        result = timeout.get_timeout()
        assert result == 2.0, "Should return 2x min_timeout with <10 samples"

        # Record 4 more (total 9, still < 10)
        for _ in range(4):
            timeout.record_duration(0.1)

        result = timeout.get_timeout()
        assert result == 2.0, "Should return 2x min_timeout with 9 samples"

        # One more sample (total 10) should trigger calculation
        timeout.record_duration(0.1)
        result = timeout.get_timeout()
        assert result != 2.0, "Should calculate P95 with 10+ samples"

    def test_p95_calculation_with_known_distribution(self):
        """CRITICAL: P95 percentile calculation returns correct value."""
        timeout = AdaptiveTimeout(
            percentile=95.0,
            min_timeout=0.0,  # No floor for testing
            max_timeout=100.0,
        )

        # Create known distribution: 100 samples from 0.0 to 0.99
        for i in range(100):
            timeout.record_duration(i / 100.0)

        result = timeout.get_timeout()

        # P95 of [0.00, 0.01, ..., 0.99] is 0.95
        # With 50% safety buffer: 0.95 * 1.5 = 1.425
        expected = 0.95 * 1.5
        assert abs(result - expected) < 0.01, f"Expected ~{expected}, got {result}"

    def test_timeout_adaptation_fast_redis(self):
        """CRITICAL: Fast Redis latencies produce short timeouts."""
        timeout = AdaptiveTimeout(
            percentile=95.0,
            min_timeout=0.01,
            max_timeout=30.0,
        )

        # Simulate fast Redis: 10-20ms responses
        for _ in range(50):
            timeout.record_duration(0.010)  # 10ms
        for _ in range(50):
            timeout.record_duration(0.020)  # 20ms

        result = timeout.get_timeout()

        # P95 should be ~0.020 (95th percentile of this distribution)
        # With 50% buffer: 0.020 * 1.5 = 0.030
        assert result < 0.05, f"Fast Redis should have short timeout, got {result}"
        assert result >= 0.01, f"Should respect min_timeout, got {result}"

    def test_timeout_adaptation_slow_redis(self):
        """CRITICAL: Slow Redis latencies produce longer timeouts."""
        timeout = AdaptiveTimeout(
            percentile=95.0,
            min_timeout=1.0,
            max_timeout=30.0,
        )

        # Simulate slow Redis: 0.5-1.5s responses
        for _ in range(50):
            timeout.record_duration(0.5)
        for _ in range(50):
            timeout.record_duration(1.5)

        result = timeout.get_timeout()

        # P95 should be ~1.5s
        # With 50% buffer: 1.5 * 1.5 = 2.25
        expected = 1.5 * 1.5
        assert abs(result - expected) < 0.1, f"Expected ~{expected}, got {result}"
        assert result >= 1.0, "Should respect min_timeout"

    def test_min_timeout_enforcement(self):
        """CRITICAL: Calculated timeout never goes below min_timeout."""
        timeout = AdaptiveTimeout(
            percentile=95.0,
            min_timeout=1.0,
            max_timeout=30.0,
        )

        # Record very fast operations (would calculate to < 1.0s)
        for _ in range(100):
            timeout.record_duration(0.001)  # 1ms

        result = timeout.get_timeout()

        # P95 would be 0.001 * 1.5 = 0.0015, but min is 1.0
        assert result == 1.0, f"Should enforce min_timeout=1.0, got {result}"

    def test_max_timeout_enforcement(self):
        """CRITICAL: Calculated timeout never exceeds max_timeout."""
        timeout = AdaptiveTimeout(
            percentile=95.0,
            min_timeout=1.0,
            max_timeout=30.0,
        )

        # Record very slow operations (would calculate to > 30.0s)
        for _ in range(100):
            timeout.record_duration(100.0)  # 100s

        result = timeout.get_timeout()

        # P95 would be 100 * 1.5 = 150, but max is 30.0
        assert result == 30.0, f"Should enforce max_timeout=30.0, got {result}"

    def test_window_size_limiting(self):
        """CRITICAL: Only last window_size samples are kept (older data evicted)."""
        timeout = AdaptiveTimeout(
            window_size=100,
            percentile=95.0,
            min_timeout=0.0,
            max_timeout=100.0,
        )

        # Fill window with slow operations
        for _ in range(100):
            timeout.record_duration(10.0)

        # At this point, P95 should reflect slow ops
        result1 = timeout.get_timeout()
        assert result1 > 10.0, "Should reflect slow operations"

        # Now record 100 fast operations (should replace old data)
        for _ in range(100):
            timeout.record_duration(0.01)

        # P95 should now reflect fast ops (old data evicted)
        result2 = timeout.get_timeout()
        assert result2 < 0.1, f"Should reflect new fast operations, got {result2}"
        assert result2 < result1, "New timeout should be much lower after window replacement"

    def test_fifty_percent_safety_buffer(self):
        """CRITICAL: 50% safety buffer is applied to P95 value."""
        timeout = AdaptiveTimeout(
            percentile=95.0,
            min_timeout=0.0,
            max_timeout=100.0,
        )

        # All operations take exactly 1.0s
        for _ in range(100):
            timeout.record_duration(1.0)

        result = timeout.get_timeout()

        # P95 of [1.0, 1.0, ...] is 1.0
        # With 50% buffer: 1.0 * 1.5 = 1.5
        expected = 1.0 * 1.5
        assert abs(result - expected) < 0.01, f"Expected {expected} (1.0 * 1.5), got {result}"

    def test_thread_safe_duration_recording(self):
        """CRITICAL: Duration recording is thread-safe under concurrent access."""
        import threading

        timeout = AdaptiveTimeout(
            window_size=1000,
            percentile=95.0,
            min_timeout=0.0,
            max_timeout=100.0,
        )

        def record_durations():
            """Record 100 durations from a thread."""
            for i in range(100):
                timeout.record_duration(i / 100.0)

        # Launch 10 threads, each recording 100 durations
        threads = [threading.Thread(target=record_durations) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # Should have exactly 1000 samples (window_size limit)
        # We can't directly inspect _durations, but we can verify it works
        result = timeout.get_timeout()
        assert result > 0, "Should calculate valid timeout after concurrent recording"

    def test_get_timeout_within_bounds(self):
        """CRITICAL: get_timeout() always returns values within configured bounds."""
        timeout = AdaptiveTimeout(
            percentile=95.0,
            min_timeout=5.0,
            max_timeout=20.0,
        )

        # Test with various distributions
        test_cases = [
            # (duration, description)
            (0.001, "Very fast operations"),
            (1.0, "Normal operations"),
            (10.0, "Medium operations"),
            (100.0, "Very slow operations"),
        ]

        for duration, description in test_cases:
            # Reset by creating new instance
            timeout = AdaptiveTimeout(
                percentile=95.0,
                min_timeout=5.0,
                max_timeout=20.0,
            )

            # Record 100 samples of this duration
            for _ in range(100):
                timeout.record_duration(duration)

            result = timeout.get_timeout()

            assert 5.0 <= result <= 20.0, f"{description}: timeout {result} should be within bounds [5.0, 20.0]"

    def test_p95_calculation_performance(self):
        """CRITICAL: P95 calculation completes in <10ms for 1000 samples."""
        timeout = AdaptiveTimeout(window_size=1000, percentile=95.0)

        # Fill window with data
        for i in range(1000):
            timeout.record_duration(i / 1000.0)

        # Measure calculation time
        start = time.time()
        for _ in range(100):  # Run 100 calculations
            _ = timeout.get_timeout()
        elapsed = time.time() - start

        avg_time = elapsed / 100
        assert avg_time < 0.01, f"P95 calculation took {avg_time * 1000:.2f}ms (should be <10ms)"

    def test_percentile_parameter_affects_calculation(self):
        """CRITICAL: Different percentile values produce different timeouts."""
        # Create two timeout calculators with different percentiles
        timeout_p50 = AdaptiveTimeout(
            percentile=50.0,  # Median
            min_timeout=0.0,
            max_timeout=100.0,
        )
        timeout_p95 = AdaptiveTimeout(
            percentile=95.0,  # P95
            min_timeout=0.0,
            max_timeout=100.0,
        )

        # Same data for both
        for i in range(100):
            duration = i / 100.0  # 0.00 to 0.99
            timeout_p50.record_duration(duration)
            timeout_p95.record_duration(duration)

        result_p50 = timeout_p50.get_timeout()
        result_p95 = timeout_p95.get_timeout()

        # P95 should be significantly higher than P50
        assert result_p95 > result_p50, f"P95 ({result_p95}) should exceed P50 ({result_p50})"
        # P50 of [0.00..0.99] is ~0.50, with buffer: 0.75
        # P95 of [0.00..0.99] is ~0.95, with buffer: 1.425
        assert abs(result_p50 - 0.75) < 0.1, f"P50 should be ~0.75, got {result_p50}"
        assert abs(result_p95 - 1.425) < 0.1, f"P95 should be ~1.425, got {result_p95}"

    def test_mixed_latency_distribution(self):
        """CRITICAL: Handles realistic mixed latency distributions correctly."""
        timeout = AdaptiveTimeout(
            percentile=95.0,
            min_timeout=0.01,
            max_timeout=30.0,
        )

        # Realistic distribution:
        # - 70% fast (10-20ms)
        # - 20% medium (50-100ms)
        # - 10% slow (200-500ms)
        for _ in range(70):
            timeout.record_duration(0.015)  # 15ms
        for _ in range(20):
            timeout.record_duration(0.075)  # 75ms
        for _ in range(10):
            timeout.record_duration(0.350)  # 350ms

        result = timeout.get_timeout()

        # P95 should catch the slow tail (~350ms)
        # With 50% buffer: ~525ms
        # But this is an approximation - the actual P95 depends on sorting
        # For 100 samples, P95 is the 95th value when sorted
        # Our distribution sorted: 70x[0.015], 20x[0.075], 10x[0.350]
        # 95th percentile index = 95, which falls in the slow bucket
        # So P95 ≈ 0.350, with buffer: 0.525
        assert result > 0.1, f"Should reflect slow tail, got {result}"
        assert result < 1.0, f"Should not be excessive, got {result}"


class TestAdaptiveTimeoutManager:
    """Critical tests for AdaptiveTimeoutManager lock-specific timeout management."""

    def test_manager_insufficient_data_returns_default_load_factor(self):
        """CRITICAL: With <5 samples, load factor returns 1.0 (default)."""
        manager = AdaptiveTimeoutManager(
            base_lock_timeout=10.0,
            base_blocking_timeout=5.0,
        )

        # No data yet
        load_factor = manager.get_load_factor()
        assert load_factor == 1.0, "Should return 1.0 with no data"

        # Record 3 samples (< 5 minimum)
        for _ in range(3):
            manager.record_lock_operation(duration=0.1, success=True)

        load_factor = manager.get_load_factor()
        assert load_factor == 1.0, f"Should return 1.0 with <{MIN_SAMPLES_FOR_LOAD_FACTOR} samples, got {load_factor}"

    def test_manager_load_factor_calculation_light_load(self):
        """CRITICAL: Light load (fast operations, low contention) keeps load factor near 1.0."""
        manager = AdaptiveTimeoutManager(
            base_lock_timeout=10.0,
            base_blocking_timeout=5.0,
        )

        # Record 10 fast, successful operations with low contention
        for _ in range(10):
            manager.record_lock_operation(
                duration=0.05,  # 50ms - very fast
                success=True,
                contention_factor=0.1,  # Low contention
            )

        load_factor = manager.get_load_factor()
        assert 0.5 <= load_factor <= 1.5, f"Light load should yield load_factor ~1.0, got {load_factor}"

    def test_manager_load_factor_calculation_heavy_load(self):
        """CRITICAL: Heavy load (slow operations, high contention, failures) increases load factor."""
        manager = AdaptiveTimeoutManager(
            base_lock_timeout=10.0,
            base_blocking_timeout=5.0,
        )

        # Record 20 slow operations with high contention and some failures
        for i in range(20):
            manager.record_lock_operation(
                duration=2.0,  # 2s - very slow (20x expected 100ms)
                success=(i % 5 != 0),  # 20% failure rate
                contention_factor=0.9,  # High contention
            )

        load_factor = manager.get_load_factor()
        assert load_factor > 2.0, f"Heavy load should yield load_factor > 2.0, got {load_factor}"
        assert load_factor <= 3.0, f"Load factor should be clamped at 3.0, got {load_factor}"

    def test_manager_timeout_adaptation_increases_on_load(self):
        """CRITICAL: Timeouts increase when load factor increases."""
        manager = AdaptiveTimeoutManager(
            base_lock_timeout=10.0,
            base_blocking_timeout=5.0,
            adaptation_rate=1.0,  # Immediate adaptation for testing
        )

        # Initial timeouts should be at base values
        lock_timeout, blocking_timeout = manager.get_lock_timeouts()
        assert lock_timeout == 10.0
        assert blocking_timeout == 5.0

        # Record slow operations to increase load
        for _ in range(20):
            manager.record_lock_operation(
                duration=1.0,  # 10x expected
                success=True,
                contention_factor=0.8,
            )

        # Timeouts should increase
        lock_timeout, blocking_timeout = manager.get_lock_timeouts()
        assert lock_timeout > 10.0, f"Lock timeout should increase under load, got {lock_timeout}"
        assert blocking_timeout > 5.0, f"Blocking timeout should increase under load, got {blocking_timeout}"

    def test_manager_timeout_bounds_enforcement(self):
        """CRITICAL: Timeouts respect min/max bounds even under extreme load."""
        manager = AdaptiveTimeoutManager(
            base_lock_timeout=10.0,
            base_blocking_timeout=5.0,
            min_lock_timeout=2.0,
            max_lock_timeout=60.0,
            min_blocking_timeout=1.0,
            max_blocking_timeout=30.0,
            adaptation_rate=1.0,  # Immediate adaptation
        )

        # Record extremely slow operations
        for _ in range(30):
            manager.record_lock_operation(
                duration=10.0,  # 100x expected (extreme)
                success=False,  # All failures
                contention_factor=1.0,  # Max contention
            )

        lock_timeout, blocking_timeout = manager.get_lock_timeouts()
        assert lock_timeout <= 60.0, f"Lock timeout must respect max_lock_timeout, got {lock_timeout}"
        assert blocking_timeout <= 30.0, f"Blocking timeout must respect max_blocking_timeout, got {blocking_timeout}"

    def test_manager_exponential_smoothing_gradual_adaptation(self):
        """CRITICAL: Exponential smoothing prevents abrupt timeout changes."""
        manager = AdaptiveTimeoutManager(
            base_lock_timeout=10.0,
            base_blocking_timeout=5.0,
            adaptation_rate=0.1,  # Slow adaptation (10% per update)
        )

        # Record one slow operation
        manager.record_lock_operation(duration=5.0, success=True, contention_factor=1.0)

        lock_timeout_1, _ = manager.get_lock_timeouts()

        # Record another slow operation
        manager.record_lock_operation(duration=5.0, success=True, contention_factor=1.0)

        lock_timeout_2, _ = manager.get_lock_timeouts()

        # Timeout should increase gradually, not jump immediately
        change = abs(lock_timeout_2 - lock_timeout_1)
        assert change < 5.0, f"Change should be gradual with adaptation_rate=0.1, got change={change}"

    def test_manager_contention_factor_auto_estimation(self):
        """CRITICAL: Contention factor auto-estimated from duration when not provided."""
        manager = AdaptiveTimeoutManager(base_blocking_timeout=5.0)

        # Record operation without explicit contention_factor
        manager.record_lock_operation(duration=2.5, success=True)

        stats = manager.get_stats()
        # Contention should be estimated as duration / base_blocking_timeout = 2.5 / 5.0 = 0.5
        assert stats["avg_contention"] == 0.5, f"Expected avg_contention=0.5, got {stats['avg_contention']}"

    def test_manager_get_stats_comprehensive(self):
        """CRITICAL: get_stats() returns all expected fields."""
        manager = AdaptiveTimeoutManager(
            base_lock_timeout=10.0,
            base_blocking_timeout=5.0,
        )

        # Record some operations
        for i in range(10):
            manager.record_lock_operation(
                duration=0.1 * (i + 1),
                success=(i % 2 == 0),  # 50% success rate
            )

        stats = manager.get_stats()

        # Verify all expected fields present
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

        # Verify values
        assert stats["total_operations"] == 10
        assert stats["data_points"] == 10
        assert stats["success_rate"] == 0.5, f"Expected 50% success rate, got {stats['success_rate']}"
        assert stats["avg_duration"] > 0

    def test_manager_reset_clears_state(self):
        """CRITICAL: reset() clears all tracking data and returns to base timeouts."""
        manager = AdaptiveTimeoutManager(
            base_lock_timeout=10.0,
            base_blocking_timeout=5.0,
            adaptation_rate=1.0,
        )

        # Record operations to change state
        for _ in range(20):
            manager.record_lock_operation(duration=2.0, success=False, contention_factor=0.9)

        # Verify state changed
        lock_timeout_before, _ = manager.get_lock_timeouts()
        stats_before = manager.get_stats()
        assert lock_timeout_before > 10.0, "Timeout should have increased"
        assert stats_before["total_operations"] == 20

        # Reset
        manager.reset()

        # Verify state cleared
        lock_timeout_after, blocking_timeout_after = manager.get_lock_timeouts()
        stats_after = manager.get_stats()

        assert lock_timeout_after == 10.0, f"Lock timeout should reset to base, got {lock_timeout_after}"
        assert blocking_timeout_after == 5.0, f"Blocking timeout should reset to base, got {blocking_timeout_after}"
        assert stats_after["total_operations"] == 0, "Total operations should reset to 0"
        assert stats_after["data_points"] == 0, "Data points should clear"
        assert stats_after["load_factor"] == 1.0, "Load factor should return to default"

    def test_manager_thread_safe_concurrent_recording(self):
        """CRITICAL: Thread-safe operation recording under concurrent access."""
        manager = AdaptiveTimeoutManager()

        def record_operations():
            for _ in range(50):
                manager.record_lock_operation(duration=0.1, success=True)

        # Run 4 threads concurrently, each recording 50 operations
        with ThreadPoolExecutor(max_workers=4) as executor:
            futures = [executor.submit(record_operations) for _ in range(4)]
            for future in futures:
                future.result()

        stats = manager.get_stats()
        assert stats["total_operations"] == 200, f"Expected 200 total operations, got {stats['total_operations']}"
        assert stats["data_points"] <= 100, "Window size should limit data points to 100"

    def test_manager_success_rate_tracking_accuracy(self):
        """CRITICAL: Success rate calculated correctly from mixed success/failure operations."""
        manager = AdaptiveTimeoutManager()

        # Record 7 successes and 3 failures (70% success rate)
        for i in range(10):
            manager.record_lock_operation(
                duration=0.1,
                success=(i < 7),  # First 7 succeed, last 3 fail
            )

        stats = manager.get_stats()
        assert stats["success_rate"] == 0.7, f"Expected 70% success rate, got {stats['success_rate']}"

    def test_manager_window_limiting_evicts_old_data(self):
        """CRITICAL: Window limiting evicts old data when maxlen exceeded."""
        manager = AdaptiveTimeoutManager(load_factor_window=10)

        # Record 20 operations (exceeds window size of 10)
        for i in range(20):
            manager.record_lock_operation(duration=float(i), success=True)

        stats = manager.get_stats()
        assert stats["data_points"] == 10, f"Window should limit to 10 data points, got {stats['data_points']}"

        # Avg duration should reflect only recent operations (10-19)
        # Expected avg: (10+11+12+13+14+15+16+17+18+19) / 10 = 145 / 10 = 14.5
        expected_avg = 14.5
        actual_avg = stats["avg_duration"]
        assert abs(actual_avg - expected_avg) < 0.01, f"Expected avg ~{expected_avg}, got {actual_avg}"
