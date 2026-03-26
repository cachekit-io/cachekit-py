"""Unit tests for MemcachedBackendConfig.

Tests configuration parsing, validation rules, environment variable loading,
and the from_env() classmethod for the Memcached cache backend.
"""

from __future__ import annotations

import os

import pytest
from pydantic import ValidationError

from cachekit.backends.memcached.config import MemcachedBackendConfig


@pytest.mark.unit
class TestMemcachedBackendConfigDefaults:
    """Test default configuration values."""

    def test_default_servers(self):
        """Test that default servers is localhost:11211."""
        config = MemcachedBackendConfig()
        assert config.servers == ["127.0.0.1:11211"]

    def test_default_connect_timeout(self):
        """Test that default connect_timeout is 2.0 seconds."""
        config = MemcachedBackendConfig()
        assert config.connect_timeout == 2.0

    def test_default_timeout(self):
        """Test that default timeout is 1.0 seconds."""
        config = MemcachedBackendConfig()
        assert config.timeout == 1.0

    def test_default_max_pool_size(self):
        """Test that default max_pool_size is 10."""
        config = MemcachedBackendConfig()
        assert config.max_pool_size == 10

    def test_default_retry_attempts(self):
        """Test that default retry_attempts is 2."""
        config = MemcachedBackendConfig()
        assert config.retry_attempts == 2

    def test_default_key_prefix(self):
        """Test that default key_prefix is empty string."""
        config = MemcachedBackendConfig()
        assert config.key_prefix == ""


@pytest.mark.unit
class TestMemcachedBackendConfigConstructor:
    """Test constructor with custom values."""

    def test_custom_servers(self):
        """Test setting custom servers via constructor."""
        config = MemcachedBackendConfig(servers=["mc1:11211", "mc2:11211"])
        assert config.servers == ["mc1:11211", "mc2:11211"]

    def test_custom_connect_timeout(self):
        """Test setting custom connect_timeout via constructor."""
        config = MemcachedBackendConfig(connect_timeout=5.0)
        assert config.connect_timeout == 5.0

    def test_custom_timeout(self):
        """Test setting custom timeout via constructor."""
        config = MemcachedBackendConfig(timeout=0.5)
        assert config.timeout == 0.5

    def test_custom_max_pool_size(self):
        """Test setting custom max_pool_size via constructor."""
        config = MemcachedBackendConfig(max_pool_size=20)
        assert config.max_pool_size == 20

    def test_custom_retry_attempts(self):
        """Test setting custom retry_attempts via constructor."""
        config = MemcachedBackendConfig(retry_attempts=5)
        assert config.retry_attempts == 5

    def test_custom_key_prefix(self):
        """Test setting custom key_prefix via constructor."""
        config = MemcachedBackendConfig(key_prefix="myapp:")
        assert config.key_prefix == "myapp:"

    def test_all_custom_values(self):
        """Test setting all values via constructor."""
        config = MemcachedBackendConfig(
            servers=["mc1:11211", "mc2:11212"],
            connect_timeout=5.0,
            timeout=2.0,
            max_pool_size=50,
            retry_attempts=3,
            key_prefix="test:",
        )
        assert config.servers == ["mc1:11211", "mc2:11212"]
        assert config.connect_timeout == 5.0
        assert config.timeout == 2.0
        assert config.max_pool_size == 50
        assert config.retry_attempts == 3
        assert config.key_prefix == "test:"


@pytest.mark.unit
class TestMemcachedBackendConfigValidation:
    """Test validation rules."""

    def test_connect_timeout_rejects_below_minimum(self):
        """Test that connect_timeout rejects values < 0.1."""
        with pytest.raises(ValidationError) as exc_info:
            MemcachedBackendConfig(connect_timeout=0.05)
        errors = exc_info.value.errors()
        assert any("greater than or equal to 0.1" in str(e) for e in errors)

    def test_connect_timeout_rejects_above_maximum(self):
        """Test that connect_timeout rejects values > 30.0."""
        with pytest.raises(ValidationError) as exc_info:
            MemcachedBackendConfig(connect_timeout=30.1)
        errors = exc_info.value.errors()
        assert any("less than or equal to 30" in str(e) for e in errors)

    def test_connect_timeout_accepts_boundaries(self):
        """Test that connect_timeout accepts boundary values."""
        config_min = MemcachedBackendConfig(connect_timeout=0.1)
        assert config_min.connect_timeout == 0.1

        config_max = MemcachedBackendConfig(connect_timeout=30.0)
        assert config_max.connect_timeout == 30.0

    def test_timeout_rejects_below_minimum(self):
        """Test that timeout rejects values < 0.1."""
        with pytest.raises(ValidationError) as exc_info:
            MemcachedBackendConfig(timeout=0.05)
        errors = exc_info.value.errors()
        assert any("greater than or equal to 0.1" in str(e) for e in errors)

    def test_timeout_rejects_above_maximum(self):
        """Test that timeout rejects values > 30.0."""
        with pytest.raises(ValidationError) as exc_info:
            MemcachedBackendConfig(timeout=30.1)
        errors = exc_info.value.errors()
        assert any("less than or equal to 30" in str(e) for e in errors)

    def test_timeout_accepts_boundaries(self):
        """Test that timeout accepts boundary values."""
        config_min = MemcachedBackendConfig(timeout=0.1)
        assert config_min.timeout == 0.1

        config_max = MemcachedBackendConfig(timeout=30.0)
        assert config_max.timeout == 30.0

    def test_max_pool_size_rejects_below_minimum(self):
        """Test that max_pool_size rejects values < 1."""
        with pytest.raises(ValidationError) as exc_info:
            MemcachedBackendConfig(max_pool_size=0)
        errors = exc_info.value.errors()
        assert any("greater than or equal to 1" in str(e) for e in errors)

    def test_max_pool_size_rejects_above_maximum(self):
        """Test that max_pool_size rejects values > 100."""
        with pytest.raises(ValidationError) as exc_info:
            MemcachedBackendConfig(max_pool_size=101)
        errors = exc_info.value.errors()
        assert any("less than or equal to 100" in str(e) for e in errors)

    def test_max_pool_size_accepts_boundaries(self):
        """Test that max_pool_size accepts boundary values."""
        config_min = MemcachedBackendConfig(max_pool_size=1)
        assert config_min.max_pool_size == 1

        config_max = MemcachedBackendConfig(max_pool_size=100)
        assert config_max.max_pool_size == 100

    def test_retry_attempts_rejects_below_minimum(self):
        """Test that retry_attempts rejects values < 0."""
        with pytest.raises(ValidationError) as exc_info:
            MemcachedBackendConfig(retry_attempts=-1)
        errors = exc_info.value.errors()
        assert any("greater than or equal to 0" in str(e) for e in errors)

    def test_retry_attempts_rejects_above_maximum(self):
        """Test that retry_attempts rejects values > 10."""
        with pytest.raises(ValidationError) as exc_info:
            MemcachedBackendConfig(retry_attempts=11)
        errors = exc_info.value.errors()
        assert any("less than or equal to 10" in str(e) for e in errors)

    def test_retry_attempts_accepts_boundaries(self):
        """Test that retry_attempts accepts boundary values."""
        config_min = MemcachedBackendConfig(retry_attempts=0)
        assert config_min.retry_attempts == 0

        config_max = MemcachedBackendConfig(retry_attempts=10)
        assert config_max.retry_attempts == 10

    def test_servers_rejects_empty_list(self):
        """Test that servers rejects empty list."""
        with pytest.raises(ValidationError) as exc_info:
            MemcachedBackendConfig(servers=[])
        errors = exc_info.value.errors()
        assert any("At least one" in str(e) for e in errors)

    def test_servers_rejects_bad_format(self):
        """Test that servers rejects entries without host:port format."""
        with pytest.raises(ValidationError) as exc_info:
            MemcachedBackendConfig(servers=["localhost"])
        errors = exc_info.value.errors()
        assert any("host:port" in str(e) for e in errors)

    def test_servers_rejects_bad_format_in_list(self):
        """Test that servers rejects if any entry is malformed."""
        with pytest.raises(ValidationError) as exc_info:
            MemcachedBackendConfig(servers=["mc1:11211", "bad_server"])
        errors = exc_info.value.errors()
        assert any("host:port" in str(e) for e in errors)

    def test_extra_fields_rejected(self):
        """Test that extra fields are rejected due to extra='forbid'."""
        with pytest.raises(ValidationError) as exc_info:
            MemcachedBackendConfig(unknown_field="value")
        errors = exc_info.value.errors()
        assert any("extra_forbidden" in str(e) for e in errors)


@pytest.mark.unit
class TestMemcachedBackendConfigEnvVars:
    """Test environment variable parsing."""

    @pytest.fixture
    def clean_env(self, monkeypatch):
        """Remove all CACHEKIT_MEMCACHED_* environment variables."""
        for key in list(os.environ.keys()):
            if key.startswith("CACHEKIT_MEMCACHED_"):
                monkeypatch.delenv(key, raising=False)
        yield
        for key in list(os.environ.keys()):
            if key.startswith("CACHEKIT_MEMCACHED_"):
                monkeypatch.delenv(key, raising=False)

    def test_env_var_servers(self, monkeypatch, clean_env):
        """Test CACHEKIT_MEMCACHED_SERVERS parsing (JSON list)."""
        monkeypatch.setenv("CACHEKIT_MEMCACHED_SERVERS", '["mc1:11211","mc2:11212"]')
        config = MemcachedBackendConfig()
        assert config.servers == ["mc1:11211", "mc2:11212"]

    def test_env_var_connect_timeout(self, monkeypatch, clean_env):
        """Test CACHEKIT_MEMCACHED_CONNECT_TIMEOUT parsing."""
        monkeypatch.setenv("CACHEKIT_MEMCACHED_CONNECT_TIMEOUT", "5.0")
        config = MemcachedBackendConfig()
        assert config.connect_timeout == 5.0

    def test_env_var_timeout(self, monkeypatch, clean_env):
        """Test CACHEKIT_MEMCACHED_TIMEOUT parsing."""
        monkeypatch.setenv("CACHEKIT_MEMCACHED_TIMEOUT", "3.0")
        config = MemcachedBackendConfig()
        assert config.timeout == 3.0

    def test_env_var_max_pool_size(self, monkeypatch, clean_env):
        """Test CACHEKIT_MEMCACHED_MAX_POOL_SIZE parsing."""
        monkeypatch.setenv("CACHEKIT_MEMCACHED_MAX_POOL_SIZE", "50")
        config = MemcachedBackendConfig()
        assert config.max_pool_size == 50

    def test_env_var_retry_attempts(self, monkeypatch, clean_env):
        """Test CACHEKIT_MEMCACHED_RETRY_ATTEMPTS parsing."""
        monkeypatch.setenv("CACHEKIT_MEMCACHED_RETRY_ATTEMPTS", "5")
        config = MemcachedBackendConfig()
        assert config.retry_attempts == 5

    def test_env_var_key_prefix(self, monkeypatch, clean_env):
        """Test CACHEKIT_MEMCACHED_KEY_PREFIX parsing."""
        monkeypatch.setenv("CACHEKIT_MEMCACHED_KEY_PREFIX", "prod:")
        config = MemcachedBackendConfig()
        assert config.key_prefix == "prod:"

    def test_env_var_case_insensitive(self, monkeypatch, clean_env):
        """Test that environment variables are case-insensitive."""
        monkeypatch.setenv("cachekit_memcached_max_pool_size", "25")
        config = MemcachedBackendConfig()
        assert config.max_pool_size == 25


@pytest.mark.unit
class TestMemcachedBackendConfigFromEnv:
    """Test from_env() classmethod."""

    @pytest.fixture
    def clean_env(self, monkeypatch):
        """Remove all CACHEKIT_MEMCACHED_* environment variables."""
        for key in list(os.environ.keys()):
            if key.startswith("CACHEKIT_MEMCACHED_"):
                monkeypatch.delenv(key, raising=False)
        yield
        for key in list(os.environ.keys()):
            if key.startswith("CACHEKIT_MEMCACHED_"):
                monkeypatch.delenv(key, raising=False)

    def test_from_env_returns_correct_type(self, clean_env):
        """Test from_env() returns MemcachedBackendConfig instance."""
        config = MemcachedBackendConfig.from_env()
        assert isinstance(config, MemcachedBackendConfig)

    def test_from_env_reads_env_vars(self, monkeypatch, clean_env):
        """Test from_env() reads environment variables."""
        monkeypatch.setenv("CACHEKIT_MEMCACHED_CONNECT_TIMEOUT", "8.0")
        monkeypatch.setenv("CACHEKIT_MEMCACHED_MAX_POOL_SIZE", "30")
        monkeypatch.setenv("CACHEKIT_MEMCACHED_KEY_PREFIX", "staging:")

        config = MemcachedBackendConfig.from_env()

        assert config.connect_timeout == 8.0
        assert config.max_pool_size == 30
        assert config.key_prefix == "staging:"

    def test_from_env_uses_defaults_when_no_env(self, clean_env):
        """Test from_env() uses defaults when no env vars set."""
        config = MemcachedBackendConfig.from_env()

        assert config.servers == ["127.0.0.1:11211"]
        assert config.connect_timeout == 2.0
        assert config.timeout == 1.0
        assert config.max_pool_size == 10
        assert config.retry_attempts == 2
        assert config.key_prefix == ""
