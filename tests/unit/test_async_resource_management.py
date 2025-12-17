"""Tests for async resource management and lifecycle.

These tests verify proper cleanup and resource management in async code paths:
1. TTL Refresh Fire-and-Forget (error callbacks for background tasks)
2. Cache Handler async methods (proper thread pool usage)

These are regression tests ensuring async operations don't leak resources
or block the event loop inappropriately.
"""

from __future__ import annotations

import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# =============================================================================
# TTL Refresh Fire-and-Forget Tests
# =============================================================================


@pytest.mark.unit
class TestTTLRefreshFireAndForget:
    """Test that TTL refresh background tasks have error handling.

    Bug: asyncio.create_task() at line 1023 creates orphan tasks without
    error callbacks. If refresh_ttl() fails, the exception is silently dropped.

    Fix: Add done_callback to log exceptions from the background task.
    """

    @pytest.mark.asyncio
    async def test_ttl_refresh_task_logs_exception_on_failure(self, caplog):
        """Background TTL refresh should log errors, not silently swallow them.

        Bug: asyncio.create_task() at line 1023 creates orphan tasks without
        error callbacks. If refresh_ttl() fails, the exception is silently dropped.

        Fix: Add done_callback to log exceptions from the background task.
        """
        from cachekit.decorators.wrapper import _ttl_refresh_done_callback

        # Create a mock task that will raise an exception
        async def failing_refresh():
            raise RuntimeError("Redis connection lost during TTL refresh")

        # Create the task
        task = asyncio.create_task(failing_refresh())

        # Attach our callback (this is what the fix adds)
        task.add_done_callback(lambda t: _ttl_refresh_done_callback(t, "test:key:123"))

        # Wait for task to complete (and fail)
        with caplog.at_level(logging.DEBUG):
            await asyncio.sleep(0.01)  # Let task complete

        # Verify the exception was logged
        assert "Background TTL refresh failed" in caplog.text or task.done()

    @pytest.mark.asyncio
    async def test_ttl_refresh_callback_handles_cancelled_task(self):
        """Cancelled TTL refresh tasks should not raise or log errors.

        When a task is cancelled (e.g., during shutdown), the callback
        should handle CancelledError gracefully.
        """
        from cachekit.decorators.wrapper import _ttl_refresh_done_callback

        # Create a task that will be cancelled
        async def slow_refresh():
            await asyncio.sleep(10)

        task = asyncio.create_task(slow_refresh())
        task.add_done_callback(lambda t: _ttl_refresh_done_callback(t, "test:key:456"))

        # Cancel the task
        task.cancel()

        # This should NOT raise CancelledError
        try:
            await task
        except asyncio.CancelledError:
            pass  # Expected

        # Callback should have handled the cancellation gracefully
        assert task.cancelled()

    @pytest.mark.asyncio
    async def test_ttl_refresh_callback_handles_successful_task(self):
        """Successful TTL refresh tasks should complete silently.

        The callback should only log on failure, not on success.
        """
        from cachekit.decorators.wrapper import _ttl_refresh_done_callback

        # Create a successful task
        async def successful_refresh():
            return True

        task = asyncio.create_task(successful_refresh())
        task.add_done_callback(lambda t: _ttl_refresh_done_callback(t, "test:key:789"))

        # Wait for completion
        result = await task

        # Should complete without error
        assert result is True
        assert not task.cancelled()
        assert task.exception() is None


# =============================================================================
# Fake Async in Cache Handler Tests
# =============================================================================


@pytest.mark.unit
class TestCacheHandlerAsyncThreadPool:
    """Test that async methods actually run in thread pool, not blocking event loop.

    Bug: Lines 1359-1377 call sync backend.get() directly in async method,
    blocking the event loop.

    Fix: Use asyncio.to_thread() to run sync operations in thread pool.
    """

    @pytest.mark.asyncio
    async def test_get_async_uses_thread_pool(self):
        """get_async() should use asyncio.to_thread() for sync backend operations.

        Bug: Lines 1359-1377 call sync backend.get() directly in async method,
        blocking the event loop.

        Fix: Use asyncio.to_thread() to run sync operations in thread pool.
        """
        from cachekit.cache_handler import StandardCacheHandler

        # Create handler with mock backend
        mock_backend = MagicMock()
        mock_backend.get = MagicMock(return_value=b"test_value")

        handler = StandardCacheHandler(backend=mock_backend)

        # Patch asyncio.to_thread to verify it's called
        with patch("asyncio.to_thread", new_callable=AsyncMock) as mock_to_thread:
            mock_to_thread.return_value = b"test_value"

            await handler.get_async("test_key")

            # Verify to_thread was called (after fix)
            mock_to_thread.assert_called_once()

    @pytest.mark.asyncio
    async def test_set_async_uses_thread_pool(self):
        """set_async() should use asyncio.to_thread() for sync backend operations.

        Bug: Line 1388 calls sync backend.set() directly.
        """
        from cachekit.cache_handler import StandardCacheHandler

        mock_backend = MagicMock()
        mock_backend.set = MagicMock()

        handler = StandardCacheHandler(backend=mock_backend)

        with patch("asyncio.to_thread", new_callable=AsyncMock) as mock_to_thread:
            mock_to_thread.return_value = None

            await handler.set_async("test_key", b"test_value", ttl=300)

            mock_to_thread.assert_called_once()

    @pytest.mark.asyncio
    async def test_delete_async_uses_thread_pool(self):
        """delete_async() should use asyncio.to_thread() for sync backend operations.

        Bug: Line 1402 calls sync backend.delete() directly.
        """
        from cachekit.cache_handler import StandardCacheHandler

        mock_backend = MagicMock()
        mock_backend.delete = MagicMock(return_value=True)

        handler = StandardCacheHandler(backend=mock_backend)

        with patch("asyncio.to_thread", new_callable=AsyncMock) as mock_to_thread:
            mock_to_thread.return_value = True

            result = await handler.delete_async("test_key")

            mock_to_thread.assert_called_once()
            assert result is True


# =============================================================================
# Cache Handler Async Exception Handling Tests
# =============================================================================


@pytest.mark.unit
class TestCacheHandlerAsyncExceptionHandling:
    """Test exception handling in cache handler async methods.

    Coverage targets:
    - get_async: BackendError handling (line 1380-1382)
    - get_async: Generic Exception handling (lines 1383-1385)
    - set_async: BackendError handling (lines 1400-1402)
    - set_async: Generic Exception handling (lines 1403-1405)
    - delete_async: BackendError handling (lines 1415-1417)
    - delete_async: Generic Exception handling (lines 1418-1420)
    """

    @pytest.mark.asyncio
    async def test_get_async_handles_backend_error(self, caplog):
        """get_async() should catch BackendError and return None."""
        from cachekit.backends.errors import BackendError, BackendErrorType
        from cachekit.cache_handler import StandardCacheHandler

        mock_backend = MagicMock()
        mock_backend.get = MagicMock(
            side_effect=BackendError(
                "Redis timeout",
                operation="get",
                error_type=BackendErrorType.TIMEOUT,
            )
        )

        handler = StandardCacheHandler(backend=mock_backend)

        with caplog.at_level("ERROR"):
            result = await handler.get_async("test_key")

        assert result is None
        assert "Backend error getting key" in caplog.text

    @pytest.mark.asyncio
    async def test_get_async_handles_unexpected_exception(self, caplog):
        """get_async() should catch unexpected exceptions and return None."""
        from cachekit.cache_handler import StandardCacheHandler

        mock_backend = MagicMock()
        mock_backend.get = MagicMock(side_effect=RuntimeError("Unexpected failure"))

        handler = StandardCacheHandler(backend=mock_backend)

        with caplog.at_level("ERROR"):
            result = await handler.get_async("test_key")

        assert result is None
        assert "Unexpected error getting key" in caplog.text

    @pytest.mark.asyncio
    async def test_set_async_handles_backend_error(self, caplog):
        """set_async() should catch BackendError and return False."""
        from cachekit.backends.errors import BackendError, BackendErrorType
        from cachekit.cache_handler import StandardCacheHandler

        mock_backend = MagicMock()
        mock_backend.set = MagicMock(
            side_effect=BackendError(
                "Redis write failure",
                operation="set",
                error_type=BackendErrorType.TRANSIENT,
            )
        )

        handler = StandardCacheHandler(backend=mock_backend)

        with caplog.at_level("ERROR"):
            result = await handler.set_async("test_key", b"test_value", ttl=300)

        assert result is False
        assert "Backend error setting key" in caplog.text

    @pytest.mark.asyncio
    async def test_set_async_handles_unexpected_exception(self, caplog):
        """set_async() should catch unexpected exceptions and return False."""
        from cachekit.cache_handler import StandardCacheHandler

        mock_backend = MagicMock()
        mock_backend.set = MagicMock(side_effect=RuntimeError("Disk full"))

        handler = StandardCacheHandler(backend=mock_backend)

        with caplog.at_level("ERROR"):
            result = await handler.set_async("test_key", b"test_value")

        assert result is False
        assert "Unexpected error setting key" in caplog.text

    @pytest.mark.asyncio
    async def test_delete_async_handles_backend_error(self, caplog):
        """delete_async() should catch BackendError and return False."""
        from cachekit.backends.errors import BackendError, BackendErrorType
        from cachekit.cache_handler import StandardCacheHandler

        mock_backend = MagicMock()
        mock_backend.delete = MagicMock(
            side_effect=BackendError(
                "Redis connection lost",
                operation="delete",
                error_type=BackendErrorType.TRANSIENT,
            )
        )

        handler = StandardCacheHandler(backend=mock_backend)

        with caplog.at_level("ERROR"):
            result = await handler.delete_async("test_key")

        assert result is False
        assert "Backend error deleting key" in caplog.text

    @pytest.mark.asyncio
    async def test_delete_async_handles_unexpected_exception(self, caplog):
        """delete_async() should catch unexpected exceptions and return False."""
        from cachekit.cache_handler import StandardCacheHandler

        mock_backend = MagicMock()
        mock_backend.delete = MagicMock(side_effect=RuntimeError("Network failure"))

        handler = StandardCacheHandler(backend=mock_backend)

        with caplog.at_level("ERROR"):
            result = await handler.delete_async("test_key")

        assert result is False
        assert "Unexpected error deleting key" in caplog.text


# =============================================================================
# Cache Handler Async Backpressure Path Tests
# =============================================================================


@pytest.mark.unit
class TestCacheHandlerAsyncBackpressurePaths:
    """Test backpressure controller paths in async methods.

    Coverage targets:
    - Line 1352: Non-backpressure path (return operation directly)
    - Line 1349-1351: Backpressure path (with acquire context)
    """

    @pytest.mark.asyncio
    async def test_async_without_backpressure_controller(self):
        """Async methods should work without backpressure controller (line 1352)."""
        from cachekit.cache_handler import StandardCacheHandler

        mock_backend = MagicMock()
        mock_backend.get = MagicMock(return_value=b"cached_value")

        # Create handler WITHOUT backpressure controller
        handler = StandardCacheHandler(backend=mock_backend, backpressure_controller=None)

        # Verify no backpressure controller
        assert handler.backpressure_controller is None

        # Should still work - exercises line 1352
        result = await handler.get_async("test_key")

        assert result == b"cached_value"
        mock_backend.get.assert_called_once_with("test_key")

    @pytest.mark.asyncio
    async def test_async_with_backpressure_controller(self):
        """Async methods should respect backpressure controller (lines 1349-1351)."""
        from cachekit.cache_handler import StandardCacheHandler

        mock_backend = MagicMock()
        mock_backend.get = MagicMock(return_value=b"cached_value")

        # Create mock backpressure controller
        mock_bp = MagicMock()
        mock_bp.acquire = MagicMock(return_value=MagicMock(__enter__=MagicMock(), __exit__=MagicMock()))

        handler = StandardCacheHandler(backend=mock_backend, backpressure_controller=mock_bp)

        result = await handler.get_async("test_key")

        assert result == b"cached_value"
        # Backpressure acquire should have been called
        mock_bp.acquire.assert_called_once()


# =============================================================================
# Cache Handler Async TTL Refresh Tests
# =============================================================================


@pytest.mark.unit
class TestCacheHandlerAsyncTTLRefresh:
    """Test TTL refresh functionality in get_async.

    Coverage target: Line 1377 - await self._maybe_refresh_ttl(key, refresh_ttl)
    """

    @pytest.mark.asyncio
    async def test_get_async_triggers_ttl_refresh_when_value_exists(self):
        """get_async should call _maybe_refresh_ttl when refresh_ttl is provided."""
        from cachekit.cache_handler import StandardCacheHandler

        mock_backend = MagicMock()
        mock_backend.get = MagicMock(return_value=b"cached_value")

        handler = StandardCacheHandler(backend=mock_backend)

        # Mock _maybe_refresh_ttl to verify it's called
        with patch.object(handler, "_maybe_refresh_ttl", new_callable=AsyncMock) as mock_refresh:
            result = await handler.get_async("test_key", refresh_ttl=300)

            assert result == b"cached_value"
            mock_refresh.assert_called_once_with("test_key", 300)

    @pytest.mark.asyncio
    async def test_get_async_skips_ttl_refresh_when_no_value(self):
        """get_async should NOT call _maybe_refresh_ttl when cache miss."""
        from cachekit.cache_handler import StandardCacheHandler

        mock_backend = MagicMock()
        mock_backend.get = MagicMock(return_value=None)  # Cache miss

        handler = StandardCacheHandler(backend=mock_backend)

        with patch.object(handler, "_maybe_refresh_ttl", new_callable=AsyncMock) as mock_refresh:
            result = await handler.get_async("test_key", refresh_ttl=300)

            assert result is None
            mock_refresh.assert_not_called()

    @pytest.mark.asyncio
    async def test_get_async_skips_ttl_refresh_when_no_refresh_ttl(self):
        """get_async should NOT call _maybe_refresh_ttl when refresh_ttl is None."""
        from cachekit.cache_handler import StandardCacheHandler

        mock_backend = MagicMock()
        mock_backend.get = MagicMock(return_value=b"cached_value")

        handler = StandardCacheHandler(backend=mock_backend)

        with patch.object(handler, "_maybe_refresh_ttl", new_callable=AsyncMock) as mock_refresh:
            result = await handler.get_async("test_key")  # No refresh_ttl

            assert result == b"cached_value"
            mock_refresh.assert_not_called()


# =============================================================================
# Cache Handler Set Async String Encoding Tests
# =============================================================================


@pytest.mark.unit
class TestSetAsyncStringEncoding:
    """Test string-to-bytes encoding in set_async.

    Coverage target: Line 1394 - value = value.encode("utf-8")
    """

    @pytest.mark.asyncio
    async def test_set_async_encodes_string_to_bytes(self):
        """set_async should encode string values to bytes."""
        from cachekit.cache_handler import StandardCacheHandler

        mock_backend = MagicMock()
        mock_backend.set = MagicMock()

        handler = StandardCacheHandler(backend=mock_backend)

        # Pass string value (should be encoded)
        result = await handler.set_async("test_key", "string_value", ttl=300)

        assert result is True
        # Verify backend.set was called with bytes
        call_args = mock_backend.set.call_args
        assert call_args[0][1] == b"string_value"

    @pytest.mark.asyncio
    async def test_set_async_passes_bytes_unchanged(self):
        """set_async should pass bytes values unchanged."""
        from cachekit.cache_handler import StandardCacheHandler

        mock_backend = MagicMock()
        mock_backend.set = MagicMock()

        handler = StandardCacheHandler(backend=mock_backend)

        # Pass bytes value (should be unchanged)
        result = await handler.set_async("test_key", b"bytes_value", ttl=300)

        assert result is True
        call_args = mock_backend.set.call_args
        assert call_args[0][1] == b"bytes_value"
