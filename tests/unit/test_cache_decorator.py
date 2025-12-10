"""Unit tests for Redis cache decorator."""

from unittest.mock import patch

import pytest

from cachekit import cache


@pytest.mark.unit
class TestRedisCacheDecorator:
    """Test cache decorator functionality with isolated Redis."""

    def test_cache_hit(self, redis_test_client):
        """Function should return cached value on cache hit."""
        call_count = 0

        @cache(ttl=300)
        def cached_func():
            nonlocal call_count
            call_count += 1
            return "fresh_result"

        # First call should execute function and cache result
        result1 = cached_func()
        assert result1 == "fresh_result"
        assert call_count == 1

        # Second call should return cached result
        result2 = cached_func()
        assert result2 == "fresh_result"
        assert call_count == 1  # Function not called again due to cache hit

    def test_cache_miss(self, redis_test_client):
        """Function should execute and cache result on cache miss."""
        call_count = 0

        @cache(ttl=300)
        def cached_func():
            nonlocal call_count
            call_count += 1
            return "fresh_result"

        result = cached_func()
        assert result == "fresh_result"
        assert call_count == 1  # Function called once due to cache miss

    def test_cache_key_generation(self, redis_test_client):
        """Cache keys should include function name and arguments."""

        @cache(ttl=300)
        def cached_func(arg1, arg2=None):
            return f"{arg1}_{arg2}"

        result = cached_func("test", arg2="value")
        assert result == "test_value"

        # Verify key was generated and stored - check if any keys exist
        keys = redis_test_client.keys("*")
        assert len(keys) > 0

    def test_namespace_prefix(self, redis_test_client):
        """Cache keys should include namespace when specified."""

        @cache(ttl=300, namespace="test_ns")
        def cached_func():
            return "result"

        result = cached_func()
        assert result == "result"

        # Verify namespace in key - check for any keys with namespace
        # The key format includes tenant prefix "t:default:" followed by "ns:" prefix
        keys = redis_test_client.keys("t:default:ns:test_ns*")
        assert len(keys) > 0, f"Expected keys with namespace, found: {redis_test_client.keys('*')}"

    def test_cache_invalidation(self, redis_test_client):
        """invalidate_cache should delete cached entries."""
        call_count = 0

        @cache(ttl=300)
        def cached_func(arg):
            nonlocal call_count
            call_count += 1
            return f"result_{arg}_{call_count}"

        # Cache a result
        result1 = cached_func("test")
        assert result1 == "result_test_1"
        assert call_count == 1

        # Verify cache hit
        result2 = cached_func("test")
        assert result2 == result1
        assert call_count == 1

        # Invalidate cache
        cached_func.invalidate_cache("test")

        # Should execute function again
        result3 = cached_func("test")
        assert result3 == "result_test_2"
        assert call_count == 2

    def test_redis_error_graceful_degradation(self):
        """Function should work normally when Redis fails."""
        call_count = 0

        @cache(ttl=300, l1_enabled=False)
        def cached_func():
            nonlocal call_count
            call_count += 1
            return f"result_{call_count}"

        # Mock Redis failure at connection level
        with patch("cachekit.decorators.wrapper.get_backend_provider") as mock_get_provider:
            mock_provider = mock_get_provider.return_value
            mock_provider.get_sync_client.side_effect = Exception("Redis down")

            # Function should work despite Redis error
            result1 = cached_func()
            assert result1 == "result_1"

            result2 = cached_func()
            assert result2 == "result_2"
            assert call_count == 2


@pytest.mark.unit
@pytest.mark.asyncio
class TestRedisCacheDecoratorAsync:
    """Test async cache decorator functionality with isolated Redis."""

    async def test_cache_hit(self, redis_test_client):
        """Async function should return cached value on cache hit."""
        call_count = 0

        @cache(ttl=300)
        async def cached_func():
            nonlocal call_count
            call_count += 1
            return "fresh_result"

        # First call should execute function and cache result
        result1 = await cached_func()
        assert result1 == "fresh_result"
        assert call_count == 1

        # Second call should return cached result
        result2 = await cached_func()
        assert result2 == "fresh_result"
        assert call_count == 1  # Function not called again due to cache hit

    async def test_cache_miss(self, redis_test_client):
        """Async function should execute and cache result on cache miss."""
        call_count = 0

        @cache(ttl=300)
        async def cached_func():
            nonlocal call_count
            call_count += 1
            return "fresh_result"

        result = await cached_func()
        assert result == "fresh_result"
        assert call_count == 1  # Function called once due to cache miss

    async def test_cache_ttl_override(self, redis_test_client):
        """Async function should respect custom TTL."""

        @cache(ttl=600)
        async def cached_func():
            return "fresh_result"

        result = await cached_func()
        assert result == "fresh_result"

        # Verify key was stored
        keys = redis_test_client.keys("*")
        assert len(keys) > 0

    async def test_cache_invalidation(self, redis_test_client):
        """invalidate_cache should delete cached entries for async funcs."""
        call_count = 0

        @cache(ttl=300)
        async def cached_func(arg):
            nonlocal call_count
            call_count += 1
            return f"result_{arg}_{call_count}"

        # Cache a result
        result1 = await cached_func("test")
        assert result1 == "result_test_1"
        assert call_count == 1

        # Verify cache hit
        result2 = await cached_func("test")
        assert result2 == result1
        assert call_count == 1

        # Invalidate cache
        await cached_func.invalidate_cache("test")

        # Should execute function again
        result3 = await cached_func("test")
        assert result3 == "result_test_2"
        assert call_count == 2

    async def test_redis_error_graceful_degradation(self):
        """Async function should work normally when Redis fails."""
        call_count = 0

        @cache(ttl=300, l1_enabled=False)
        async def cached_func():
            nonlocal call_count
            call_count += 1
            return f"result_{call_count}"

        # Mock Redis failure at connection level
        with patch("cachekit.decorators.wrapper.get_backend_provider") as mock_get_provider:
            mock_provider = mock_get_provider.return_value
            mock_provider.get_async_client.side_effect = Exception("Redis down")

            result1 = await cached_func()
            assert result1 == "result_1"

            result2 = await cached_func()
            assert result2 == "result_2"
            assert call_count == 2
