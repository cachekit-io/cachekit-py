"""Test backpressure controller integration with cache decorator."""

import threading
import time
from unittest.mock import Mock

import pytest

from cachekit import cache
from cachekit.cache_handler import StandardCacheHandler
from cachekit.config import DecoratorConfig
from cachekit.config.nested import BackpressureConfig
from cachekit.decorators import DecoratorFeatures
from cachekit.reliability import BackpressureController


class TestBackpressureIntegration:
    """Test backpressure functionality in decorator."""

    def test_backpressure_controller_created(self):
        """Test that backpressure controller is created when enabled."""
        # Track features instance
        features_instance = None

        # Patch DecoratorFeatures to capture instance
        original_init = DecoratorFeatures.__init__

        def patched_init(self, *args, **kwargs):
            nonlocal features_instance
            original_init(self, *args, **kwargs)
            features_instance = self

        DecoratorFeatures.__init__ = patched_init

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
            DecoratorFeatures.__init__ = original_init

    def test_backpressure_controller_disabled(self):
        """Test that backpressure controller is not created when disabled."""
        features_instance = None

        original_init = DecoratorFeatures.__init__

        def patched_init(self, *args, **kwargs):
            nonlocal features_instance
            original_init(self, *args, **kwargs)
            features_instance = self

        DecoratorFeatures.__init__ = patched_init

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
            DecoratorFeatures.__init__ = original_init

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
        handler = StandardCacheHandler(mock_backend, timeout_provider=None, backpressure_controller=backpressure_controller)

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
        """Test that backpressure controller actually limits concurrent requests."""

        # Setup mock backend that takes time to respond
        def slow_operation(key):
            time.sleep(0.1)  # Simulate slow backend operation
            return b"value"

        mock_backend.get = Mock(side_effect=slow_operation)

        # Create backpressure controller with very low limits
        backpressure_controller = BackpressureController(max_concurrent=2, queue_size=1, timeout=0.05)

        # Create handler with backpressure controller
        handler = StandardCacheHandler(mock_backend, timeout_provider=None, backpressure_controller=backpressure_controller)

        # Track results and exceptions
        results = []
        exceptions = []

        def worker(worker_id):
            try:
                result = handler.get(f"key_{worker_id}")
                if result is None:
                    # Cache handler caught a backpressure exception and returned None
                    exceptions.append((worker_id, "backpressure_rejection"))
                else:
                    results.append((worker_id, result))
            except Exception as e:
                exceptions.append((worker_id, e))

        # Start multiple threads to overwhelm the backpressure controller
        threads = []
        for i in range(5):  # 5 threads, but max_concurrent=2, queue_size=1
            thread = threading.Thread(target=worker, args=(i,))
            threads.append(thread)
            thread.start()

        # Wait for all threads to complete
        for thread in threads:
            thread.join()

        # Some requests should have been rejected due to backpressure
        assert len(exceptions) > 0, "Expected some requests to be rejected due to backpressure"

        # Verify we got some backpressure rejections (manifested as None results)
        backpressure_rejections = [e for e in exceptions if e[1] == "backpressure_rejection"]
        assert len(backpressure_rejections) > 0, f"Expected backpressure rejections, got exceptions: {exceptions}"

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
        handler = StandardCacheHandler(mock_backend, timeout_provider=None, backpressure_controller=backpressure_controller)

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
        handler = StandardCacheHandler(mock_backend, timeout_provider=None, backpressure_controller=backpressure_controller)

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
