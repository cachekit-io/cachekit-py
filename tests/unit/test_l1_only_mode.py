"""
Test for L1-only mode bug: @cache(backend=None) should NOT attempt Redis connection.

Bug: When backend=None is explicitly passed, the decorator should use L1-only mode
(no Redis). However, the wrapper tries to get a backend from the provider on first
use, causing Redis connection attempts that fail without Redis running.

Root cause: Sentinel problem - can't distinguish between:
1. User passed @cache(backend=None) explicitly -> should be L1-only
2. User didn't pass backend at all -> should try provider

This test reproduces the bug from docs/getting-started.md doctest failure.
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock, patch


class TestL1OnlyModeBug:
    """Tests for L1-only mode (backend=None) behavior.

    These tests mock the backend provider to simulate Redis being unavailable,
    ensuring the tests work regardless of whether Redis is running locally.
    """

    def test_explicit_backend_none_should_not_call_backend_provider(self):
        """
        BUG REPRODUCTION: @cache(backend=None) should NOT call get_backend_provider().

        When backend=None is explicitly passed, the decorator should:
        1. Use L1 (in-memory) cache ONLY
        2. Never call get_backend_provider().get_backend()
        3. Work without Redis running

        This test fails because the wrapper ignores explicit backend=None and
        tries to get a backend from the provider.
        """
        from cachekit.decorators import cache

        # Mock the backend provider to detect if it's called
        with patch("cachekit.decorators.wrapper.get_backend_provider") as mock_provider:
            mock_provider.return_value.get_backend.side_effect = RuntimeError("Should not be called!")

            call_count = 0

            @cache(backend=None)
            def compute_result() -> int:
                nonlocal call_count
                call_count += 1
                return 42

            # First call - should cache in L1, NOT call backend provider
            result1 = compute_result()
            assert result1 == 42
            assert call_count == 1

            # Second call - should hit L1 cache
            result2 = compute_result()
            assert result2 == 42
            # If L1-only mode works, call_count should still be 1
            assert call_count == 1, f"L1 cache miss - function called {call_count} times (expected 1)"

            # Backend provider should NEVER have been called
            mock_provider.return_value.get_backend.assert_not_called()

    def test_l1_only_mode_performance(self):
        """
        L1-only mode should provide significant speedup on cache hit.

        This reproduces the doctest failure from docs/getting-started.md:
        - First call: cache miss, function executes (~10ms sleep)
        - Second call: L1 cache hit, should be much faster

        Without the fix, the second call triggers Redis connection attempt,
        which fails and falls back to re-executing the function.
        """
        from cachekit.decorators import cache

        # Mock backend provider to fail (simulating no Redis)
        with patch("cachekit.decorators.wrapper.get_backend_provider") as mock_provider:
            mock_provider.return_value.get_backend.side_effect = ConnectionError("Redis unavailable")

            @cache(backend=None)
            def slow_function() -> int:
                time.sleep(0.01)  # 10ms delay
                return 42

            # First call - cache miss
            start1 = time.perf_counter()
            result1 = slow_function()
            duration1 = time.perf_counter() - start1

            # Second call - should be L1 cache hit
            start2 = time.perf_counter()
            result2 = slow_function()
            duration2 = time.perf_counter() - start2

            assert result1 == 42
            assert result2 == 42

            # L1 cache hit should be at least 10x faster (sub-millisecond vs 10ms)
            # The doctest assertion was: assert duration2 < duration1 / 2
            assert duration2 < duration1 / 2, (
                f"L1 cache not working: second call ({duration2 * 1000:.2f}ms) "
                f"should be much faster than first ({duration1 * 1000:.2f}ms)"
            )

    def test_config_minimal_with_backend_none(self):
        """
        Test L1-only mode with DecoratorConfig preset AND backend=None.

        NOTE: L1-only mode requires backend=None at the decorator level, not in config.
        This is because DecoratorConfig.backend defaults to None, and we can't
        distinguish "explicit None" from "default None" in the config.

        Correct usage for L1-only with presets:
            @cache(backend=None, config=DecoratorConfig.minimal())
        """
        from cachekit.config import DecoratorConfig
        from cachekit.decorators import cache

        with patch("cachekit.decorators.wrapper.get_backend_provider") as mock_provider:
            mock_provider.return_value.get_backend.side_effect = RuntimeError("Should not be called!")

            call_count = 0

            # L1-only mode: backend=None passed directly to @cache, config for preset settings
            @cache(backend=None, config=DecoratorConfig.minimal())
            def minimal_func() -> str:
                nonlocal call_count
                call_count += 1
                return "cached"

            result1 = minimal_func()
            result2 = minimal_func()

            assert result1 == "cached"
            assert result2 == "cached"
            assert call_count == 1, f"L1 cache miss - function called {call_count} times"

    def test_explicit_backend_none_vs_default_behavior(self):
        """
        Verify the semantic difference between explicit backend=None and no backend specified.

        - @cache(backend=None) -> L1-only mode, no provider lookup
        - @cache() -> should attempt to get backend from provider (may fail without Redis)

        This test documents the expected behavior distinction.
        """
        from cachekit.config import DecoratorConfig

        # Explicit backend=None in config
        config_explicit = DecoratorConfig(backend=None, ttl=60)
        # The config stores the backend
        assert config_explicit.backend is None

        # This test just documents that we CAN configure backend=None
        # The fix should make the wrapper respect this and NOT call get_backend_provider()

    def test_async_l1_only_mode(self):
        """
        Async functions should also respect backend=None for L1-only mode.
        """
        import asyncio

        from cachekit.decorators import cache

        with patch("cachekit.decorators.wrapper.get_backend_provider") as mock_provider:
            mock_provider.return_value.get_backend.side_effect = RuntimeError("Should not be called!")

            call_count = 0

            @cache(backend=None)
            async def async_compute() -> int:
                nonlocal call_count
                call_count += 1
                return 123

            async def run_test():
                result1 = await async_compute()
                result2 = await async_compute()
                return result1, result2

            result1, result2 = asyncio.run(run_test())
            assert result1 == 123
            assert result2 == 123
            assert call_count == 1, f"Async L1 cache miss - function called {call_count} times"

    def test_intent_presets_with_backend_none(self):
        """
        Intent-based presets (@cache.minimal, @cache.production, etc.) should respect backend=None.

        This tests the edge case where backend=None is passed to intent presets like:
        - @cache.minimal(backend=None)
        - @cache.production(backend=None)
        - @cache.secure(master_key="...", backend=None)
        """
        from cachekit.decorators import cache

        with patch("cachekit.decorators.wrapper.get_backend_provider") as mock_provider:
            mock_provider.return_value.get_backend.side_effect = RuntimeError("Should not be called!")

            # Test @cache.minimal(backend=None)
            minimal_call_count = 0

            @cache.minimal(backend=None)
            def minimal_func() -> str:
                nonlocal minimal_call_count
                minimal_call_count += 1
                return "minimal"

            assert minimal_func() == "minimal"
            assert minimal_func() == "minimal"
            assert minimal_call_count == 1, f"@cache.minimal L1 miss - called {minimal_call_count} times"

            # Test @cache.production(backend=None)
            production_call_count = 0

            @cache.production(backend=None)
            def production_func() -> str:
                nonlocal production_call_count
                production_call_count += 1
                return "production"

            assert production_func() == "production"
            assert production_func() == "production"
            assert production_call_count == 1, f"@cache.production L1 miss - called {production_call_count} times"

            # Test @cache.secure(master_key="...", backend=None)
            secure_call_count = 0

            @cache.secure(master_key="a" * 64, backend=None)
            def secure_func() -> str:
                nonlocal secure_call_count
                secure_call_count += 1
                return "secure"

            assert secure_func() == "secure"
            assert secure_func() == "secure"
            assert secure_call_count == 1, f"@cache.secure L1 miss - called {secure_call_count} times"

            # Backend provider should NEVER have been called for any preset
            mock_provider.return_value.get_backend.assert_not_called()


class TestL1OnlyModeInvalidation:
    """Tests for L1-only invalidation - should NOT attempt backend lookup."""

    def test_invalidate_cache_should_not_call_backend_provider(self):
        """
        BUG REPRODUCTION: invalidate_cache() in L1-only mode should NOT call get_backend_provider().

        When backend=None is explicitly passed, invalidate_cache() should:
        1. Only invalidate the L1 cache
        2. Never call get_backend_provider().get_backend()
        3. Work without Redis running
        """
        from cachekit.decorators import cache

        with patch("cachekit.decorators.wrapper.get_backend_provider") as mock_provider:
            mock_provider.return_value.get_backend.side_effect = RuntimeError("Should not be called!")

            call_count = 0

            @cache(backend=None)
            def cached_func(x: int) -> int:
                nonlocal call_count
                call_count += 1
                return x * 2

            # Cache a value
            result = cached_func(5)
            assert result == 10
            assert call_count == 1

            # invalidate_cache should NOT call backend provider
            cached_func.invalidate_cache(5)

            # After invalidation, next call should re-execute function
            result2 = cached_func(5)
            assert result2 == 10
            assert call_count == 2, "Function should have been called again after invalidation"

            # Backend provider should NEVER have been called
            mock_provider.return_value.get_backend.assert_not_called()

    def test_ainvalidate_cache_should_not_call_backend_provider(self):
        """
        BUG REPRODUCTION: ainvalidate_cache() in L1-only mode should NOT call get_backend_provider().

        Async version of invalidate_cache() should also respect L1-only mode.
        """
        import asyncio

        from cachekit.decorators import cache

        with patch("cachekit.decorators.wrapper.get_backend_provider") as mock_provider:
            mock_provider.return_value.get_backend.side_effect = RuntimeError("Should not be called!")

            call_count = 0

            @cache(backend=None)
            async def async_cached_func(x: int) -> int:
                nonlocal call_count
                call_count += 1
                return x * 3

            async def run_test():
                nonlocal call_count

                # Cache a value
                result = await async_cached_func(7)
                assert result == 21
                assert call_count == 1

                # ainvalidate_cache should NOT call backend provider
                await async_cached_func.ainvalidate_cache(7)

                # After invalidation, next call should re-execute function
                result2 = await async_cached_func(7)
                assert result2 == 21
                assert call_count == 2, "Function should have been called again after invalidation"

            asyncio.run(run_test())

            # Backend provider should NEVER have been called
            mock_provider.return_value.get_backend.assert_not_called()

    def test_invalidate_cache_actually_clears_l1(self):
        """Verify invalidate_cache() actually clears the L1 cache in L1-only mode."""
        from cachekit.decorators import cache

        with patch("cachekit.decorators.wrapper.get_backend_provider") as mock_provider:
            mock_provider.return_value.get_backend.side_effect = RuntimeError("Should not be called!")

            call_count = 0
            return_values = [100, 200]  # Different values for each call

            @cache(backend=None)
            def changing_func(x: int) -> int:
                nonlocal call_count
                result = return_values[call_count]
                call_count += 1
                return result

            # First call returns 100
            result1 = changing_func(1)
            assert result1 == 100

            # Second call should hit cache and return 100
            result2 = changing_func(1)
            assert result2 == 100
            assert call_count == 1, "Should have hit L1 cache"

            # Invalidate the cache
            changing_func.invalidate_cache(1)

            # Third call should re-execute and return 200
            result3 = changing_func(1)
            assert result3 == 200
            assert call_count == 2, "Should have re-executed after invalidation"


class TestDefaultBackendBehavior:
    """
    CRITICAL: Tests that @cache() WITHOUT backend=None DOES attempt provider lookup.

    This is the regression test for the bug where we accidentally made ALL decorators
    L1-only by checking `config.backend is None` (which is the default).
    """

    def test_default_cache_should_call_backend_provider(self):
        """
        @cache() without backend=None SHOULD call get_backend_provider().

        This is the INVERSE of L1-only mode - verifies we didn't break default behavior.
        """
        from cachekit.decorators import cache

        with patch("cachekit.decorators.wrapper.get_backend_provider") as mock_provider:
            # Make provider return a mock backend
            mock_backend = MagicMock()
            mock_provider.return_value.get_backend.return_value = mock_backend

            @cache(ttl=60)  # NO backend=None - should use provider
            def default_func() -> str:
                return "result"

            # Call the function - this should trigger provider lookup
            default_func()

            # Backend provider SHOULD have been called
            mock_provider.return_value.get_backend.assert_called()

    def test_cache_minimal_without_backend_none_should_call_provider(self):
        """
        @cache.minimal() without backend=None SHOULD call get_backend_provider().
        """
        from cachekit.decorators import cache

        with patch("cachekit.decorators.wrapper.get_backend_provider") as mock_provider:
            mock_backend = MagicMock()
            mock_provider.return_value.get_backend.return_value = mock_backend

            @cache.minimal(ttl=60)  # NO backend=None
            def minimal_func() -> str:
                return "result"

            minimal_func()

            # Backend provider SHOULD have been called
            mock_provider.return_value.get_backend.assert_called()

    def test_decorator_config_default_backend_should_call_provider(self):
        """
        DecoratorConfig() with default backend SHOULD call get_backend_provider().

        This specifically tests that DecoratorConfig.backend defaulting to None
        does NOT trigger L1-only mode (the bug we fixed).
        """
        from cachekit.config import DecoratorConfig
        from cachekit.decorators import cache

        with patch("cachekit.decorators.wrapper.get_backend_provider") as mock_provider:
            mock_backend = MagicMock()
            mock_provider.return_value.get_backend.return_value = mock_backend

            # DecoratorConfig() has backend=None by DEFAULT - should NOT be L1-only
            @cache(config=DecoratorConfig(ttl=60))
            def config_func() -> str:
                return "result"

            config_func()

            # Backend provider SHOULD have been called (default != explicit None)
            mock_provider.return_value.get_backend.assert_called()

    def test_explicit_backend_instance_should_be_used(self):
        """
        @cache(backend=explicit_backend) should use that backend, not provider.
        """
        from cachekit.decorators import cache

        with patch("cachekit.decorators.wrapper.get_backend_provider") as mock_provider:
            mock_provider.return_value.get_backend.side_effect = RuntimeError("Should not be called!")

            # Create an explicit mock backend
            explicit_backend = MagicMock()
            explicit_backend.get.return_value = None  # Cache miss

            @cache(backend=explicit_backend, ttl=60)
            def explicit_func() -> str:
                return "result"

            explicit_func()

            # Provider should NOT be called - explicit backend provided
            mock_provider.return_value.get_backend.assert_not_called()

    def test_dev_and_test_presets_without_backend_none(self):
        """
        @cache.dev() and @cache.test() without backend=None SHOULD call provider.

        Completes coverage for all intent presets.
        """
        from cachekit.decorators import cache

        with patch("cachekit.decorators.wrapper.get_backend_provider") as mock_provider:
            mock_backend = MagicMock()
            mock_provider.return_value.get_backend.return_value = mock_backend

            @cache.dev(ttl=60)
            def dev_func() -> str:
                return "dev"

            @cache.test(ttl=60)
            def test_func() -> str:
                return "test"

            dev_func()
            test_func()

            # Both should have triggered provider lookup
            assert mock_provider.return_value.get_backend.call_count >= 2

    def test_dev_and_test_presets_with_backend_none(self):
        """
        @cache.dev(backend=None) and @cache.test(backend=None) should be L1-only.

        Completes L1-only coverage for all intent presets.
        """
        from cachekit.decorators import cache

        with patch("cachekit.decorators.wrapper.get_backend_provider") as mock_provider:
            mock_provider.return_value.get_backend.side_effect = RuntimeError("Should not be called!")

            dev_count = 0

            @cache.dev(backend=None)
            def dev_func() -> str:
                nonlocal dev_count
                dev_count += 1
                return "dev"

            test_count = 0

            @cache.test(backend=None)
            def test_func() -> str:
                nonlocal test_count
                test_count += 1
                return "test"

            # Execute twice each - should hit L1 cache
            dev_func()
            dev_func()
            test_func()
            test_func()

            assert dev_count == 1, f"@cache.dev L1 miss - called {dev_count} times"
            assert test_count == 1, f"@cache.test L1 miss - called {test_count} times"

            # Provider should NEVER be called
            mock_provider.return_value.get_backend.assert_not_called()


class TestL1OnlyModeNoRedisWarnings:
    """
    Verify that L1-only mode doesn't produce Redis connection warnings.

    The original bug manifests as:
    WARNING cachekit.decorators.orchestrator:provider.py:45
    Cache operation 'client_creation' failed for key '...':
    Transient Redis error: Error 111 connecting to localhost:6379. Connection refused.
    """

    def test_no_redis_warnings_on_l1_only(self, caplog):
        """
        L1-only mode should not log Redis connection errors.
        """
        import logging

        from cachekit.decorators import cache

        with patch("cachekit.decorators.wrapper.get_backend_provider") as mock_provider:
            # Make provider raise Redis error if called
            mock_provider.return_value.get_backend.side_effect = ConnectionError("Transient Redis error: Connection refused")

            with caplog.at_level(logging.WARNING):

                @cache(backend=None)
                def cached_func() -> str:
                    return "value"

                # Execute multiple times
                for _ in range(3):
                    cached_func()

            # No Redis-related warnings should appear
            redis_warnings = [r for r in caplog.records if "Redis" in r.message or "Connection refused" in r.message]
            assert len(redis_warnings) == 0, f"Unexpected Redis warnings: {[r.message for r in redis_warnings]}"
