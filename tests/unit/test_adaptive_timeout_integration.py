"""Test adaptive timeout integration with cache decorator."""

import pytest

from cachekit import cache
from cachekit.cache_handler import StandardCacheHandler
from cachekit.config import DecoratorConfig
from cachekit.config.nested import TimeoutConfig
from cachekit.decorators import DecoratorFeatures
from cachekit.reliability.adaptive_timeout import AdaptiveTimeout

# Note: DecoratorFeatures is maintained for backward compatibility
# New code should use: from cachekit.decorator.feature_orchestrator import FeatureOrchestrator


class TestAdaptiveTimeoutIntegration:
    """Test adaptive timeout functionality in decorator."""

    def test_adaptive_timeout_records_durations(self):
        """Test that operation durations are recorded."""
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

            @cache(config=DecoratorConfig(ttl=60, timeout=TimeoutConfig(enabled=True)))
            def test_function(x):
                return x * 2

            # Trigger decorator initialization
            try:
                test_function(1)
            except Exception:
                pass

            # Verify adaptive timeout was created
            assert features_instance is not None
            assert features_instance.adaptive_timeout is not None
            assert isinstance(features_instance.adaptive_timeout, AdaptiveTimeout)

            # Manually record some durations
            features_instance.record_duration(0.01)  # 10ms
            features_instance.record_duration(0.02)  # 20ms
            features_instance.record_duration(0.015)  # 15ms

            # Get timeout should return a value based on recorded durations
            timeout = features_instance.get_timeout()
            assert timeout is not None
            assert timeout > 0

        finally:
            DecoratorFeatures.__init__ = original_init

    def test_adaptive_timeout_disabled(self):
        """Test that adaptive timeout can be disabled."""
        features_instance = None

        original_init = DecoratorFeatures.__init__

        def patched_init(self, *args, **kwargs):
            nonlocal features_instance
            original_init(self, *args, **kwargs)
            features_instance = self

        DecoratorFeatures.__init__ = patched_init

        try:

            @cache(config=DecoratorConfig(ttl=60, timeout=TimeoutConfig(enabled=False)))
            def test_function(x):
                return x * 2

            try:
                test_function(1)
            except Exception:
                pass

            assert features_instance is not None
            assert features_instance.adaptive_timeout is None
            assert features_instance.get_timeout() == 5.0

        finally:
            DecoratorFeatures.__init__ = original_init

    def test_cache_handler_uses_timeout_provider(self, mock_backend):
        """Test that StandardCacheHandler uses the timeout provider."""
        # Setup mock backend
        mock_backend.get.return_value = b"cached_value"
        mock_backend.set.return_value = None
        mock_backend.delete.return_value = True
        mock_backend.get_ttl.return_value = 100
        mock_backend.refresh_ttl.return_value = True

        # Create timeout provider
        timeout_values = [1.0, 2.0, 3.0]
        call_count = 0

        def mock_timeout_provider():
            nonlocal call_count
            if call_count < len(timeout_values):
                val = timeout_values[call_count]
                call_count += 1
                return val
            return 5.0

        # Create handler with timeout provider
        handler = StandardCacheHandler(mock_backend, timeout_provider=mock_timeout_provider)

        # Test get operation
        handler.get("test_key")
        assert mock_backend.connection_pool.connection_kwargs["socket_timeout"] == 5.0  # Restored

        # Test set operation
        handler.set("test_key", b"value", ttl=60)
        assert mock_backend.connection_pool.connection_kwargs["socket_timeout"] == 5.0  # Restored

        # Test delete operation
        handler.delete("test_key")
        assert mock_backend.connection_pool.connection_kwargs["socket_timeout"] == 5.0  # Restored

        # Verify operations were called
        assert mock_backend.get.called
        assert mock_backend.set.called
        assert mock_backend.delete.called

    @pytest.mark.asyncio
    async def test_async_handler_uses_timeout(self, mock_backend):
        """Test that async handler uses adaptive timeout.

        NOTE: Backend methods are sync (not async), even when called from async handlers.
        The async wrapper is just for API compatibility.
        """
        # Setup sync backend methods (async handlers call sync backend methods)
        mock_backend.get.return_value = b"cached_value"
        mock_backend.set.return_value = None
        mock_backend.delete.return_value = True
        mock_backend.get_ttl.return_value = 100
        mock_backend.refresh_ttl.return_value = True

        # Create timeout provider
        def mock_timeout_provider():
            return 0.5  # 500ms timeout

        # Create handler with timeout provider
        handler = StandardCacheHandler(mock_backend, timeout_provider=mock_timeout_provider)

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

    def test_adaptive_timeout_with_real_operations(self):
        """Test adaptive timeout adjusts based on actual operation durations."""
        timeout = AdaptiveTimeout(window_size=100, percentile=95.0, min_timeout=0.1, max_timeout=5.0)

        # Initially should return conservative default
        assert timeout.get_timeout() == 0.2  # 2x min_timeout

        # Record some fast operations
        for _ in range(20):
            timeout.record_duration(0.01)  # 10ms operations

        # Timeout should be based on P95 + 50% buffer
        # P95 of 10ms = 10ms, + 50% = 15ms, but min is 100ms
        assert timeout.get_timeout() == 0.1  # min_timeout

        # Record some slower operations
        for _ in range(20):
            timeout.record_duration(0.5)  # 500ms operations

        # Now timeout should adjust higher
        # Mix of 10ms and 500ms operations
        current_timeout = timeout.get_timeout()
        assert current_timeout > 0.1
        assert current_timeout <= 5.0
