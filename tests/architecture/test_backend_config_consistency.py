"""Enforce consistent model_config across all backend configs.

This test ensures all backend configurations:
1. Inherit from BaseBackendConfig
2. Properly spread parent model_config settings
3. Have consistent validation behavior

Why this matters:
- Prevents silent config errors (extra="forbid" vs "ignore")
- Ensures env var parsing is consistent across backends
- Makes isinstance() checks reliable for type safety
"""

from __future__ import annotations

import pytest

from cachekit.backends.base_config import BaseBackendConfig
from cachekit.backends.cachekitio.config import CachekitIOBackendConfig
from cachekit.backends.file.config import FileBackendConfig
from cachekit.backends.redis.config import RedisBackendConfig

# All backend config classes that must follow the pattern
BACKEND_CONFIGS: list[type[BaseBackendConfig]] = [
    RedisBackendConfig,
    FileBackendConfig,
    CachekitIOBackendConfig,
]

# Required model_config settings from BaseBackendConfig
REQUIRED_MODEL_CONFIG_KEYS = {
    "env_nested_delimiter": "__",
    "case_sensitive": False,
    "extra": "forbid",
    "populate_by_name": True,
}


class TestBackendConfigInheritance:
    """Ensure all backend configs inherit from BaseBackendConfig."""

    @pytest.mark.parametrize("config_cls", BACKEND_CONFIGS)
    def test_inherits_from_base_backend_config(self, config_cls: type[BaseBackendConfig]) -> None:
        """All backend configs must inherit from BaseBackendConfig."""
        assert issubclass(config_cls, BaseBackendConfig), f"{config_cls.__name__} must inherit from BaseBackendConfig"

    @pytest.mark.parametrize("config_cls", BACKEND_CONFIGS)
    def test_isinstance_check_works(self, config_cls: type[BaseBackendConfig]) -> None:
        """Instances should pass isinstance check against BaseBackendConfig."""
        # Skip CachekitIOBackendConfig as it requires api_key
        if config_cls is CachekitIOBackendConfig:
            pytest.skip("CachekitIOBackendConfig requires api_key")

        instance = config_cls()
        assert isinstance(instance, BaseBackendConfig), f"{config_cls.__name__} instance should be instanceof BaseBackendConfig"


class TestModelConfigConsistency:
    """Ensure model_config settings are consistent across backends."""

    @pytest.mark.parametrize("config_cls", BACKEND_CONFIGS)
    def test_has_required_model_config_settings(self, config_cls: type[BaseBackendConfig]) -> None:
        """All backend configs must have the required model_config settings."""
        model_config = config_cls.model_config

        for key, expected_value in REQUIRED_MODEL_CONFIG_KEYS.items():
            actual_value = model_config.get(key)
            assert actual_value == expected_value, (
                f"{config_cls.__name__}.model_config['{key}'] = {actual_value!r}, "
                f"expected {expected_value!r}. "
                f"Did you forget to spread BaseBackendConfig.model_config?"
            )

    @pytest.mark.parametrize("config_cls", BACKEND_CONFIGS)
    def test_has_env_prefix(self, config_cls: type[BaseBackendConfig]) -> None:
        """All backend configs must define an env_prefix."""
        model_config = config_cls.model_config
        env_prefix = model_config.get("env_prefix")

        assert env_prefix is not None, f"{config_cls.__name__} must define env_prefix in model_config"
        assert env_prefix.startswith("CACHEKIT"), (
            f"{config_cls.__name__}.model_config['env_prefix'] should start with 'CACHEKIT', got {env_prefix!r}"
        )

    @pytest.mark.parametrize("config_cls", BACKEND_CONFIGS)
    def test_extra_forbid_catches_typos(self, config_cls: type[BaseBackendConfig]) -> None:
        """extra='forbid' should reject unknown fields (catches config typos)."""
        from pydantic import ValidationError

        # Skip CachekitIOBackendConfig as it requires api_key
        if config_cls is CachekitIOBackendConfig:
            pytest.skip("CachekitIOBackendConfig requires api_key")

        with pytest.raises(ValidationError) as exc_info:
            config_cls(totally_fake_field_that_doesnt_exist="value")  # type: ignore[call-arg]

        # Verify it's an "extra fields" error
        errors = exc_info.value.errors()
        assert any("extra" in str(e).lower() for e in errors), (
            f"{config_cls.__name__} should reject unknown fields with extra='forbid'"
        )


class TestFromEnvClassmethod:
    """Ensure all configs have from_env() classmethod."""

    @pytest.mark.parametrize("config_cls", BACKEND_CONFIGS)
    def test_has_from_env_classmethod(self, config_cls: type[BaseBackendConfig]) -> None:
        """All backend configs must have from_env() classmethod."""
        assert hasattr(config_cls, "from_env"), f"{config_cls.__name__} must have from_env() classmethod"
        assert callable(config_cls.from_env), f"{config_cls.__name__}.from_env must be callable"
