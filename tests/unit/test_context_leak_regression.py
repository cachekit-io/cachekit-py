"""Regression tests for ContextVar leak prevention.

These tests verify that stats context is properly reset in ALL code paths:
1. Backend initialization failure
2. Backend operation failure (get/set/delete)
3. Function raises exception
4. Multiple sequential calls don't pollute context
5. Async wrapper exception handling

Critical for preventing context leaks that cause cross-function stats pollution.

Historical Context:
- Original bug: ContextVar token not reset in finally block
- Symptom: Inner function stats leaked into outer function stats
- Root cause: Exception paths didn't reset context
- Fix: Always reset token in finally block, even on exception
"""

from __future__ import annotations

from unittest.mock import Mock, patch

import pytest

from cachekit import cache
from cachekit.backends.errors import BackendError, BackendErrorType
from cachekit.decorators.stats_context import get_current_function_stats


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
