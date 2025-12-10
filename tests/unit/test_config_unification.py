"""Test backend-specific configuration separation."""

from cachekit.backends.redis.client import reset_global_pool
from cachekit.backends.redis.config import RedisBackendConfig
from cachekit.config import CachekitConfig, reset_settings


class TestConfigSeparation:
    """Test that Redis-specific config is separated from generic cache config."""

    def setup_method(self):
        """Reset configurations before each test."""
        reset_settings()
        reset_global_pool()

    def test_cachekit_config_has_no_redis_fields(self):
        """Test that CachekitConfig contains no Redis-specific fields."""
        cache_config = CachekitConfig.from_env()

        # Verify Redis-specific fields are NOT in CachekitConfig
        assert not hasattr(cache_config, "redis_url")
        assert not hasattr(cache_config, "connection_pool_size")
        assert not hasattr(cache_config, "socket_keepalive")
        assert not hasattr(cache_config, "disable_hiredis")

        # Verify generic cache fields ARE present
        assert hasattr(cache_config, "default_ttl")
        assert hasattr(cache_config, "max_chunk_size_mb")
        assert hasattr(cache_config, "enable_compression")

    def test_redis_backend_config_loads_from_env(self, monkeypatch):
        """Test that RedisBackendConfig loads Redis-specific settings."""
        monkeypatch.setenv("CACHEKIT_REDIS_URL", "redis://test:6379/0")
        monkeypatch.setenv("CACHEKIT_CONNECTION_POOL_SIZE", "20")
        monkeypatch.setenv("CACHEKIT_DISABLE_HIREDIS", "true")

        redis_config = RedisBackendConfig.from_env()

        assert redis_config.redis_url == "redis://test:6379/0"
        assert redis_config.connection_pool_size == 20
        assert redis_config.disable_hiredis is True

    def test_redis_backend_config_defaults(self):
        """Test that RedisBackendConfig has sensible defaults."""
        redis_config = RedisBackendConfig()

        assert redis_config.redis_url == "redis://localhost:6379"
        assert redis_config.connection_pool_size == 10
        assert redis_config.socket_keepalive is True
        assert redis_config.disable_hiredis is False
