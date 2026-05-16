"""Unit tests for backend provider interfaces and implementations.

Tests for backends/provider.py covering:
- Abstract provider interfaces (NotImplementedError stubs)
- SimpleLogger all logging methods
- DefaultLoggerProvider
- DefaultCacheClientProvider (sync and async)
- DefaultBackendProvider auto-detection from environment variables
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
        provider = CacheClientProvider()
        with pytest.raises(NotImplementedError):
            provider.get_sync_client()

    def test_get_async_client_not_implemented(self) -> None:
        provider = CacheClientProvider()
        with pytest.raises(NotImplementedError):
            import asyncio

            asyncio.run(provider.get_async_client())


@pytest.mark.unit
class TestLoggerProvider:
    """Test LoggerProvider abstract interface."""

    def test_get_logger_not_implemented(self) -> None:
        provider = LoggerProvider()
        with pytest.raises(NotImplementedError):
            provider.get_logger("test")


@pytest.mark.unit
class TestSimpleLogger:
    """Test SimpleLogger wrapper for cache-specific logging."""

    def test_debug_message(self) -> None:
        mock_logger = mock.MagicMock()
        logger = SimpleLogger(mock_logger)
        logger.debug("test message")
        mock_logger.debug.assert_called_once_with("test message")

    def test_info_message(self) -> None:
        mock_logger = mock.MagicMock()
        logger = SimpleLogger(mock_logger)
        logger.info("test info")
        mock_logger.info.assert_called_once_with("test info")

    def test_info_message_with_extra(self) -> None:
        mock_logger = mock.MagicMock()
        logger = SimpleLogger(mock_logger)
        logger.info("test info", extra={"key": "value"})
        mock_logger.info.assert_called_once_with("test info")

    def test_warning_message(self) -> None:
        mock_logger = mock.MagicMock()
        logger = SimpleLogger(mock_logger)
        logger.warning("test warning")
        mock_logger.warning.assert_called_once_with("test warning")

    def test_error_message(self) -> None:
        mock_logger = mock.MagicMock()
        logger = SimpleLogger(mock_logger)
        logger.error("test error")
        mock_logger.error.assert_called_once_with("test error")

    def test_cache_hit_default_source(self) -> None:
        mock_logger = mock.MagicMock()
        logger = SimpleLogger(mock_logger)
        logger.cache_hit("key:123")
        mock_logger.debug.assert_called_once_with("Redis cache hit for key: key:123")

    def test_cache_hit_custom_source(self) -> None:
        mock_logger = mock.MagicMock()
        logger = SimpleLogger(mock_logger)
        logger.cache_hit("key:456", source="Memcached")
        mock_logger.debug.assert_called_once_with("Memcached cache hit for key: key:456")

    def test_cache_miss(self) -> None:
        mock_logger = mock.MagicMock()
        logger = SimpleLogger(mock_logger)
        logger.cache_miss("key:789")
        mock_logger.debug.assert_called_once_with("Cache miss for key: key:789")

    def test_cache_stored_without_ttl(self) -> None:
        mock_logger = mock.MagicMock()
        logger = SimpleLogger(mock_logger)
        logger.cache_stored("key:111")
        mock_logger.debug.assert_called_once_with("Cached result for key: key:111")

    def test_cache_stored_with_ttl(self) -> None:
        mock_logger = mock.MagicMock()
        logger = SimpleLogger(mock_logger)
        logger.cache_stored("key:222", ttl=3600)
        mock_logger.debug.assert_called_once_with("Cached result for key: key:222 with TTL 3600")

    def test_cache_invalidated_default_source(self) -> None:
        mock_logger = mock.MagicMock()
        logger = SimpleLogger(mock_logger)
        logger.cache_invalidated("key:333")
        mock_logger.debug.assert_called_once_with("Invalidated Redis cache for key: key:333")

    def test_cache_invalidated_custom_source(self) -> None:
        mock_logger = mock.MagicMock()
        logger = SimpleLogger(mock_logger)
        logger.cache_invalidated("key:444", source="L1")
        mock_logger.debug.assert_called_once_with("Invalidated L1 cache for key: key:444")


@pytest.mark.unit
class TestDefaultLoggerProvider:
    """Test DefaultLoggerProvider creates SimpleLogger instances."""

    def test_get_logger_returns_simple_logger(self) -> None:
        provider = DefaultLoggerProvider()
        logger = provider.get_logger("test.module")
        assert isinstance(logger, SimpleLogger)

    def test_get_logger_uses_standard_logging(self) -> None:
        provider = DefaultLoggerProvider()
        logger = provider.get_logger("test.module")
        assert isinstance(logger._logger, logging.Logger)
        assert logger._logger.name == "test.module"

    def test_get_logger_different_names(self) -> None:
        provider = DefaultLoggerProvider()
        logger1 = provider.get_logger("module1")
        logger2 = provider.get_logger("module2")
        assert logger1._logger.name == "module1"
        assert logger2._logger.name == "module2"


@pytest.mark.unit
class TestBackendProviderInterface:
    """Test BackendProviderInterface abstract interface."""

    def test_get_backend_not_implemented(self) -> None:
        provider = BackendProviderInterface()
        with pytest.raises(NotImplementedError):
            provider.get_backend()


@pytest.mark.unit
class TestDefaultCacheClientProvider:
    """Test DefaultCacheClientProvider implementation."""

    def test_get_sync_client(self) -> None:
        provider = DefaultCacheClientProvider()
        with mock.patch("cachekit.backends.redis.client.get_cached_redis_client") as mock_get:
            mock_client = mock.MagicMock()
            mock_get.return_value = mock_client
            client = provider.get_sync_client()
            assert client is mock_client
            mock_get.assert_called_once()

    @pytest.mark.asyncio
    async def test_get_async_client(self) -> None:
        provider = DefaultCacheClientProvider()
        with mock.patch("cachekit.backends.redis.client.get_cached_async_redis_client") as mock_get:
            mock_client = mock.AsyncMock()
            mock_get.return_value = mock_client
            client = await provider.get_async_client()
            assert client is mock_client
            mock_get.assert_called_once()


@pytest.mark.unit
class TestDefaultBackendProviderAutoDetect:
    """Test DefaultBackendProvider auto-detection from environment variables."""

    def test_cachekitio_from_api_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """CACHEKIT_API_KEY resolves to CachekitIOBackend."""
        monkeypatch.setenv("CACHEKIT_API_KEY", "ck_test_abc123")
        monkeypatch.delenv("CACHEKIT_REDIS_URL", raising=False)
        monkeypatch.delenv("REDIS_URL", raising=False)
        monkeypatch.delenv("CACHEKIT_MEMCACHED_SERVERS", raising=False)
        monkeypatch.delenv("CACHEKIT_FILE_CACHE_DIR", raising=False)

        with mock.patch("cachekit.backends.cachekitio.CachekitIOBackend") as mock_backend:
            mock_instance = mock.MagicMock()
            mock_backend.return_value = mock_instance

            provider = DefaultBackendProvider()
            backend = provider.get_backend()

            assert backend is mock_instance
            mock_backend.assert_called_once()

    def test_redis_from_cachekit_redis_url(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """CACHEKIT_REDIS_URL resolves to RedisBackend."""
        monkeypatch.setenv("CACHEKIT_REDIS_URL", "redis://localhost:6379")
        monkeypatch.delenv("CACHEKIT_API_KEY", raising=False)
        monkeypatch.delenv("REDIS_URL", raising=False)
        monkeypatch.delenv("CACHEKIT_MEMCACHED_SERVERS", raising=False)
        monkeypatch.delenv("CACHEKIT_FILE_CACHE_DIR", raising=False)

        with mock.patch("cachekit.backends.redis.provider.RedisBackendProvider") as mock_provider:
            with mock.patch("cachekit.backends.redis.provider.tenant_context") as mock_ctx:
                mock_ctx.get.return_value = None
                mock_inst = mock.MagicMock()
                mock_backend = mock.MagicMock()
                mock_provider.return_value = mock_inst
                mock_inst.get_backend.return_value = mock_backend

                provider = DefaultBackendProvider()
                backend = provider.get_backend()

                assert backend is mock_backend
                mock_provider.assert_called_once()

    def test_redis_from_fallback_redis_url(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """REDIS_URL (fallback) resolves to RedisBackend."""
        monkeypatch.setenv("REDIS_URL", "redis://fallback:6379")
        monkeypatch.delenv("CACHEKIT_API_KEY", raising=False)
        monkeypatch.delenv("CACHEKIT_REDIS_URL", raising=False)
        monkeypatch.delenv("CACHEKIT_MEMCACHED_SERVERS", raising=False)
        monkeypatch.delenv("CACHEKIT_FILE_CACHE_DIR", raising=False)

        with mock.patch("cachekit.backends.redis.provider.RedisBackendProvider") as mock_provider:
            with mock.patch("cachekit.backends.redis.provider.tenant_context") as mock_ctx:
                mock_ctx.get.return_value = None
                mock_inst = mock.MagicMock()
                mock_backend = mock.MagicMock()
                mock_provider.return_value = mock_inst
                mock_inst.get_backend.return_value = mock_backend

                provider = DefaultBackendProvider()
                backend = provider.get_backend()

                assert backend is mock_backend

    def test_memcached_from_servers(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """CACHEKIT_MEMCACHED_SERVERS resolves to MemcachedBackend."""
        monkeypatch.setenv("CACHEKIT_MEMCACHED_SERVERS", '["127.0.0.1:11211"]')
        monkeypatch.delenv("CACHEKIT_API_KEY", raising=False)
        monkeypatch.delenv("CACHEKIT_REDIS_URL", raising=False)
        monkeypatch.delenv("REDIS_URL", raising=False)
        monkeypatch.delenv("CACHEKIT_FILE_CACHE_DIR", raising=False)

        with mock.patch("cachekit.backends.memcached.MemcachedBackend") as mock_backend:
            mock_instance = mock.MagicMock()
            mock_backend.return_value = mock_instance

            provider = DefaultBackendProvider()
            backend = provider.get_backend()

            assert backend is mock_instance
            mock_backend.assert_called_once()

    def test_file_from_cache_dir(self, monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
        """CACHEKIT_FILE_CACHE_DIR resolves to FileBackend."""
        monkeypatch.setenv("CACHEKIT_FILE_CACHE_DIR", str(tmp_path))
        monkeypatch.delenv("CACHEKIT_API_KEY", raising=False)
        monkeypatch.delenv("CACHEKIT_REDIS_URL", raising=False)
        monkeypatch.delenv("REDIS_URL", raising=False)
        monkeypatch.delenv("CACHEKIT_MEMCACHED_SERVERS", raising=False)

        with mock.patch("cachekit.backends.file.FileBackend") as mock_backend:
            mock_instance = mock.MagicMock()
            mock_backend.return_value = mock_instance

            provider = DefaultBackendProvider()
            backend = provider.get_backend()

            assert backend is mock_instance
            mock_backend.assert_called_once()

    def test_api_key_wins_over_redis_url(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """CACHEKIT_API_KEY has higher priority than REDIS_URL."""
        monkeypatch.setenv("CACHEKIT_API_KEY", "ck_test_priority")
        monkeypatch.setenv("REDIS_URL", "redis://localhost:6379")
        monkeypatch.delenv("CACHEKIT_REDIS_URL", raising=False)
        monkeypatch.delenv("CACHEKIT_MEMCACHED_SERVERS", raising=False)
        monkeypatch.delenv("CACHEKIT_FILE_CACHE_DIR", raising=False)

        with mock.patch("cachekit.backends.cachekitio.CachekitIOBackend") as mock_backend:
            mock_instance = mock.MagicMock()
            mock_backend.return_value = mock_instance

            provider = DefaultBackendProvider()
            backend = provider.get_backend()

            assert backend is mock_instance

    def test_warns_when_both_api_key_and_redis(self, monkeypatch: pytest.MonkeyPatch, caplog) -> None:
        """Logs warning when both CachekitIO and Redis env vars are set."""
        monkeypatch.setenv("CACHEKIT_API_KEY", "ck_test_warn")
        monkeypatch.setenv("CACHEKIT_REDIS_URL", "redis://localhost:6379")
        monkeypatch.delenv("CACHEKIT_MEMCACHED_SERVERS", raising=False)
        monkeypatch.delenv("CACHEKIT_FILE_CACHE_DIR", raising=False)

        with mock.patch("cachekit.backends.cachekitio.CachekitIOBackend") as mock_backend:
            mock_backend.return_value = mock.MagicMock()

            with caplog.at_level(logging.WARNING):
                provider = DefaultBackendProvider()
                provider.get_backend()

            assert "Both CACHEKIT_API_KEY and Redis URL configured" in caplog.text

    def test_caches_backend_on_subsequent_calls(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Second call reuses the backend from first call."""
        monkeypatch.setenv("CACHEKIT_API_KEY", "ck_test_cache")
        monkeypatch.delenv("CACHEKIT_REDIS_URL", raising=False)
        monkeypatch.delenv("REDIS_URL", raising=False)
        monkeypatch.delenv("CACHEKIT_MEMCACHED_SERVERS", raising=False)
        monkeypatch.delenv("CACHEKIT_FILE_CACHE_DIR", raising=False)

        with mock.patch("cachekit.backends.cachekitio.CachekitIOBackend") as mock_backend:
            mock_instance = mock.MagicMock()
            mock_backend.return_value = mock_instance

            provider = DefaultBackendProvider()
            b1 = provider.get_backend()
            b2 = provider.get_backend()

            assert b1 is b2
            mock_backend.assert_called_once()

    def test_fallback_to_redis_when_no_env_vars(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """No env vars → falls back to Redis (will fail at connection time)."""
        monkeypatch.delenv("CACHEKIT_API_KEY", raising=False)
        monkeypatch.delenv("CACHEKIT_REDIS_URL", raising=False)
        monkeypatch.delenv("REDIS_URL", raising=False)
        monkeypatch.delenv("CACHEKIT_MEMCACHED_SERVERS", raising=False)
        monkeypatch.delenv("CACHEKIT_FILE_CACHE_DIR", raising=False)

        with mock.patch("cachekit.backends.redis.provider.RedisBackendProvider") as mock_provider:
            with mock.patch("cachekit.backends.redis.provider.tenant_context") as mock_ctx:
                mock_ctx.get.return_value = None
                mock_inst = mock.MagicMock()
                mock_backend = mock.MagicMock()
                mock_provider.return_value = mock_inst
                mock_inst.get_backend.return_value = mock_backend

                provider = DefaultBackendProvider()
                backend = provider.get_backend()

                assert backend is mock_backend

    def test_redis_sets_default_tenant(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Redis path sets tenant_context to 'default' if not already set."""
        monkeypatch.setenv("CACHEKIT_REDIS_URL", "redis://localhost:6379")
        monkeypatch.delenv("CACHEKIT_API_KEY", raising=False)
        monkeypatch.delenv("REDIS_URL", raising=False)
        monkeypatch.delenv("CACHEKIT_MEMCACHED_SERVERS", raising=False)
        monkeypatch.delenv("CACHEKIT_FILE_CACHE_DIR", raising=False)

        with mock.patch("cachekit.backends.redis.provider.RedisBackendProvider") as mock_provider:
            with mock.patch("cachekit.backends.redis.provider.tenant_context") as mock_ctx:
                mock_ctx.get.return_value = None
                mock_provider.return_value = mock.MagicMock()
                mock_provider.return_value.get_backend.return_value = mock.MagicMock()

                provider = DefaultBackendProvider()
                provider.get_backend()

                mock_ctx.set.assert_called_once_with("default")
