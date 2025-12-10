"""Regression tests for nested @cache decorators and stats isolation.

These tests verify that:
1. Nested decorated functions maintain independent stats
2. Stats context is properly restored after inner function returns
3. Deep nesting (3+ levels) works correctly
4. Backend calls track the correct stats object at each level

Critical for preventing context pollution bugs where inner function stats
bleed into outer function stats.
"""

from __future__ import annotations

from unittest.mock import Mock

import pytest

from cachekit import cache
from cachekit.decorators.stats_context import get_current_function_stats


@pytest.mark.unit
class TestNestedDecoratorsIsolation:
    """Test nested @cache decorators maintain independent statistics."""

    def test_nested_decorators_isolated_stats(self, mock_backend):
        """Nested functions should have independent stats objects.

        Verifies:
        - Outer and inner functions have different _FunctionStats objects
        - After inner returns, outer's stats are restored
        - Both functions track their own hits/misses independently
        """
        outer_stats_captured = None
        inner_stats_captured = None

        @cache(backend=mock_backend)
        def outer(x):
            nonlocal outer_stats_captured
            outer_stats_captured = get_current_function_stats()

            # Call inner function
            result = inner(x)

            # Verify outer's stats restored after inner returns
            assert get_current_function_stats() is outer_stats_captured

            return result

        @cache(backend=mock_backend)
        def inner(x):
            nonlocal inner_stats_captured
            inner_stats_captured = get_current_function_stats()
            return x * 2

        # Execute nested call
        result = outer(5)
        assert result == 10

        # Verify stats objects are different
        assert outer_stats_captured is not None
        assert inner_stats_captured is not None
        assert outer_stats_captured is not inner_stats_captured

        # Verify context is None after outer returns
        assert get_current_function_stats() is None

    def test_nested_decorators_context_restoration(self, mock_backend):
        """Backend calls should track correct stats at each nesting level.

        Verifies:
        - During outer's backend call: outer's stats active
        - During inner's backend call: inner's stats active
        - After inner returns: outer's stats active again
        """
        backend_call_stats = []

        # Intercept backend.get to capture active stats
        original_get = mock_backend.get

        def tracking_get(key):
            backend_call_stats.append((key, get_current_function_stats()))
            return original_get(key)

        mock_backend.get = tracking_get

        @cache(backend=mock_backend)
        def outer(x):
            outer_stats = get_current_function_stats()
            result = inner(x)
            # Verify outer's stats restored
            assert get_current_function_stats() is outer_stats
            return result

        @cache(backend=mock_backend)
        def inner(x):
            return x * 2

        # Execute
        outer(5)

        # Verify backend.get was called twice (once for outer, once for inner)
        assert len(backend_call_stats) == 2

        outer_key, outer_stats = backend_call_stats[0]
        inner_key, inner_stats = backend_call_stats[1]

        # Verify stats objects are different
        assert outer_stats is not None
        assert inner_stats is not None
        assert outer_stats is not inner_stats

        # Verify keys are function-specific
        assert "outer" in outer_key
        assert "inner" in inner_key

    def test_deeply_nested_decorators(self, mock_backend):
        """Three levels of nesting should maintain correct context at each level.

        Verifies:
        - Context is properly restored at each level (level1 → level2 → level3 → level2 → level1)
        - Each function has its own stats object
        - No context leaks after all functions return
        """
        stats_at_each_level = []

        @cache(backend=mock_backend)
        def level1(x):
            l1_stats = get_current_function_stats()
            stats_at_each_level.append(("level1_entry", l1_stats))

            result = level2(x)

            # Verify level1 stats restored after level2 returns
            assert get_current_function_stats() is l1_stats
            stats_at_each_level.append(("level1_exit", get_current_function_stats()))

            return result

        @cache(backend=mock_backend)
        def level2(x):
            l2_stats = get_current_function_stats()
            stats_at_each_level.append(("level2_entry", l2_stats))

            result = level3(x)

            # Verify level2 stats restored after level3 returns
            assert get_current_function_stats() is l2_stats
            stats_at_each_level.append(("level2_exit", get_current_function_stats()))

            return result

        @cache(backend=mock_backend)
        def level3(x):
            l3_stats = get_current_function_stats()
            stats_at_each_level.append(("level3_entry", l3_stats))
            stats_at_each_level.append(("level3_exit", get_current_function_stats()))
            return x * 3

        # Execute
        result = level1(10)
        assert result == 30

        # Verify context is None after all functions return
        assert get_current_function_stats() is None

        # Verify stats objects at each level
        assert len(stats_at_each_level) == 6

        # Extract stats objects
        l1_entry_stats = stats_at_each_level[0][1]
        l2_entry_stats = stats_at_each_level[1][1]
        l3_entry_stats = stats_at_each_level[2][1]
        l3_exit_stats = stats_at_each_level[3][1]
        l2_exit_stats = stats_at_each_level[4][1]
        l1_exit_stats = stats_at_each_level[5][1]

        # All should be non-None
        assert all(
            stats is not None
            for stats in [
                l1_entry_stats,
                l2_entry_stats,
                l3_entry_stats,
                l3_exit_stats,
                l2_exit_stats,
                l1_exit_stats,
            ]
        )

        # Each level should have unique stats
        assert l1_entry_stats is not l2_entry_stats
        assert l2_entry_stats is not l3_entry_stats
        assert l1_entry_stats is not l3_entry_stats

        # Stats should be preserved within same level
        assert l1_entry_stats is l1_exit_stats
        assert l2_entry_stats is l2_exit_stats
        assert l3_entry_stats is l3_exit_stats

    def test_nested_decorators_stats_independence(self, mock_backend):
        """Inner and outer functions should track hits/misses independently.

        Verifies:
        - Inner function hits don't affect outer function stats
        - Outer function hits don't affect inner function stats
        - cache_info() returns correct stats for each function
        """
        # Disable L1 cache to ensure backend is always called
        from cachekit.l1_cache import get_l1_cache_manager

        get_l1_cache_manager().clear_all()

        mock_backend.get = Mock(return_value=None)  # Force misses

        @cache(backend=mock_backend, l1_enabled=False)
        def outer(x):
            return inner(x)

        @cache(backend=mock_backend, l1_enabled=False)
        def inner(x):
            return x * 2

        # First call - both miss
        result1 = outer(5)
        assert result1 == 10

        # Check stats
        outer_info = outer.cache_info()
        inner_info = inner.cache_info()

        # Both should have 1 miss
        assert outer_info.misses == 1
        assert inner_info.misses == 1
        assert outer_info.hits == 0
        assert inner_info.hits == 0

        # Second call with same argument - both should miss again (backend.get returns None)
        result2 = outer(5)
        assert result2 == 10

        outer_info2 = outer.cache_info()
        inner_info2 = inner.cache_info()

        # Both should have 2 misses now
        assert outer_info2.misses == 2
        assert inner_info2.misses == 2
