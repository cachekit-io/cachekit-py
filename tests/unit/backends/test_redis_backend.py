"""Unit tests for the Redis connection-pool configuration and RedisBackend.get() contract.

These are mocked (no real Redis) and live under tests/unit/ so they run on pull
requests — unlike the real-Redis suite in tests/integration/test_redis_backend.py,
which CI only runs on push-to-main.

Regression coverage for #154: the shared pools must use decode_responses=False so
binary payloads (LZ4 / Arrow IPC / AES-256-GCM ciphertext) are never UTF-8 decoded,
and RedisBackend.get() must return those raw bytes (or None) without coercion.
"""

from __future__ import annotations

from unittest.mock import Mock, patch

import pytest

from cachekit.backends.redis import RedisBackend


@pytest.mark.unit
class TestRedisPoolDecodeResponses:
    """The shared sync and async pools must be created with decode_responses=False."""

    @staticmethod
    def _reset(monkeypatch):
        import cachekit.backends.redis.client as rc
        from cachekit.config.singleton import reset_settings

        monkeypatch.setenv("CACHEKIT_REDIS_URL", "redis://localhost:6379")
        rc._pool_instance = None
        rc._async_pool_instance = None
        # get_cached_redis_client() short-circuits on the thread-local client,
        # so a client cached by an earlier test would skip pool creation.
        if hasattr(rc._thread_local, "sync_client"):
            rc._thread_local.sync_client = None
        reset_settings()
        return rc

    def test_sync_pool_uses_decode_responses_false(self, monkeypatch):
        rc = self._reset(monkeypatch)
        from cachekit.config.singleton import reset_settings

        with patch("redis.ConnectionPool.from_url") as mock_from_url, patch("redis.Redis"):
            try:
                rc.get_cached_redis_client()
            finally:
                rc._pool_instance = None
                reset_settings()
        assert mock_from_url.call_args.kwargs["decode_responses"] is False

    async def test_async_pool_uses_decode_responses_false(self, monkeypatch):
        rc = self._reset(monkeypatch)
        from cachekit.config.singleton import reset_settings

        with patch("redis.asyncio.ConnectionPool.from_url") as mock_from_url, patch("redis.asyncio.Redis"):
            try:
                await rc.get_async_redis_client()
            finally:
                rc._async_pool_instance = None
                reset_settings()
        assert mock_from_url.call_args.kwargs["decode_responses"] is False

    def test_sync_pool_has_finite_socket_timeouts(self, monkeypatch):
        """#222: an unreachable Redis must fail fast, not block on the OS TCP timeout."""
        rc = self._reset(monkeypatch)
        from cachekit.config.singleton import reset_settings

        with patch("redis.ConnectionPool.from_url") as mock_from_url, patch("redis.Redis"):
            try:
                rc.get_cached_redis_client()
            finally:
                rc._pool_instance = None
                reset_settings()
        assert mock_from_url.call_args.kwargs["socket_timeout"] == 5.0
        assert mock_from_url.call_args.kwargs["socket_connect_timeout"] == 5.0

    async def test_async_pool_has_finite_socket_timeouts(self, monkeypatch):
        rc = self._reset(monkeypatch)
        from cachekit.config.singleton import reset_settings

        with patch("redis.asyncio.ConnectionPool.from_url") as mock_from_url, patch("redis.asyncio.Redis"):
            try:
                await rc.get_async_redis_client()
            finally:
                rc._async_pool_instance = None
                reset_settings()
        assert mock_from_url.call_args.kwargs["socket_timeout"] == 5.0
        assert mock_from_url.call_args.kwargs["socket_connect_timeout"] == 5.0

    def test_socket_timeouts_configurable_via_env(self, monkeypatch):
        from cachekit.backends.redis.config import RedisBackendConfig

        monkeypatch.setenv("CACHEKIT_SOCKET_TIMEOUT", "1.5")
        monkeypatch.setenv("CACHEKIT_SOCKET_CONNECT_TIMEOUT", "0.7")
        config = RedisBackendConfig.from_env()
        assert config.socket_timeout == 1.5
        assert config.socket_connect_timeout == 0.7


@pytest.mark.unit
class TestRedisBackendProviderResolution:
    """#222 regression: RedisBackend honours redis_url and works zero-config.

    Construction never touches the network (pools connect lazily), so these
    run without a real Redis.
    """

    def test_explicit_url_wins_over_env(self, monkeypatch):
        """A URL argument that differs from env must connect to the argument."""
        from cachekit.backends.provider import PooledClientProvider

        monkeypatch.setenv("CACHEKIT_REDIS_URL", "redis://env-host:6379")
        backend = RedisBackend(redis_url="redis://arg-host:6390/3")

        provider = backend._client_provider
        assert isinstance(provider, PooledClientProvider)
        kwargs = provider._pool.connection_kwargs
        assert kwargs["host"] == "arg-host"
        assert kwargs["port"] == 6390
        assert kwargs["db"] == 3

    def test_explicit_url_never_consults_di_container(self):
        with patch("cachekit.backends.redis.backend.DIContainer") as mock_di:
            RedisBackend(redis_url="redis://arg-host:6379")
        mock_di.assert_not_called()

    def test_zero_config_without_registered_provider(self, monkeypatch):
        """The documented RedisBackend() example must not crash (#222 defect 2)."""
        from cachekit.backends.provider import CacheClientProvider, PooledClientProvider
        from cachekit.di import DIContainer

        monkeypatch.delenv("CACHEKIT_REDIS_URL", raising=False)
        monkeypatch.delenv("REDIS_URL", raising=False)

        # Simulate a fresh process: no CacheClientProvider registered
        # (the test conftest registers one; production code never does).
        container = DIContainer()
        saved_service = container._services.pop(CacheClientProvider, None)
        saved_singleton = container._singletons.pop(CacheClientProvider, None)
        try:
            backend = RedisBackend()
            assert isinstance(backend._client_provider, PooledClientProvider)
            assert backend._client_provider._pool.connection_kwargs["host"] == "localhost"
        finally:
            if saved_service is not None:
                container._services[CacheClientProvider] = saved_service
            if saved_singleton is not None:
                container._singletons[CacheClientProvider] = saved_singleton

    def test_zero_config_honours_di_registered_provider(self):
        """Back-compat: a DI-registered provider still wins for RedisBackend()."""
        from cachekit.backends.provider import CacheClientProvider
        from cachekit.di import DIContainer

        class _SentinelProvider(CacheClientProvider):
            pass

        container = DIContainer()
        saved_service = container._services.get(CacheClientProvider)
        saved_singleton = container._singletons.get(CacheClientProvider)
        container.register(CacheClientProvider, _SentinelProvider, singleton=False)
        try:
            backend = RedisBackend()
            assert isinstance(backend._client_provider, _SentinelProvider)
        finally:
            container._singletons.pop(CacheClientProvider, None)
            if saved_service is not None:
                container._services[CacheClientProvider] = saved_service
            else:
                container._services.pop(CacheClientProvider, None)
            if saved_singleton is not None:
                container._singletons[CacheClientProvider] = saved_singleton

    def test_explicit_client_provider_wins_over_url(self):
        """The radar workaround pattern: an injected provider is used as-is."""
        from cachekit.backends.provider import CacheClientProvider

        provider = Mock(spec=CacheClientProvider)
        backend = RedisBackend(redis_url="redis://ignored:6379", client_provider=provider)
        assert backend._client_provider is provider

    def test_config_object_accepted_positionally(self):
        """docs/backends/redis.md promises RedisBackend(config) — honour it."""
        from cachekit.backends.provider import PooledClientProvider
        from cachekit.backends.redis.config import RedisBackendConfig

        config = RedisBackendConfig(
            redis_url="redis://cfg-host:7000",
            connection_pool_size=3,
            socket_timeout=1.5,
        )
        backend = RedisBackend(config)

        provider = backend._client_provider
        assert isinstance(provider, PooledClientProvider)
        assert provider._pool.connection_kwargs["host"] == "cfg-host"
        assert provider._pool.max_connections == 3
        assert provider._pool.connection_kwargs["socket_timeout"] == 1.5

    def test_per_instance_pool_has_finite_socket_timeouts(self):
        from cachekit.backends.provider import PooledClientProvider

        backend = RedisBackend(redis_url="redis://arg-host:6379")
        provider = backend._client_provider
        assert isinstance(provider, PooledClientProvider)
        kwargs = provider._pool.connection_kwargs
        assert kwargs["socket_timeout"] == 5.0
        assert kwargs["socket_connect_timeout"] == 5.0


@pytest.mark.unit
class TestRedisBackendGetContract:
    """get() returns raw bytes (or None) — never str, never UTF-8 decoded.

    Uses explicit client_provider injection (no DIContainer / env patching) so the
    tests are independent of REDIS_URL vs CACHEKIT_REDIS_URL alias resolution.
    """

    @staticmethod
    def _backend_returning(value):
        from cachekit.backends.provider import CacheClientProvider

        mock_client = Mock()
        mock_client.get.return_value = value
        provider = Mock(spec=CacheClientProvider)
        provider.get_sync_client.return_value = mock_client
        return RedisBackend("redis://localhost:6379", client_provider=provider)

    def test_get_returns_non_utf8_bytes_unchanged(self):
        backend = self._backend_returning(b"\x82\xa3val\xff\xfe")
        result = backend.get("k")
        assert result == b"\x82\xa3val\xff\xfe"
        assert isinstance(result, bytes)

    def test_get_returns_none_for_missing_key(self):
        backend = self._backend_returning(None)
        assert backend.get("missing") is None

    def test_get_returns_none_for_non_bytes_response(self):
        # decode_responses=False means this never happens in practice, but the
        # bytes|None narrowing guard must hold defensively (no str coercion).
        backend = self._backend_returning("unexpected-str")
        assert backend.get("k") is None
