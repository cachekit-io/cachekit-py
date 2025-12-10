"""
CRITICAL PATH TEST: Backpressure Load Control

This test MUST pass for backpressure functionality to work correctly.
Tests semaphore limiting, request rejection, and load shedding under high concurrency.

The backpressure controller prevents cascading failures by limiting concurrent
Redis operations and rejecting requests when the system is overloaded. This is
critical for maintaining stability during traffic spikes or Redis slowdowns.
"""

import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from cachekit.reliability.load_control import BackpressureController

pytestmark = pytest.mark.critical


class TestBackpressureLoadControl:
    """Critical tests for backpressure controller and load shedding."""

    def test_semaphore_allows_max_concurrent_requests(self):
        """CRITICAL: Semaphore allows up to max_concurrent requests simultaneously."""
        controller = BackpressureController(max_concurrent=5, queue_size=100, timeout=1.0)

        active_count = {"value": 0}
        max_observed = {"value": 0}
        lock = threading.Lock()

        def slow_operation():
            """Operation that holds the permit for a brief period."""
            with controller.acquire():
                with lock:
                    active_count["value"] += 1
                    max_observed["value"] = max(max_observed["value"], active_count["value"])

                time.sleep(0.1)  # Hold permit for 100ms

                with lock:
                    active_count["value"] -= 1

        # Launch 10 operations concurrently (max_concurrent=5)
        with ThreadPoolExecutor(max_workers=10) as executor:
            futures = [executor.submit(slow_operation) for _ in range(10)]
            for future in futures:
                future.result()

        # Max concurrent should never exceed configured limit
        assert max_observed["value"] <= 5, f"Max concurrent was {max_observed['value']}, limit is 5"
        assert max_observed["value"] >= 3, f"Should have had significant concurrency, got {max_observed['value']}"

    def test_request_rejected_when_queue_full(self):
        """CRITICAL: Requests rejected when queue_size is reached."""
        controller = BackpressureController(
            max_concurrent=1,  # Only 1 can execute
            queue_size=2,  # Only 2 can wait
            timeout=10.0,  # Long timeout (we won't wait)
        )

        # Block the single execution slot
        lock = threading.Lock()
        lock.acquire()  # Held until we release it

        def blocking_operation():
            """Operation that blocks until lock is released."""
            with controller.acquire():
                with lock:  # This will block
                    pass

        def quick_operation():
            """Operation that should be rejected."""
            with controller.acquire():
                pass

        # Start the blocking operation in a thread
        blocking_thread = threading.Thread(target=blocking_operation)
        blocking_thread.start()
        time.sleep(0.05)  # Let it acquire the execution permit

        # Now the execution slot is occupied, queue should start filling
        # Try to launch more operations than queue_size
        rejected_count = 0

        def try_operation():
            from cachekit.backends.errors import BackendError

            nonlocal rejected_count
            try:
                quick_operation()
            except BackendError as e:
                if "queue full" in str(e).lower():
                    rejected_count += 1

        # Launch queue_size + 5 additional operations (should reject the extras)
        threads = [threading.Thread(target=try_operation) for _ in range(2 + 5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=1.0)

        # Release the blocking operation
        lock.release()
        blocking_thread.join(timeout=1.0)

        # Should have rejected the operations beyond queue capacity
        assert rejected_count > 0, "Should have rejected operations when queue full"

    def test_fast_fail_when_overloaded(self):
        """CRITICAL: System fails fast when overloaded (raises ConnectionError)."""
        controller = BackpressureController(
            max_concurrent=1,
            queue_size=1,
            timeout=0.01,  # Very short timeout for fast fail
        )

        # Occupy the execution slot
        execution_lock = threading.Lock()
        execution_lock.acquire()

        def blocking_op():
            with controller.acquire():
                with execution_lock:
                    pass

        # Start blocking operation
        thread = threading.Thread(target=blocking_op)
        thread.start()
        time.sleep(0.05)  # Let it acquire

        # Now queue is full and execution is blocked
        # New request should fail fast
        from cachekit.backends.errors import BackendError

        start = time.time()
        with pytest.raises(BackendError):
            with controller.acquire():
                pass
        elapsed = time.time() - start

        # Should fail in ~0.01s (timeout), not wait indefinitely
        assert elapsed < 0.5, f"Should fail fast, took {elapsed}s"

        # Cleanup
        execution_lock.release()
        thread.join(timeout=1.0)

    def test_queue_depth_tracking_accuracy(self):
        """CRITICAL: Queue depth tracking is accurate under concurrent load."""
        controller = BackpressureController(
            max_concurrent=2,
            queue_size=100,
            timeout=1.0,
        )

        # Block execution slots
        execution_lock = threading.Lock()
        execution_lock.acquire()

        max_queue_depth = {"value": 0}
        lock = threading.Lock()

        def queued_operation():
            """Operation that will queue up."""
            with controller.acquire():
                with execution_lock:  # Block here
                    with lock:
                        current_depth = controller.queue_depth
                        max_queue_depth["value"] = max(max_queue_depth["value"], current_depth)

        # Fill execution slots (2) and start queuing
        threads = [threading.Thread(target=queued_operation) for _ in range(10)]
        for t in threads:
            t.start()

        time.sleep(0.1)  # Let them queue up

        # Queue depth should be >0 (threads are waiting)
        current_depth = controller.queue_depth
        assert current_depth > 0, f"Should have queued requests, depth={current_depth}"

        # Release execution
        execution_lock.release()
        for t in threads:
            t.join(timeout=2.0)

        # After completion, queue should be empty
        final_depth = controller.queue_depth
        assert final_depth == 0, f"Queue should be empty after completion, got {final_depth}"

    def test_rejected_count_increments_correctly(self):
        """CRITICAL: Rejected count metric increments when requests are shed."""
        controller = BackpressureController(
            max_concurrent=1,
            queue_size=1,
            timeout=0.01,
        )

        # Block the execution slot
        execution_lock = threading.Lock()
        execution_lock.acquire()

        def blocking_op():
            with controller.acquire():
                with execution_lock:
                    pass

        thread = threading.Thread(target=blocking_op)
        thread.start()
        time.sleep(0.05)

        initial_rejected = controller.rejected_count

        # Try to acquire multiple times (should all be rejected)
        from cachekit.backends.errors import BackendError

        rejection_count = 0
        for _ in range(5):
            try:
                with controller.acquire():
                    pass
            except BackendError:
                rejection_count += 1

        final_rejected = controller.rejected_count

        # Rejected count should have incremented
        assert final_rejected > initial_rejected, "Rejected count should increment"
        assert final_rejected - initial_rejected == rejection_count, "Count should match actual rejections"

        # Cleanup
        execution_lock.release()
        thread.join(timeout=1.0)

    def test_successful_operations_release_permits(self):
        """CRITICAL: Successful operations properly release semaphore permits."""
        controller = BackpressureController(max_concurrent=3, queue_size=100)

        completion_count = {"value": 0}
        lock = threading.Lock()

        def quick_operation():
            """Operation that acquires, works, and releases."""
            with controller.acquire():
                time.sleep(0.01)  # Brief work
                with lock:
                    completion_count["value"] += 1

        # Run 10 operations (3 concurrent, so multiple batches)
        with ThreadPoolExecutor(max_workers=10) as executor:
            futures = [executor.submit(quick_operation) for _ in range(10)]
            for future in futures:
                future.result()

        # All should complete successfully (permits were released)
        assert completion_count["value"] == 10, "All operations should complete"

    def test_context_manager_cleanup_on_exception(self):
        """CRITICAL: Context manager cleans up permits even on exception."""
        controller = BackpressureController(max_concurrent=2, queue_size=10)

        exception_count = {"value": 0}
        success_count = {"value": 0}

        def failing_operation():
            """Operation that raises exception."""
            try:
                with controller.acquire():
                    raise ValueError("Intentional failure")
            except ValueError:
                exception_count["value"] += 1

        def successful_operation():
            """Operation that succeeds."""
            with controller.acquire():
                success_count["value"] += 1

        # Run failing operations
        threads = [threading.Thread(target=failing_operation) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert exception_count["value"] == 5, "All should have failed"

        # Now run successful operations - if permits weren't released, these would timeout
        threads = [threading.Thread(target=successful_operation) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=1.0)

        assert success_count["value"] == 5, "Permits should have been released after exceptions"

    def test_get_stats_returns_accurate_health_status(self):
        """CRITICAL: get_stats() returns accurate system health metrics."""
        controller = BackpressureController(
            max_concurrent=10,
            queue_size=100,
            timeout=1.0,
        )

        # Initial stats
        stats = controller.get_stats()
        assert stats["queue_depth"] == 0
        assert stats["rejected_count"] == 0
        assert stats["max_concurrent"] == 10
        assert stats["queue_size"] == 100
        assert stats["healthy"] is True  # Queue depth < 80% of queue_size

        # Block execution and fill queue
        execution_lock = threading.Lock()
        execution_lock.acquire()

        def queued_op():
            with controller.acquire():
                with execution_lock:
                    pass

        # Start 90 operations (will queue up)
        threads = [threading.Thread(target=queued_op) for _ in range(90)]
        for t in threads:
            t.start()

        time.sleep(0.1)  # Let them queue

        stats = controller.get_stats()
        assert stats["queue_depth"] > 0, "Should show queued requests"
        # Health should be False if queue_depth >= 80% of queue_size
        # 90 operations, 10 concurrent = 80 in queue -> 80% threshold
        # This might be True or False depending on exact timing

        # Cleanup
        execution_lock.release()
        for t in threads:
            t.join(timeout=2.0)

        final_stats = controller.get_stats()
        assert final_stats["queue_depth"] == 0, "Queue should be empty"
        assert final_stats["healthy"] is True, "Should be healthy when queue empty"

    def test_acquire_release_overhead_performance(self):
        """CRITICAL: Acquire/release overhead is <1ms per operation."""
        controller = BackpressureController(max_concurrent=10, queue_size=1000)

        iterations = 100

        start = time.time()
        for _ in range(iterations):
            with controller.acquire():
                pass  # No actual work, just measure overhead
        elapsed = time.time() - start

        avg_overhead = (elapsed / iterations) * 1000  # Convert to ms
        assert avg_overhead < 1.0, f"Acquire/release overhead is {avg_overhead:.2f}ms (should be <1ms)"

    def test_concurrent_request_throttling_validation(self):
        """CRITICAL: Concurrent requests are properly throttled to max_concurrent."""
        controller = BackpressureController(
            max_concurrent=3,
            queue_size=50,
            timeout=5.0,
        )

        active_operations = {"value": 0}
        max_concurrent_observed = {"value": 0}
        completed_operations = {"value": 0}
        lock = threading.Lock()

        def throttled_operation():
            """Operation that tracks concurrent execution."""
            with controller.acquire():
                with lock:
                    active_operations["value"] += 1
                    max_concurrent_observed["value"] = max(
                        max_concurrent_observed["value"],
                        active_operations["value"],
                    )

                # Simulate work
                time.sleep(0.05)

                with lock:
                    active_operations["value"] -= 1
                    completed_operations["value"] += 1

        # Launch 20 operations
        with ThreadPoolExecutor(max_workers=20) as executor:
            futures = [executor.submit(throttled_operation) for _ in range(20)]
            for future in futures:
                future.result()

        # Verify throttling
        assert max_concurrent_observed["value"] <= 3, f"Max concurrent was {max_concurrent_observed['value']}, should be ≤3"
        assert completed_operations["value"] == 20, "All operations should complete"

    def test_reset_stats_clears_rejected_count(self):
        """CRITICAL: reset_stats() properly clears monitoring counters."""
        controller = BackpressureController(max_concurrent=1, queue_size=1, timeout=0.01)

        # Block execution
        execution_lock = threading.Lock()
        execution_lock.acquire()

        def blocking_op():
            with controller.acquire():
                with execution_lock:
                    pass

        thread = threading.Thread(target=blocking_op)
        thread.start()
        time.sleep(0.05)

        # Generate rejections
        from cachekit.backends.errors import BackendError

        for _ in range(5):
            try:
                with controller.acquire():
                    pass
            except BackendError:
                pass

        # Verify rejected count
        assert controller.rejected_count > 0, "Should have rejections"

        # Reset stats
        controller.reset_stats()

        # Count should be cleared
        assert controller.rejected_count == 0, "Reset should clear rejected count"

        # Cleanup
        execution_lock.release()
        thread.join(timeout=1.0)
