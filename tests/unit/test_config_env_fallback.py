"""Test that pydantic-settings properly handles environment variables for backend configs."""

import pytest

from cachekit.backends.redis.config import RedisBackendConfig
from cachekit.config import CachekitConfig


class TestRedisBackendConfigEnv:
    """Test that RedisBackendConfig properly loads from environment variables."""

    def test_redis_config_default_values_no_env(self, monkeypatch):
        """Test default values when no env vars are set."""
        # Clear all possible env vars
        env_vars = [
            "CACHEKIT_REDIS_URL",
            "CACHEKIT_CONNECTION_POOL_SIZE",
            "CACHEKIT_SOCKET_KEEPALIVE",
            "CACHEKIT_DISABLE_HIREDIS",
        ]
        for var in env_vars:
            monkeypatch.delenv(var, raising=False)

        config = RedisBackendConfig()
        assert config.redis_url == "redis://localhost:6379"
        assert config.connection_pool_size == 10
        assert config.socket_keepalive is True
        assert config.disable_hiredis is False

    def test_redis_config_from_env(self, monkeypatch):
        """Test that RedisBackendConfig loads from CACHEKIT_* env vars."""
        monkeypatch.setenv("CACHEKIT_REDIS_URL", "redis://primary:6379/1")
        monkeypatch.setenv("CACHEKIT_CONNECTION_POOL_SIZE", "25")
        monkeypatch.setenv("CACHEKIT_SOCKET_KEEPALIVE", "false")
        monkeypatch.setenv("CACHEKIT_DISABLE_HIREDIS", "true")

        config = RedisBackendConfig.from_env()
        assert config.redis_url == "redis://primary:6379/1"
        assert config.connection_pool_size == 25
        assert config.socket_keepalive is False
        assert config.disable_hiredis is True

    def test_redis_config_validation_pool_size(self, monkeypatch):
        """Test that connection_pool_size validation works."""
        monkeypatch.setenv("CACHEKIT_CONNECTION_POOL_SIZE", "0")  # Must be > 0

        with pytest.raises(ValueError) as exc_info:
            RedisBackendConfig()
        assert "greater than 0" in str(exc_info.value).lower()

    def test_cachekit_config_has_no_redis_fields(self):
        """Test that CachekitConfig contains no Redis-specific fields."""
        config = CachekitConfig()

        # Verify Redis fields are not in CachekitConfig
        assert not hasattr(config, "redis_url")
        assert not hasattr(config, "connection_pool_size")
        assert not hasattr(config, "socket_keepalive")
        assert not hasattr(config, "disable_hiredis")

    def test_cachekit_config_generic_fields(self, monkeypatch):
        """Test that CachekitConfig loads generic cache settings."""
        monkeypatch.setenv("CACHEKIT_DEFAULT_TTL", "7200")
        monkeypatch.setenv("CACHEKIT_MAX_RETRIES", "5")
        monkeypatch.setenv("CACHEKIT_MAX_CHUNK_SIZE_MB", "100")

        config = CachekitConfig.from_env()
        assert config.default_ttl == 7200
        assert config.max_retries == 5
        assert config.max_chunk_size_mb == 100

    def test_backend_and_cache_configs_independent(self, monkeypatch):
        """Test that backend and cache configs are loaded independently."""
        # Set Redis-specific env vars
        monkeypatch.setenv("CACHEKIT_REDIS_URL", "redis://backend:6379/2")
        monkeypatch.setenv("CACHEKIT_CONNECTION_POOL_SIZE", "30")

        # Set cache-specific env vars
        monkeypatch.setenv("CACHEKIT_DEFAULT_TTL", "1800")
        monkeypatch.setenv("CACHEKIT_ENABLE_COMPRESSION", "false")

        redis_config = RedisBackendConfig.from_env()
        cache_config = CachekitConfig.from_env()

        # Verify backend config loaded correctly
        assert redis_config.redis_url == "redis://backend:6379/2"
        assert redis_config.connection_pool_size == 30

        # Verify cache config loaded correctly
        assert cache_config.default_ttl == 1800
        assert cache_config.enable_compression is False

        # Verify no cross-contamination
        assert not hasattr(cache_config, "redis_url")
        assert hasattr(cache_config, "default_ttl")


class TestRedisUrlAliasChoicesPriority:
    """Test AliasChoices priority for redis_url field.

    The redis_url field uses AliasChoices to support both:
    - CACHEKIT_REDIS_URL (namespaced, takes priority)
    - REDIS_URL (standard 12-factor convention, fallback)
    """

    def test_only_redis_url_set(self, monkeypatch):
        """REDIS_URL alone should work (12-factor convention)."""
        monkeypatch.delenv("CACHEKIT_REDIS_URL", raising=False)
        monkeypatch.setenv("REDIS_URL", "redis://generic:6379")

        config = RedisBackendConfig()
        assert config.redis_url == "redis://generic:6379"

    def test_only_redis_url_via_from_env(self, monkeypatch):
        """REDIS_URL works via from_env() factory method."""
        monkeypatch.delenv("CACHEKIT_REDIS_URL", raising=False)
        monkeypatch.setenv("REDIS_URL", "redis://factory:6379")

        config = RedisBackendConfig.from_env()
        assert config.redis_url == "redis://factory:6379"

    def test_only_cachekit_redis_url_set(self, monkeypatch):
        """CACHEKIT_REDIS_URL alone should work (namespaced)."""
        monkeypatch.delenv("REDIS_URL", raising=False)
        monkeypatch.setenv("CACHEKIT_REDIS_URL", "redis://cachekit:6379")

        config = RedisBackendConfig()
        assert config.redis_url == "redis://cachekit:6379"

    def test_both_set_cachekit_wins(self, monkeypatch):
        """CACHEKIT_REDIS_URL takes priority over REDIS_URL."""
        monkeypatch.setenv("CACHEKIT_REDIS_URL", "redis://cachekit-priority:6379")
        monkeypatch.setenv("REDIS_URL", "redis://generic-fallback:6379")

        config = RedisBackendConfig()
        assert config.redis_url == "redis://cachekit-priority:6379"

    def test_neither_set_uses_default(self, monkeypatch):
        """Falls back to localhost:6379 when neither env var is set."""
        monkeypatch.delenv("CACHEKIT_REDIS_URL", raising=False)
        monkeypatch.delenv("REDIS_URL", raising=False)

        config = RedisBackendConfig()
        assert config.redis_url == "redis://localhost:6379"
