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
