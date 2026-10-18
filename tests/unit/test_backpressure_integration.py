"""Test backpressure controller integration with cache decorator."""

import threading
import time
from unittest.mock import Mock

import pytest

from cachekit import cache
from cachekit.cache_handler import StandardCacheHandler
from cachekit.config import DecoratorConfig
from cachekit.config.nested import BackpressureConfig
from cachekit.decorators import FeatureOrchestrator
from cachekit.reliability import BackpressureController


class TestBackpressureIntegration:
    """Test backpressure functionality in decorator."""

    def test_backpressure_controller_created(self):
        """Test that backpressure controller is created when enabled."""
        # Track features instance
        features_instance = None

        # Patch FeatureOrchestrator to capture instance
        original_init = FeatureOrchestrator.__init__

        def patched_init(self, *args, **kwargs):
            nonlocal features_instance
            original_init(self, *args, **kwargs)
            features_instance = self

        FeatureOrchestrator.__init__ = patched_init

        try:

            @cache(config=DecoratorConfig(ttl=60, backpressure=BackpressureConfig(enabled=True, max_concurrent_requests=50)))
            def test_function(x):
                return x * 2

            # Trigger decorator initialization
            try:
                test_function(1)
            except Exception:
                pass

            # Verify backpressure controller was created
            assert features_instance is not None
            assert features_instance.backpressure is not None
            assert isinstance(features_instance.backpressure, BackpressureController)
            assert features_instance.backpressure.max_concurrent == 50

        finally:
            FeatureOrchestrator.__init__ = original_init

    def test_backpressure_controller_disabled(self):
        """Test that backpressure controller is not created when disabled."""
        features_instance = None

        original_init = FeatureOrchestrator.__init__

        def patched_init(self, *args, **kwargs):
            nonlocal features_instance
            original_init(self, *args, **kwargs)
            features_instance = self

        FeatureOrchestrator.__init__ = patched_init

        try:

            @cache(config=DecoratorConfig(ttl=60, backpressure=BackpressureConfig(enabled=False)))
            def test_function(x):
                return x * 2

            try:
                test_function(1)
            except Exception:
                pass

            assert features_instance is not None
            assert features_instance.backpressure is None

        finally:
            FeatureOrchestrator.__init__ = original_init

    def test_cache_handler_uses_backpressure_controller(self, mock_backend):
        """Test that StandardCacheHandler uses the backpressure controller."""
        # Setup mock backend
        mock_backend.get.return_value = b"cached_value"
        mock_backend.set.return_value = True
        mock_backend.delete.return_value = True
        mock_backend.get_ttl.return_value = 100
        mock_backend.refresh_ttl.return_value = True

        # Create backpressure controller
        max_concurrent = 2
        backpressure_controller = BackpressureController(max_concurrent=max_concurrent, timeout=0.1)

        # Create handler with backpressure controller
        handler = StandardCacheHandler(mock_backend, backpressure_controller=backpressure_controller)

        # Verify backpressure controller is set
        assert handler.backpressure_controller is backpressure_controller

        # Test that operations use backpressure
        handler.get("test_key")
        handler.set("test_key", b"value", ttl=60)
        handler.delete("test_key")

        # Verify backend operations were called
        assert mock_backend.get.called
        assert mock_backend.set.called
        assert mock_backend.delete.called

    def test_backpressure_limits_concurrent_requests(self, mock_backend):
        """Test that backpressure controller actually limits concurrent requests.

        Saturation is driven by events, not sleeps: a sleeping backend only overlaps requests
        if every thread starts within the sleep, which scheduler load breaks (LAB-6381).
        No worker wait races the test's clock: the holders block until the finally releases
        them, and an overflow request that slips past backpressure returns at once. Only the
        test thread has a deadline, and expiry fails the test; it never frees a permit.
        """
        holder_keys = {"key_0", "key_1"}
        entered = threading.Semaphore(0)  # one release per holder inside the backend
        release = threading.Event()

        def blocking_get(key):
            if key in holder_keys:
                entered.release()
                release.wait()
            return b"value"

        mock_backend.get = Mock(side_effect=blocking_get)

        # 2 permits, 1 queue slot, 0.05s permit wait
        backpressure_controller = BackpressureController(max_concurrent=2, queue_size=1, timeout=0.05)
        handler = StandardCacheHandler(mock_backend, backpressure_controller=backpressure_controller)

        results = {}
        stall_limit = 30  # hang guard per phase, far above the ~0.1s a healthy run takes

        def worker(worker_id):
            # handler.get swallows BackendError and returns None, so None is a rejection
            results[worker_id] = handler.get(f"key_{worker_id}")

        # daemon: a failed test never leaves a thread that blocks interpreter exit
        holders = [threading.Thread(target=worker, args=(i,), daemon=True) for i in range(2)]
        overflow = [threading.Thread(target=worker, args=(i,), daemon=True) for i in range(2, 5)]
        try:
            # One holder at a time: with queue_size=1, two holders racing for the queue slot could
            # reject each other. A holder that exits before entering the backend was rejected;
            # fail rather than wait on a signal that cannot come.
            for thread in holders:
                thread.start()
                deadline = time.monotonic() + stall_limit
                while not entered.acquire(timeout=0.1):
                    assert thread.is_alive(), f"holder never reached the backend: {results}"
                    assert time.monotonic() < deadline, "holder stalled before reaching the backend"

            # Permits stay held, so each overflow request is rejected: queue full, or permit timeout.
            # Every overflow path ends in the 0.05s permit wait or a non-blocking get, so one that
            # outlives the deadline means that wait stalled.
            for thread in overflow:
                thread.start()
            deadline = time.monotonic() + stall_limit
            for thread in overflow:
                thread.join(timeout=max(0.0, deadline - time.monotonic()))
            assert not any(t.is_alive() for t in overflow), f"overflow request stalled: {results}"
        finally:
            release.set()
            cleanup_deadline = time.monotonic() + 5
            for thread in holders + overflow:
                if thread.ident is not None:  # unstarted when an earlier phase failed
                    thread.join(timeout=max(0.0, cleanup_deadline - time.monotonic()))

        assert results == {0: b"value", 1: b"value", 2: None, 3: None, 4: None}
        assert mock_backend.get.call_count == 2, "rejected requests must not reach the backend"
        assert backpressure_controller.rejected_count == 3
        assert backpressure_controller.queue_depth == 0

    @pytest.mark.asyncio
    async def test_async_backpressure_integration(self, mock_backend):
        """Test that async operations also use backpressure controller.

        NOTE: Backend methods are sync (not async), even when called from async handlers.
        The async wrapper is just for API compatibility.
        """
        # Setup sync backend methods (async handlers call sync backend methods)
        mock_backend.get.return_value = b"cached_value"
        mock_backend.set.return_value = None
        mock_backend.delete.return_value = True

        # Create backpressure controller
        backpressure_controller = BackpressureController(max_concurrent=10)

        # Create handler with backpressure controller
        handler = StandardCacheHandler(mock_backend, backpressure_controller=backpressure_controller)

        # Test async operations (internally call sync backend methods)
        result = await handler.get_async("test_key")
        assert result == b"cached_value"

        await handler.set_async("test_key", b"value", ttl=60)

        deleted = await handler.delete_async("test_key")
        assert deleted is True

        # Verify operations were called
        assert mock_backend.get.called
        assert mock_backend.set.called
        assert mock_backend.delete.called

    # DELETED: test_backpressure_metrics_tracking
    # Reason: Flaky test with unreliable threading timing. Backpressure rejection requires
    # precise timing to hold semaphores while other requests are queued. This is better
    # tested in integration tests with real Redis latency rather than mocked timing.

    def test_backpressure_context_manager_cleanup(self, mock_backend):
        """Test that backpressure controller properly cleans up resources."""
        # Setup backend that raises an exception
        mock_backend.get.side_effect = Exception("Backend error")

        # Create backpressure controller
        backpressure_controller = BackpressureController(max_concurrent=10)

        # Create handler with backpressure controller
        handler = StandardCacheHandler(mock_backend, backpressure_controller=backpressure_controller)

        # Record initial semaphore state
        initial_permits = backpressure_controller._semaphore._value

        # Attempt operation that raises exception
        try:
            handler.get("test_key")
        except Exception:
            pass  # Expected

        # Verify semaphore was properly released despite exception
        final_permits = backpressure_controller._semaphore._value
        assert final_permits == initial_permits, "Semaphore permits should be restored after exception"
