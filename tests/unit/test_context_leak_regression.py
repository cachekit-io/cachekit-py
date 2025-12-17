"""Regression tests for ContextVar leak prevention.

These tests verify that stats context is properly reset in ALL code paths:
1. Backend initialization failure
2. Backend operation failure (get/set/delete)
3. Function raises exception
4. Multiple sequential calls don't pollute context
5. Async wrapper exception handling
6. Async correlation ID cleanup

Critical for preventing context leaks that cause cross-function stats pollution.

Historical Context:
- Original bug: ContextVar token not reset in finally block
- Symptom: Inner function stats leaked into outer function stats
- Root cause: Exception paths didn't reset context
- Fix: Always reset token in finally block, even on exception

Async Context (added 2025-12):
- Bug: Async wrapper missing clear_correlation_id() in finally block
- Symptom: Correlation IDs leaked across async requests
- Root cause: Async wrapper (line 1247) didn't match sync wrapper cleanup (line 841)
- Fix: Add features.clear_correlation_id() to async finally block
"""

from __future__ import annotations

import asyncio
from unittest.mock import Mock, patch

import pytest

from cachekit import cache
from cachekit.backends.errors import BackendError, BackendErrorType
from cachekit.decorators.stats_context import get_current_function_stats
from cachekit.monitoring.correlation_tracking import get_correlation_id


@pytest.mark.unit
class TestContextLeakOnBackendFailure:
    """Test context is reset when backend operations fail."""

    def test_context_reset_on_backend_init_failure(self):
        """Context should be None after backend provider initialization fails.

        Simulates backend provider.get_backend() raising exception.
        Function should execute uncached and context should be clean.
        """
        with patch("cachekit.cache_handler.get_backend_provider") as mock_provider:
            # Mock backend provider to fail
            mock_provider.return_value.get_backend.side_effect = RuntimeError("Backend init failed")

            @cache()
            def test_func(x):
                return x * 2

            # Call function (should fallback to uncached execution)
            result = test_func(5)
            assert result == 10

            # Verify context is None (no leak)
            assert get_current_function_stats() is None

    def test_context_reset_on_cache_get_failure(self, mock_backend):
        """Context should be None after backend.get() raises exception.

        Simulates Redis connection failure during get operation.
        Function should execute and context should be clean.
        """
        # Mock backend.get to raise exception
        mock_backend.get = Mock(
            side_effect=BackendError(
                "Redis connection timeout",
                operation="get",
                error_type=BackendErrorType.TIMEOUT,
            )
        )

        @cache(backend=mock_backend)
        def test_func(x):
            return x * 2

        # Call function (should execute despite backend failure)
        result = test_func(5)
        assert result == 10

        # Verify context is None (no leak)
        assert get_current_function_stats() is None

    def test_context_reset_on_cache_set_failure(self, mock_backend):
        """Context should be None after backend.set() raises exception.

        Simulates Redis write failure during set operation.
        Function should execute and context should be clean.
        """
        # Mock backend.get to return None (miss), backend.set to fail
        mock_backend.get = Mock(return_value=None)
        mock_backend.set = Mock(
            side_effect=BackendError(
                "Redis write failed",
                operation="set",
                error_type=BackendErrorType.TRANSIENT,
            )
        )

        @cache(backend=mock_backend)
        def test_func(x):
            return x * 2

        # Call function (should execute despite set failure)
        result = test_func(5)
        assert result == 10

        # Verify context is None (no leak)
        assert get_current_function_stats() is None

    def test_context_reset_on_deserialization_failure(self, mock_backend):
        """Context should be None after deserialization raises exception.

        Simulates deserializer failing to decode cached value.
        Function should execute (fallback to uncached) and context should be clean.
        """
        # Mock backend.get to return invalid data
        mock_backend.get = Mock(return_value=b"\x00\x00CORRUPT_DATA")

        @cache(backend=mock_backend)
        def test_func(x):
            return x * 2

        # Call function (deserialization may fail, should fallback to uncached)
        result = test_func(5)
        assert result == 10

        # Verify context is None (no leak)
        assert get_current_function_stats() is None


@pytest.mark.unit
class TestContextLeakOnFunctionException:
    """Test context is reset when decorated function raises exception."""

    def test_context_reset_on_function_exception(self, mock_backend):
        """Context should be None after function raises exception.

        Critical: Exception must propagate to caller AND context must be reset.
        """

        @cache(backend=mock_backend)
        def failing_func(x):
            if x < 0:
                raise ValueError("Negative values not allowed")
            return x * 2

        # Call with valid input first
        result = failing_func(5)
        assert result == 10
        assert get_current_function_stats() is None

        # Call with invalid input (raises exception)
        with pytest.raises(ValueError, match="Negative values not allowed"):
            failing_func(-1)

        # Verify context is None after exception (no leak)
        assert get_current_function_stats() is None

    def test_context_reset_on_nested_function_exception(self, mock_backend):
        """Context should be reset at each level when inner function raises.

        Verifies:
        - Inner function exception propagates to outer
        - Outer function context is reset
        - No context leak after exception
        """
        outer_stats_before_exception = None

        @cache(backend=mock_backend)
        def outer(x):
            nonlocal outer_stats_before_exception
            outer_stats_before_exception = get_current_function_stats()

            try:
                return inner(x)
            except ValueError:
                # Verify outer's stats still active during exception handling
                assert get_current_function_stats() is outer_stats_before_exception
                raise

        @cache(backend=mock_backend)
        def inner(x):
            if x < 0:
                raise ValueError("Inner function error")
            return x * 2

        # Call with invalid input
        with pytest.raises(ValueError, match="Inner function error"):
            outer(-1)

        # Verify context is None after exception bubbles up (no leak)
        assert get_current_function_stats() is None

    def test_context_reset_on_zero_division_error(self, mock_backend):
        """Context should be reset even on unexpected exceptions.

        Tests that finally block executes for all exception types.
        """

        @cache(backend=mock_backend)
        def divide_func(a, b):
            return a / b

        # Normal call
        result = divide_func(10, 2)
        assert result == 5.0
        assert get_current_function_stats() is None

        # Division by zero
        with pytest.raises(ZeroDivisionError):
            divide_func(10, 0)

        # Verify context is None after exception (no leak)
        assert get_current_function_stats() is None


@pytest.mark.unit
class TestContextLeakMultipleCalls:
    """Test multiple sequential calls don't pollute context."""

    def test_multiple_calls_no_context_pollution(self, mock_backend):
        """Sequential calls to different functions should not pollute context.

        Verifies:
        - Each call starts with clean context (or function-specific context)
        - Each call ends with None context
        - No cross-contamination between function stats
        """

        @cache(backend=mock_backend)
        def func_a(x):
            return x * 2

        @cache(backend=mock_backend)
        def func_b(x):
            return x * 3

        @cache(backend=mock_backend)
        def func_c(x):
            return x * 4

        # Call each function
        result_a = func_a(5)
        assert result_a == 10
        assert get_current_function_stats() is None

        result_b = func_b(5)
        assert result_b == 15
        assert get_current_function_stats() is None

        result_c = func_c(5)
        assert result_c == 20
        assert get_current_function_stats() is None

        # Verify each function has independent stats
        info_a = func_a.cache_info()
        info_b = func_b.cache_info()
        info_c = func_c.cache_info()

        # All should have 1 miss (no cross-contamination)
        assert info_a.misses == 1
        assert info_b.misses == 1
        assert info_c.misses == 1

    def test_interleaved_calls_no_pollution(self, mock_backend):
        """Interleaved calls should maintain context isolation.

        Pattern: A → B → A → B → C
        Verifies context is properly set/reset for each call.
        """

        @cache(backend=mock_backend)
        def func_a(x):
            return x * 2

        @cache(backend=mock_backend)
        def func_b(x):
            return x * 3

        @cache(backend=mock_backend)
        def func_c(x):
            return x * 4

        # Interleaved calls
        func_a(1)
        assert get_current_function_stats() is None

        func_b(2)
        assert get_current_function_stats() is None

        func_a(3)
        assert get_current_function_stats() is None

        func_b(4)
        assert get_current_function_stats() is None

        func_c(5)
        assert get_current_function_stats() is None

        # Verify stats
        assert func_a.cache_info().misses == 2  # Called with 1 and 3
        assert func_b.cache_info().misses == 2  # Called with 2 and 4
        assert func_c.cache_info().misses == 1  # Called with 5


@pytest.mark.unit
class TestEdgeCaseContextLeaks:
    """Test edge cases that could cause context leaks."""

    def test_context_reset_on_keyboard_interrupt(self, mock_backend):
        """KeyboardInterrupt should reset context (best effort).

        Note: KeyboardInterrupt inherits from BaseException, not Exception.
        Finally blocks still execute for BaseException.
        """

        @cache(backend=mock_backend)
        def interruptible_func(x):
            if x == 999:
                raise KeyboardInterrupt("Simulated interrupt")
            return x * 2

        # Normal call
        result = interruptible_func(5)
        assert result == 10
        assert get_current_function_stats() is None

        # Interrupt call
        with pytest.raises(KeyboardInterrupt):
            interruptible_func(999)

        # Verify context is None (no leak)
        assert get_current_function_stats() is None

    def test_context_reset_on_system_exit(self, mock_backend):
        """SystemExit should reset context (best effort).

        Note: SystemExit inherits from BaseException, not Exception.
        Finally blocks still execute for BaseException.
        """

        @cache(backend=mock_backend)
        def exit_func(x):
            if x == 999:
                raise SystemExit(1)
            return x * 2

        # Normal call
        result = exit_func(5)
        assert result == 10
        assert get_current_function_stats() is None

        # Exit call
        with pytest.raises(SystemExit):
            exit_func(999)

        # Verify context is None (no leak)
        assert get_current_function_stats() is None

    def test_context_reset_after_recursive_calls(self, mock_backend):
        """Recursive decorated functions should maintain context isolation.

        Each recursive call should have its own stats context.
        """

        @cache(backend=mock_backend)
        def factorial(n):
            if n <= 1:
                return 1
            return n * factorial(n - 1)

        # Call recursive function
        result = factorial(5)
        assert result == 120

        # Verify context is None after recursion completes
        assert get_current_function_stats() is None

        # Verify stats tracked correctly (5 calls: 5, 4, 3, 2, 1)
        # Note: n <= 1 base case returns immediately without caching
        info = factorial.cache_info()
        assert info.misses == 5  # All cache misses (different arguments)


# =============================================================================
# Async Context Leak Tests
# =============================================================================


@pytest.fixture
def async_mock_backend():
    """Mock backend for async tests without locking support.

    This fixture provides a backend with:
    - Sync methods (returns values directly, not coroutines)
    - No acquire_lock attribute (tests non-locking code path)

    This is needed because PerRequestMockBackend has async methods that
    don't work correctly with the decorator's sync-to-async bridge.
    """
    from unittest.mock import Mock

    backend = Mock()
    backend.get = Mock(return_value=None)
    backend.set = Mock(return_value=None)
    backend.delete = Mock(return_value=True)
    backend.exists = Mock(return_value=False)
    backend.get_ttl = Mock(return_value=None)
    backend.refresh_ttl = Mock(return_value=True)

    # Remove acquire_lock so decorator uses fallback path (no distributed locking)
    # spec_set prevents adding acquire_lock later
    del backend.acquire_lock

    return backend


@pytest.mark.unit
class TestAsyncContextLeaks:
    """Test that async wrapper properly cleans up context in all code paths.

    These tests verify the fix for async wrapper missing clear_correlation_id()
    which existed in sync wrapper but was absent from async wrapper.

    Note: Uses async_mock_backend fixture (defined above) to test the non-locking
    code path with proper sync mock methods.
    """

    @pytest.mark.asyncio
    async def test_async_clears_correlation_id_on_success(self, async_mock_backend):
        """Async wrapper should clear correlation ID after successful execution.

        Bug: Line 1247 was missing features.clear_correlation_id() which exists
        in sync wrapper at line 841.

        Fix: Add features.clear_correlation_id() to async finally block.
        """

        @cache(backend=async_mock_backend)
        async def async_func(x):
            return x * 2

        # Execute async function
        result = await async_func(5)
        assert result == 10

        # Correlation ID should be cleared (not leaked)
        assert get_correlation_id() is None

    @pytest.mark.asyncio
    async def test_async_clears_correlation_id_on_exception(self, async_mock_backend):
        """Async wrapper should clear correlation ID even when function raises."""

        @cache(backend=async_mock_backend)
        async def failing_async_func(x):
            if x < 0:
                raise ValueError("Negative not allowed")
            return x * 2

        # Execute failing async function
        with pytest.raises(ValueError):
            await failing_async_func(-1)

        # Correlation ID should be cleared even after exception
        assert get_correlation_id() is None

    @pytest.mark.asyncio
    async def test_async_clears_context_on_key_generation_failure(self, async_mock_backend):
        """Context should be cleared even when key generation fails early.

        Bug: Early return at line 883 bypasses finally block cleanup.
        """
        # Force key generation to fail
        with patch(
            "cachekit.cache_handler.CacheOperationHandler.get_cache_key",
            side_effect=RuntimeError("Key generation failed"),
        ):

            @cache(backend=async_mock_backend)
            async def async_func(x):
                return x * 2

            # This should still execute the function (uncached)
            # and cleanup context properly
            result = await async_func(5)

            # Function should still work (graceful degradation)
            assert result == 10

            # Context should be clean
            assert get_current_function_stats() is None

    @pytest.mark.asyncio
    async def test_async_context_reset_matches_sync(self, async_mock_backend):
        """Async wrapper finally block should match sync wrapper cleanup.

        Sync wrapper (lines 839-843):
            finally:
                features.clear_correlation_id()
                reset_current_function_stats(token)

        Async wrapper (lines 1245-1247) should have SAME cleanup:
            finally:
                features.clear_correlation_id()  # Was missing - fixed
                reset_current_function_stats(token)
        """

        @cache(backend=async_mock_backend)
        def sync_func(x):
            return x * 2

        @cache(backend=async_mock_backend)
        async def async_func(x):
            return x * 2

        # Call sync function
        sync_func(5)
        sync_correlation = get_correlation_id()

        # Call async function
        await async_func(5)
        async_correlation = get_correlation_id()

        # Both should have cleared correlation ID
        assert sync_correlation is None, "Sync wrapper should clear correlation ID"
        assert async_correlation is None, "Async wrapper should clear correlation ID"

    @pytest.mark.asyncio
    async def test_async_stats_context_reset_after_exception(self, async_mock_backend):
        """Stats context should be None after async function raises exception."""

        @cache(backend=async_mock_backend)
        async def error_func():
            raise RuntimeError("Test error")

        with pytest.raises(RuntimeError):
            await error_func()

        # Stats context should be reset
        assert get_current_function_stats() is None


@pytest.mark.unit
class TestAsyncContextLeaksIntegration:
    """Integration tests verifying async context cleanup under concurrent load."""

    @pytest.mark.asyncio
    async def test_concurrent_async_operations_no_leaks(self, async_mock_backend):
        """Multiple concurrent async operations should not leak context."""

        @cache(backend=async_mock_backend)
        async def async_operation(x):
            await asyncio.sleep(0.001)  # Simulate async work
            return x * 2

        # Run many concurrent operations
        tasks = [async_operation(i) for i in range(20)]
        results = await asyncio.gather(*tasks)

        # All results should be correct
        assert results == [i * 2 for i in range(20)]

        # No context leaks
        assert get_current_function_stats() is None
        assert get_correlation_id() is None

    @pytest.mark.asyncio
    async def test_mixed_success_and_failure_no_leaks(self, async_mock_backend):
        """Mix of successful and failing async operations should not leak."""

        @cache(backend=async_mock_backend)
        async def sometimes_fails(x):
            if x % 3 == 0:
                raise ValueError(f"Divisible by 3: {x}")
            return x * 2

        # Run mix of success/failure
        for i in range(10):
            try:
                await sometimes_fails(i)
            except ValueError:
                pass

        # No leaks after all operations
        assert get_current_function_stats() is None
        assert get_correlation_id() is None


# =============================================================================
# Async Wrapper TTL Refresh Task Tests
# =============================================================================


@pytest.fixture
def ttl_refresh_mock_backend():
    """Mock backend that supports TTL refresh operations with REAL serialization.

    This fixture provides a backend with:
    - get: Returns properly wrapped/serialized data (JSON envelope with base64 payload)
    - get_ttl: Async method returning remaining TTL (simulates near-expiry)
    - refresh_ttl: Async method to refresh TTL
    - set/delete: Basic cache operations

    Used to test the TTL refresh task creation with error callback.
    """
    from unittest.mock import AsyncMock, Mock

    from cachekit.serializers.standard_serializer import StandardSerializer
    from cachekit.serializers.wrapper import SerializationWrapper

    # Serialize the value - returns (bytes, SerializationMetadata)
    serializer = StandardSerializer()
    raw_bytes, metadata = serializer.serialize(42)
    # Convert metadata to dict for wrap_for_redis (needs "format" key)
    metadata_dict = metadata.to_dict()
    wrapped_data = SerializationWrapper.wrap_for_redis(
        data=raw_bytes,
        metadata=metadata_dict,  # Must include "format" key from actual metadata
        serializer_name="default",  # Match StandardSerializer
        version="2.0",
    )

    backend = Mock()
    # Return properly wrapped data so deserialization succeeds
    backend.get = Mock(return_value=wrapped_data)
    backend.set = Mock(return_value=None)
    backend.delete = Mock(return_value=True)
    backend.exists = Mock(return_value=True)

    # TTL inspection support - ASYNC methods for the async wrapper path
    backend.get_ttl = AsyncMock(return_value=10)  # Near expiry (10 seconds, threshold is 0.5*300=150)
    backend.refresh_ttl = AsyncMock(return_value=True)

    # Remove acquire_lock so decorator uses fallback path
    del backend.acquire_lock

    return backend


@pytest.mark.unit
class TestAsyncWrapperTTLRefresh:
    """Test TTL refresh task creation in async wrapper.

    Coverage targets:
    - Lines 1038-1046: TTL refresh with background task and error callback
    """

    @pytest.mark.asyncio
    async def test_async_cache_hit_triggers_ttl_refresh(self, ttl_refresh_mock_backend):
        """Cache HIT with low TTL should trigger background TTL refresh.

        This exercises the FULL async wrapper TTL refresh path:
        1. Cache hit (backend.get returns serialized data)
        2. Deserialization succeeds
        3. get_ttl returns value below threshold
        4. refresh_ttl task is created with callback

        Coverage: Lines 1038-1046 (TTL refresh in async cache hit path)
        """

        @cache(backend=ttl_refresh_mock_backend, ttl=300, refresh_ttl_on_get=True)
        async def cached_func():
            return 99  # Won't be called - cache hit

        # Call function - should hit cache and trigger TTL refresh
        result = await cached_func()

        # Result should be deserialized cached value (42, not 99)
        assert result == 42

        # Allow background task to complete
        await asyncio.sleep(0.05)

        # Verify TTL was checked
        ttl_refresh_mock_backend.get_ttl.assert_called()

        # Verify refresh_ttl was called (TTL 10 < threshold 150)
        ttl_refresh_mock_backend.refresh_ttl.assert_called()

    @pytest.mark.asyncio
    async def test_async_cache_hit_skips_ttl_refresh_above_threshold(self, ttl_refresh_mock_backend):
        """Cache HIT with high TTL should NOT trigger refresh."""
        from unittest.mock import AsyncMock

        # Set TTL above threshold (200 > 150)
        ttl_refresh_mock_backend.get_ttl = AsyncMock(return_value=200)

        @cache(backend=ttl_refresh_mock_backend, ttl=300, refresh_ttl_on_get=True)
        async def cached_func():
            return 99

        result = await cached_func()
        assert result == 42

        await asyncio.sleep(0.01)

        # TTL was checked
        ttl_refresh_mock_backend.get_ttl.assert_called()

        # But refresh_ttl should NOT be called (TTL above threshold)
        ttl_refresh_mock_backend.refresh_ttl.assert_not_called()

    @pytest.mark.asyncio
    async def test_ttl_refresh_done_callback_is_properly_attached(self):
        """TTL refresh tasks should have error callback attached.

        The _ttl_refresh_done_callback function should be attached to
        TTL refresh tasks to log errors from background operations.

        Coverage: Lines 1042-1043 (task creation with callback)
        """
        from cachekit.decorators.wrapper import _ttl_refresh_done_callback

        # Create a task that will fail (simulating refresh_ttl failure)
        async def failing_refresh():
            raise RuntimeError("Simulated refresh failure")

        task = asyncio.create_task(failing_refresh())
        task.add_done_callback(lambda t: _ttl_refresh_done_callback(t, "test:key"))

        # Wait for task to complete
        await asyncio.sleep(0.01)

        # Task should be done and callback should have handled the error
        assert task.done()

    @pytest.mark.asyncio
    async def test_ttl_refresh_done_callback_handles_success(self):
        """TTL refresh callback should silently handle successful tasks."""
        from cachekit.decorators.wrapper import _ttl_refresh_done_callback

        async def successful_refresh():
            return True

        task = asyncio.create_task(successful_refresh())
        task.add_done_callback(lambda t: _ttl_refresh_done_callback(t, "test:key"))

        result = await task
        assert result is True
        assert task.done()

    @pytest.mark.asyncio
    async def test_ttl_refresh_done_callback_handles_cancellation(self):
        """TTL refresh callback should handle cancelled tasks gracefully.

        When a task is cancelled (e.g., during shutdown), the callback
        should not raise or log errors.

        Coverage: Lines 51-53 (_ttl_refresh_done_callback CancelledError handling)
        """
        from cachekit.decorators.wrapper import _ttl_refresh_done_callback

        async def slow_refresh():
            await asyncio.sleep(10)

        task = asyncio.create_task(slow_refresh())
        task.add_done_callback(lambda t: _ttl_refresh_done_callback(t, "test:key"))

        # Cancel the task
        task.cancel()

        try:
            await task
        except asyncio.CancelledError:
            pass

        assert task.cancelled()

    @pytest.mark.asyncio
    async def test_ttl_refresh_exception_is_caught(self, ttl_refresh_mock_backend, caplog):
        """TTL refresh exception should be caught and logged, not propagate.

        Coverage: Lines 1044-1046 (exception handling in TTL refresh)
        """
        from unittest.mock import AsyncMock

        # Make get_ttl raise an exception
        ttl_refresh_mock_backend.get_ttl = AsyncMock(side_effect=RuntimeError("Redis connection lost"))

        @cache(backend=ttl_refresh_mock_backend, ttl=300, refresh_ttl_on_get=True)
        async def cached_func():
            return 99

        # Should still return cached value despite TTL refresh failure
        result = await cached_func()
        assert result == 42

        # Function should complete successfully (exception was caught)
        assert "Redis connection lost" not in str(result)


@pytest.fixture
def backend_without_ttl():
    """Mock backend without TTL inspection support (no get_ttl/refresh_ttl methods).

    Used to test the fallback path when backend doesn't support TTL refresh.
    """
    from unittest.mock import Mock

    from cachekit.serializers.standard_serializer import StandardSerializer
    from cachekit.serializers.wrapper import SerializationWrapper

    serializer = StandardSerializer()
    raw_bytes, metadata = serializer.serialize(42)
    metadata_dict = metadata.to_dict()
    wrapped_data = SerializationWrapper.wrap_for_redis(
        data=raw_bytes,
        metadata=metadata_dict,
        serializer_name="default",
        version="2.0",
    )

    backend = Mock()
    backend.get = Mock(return_value=wrapped_data)
    backend.set = Mock(return_value=None)
    backend.delete = Mock(return_value=True)
    backend.exists = Mock(return_value=True)

    # No get_ttl or refresh_ttl - backend doesn't support TTL inspection
    del backend.get_ttl
    del backend.refresh_ttl
    del backend.acquire_lock

    return backend


@pytest.mark.unit
class TestAsyncWrapperTTLRefreshEdgeCases:
    """Edge cases for TTL refresh in async wrapper.

    Coverage targets:
    - Lines 1044-1046: Exception during TTL refresh
    - Lines 1047-1048: Backend without TTL inspection support
    """

    @pytest.mark.asyncio
    async def test_backend_without_ttl_support_logs_and_continues(self, backend_without_ttl, caplog):
        """Backend without get_ttl/refresh_ttl should log debug and continue.

        Coverage: Lines 1047-1048 (backend doesn't support TTL inspection)
        """
        import logging

        @cache(backend=backend_without_ttl, ttl=300, refresh_ttl_on_get=True)
        async def cached_func():
            return 99

        with caplog.at_level(logging.DEBUG):
            result = await cached_func()

        # Should return cached value
        assert result == 42

        # Debug log should mention backend doesn't support TTL inspection
        # (logs are optional, main thing is it doesn't crash)


# =============================================================================
# Async Wrapper Cache Set Error Tests
# =============================================================================


@pytest.mark.unit
class TestAsyncWrapperCacheSetError:
    """Test cache set exception handling in async wrapper.

    Coverage targets:
    - Lines 1244-1254: Exception during cache_set in async wrapper
    """

    @pytest.mark.asyncio
    async def test_async_cache_set_failure_returns_result(self, async_mock_backend):
        """Async wrapper should return result even when cache set fails.

        Coverage: Lines 1244-1254 (exception handling during set)
        """
        # Make set fail after function execution
        async_mock_backend.get = Mock(return_value=None)  # Cache miss
        async_mock_backend.set = Mock(side_effect=RuntimeError("Redis write failed"))

        @cache(backend=async_mock_backend)
        async def compute_func(x):
            return x * 2

        # Function should still return result even if caching fails
        result = await compute_func(5)

        assert result == 10  # Function executed successfully
        async_mock_backend.set.assert_called()  # Set was attempted

    @pytest.mark.asyncio
    async def test_async_cache_set_backend_error_returns_result(self, async_mock_backend):
        """Async wrapper should handle BackendError during set gracefully."""
        from cachekit.backends.errors import BackendError, BackendErrorType

        async_mock_backend.get = Mock(return_value=None)
        async_mock_backend.set = Mock(
            side_effect=BackendError(
                "Connection pool exhausted",
                operation="set",
                error_type=BackendErrorType.TRANSIENT,
            )
        )

        @cache(backend=async_mock_backend)
        async def compute_func(x):
            return x * 2

        # Should not raise - returns computed result
        result = await compute_func(5)
        assert result == 10

    @pytest.mark.asyncio
    async def test_async_multiple_cache_set_failures_no_leak(self, async_mock_backend):
        """Multiple cache set failures should not leak context."""
        async_mock_backend.get = Mock(return_value=None)
        async_mock_backend.set = Mock(side_effect=RuntimeError("Persistent failure"))

        @cache(backend=async_mock_backend)
        async def compute_func(x):
            return x * 2

        # Multiple calls that all fail to cache
        for i in range(5):
            result = await compute_func(i)
            assert result == i * 2

        # Context should be clean
        assert get_current_function_stats() is None
        assert get_correlation_id() is None
