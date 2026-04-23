"""Unit tests for backend provider interfaces and implementations.

Tests for backends/provider.py covering:
- Abstract provider interfaces (NotImplementedError stubs)
- SimpleLogger all logging methods
- DefaultLoggerProvider
- DefaultCacheClientProvider (sync and async)
- DefaultBackendProvider with environment-based auto-detection
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
from cachekit.config.validation import ConfigurationError


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
    """Test DefaultBackendProvider initialization state."""

    def test_init_provider_is_none(self) -> None:
        """Test __init__ sets _provider to None and _resolved to False."""
        provider = DefaultBackendProvider()

        assert provider._provider is None
        assert provider._resolved is False


@pytest.mark.unit
class TestDefaultBackendProviderAutoDetect:
    """Test DefaultBackendProvider environment-based auto-detection."""

    @pytest.fixture(autouse=True)
    def _clean_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Remove all backend env vars before each test."""
        for var in (
            "CACHEKIT_API_KEY",
            "CACHEKIT_REDIS_URL",
            "CACHEKIT_MEMCACHED_SERVERS",
            "CACHEKIT_FILE_CACHE_DIR",
            "REDIS_URL",
        ):
            monkeypatch.delenv(var, raising=False)

    def test_auto_detect_cachekitio_from_api_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """CACHEKIT_API_KEY → CachekitIOBackend."""
        monkeypatch.setenv("CACHEKIT_API_KEY", "ck_test_abc123")

        provider = DefaultBackendProvider()
        with mock.patch("cachekit.backends.cachekitio.CachekitIOBackend") as mock_backend_class:
            mock_backend = mock.MagicMock()
            mock_backend_class.return_value = mock_backend

            result = provider.get_backend()

            assert result is mock_backend
            mock_backend_class.assert_called_once()

    def test_auto_detect_redis_from_cachekit_redis_url(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """CACHEKIT_REDIS_URL → RedisBackend via RedisBackendProvider."""
        monkeypatch.setenv("CACHEKIT_REDIS_URL", "redis://myhost:6379")

        provider = DefaultBackendProvider()
        with mock.patch("cachekit.backends.redis.config.RedisBackendConfig") as mock_config_class:
            with mock.patch("cachekit.backends.redis.provider.RedisBackendProvider") as mock_provider_class:
                with mock.patch("cachekit.backends.redis.provider.tenant_context") as mock_context:
                    mock_config = mock.MagicMock()
                    mock_config.redis_url = "redis://myhost:6379"
                    mock_config_class.from_env.return_value = mock_config

                    mock_backend = mock.MagicMock()
                    mock_provider = mock.MagicMock()
                    mock_provider.get_backend.return_value = mock_backend
                    mock_provider_class.return_value = mock_provider

                    mock_context.get.return_value = None

                    result = provider.get_backend()

                    assert result is mock_backend

    def test_auto_detect_memcached_from_servers(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """CACHEKIT_MEMCACHED_SERVERS → MemcachedBackend."""
        monkeypatch.setenv("CACHEKIT_MEMCACHED_SERVERS", '["mc1:11211"]')

        provider = DefaultBackendProvider()
        with mock.patch("cachekit.backends.memcached.MemcachedBackend") as mock_backend_class:
            with mock.patch("cachekit.backends.memcached.config.MemcachedBackendConfig") as mock_config_class:
                mock_config = mock.MagicMock()
                mock_config_class.from_env.return_value = mock_config
                mock_backend = mock.MagicMock()
                mock_backend_class.return_value = mock_backend

                result = provider.get_backend()

                assert result is mock_backend

    def test_auto_detect_file_from_cache_dir(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """CACHEKIT_FILE_CACHE_DIR → FileBackend."""
        monkeypatch.setenv("CACHEKIT_FILE_CACHE_DIR", "/var/cache/test-cache")

        provider = DefaultBackendProvider()
        with mock.patch("cachekit.backends.file.FileBackend") as mock_backend_class:
            with mock.patch("cachekit.backends.file.config.FileBackendConfig") as mock_config_class:
                mock_config = mock.MagicMock()
                mock_config_class.from_env.return_value = mock_config
                mock_backend = mock.MagicMock()
                mock_backend_class.return_value = mock_backend

                result = provider.get_backend()

                assert result is mock_backend

    def test_fallback_redis_url_no_prefix(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """REDIS_URL (no CACHEKIT_ prefix) → RedisBackend as 12-factor fallback."""
        monkeypatch.setenv("REDIS_URL", "redis://fallback:6379")

        provider = DefaultBackendProvider()
        with mock.patch("cachekit.backends.redis.config.RedisBackendConfig") as mock_config_class:
            with mock.patch("cachekit.backends.redis.provider.RedisBackendProvider") as mock_provider_class:
                with mock.patch("cachekit.backends.redis.provider.tenant_context") as mock_context:
                    mock_config = mock.MagicMock()
                    mock_config.redis_url = "redis://fallback:6379"
                    mock_config_class.from_env.return_value = mock_config

                    mock_backend = mock.MagicMock()
                    mock_provider = mock.MagicMock()
                    mock_provider.get_backend.return_value = mock_backend
                    mock_provider_class.return_value = mock_provider

                    mock_context.get.return_value = None

                    result = provider.get_backend()

                    assert result is mock_backend

    def test_no_env_vars_returns_none(self) -> None:
        """No backend env vars set → None (L1-only mode)."""
        provider = DefaultBackendProvider()

        result = provider.get_backend()

        assert result is None

    def test_conflict_api_key_and_cachekit_redis_url_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """CACHEKIT_API_KEY + CACHEKIT_REDIS_URL → ConfigurationError."""
        monkeypatch.setenv("CACHEKIT_API_KEY", "ck_test_abc123")
        monkeypatch.setenv("CACHEKIT_REDIS_URL", "redis://localhost:6379")

        provider = DefaultBackendProvider()

        with pytest.raises(ConfigurationError, match="Ambiguous backend configuration"):
            provider.get_backend()

    def test_conflict_api_key_and_memcached_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """CACHEKIT_API_KEY + CACHEKIT_MEMCACHED_SERVERS → ConfigurationError."""
        monkeypatch.setenv("CACHEKIT_API_KEY", "ck_test_abc123")
        monkeypatch.setenv("CACHEKIT_MEMCACHED_SERVERS", '["mc1:11211"]')

        provider = DefaultBackendProvider()

        with pytest.raises(ConfigurationError, match="Ambiguous backend configuration"):
            provider.get_backend()

    def test_conflict_redis_and_file_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """CACHEKIT_REDIS_URL + CACHEKIT_FILE_CACHE_DIR → ConfigurationError."""
        monkeypatch.setenv("CACHEKIT_REDIS_URL", "redis://localhost:6379")
        monkeypatch.setenv("CACHEKIT_FILE_CACHE_DIR", "/var/cache/test-cache")

        provider = DefaultBackendProvider()

        with pytest.raises(ConfigurationError, match="Ambiguous backend configuration"):
            provider.get_backend()

    def test_conflict_three_backends_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Three CACHEKIT_ backend vars → ConfigurationError listing all."""
        monkeypatch.setenv("CACHEKIT_API_KEY", "ck_test_abc123")
        monkeypatch.setenv("CACHEKIT_REDIS_URL", "redis://localhost:6379")
        monkeypatch.setenv("CACHEKIT_MEMCACHED_SERVERS", '["mc1:11211"]')

        provider = DefaultBackendProvider()

        with pytest.raises(ConfigurationError, match="multiple CACHEKIT_ backend variables set"):
            provider.get_backend()

    def test_no_conflict_api_key_with_plain_redis_url(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """CACHEKIT_API_KEY + REDIS_URL (no prefix) → CachekitIO wins, no error."""
        monkeypatch.setenv("CACHEKIT_API_KEY", "ck_test_abc123")
        monkeypatch.setenv("REDIS_URL", "redis://fallback:6379")

        provider = DefaultBackendProvider()
        with mock.patch("cachekit.backends.cachekitio.CachekitIOBackend") as mock_backend_class:
            mock_backend = mock.MagicMock()
            mock_backend_class.return_value = mock_backend

            # Should NOT raise — REDIS_URL (no prefix) doesn't conflict
            result = provider.get_backend()

            assert result is mock_backend

    def test_provider_caches_after_first_call(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Provider resolution runs once, subsequent calls use cached result."""
        monkeypatch.setenv("CACHEKIT_API_KEY", "ck_test_abc123")

        provider = DefaultBackendProvider()
        with mock.patch("cachekit.backends.cachekitio.CachekitIOBackend") as mock_backend_class:
            mock_backend = mock.MagicMock()
            mock_backend_class.return_value = mock_backend

            result1 = provider.get_backend()
            result2 = provider.get_backend()

            assert result1 is result2
            # CachekitIOBackend constructor called only once
            mock_backend_class.assert_called_once()

    def test_none_result_cached(self) -> None:
        """None result (L1-only) is cached — don't re-detect on every call."""
        provider = DefaultBackendProvider()

        result1 = provider.get_backend()
        result2 = provider.get_backend()

        assert result1 is None
        assert result2 is None
        assert provider._resolved is True

    def test_error_message_includes_conflicting_vars(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """ConfigurationError message lists the conflicting variables and their backends."""
        monkeypatch.setenv("CACHEKIT_API_KEY", "ck_test_abc123")
        monkeypatch.setenv("CACHEKIT_FILE_CACHE_DIR", "/var/cache/test")

        provider = DefaultBackendProvider()

        with pytest.raises(ConfigurationError, match="CACHEKIT_API_KEY.*CachekitIO") as exc_info:
            provider.get_backend()

        assert "CACHEKIT_FILE_CACHE_DIR" in str(exc_info.value)
        assert "File" in str(exc_info.value)
