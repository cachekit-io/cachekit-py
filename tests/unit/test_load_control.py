"""Test load control and backpressure functionality."""

import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from unittest.mock import patch

import pytest

from cachekit.reliability.load_control import BackpressureController


class TestBackpressureController:
    """Test BackpressureController functionality."""

    def test_controller_initialization(self):
        """Test that controller initializes with correct parameters."""
        controller = BackpressureController(max_concurrent=50, queue_size=100, timeout=0.5)

        assert controller.max_concurrent == 50
        assert controller.queue_size == 100
        assert controller.timeout == 0.5
        assert controller._semaphore._value == 50
        assert controller._queue_depth == 0
        assert controller._rejected_count == 0

    def test_controller_default_parameters(self):
        """Test controller with default parameters."""
        controller = BackpressureController()

        assert controller.max_concurrent == 100
        assert controller.queue_size == 1000
        assert controller.timeout == 0.1
        assert controller._semaphore._value == 100

    def test_successful_operation(self):
        """Test successful operation acquisition and release."""
        controller = BackpressureController(max_concurrent=10)

        with controller.acquire():
            # Should be able to acquire permit and execute
            assert controller.queue_depth == 0  # Not in queue anymore
            # Semaphore should be reduced by 1
            assert controller._semaphore._value == 9

        # After context exit, semaphore should be restored
        assert controller._semaphore._value == 10
        assert controller.queue_depth == 0

    def test_queue_depth_limiting(self):
        """Test that requests are rejected when queue is full."""
        controller = BackpressureController(max_concurrent=1, queue_size=2, timeout=0.1)

        # Fill the semaphore with a long-running operation
        def blocking_operation():
            with controller.acquire():
                time.sleep(0.2)  # Hold the permit

        # Start background thread to hold the semaphore
        thread = threading.Thread(target=blocking_operation)
        thread.start()

        # Give time for thread to acquire semaphore
        time.sleep(0.05)

        # These requests should be able to join the queue
        exceptions = []

        def queue_request(request_id):
            from cachekit.backends.errors import BackendError

            try:
                with controller.acquire():
                    pass
            except BackendError as e:
                exceptions.append((request_id, str(e)))

        # Start requests that should fill the queue
        queue_threads = []
        for i in range(2):  # Should fit in queue
            t = threading.Thread(target=queue_request, args=(i,))
            queue_threads.append(t)
            t.start()

        time.sleep(0.05)  # Let them enter queue

        # This request should be rejected (queue full)
        from cachekit.backends.errors import BackendError

        with pytest.raises(BackendError, match="Request queue full"):
            with controller.acquire():
                pass

        # Verify rejection was counted (could be more than 1 due to timing)
        assert controller.rejected_count >= 1

        # Clean up
        thread.join()
        for t in queue_threads:
            t.join()

    def test_semaphore_timeout(self):
        """Test that requests timeout when unable to acquire semaphore."""
        controller = BackpressureController(max_concurrent=1, timeout=0.05)

        # Hold the semaphore with a blocking operation
        def blocking_operation():
            with controller.acquire():
                time.sleep(0.2)

        thread = threading.Thread(target=blocking_operation)
        thread.start()

        # Give time for thread to acquire semaphore
        time.sleep(0.02)

        # This should timeout
        from cachekit.backends.errors import BackendError

        with pytest.raises(BackendError, match="Failed to acquire permit"):
            with controller.acquire():
                pass

        # Verify rejection was counted
        assert controller.rejected_count == 1

        # Clean up
        thread.join()

    def test_context_manager_exception_handling(self):
        """Test that semaphore is properly released even when operation fails."""
        controller = BackpressureController(max_concurrent=10)

        initial_permits = controller._semaphore._value

        try:
            with controller.acquire():
                raise ValueError("Simulated operation failure")
        except ValueError:
            pass

        # Semaphore should be restored despite exception
        assert controller._semaphore._value == initial_permits
        assert controller.queue_depth == 0

    def test_concurrent_operations(self):
        """Test controller under high concurrency."""
        controller = BackpressureController(max_concurrent=5, queue_size=10, timeout=0.1)

        results = []
        exceptions = []

        def worker(worker_id):
            from cachekit.backends.errors import BackendError

            try:
                with controller.acquire():
                    time.sleep(0.05)  # Simulate work
                    results.append(worker_id)
            except BackendError as e:
                exceptions.append((worker_id, str(e)))

        # Launch many concurrent workers
        threads = []
        for i in range(20):  # More than max_concurrent + queue_size
            t = threading.Thread(target=worker, args=(i,))
            threads.append(t)
            t.start()

        # Wait for all threads
        for t in threads:
            t.join()

        # Some should succeed, some should be rejected
        assert len(results) > 0, "Some operations should succeed"
        assert len(exceptions) > 0, "Some operations should be rejected"
        assert len(results) + len(exceptions) == 20

        # Verify final state is clean
        assert controller.queue_depth == 0
        assert controller._semaphore._value == 5  # Back to max_concurrent

    def test_metrics_tracking(self):
        """Test that metrics are properly tracked."""
        controller = BackpressureController(max_concurrent=1, queue_size=1, timeout=0.01)

        # Mock the prometheus metrics
        with patch("cachekit.reliability.load_control.cache_operations") as mock_metrics:
            # Set queue size to 0 to trigger immediate rejection
            controller.queue_size = 0

            # This should fail due to queue size 0 (queue full immediately)
            from cachekit.backends.errors import BackendError

            try:
                with controller.acquire():
                    pass
            except BackendError:
                pass

            # Verify metrics were called
            assert mock_metrics.labels.called
            mock_metrics.labels.assert_called_with(
                operation="backpressure",
                status="rejected",
                serializer="",
                namespace="",
            )

    def test_properties_access(self):
        """Test property access methods."""
        controller = BackpressureController(max_concurrent=5)

        # Test initial values
        assert controller.queue_depth == 0
        assert controller.rejected_count == 0

        # Simulate queue entries and rejections
        with controller._lock:
            controller._queue_depth = 3
            controller._rejected_count = 5

        assert controller.queue_depth == 3
        assert controller.rejected_count == 5

    def test_reset_stats(self):
        """Test statistics reset functionality."""
        controller = BackpressureController()

        # Simulate some rejections
        with controller._lock:
            controller._rejected_count = 10

        assert controller.rejected_count == 10

        # Reset stats
        controller.reset_stats()

        assert controller.rejected_count == 0

    def test_thread_safety(self):
        """Test thread safety of counter operations."""
        from cachekit.backends.errors import BackendError

        controller = BackpressureController(max_concurrent=1, queue_size=0, timeout=0.001)

        def increment_rejections():
            # Simulate multiple threads trying to reject simultaneously
            for _ in range(20):
                try:
                    with controller.acquire():
                        time.sleep(0.001)  # Hold very briefly if acquired
                except BackendError:
                    pass  # Expected rejections (queue full or timeout)

        # Run multiple threads that will cause rejections
        threads = []
        for _ in range(5):
            t = threading.Thread(target=increment_rejections)
            threads.append(t)
            t.start()

        for t in threads:
            t.join()

        # The exact count depends on timing, but should be > 0 and consistent
        final_count = controller.rejected_count
        assert final_count > 0, f"Expected rejections but got {final_count}"

        # Multiple reads should return the same value (thread safety)
        assert controller.rejected_count == final_count
        assert controller.rejected_count == final_count

    def test_high_load_scenario(self):
        """Test behavior under extreme load conditions."""
        controller = BackpressureController(max_concurrent=2, queue_size=3, timeout=0.05)

        completed_operations = []
        rejected_operations = []

        def high_load_worker(worker_id):
            from cachekit.backends.errors import BackendError

            try:
                with controller.acquire():
                    time.sleep(0.1)  # Simulate work
                    completed_operations.append(worker_id)
            except BackendError:
                rejected_operations.append(worker_id)

        # Use ThreadPoolExecutor for controlled concurrency
        with ThreadPoolExecutor(max_workers=10) as executor:
            # Submit many tasks quickly
            futures = [executor.submit(high_load_worker, i) for i in range(50)]

            # Wait for all to complete
            for future in as_completed(futures):
                future.result()  # This will raise any exceptions

        # Verify system handled load appropriately
        total_ops = len(completed_operations) + len(rejected_operations)
        assert total_ops == 50

        # Some operations should have completed successfully
        assert len(completed_operations) >= 2  # At least max_concurrent

        # System should have rejected excess load
        assert len(rejected_operations) > 0

        # Final state should be clean
        assert controller.queue_depth == 0
        assert controller._semaphore._value == controller.max_concurrent

    def test_zero_concurrency_edge_case(self):
        """Test edge case with zero max_concurrent."""
        from cachekit.backends.errors import BackendError

        controller = BackpressureController(max_concurrent=0)

        # Should immediately fail to acquire
        with pytest.raises(BackendError, match="Failed to acquire permit"):
            with controller.acquire():
                pass

    def test_very_small_timeout(self):
        """Test behavior with very small timeouts."""
        controller = BackpressureController(max_concurrent=1, timeout=0.001)

        # Hold the semaphore
        def blocking_operation():
            with controller.acquire():
                time.sleep(0.1)

        thread = threading.Thread(target=blocking_operation)
        thread.start()
        time.sleep(0.01)  # Ensure semaphore is held

        # This should timeout very quickly
        from cachekit.backends.errors import BackendError

        start_time = time.time()
        with pytest.raises(BackendError, match="Failed to acquire permit"):
            with controller.acquire():
                pass

        elapsed = time.time() - start_time
        assert elapsed < 0.05  # Should timeout quickly

        thread.join()
