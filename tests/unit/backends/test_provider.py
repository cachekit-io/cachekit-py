"""Unit tests for backend provider interfaces and implementations.

Tests for backends/provider.py covering:
- Abstract provider interfaces (NotImplementedError stubs)
- SimpleLogger all logging methods
- DefaultLoggerProvider
- DefaultCacheClientProvider (sync and async)
- DefaultBackendProvider with lazy initialization and tenant context
"""

from __future__ import annotations

import logging
from unittest import mock

import pytest

from cachekit.backends.provider import (
    BackendProviderInterface,
    CacheClientProvider,
    DefaultBackendProvider,
    DefaultCacheClientProvider,
    DefaultLoggerProvider,
    LoggerProvider,
    SimpleLogger,
)


@pytest.mark.unit
class TestCacheClientProvider:
    """Test CacheClientProvider abstract interface."""

    def test_get_sync_client_not_implemented(self) -> None:
        """Test get_sync_client raises NotImplementedError."""
        provider = CacheClientProvider()

        with pytest.raises(NotImplementedError):
            provider.get_sync_client()

    def test_get_async_client_not_implemented(self) -> None:
        """Test get_async_client raises NotImplementedError."""
        provider = CacheClientProvider()

        with pytest.raises(NotImplementedError):
            import asyncio

            asyncio.run(provider.get_async_client())


@pytest.mark.unit
class TestLoggerProvider:
    """Test LoggerProvider abstract interface."""

    def test_get_logger_not_implemented(self) -> None:
        """Test get_logger raises NotImplementedError."""
        provider = LoggerProvider()

        with pytest.raises(NotImplementedError):
            provider.get_logger("test")


@pytest.mark.unit
class TestSimpleLogger:
    """Test SimpleLogger wrapper for cache-specific logging."""

    def test_debug_message(self) -> None:
        """Test debug logging."""
        mock_logger = mock.MagicMock()
        logger = SimpleLogger(mock_logger)

        logger.debug("test message")

        mock_logger.debug.assert_called_once_with("test message")

    def test_info_message(self) -> None:
        """Test info logging."""
        mock_logger = mock.MagicMock()
        logger = SimpleLogger(mock_logger)

        logger.info("test info")

        mock_logger.info.assert_called_once_with("test info")

    def test_info_message_with_extra(self) -> None:
        """Test info logging with extra parameter (ignored by SimpleLogger)."""
        mock_logger = mock.MagicMock()
        logger = SimpleLogger(mock_logger)

        logger.info("test info", extra={"key": "value"})

        # SimpleLogger ignores extra parameter
        mock_logger.info.assert_called_once_with("test info")

    def test_warning_message(self) -> None:
        """Test warning logging."""
        mock_logger = mock.MagicMock()
        logger = SimpleLogger(mock_logger)

        logger.warning("test warning")

        mock_logger.warning.assert_called_once_with("test warning")

    def test_error_message(self) -> None:
        """Test error logging."""
        mock_logger = mock.MagicMock()
        logger = SimpleLogger(mock_logger)

        logger.error("test error")

        mock_logger.error.assert_called_once_with("test error")

    def test_cache_hit_default_source(self) -> None:
        """Test cache hit logging with default source."""
        mock_logger = mock.MagicMock()
        logger = SimpleLogger(mock_logger)

        logger.cache_hit("key:123")

        mock_logger.debug.assert_called_once_with("Redis cache hit for key: key:123")

    def test_cache_hit_custom_source(self) -> None:
        """Test cache hit logging with custom source."""
        mock_logger = mock.MagicMock()
        logger = SimpleLogger(mock_logger)

        logger.cache_hit("key:456", source="Memcached")

        mock_logger.debug.assert_called_once_with("Memcached cache hit for key: key:456")

    def test_cache_miss(self) -> None:
        """Test cache miss logging."""
        mock_logger = mock.MagicMock()
        logger = SimpleLogger(mock_logger)

        logger.cache_miss("key:789")

        mock_logger.debug.assert_called_once_with("Cache miss for key: key:789")

    def test_cache_stored_without_ttl(self) -> None:
        """Test cache storage logging without TTL."""
        mock_logger = mock.MagicMock()
        logger = SimpleLogger(mock_logger)

        logger.cache_stored("key:111")

        mock_logger.debug.assert_called_once_with("Cached result for key: key:111")

    def test_cache_stored_with_ttl(self) -> None:
        """Test cache storage logging with TTL."""
        mock_logger = mock.MagicMock()
        logger = SimpleLogger(mock_logger)

        logger.cache_stored("key:222", ttl=3600)

        mock_logger.debug.assert_called_once_with("Cached result for key: key:222 with TTL 3600")

    def test_cache_invalidated_default_source(self) -> None:
        """Test cache invalidation logging with default source."""
        mock_logger = mock.MagicMock()
        logger = SimpleLogger(mock_logger)

        logger.cache_invalidated("key:333")

        mock_logger.debug.assert_called_once_with("Invalidated Redis cache for key: key:333")

    def test_cache_invalidated_custom_source(self) -> None:
        """Test cache invalidation logging with custom source."""
        mock_logger = mock.MagicMock()
        logger = SimpleLogger(mock_logger)

        logger.cache_invalidated("key:444", source="L1")

        mock_logger.debug.assert_called_once_with("Invalidated L1 cache for key: key:444")


@pytest.mark.unit
class TestDefaultLoggerProvider:
    """Test DefaultLoggerProvider creates SimpleLogger instances."""

    def test_get_logger_returns_simple_logger(self) -> None:
        """Test get_logger returns a SimpleLogger instance."""
        provider = DefaultLoggerProvider()

        logger = provider.get_logger("test.module")

        assert isinstance(logger, SimpleLogger)

    def test_get_logger_uses_standard_logging(self) -> None:
        """Test get_logger wraps standard Python logger."""
        provider = DefaultLoggerProvider()

        logger = provider.get_logger("test.module")

        # The inner logger should be a standard Python logger
        assert isinstance(logger._logger, logging.Logger)
        assert logger._logger.name == "test.module"

    def test_get_logger_different_names(self) -> None:
        """Test get_logger creates loggers with different names."""
        provider = DefaultLoggerProvider()

        logger1 = provider.get_logger("module1")
        logger2 = provider.get_logger("module2")

        assert logger1._logger.name == "module1"
        assert logger2._logger.name == "module2"


@pytest.mark.unit
class TestBackendProviderInterface:
    """Test BackendProviderInterface abstract interface."""

    def test_get_backend_not_implemented(self) -> None:
        """Test get_backend raises NotImplementedError."""
        provider = BackendProviderInterface()

        with pytest.raises(NotImplementedError):
            provider.get_backend()


@pytest.mark.unit
class TestDefaultCacheClientProvider:
    """Test DefaultCacheClientProvider implementation."""

    def test_get_sync_client(self) -> None:
        """Test get_sync_client returns a Redis client."""
        provider = DefaultCacheClientProvider()

        with mock.patch("cachekit.backends.redis.client.get_cached_redis_client") as mock_get:
            mock_client = mock.MagicMock()
            mock_get.return_value = mock_client

            client = provider.get_sync_client()

            assert client is mock_client
            mock_get.assert_called_once()

    @pytest.mark.asyncio
    async def test_get_async_client(self) -> None:
        """Test get_async_client returns an async Redis client."""
        provider = DefaultCacheClientProvider()

        with mock.patch("cachekit.backends.redis.client.get_cached_async_redis_client") as mock_get:
            mock_client = mock.AsyncMock()
            mock_get.return_value = mock_client

            client = await provider.get_async_client()

            assert client is mock_client
            mock_get.assert_called_once()


@pytest.mark.unit
class TestDefaultBackendProvider:
    """Test DefaultBackendProvider lazy initialization and tenant context."""

    def test_init_provider_is_none(self) -> None:
        """Test __init__ sets _provider to None."""
        provider = DefaultBackendProvider()

        assert provider._provider is None

    def test_get_backend_lazy_initialization(self) -> None:
        """Test get_backend creates provider on first call."""
        provider = DefaultBackendProvider()

        with mock.patch("cachekit.backends.redis.config.RedisBackendConfig") as mock_config_class:
            with mock.patch("cachekit.backends.redis.provider.RedisBackendProvider") as mock_provider_class:
                with mock.patch("cachekit.backends.redis.provider.tenant_context") as mock_context:
                    mock_config_instance = mock.MagicMock()
                    mock_config_class.from_env.return_value = mock_config_instance
                    mock_config_instance.redis_url = "redis://localhost:6379"

                    mock_provider_instance = mock.MagicMock()
                    mock_backend_instance = mock.MagicMock()
                    mock_provider_class.return_value = mock_provider_instance
                    mock_provider_instance.get_backend.return_value = mock_backend_instance

                    mock_context.get.return_value = None

                    backend = provider.get_backend()

                    assert backend is mock_backend_instance
                    mock_config_class.from_env.assert_called_once()
                    mock_provider_class.assert_called_once_with(redis_url="redis://localhost:6379")

    def test_get_backend_uses_cached_provider(self) -> None:
        """Test get_backend reuses provider on subsequent calls."""
        provider = DefaultBackendProvider()

        with mock.patch("cachekit.backends.redis.config.RedisBackendConfig") as mock_config_class:
            with mock.patch("cachekit.backends.redis.provider.RedisBackendProvider") as mock_provider_class:
                with mock.patch("cachekit.backends.redis.provider.tenant_context") as mock_context:
                    mock_config_instance = mock.MagicMock()
                    mock_config_class.from_env.return_value = mock_config_instance
                    mock_config_instance.redis_url = "redis://localhost:6379"

                    mock_provider_instance = mock.MagicMock()
                    mock_backend_instance = mock.MagicMock()
                    mock_provider_class.return_value = mock_provider_instance
                    mock_provider_instance.get_backend.return_value = mock_backend_instance

                    mock_context.get.return_value = None

                    # First call
                    backend1 = provider.get_backend()

                    # Second call
                    backend2 = provider.get_backend()

                    # Should use cached provider
                    assert backend1 is mock_backend_instance
                    assert backend2 is mock_backend_instance
                    # RedisBackendProvider should only be instantiated once
                    mock_provider_class.assert_called_once()

    def test_get_backend_sets_default_tenant_on_init(self) -> None:
        """Test get_backend sets default tenant context if not already set."""
        provider = DefaultBackendProvider()

        with mock.patch("cachekit.backends.redis.config.RedisBackendConfig") as mock_config_class:
            with mock.patch("cachekit.backends.redis.provider.RedisBackendProvider") as mock_provider_class:
                with mock.patch("cachekit.backends.redis.provider.tenant_context") as mock_context:
                    mock_config_instance = mock.MagicMock()
                    mock_config_class.from_env.return_value = mock_config_instance
                    mock_config_instance.redis_url = "redis://localhost:6379"

                    mock_provider_instance = mock.MagicMock()
                    mock_backend_instance = mock.MagicMock()
                    mock_provider_class.return_value = mock_provider_instance
                    mock_provider_instance.get_backend.return_value = mock_backend_instance

                    # Tenant context is None initially
                    mock_context.get.return_value = None

                    provider.get_backend()

                    # Should set default tenant
                    mock_context.set.assert_called_once_with("default")

    def test_get_backend_skips_tenant_setup_if_already_set(self) -> None:
        """Test get_backend doesn't override existing tenant context."""
        provider = DefaultBackendProvider()

        with mock.patch("cachekit.backends.redis.config.RedisBackendConfig") as mock_config_class:
            with mock.patch("cachekit.backends.redis.provider.RedisBackendProvider") as mock_provider_class:
                with mock.patch("cachekit.backends.redis.provider.tenant_context") as mock_context:
                    mock_config_instance = mock.MagicMock()
                    mock_config_class.from_env.return_value = mock_config_instance
                    mock_config_instance.redis_url = "redis://localhost:6379"

                    mock_provider_instance = mock.MagicMock()
                    mock_backend_instance = mock.MagicMock()
                    mock_provider_class.return_value = mock_provider_instance
                    mock_provider_instance.get_backend.return_value = mock_backend_instance

                    # Tenant context is already set
                    mock_context.get.return_value = "existing-tenant"

                    provider.get_backend()

                    # Should NOT call set
                    mock_context.set.assert_not_called()

    def test_get_backend_multiple_calls_no_tenant_override(self) -> None:
        """Test that subsequent calls don't reset tenant context."""
        provider = DefaultBackendProvider()

        with mock.patch("cachekit.backends.redis.config.RedisBackendConfig") as mock_config_class:
            with mock.patch("cachekit.backends.redis.provider.RedisBackendProvider") as mock_provider_class:
                with mock.patch("cachekit.backends.redis.provider.tenant_context") as mock_context:
                    mock_config_instance = mock.MagicMock()
                    mock_config_class.from_env.return_value = mock_config_instance
                    mock_config_instance.redis_url = "redis://localhost:6379"

                    mock_provider_instance = mock.MagicMock()
                    mock_backend_instance = mock.MagicMock()
                    mock_provider_class.return_value = mock_provider_instance
                    mock_provider_instance.get_backend.return_value = mock_backend_instance

                    # First call - tenant is None
                    mock_context.get.return_value = None
                    provider.get_backend()

                    # Second call - tenant was set by first call
                    mock_context.get.return_value = "default"
                    provider.get_backend()

                    # Should only set once (on first call)
                    mock_context.set.assert_called_once_with("default")
