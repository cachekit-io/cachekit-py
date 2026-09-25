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

from collections.abc import Callable

import pytest
from pydantic import SecretStr, ValidationError

from cachekit.backends.base_config import BaseBackendConfig
from cachekit.backends.cachekitio.config import CachekitIOBackendConfig
from cachekit.backends.file.config import FileBackendConfig
from cachekit.backends.memcached.config import MemcachedBackendConfig
from cachekit.backends.redis.config import RedisBackendConfig
from cachekit.config.settings import CachekitConfig

# All backend config classes that must follow the pattern
BACKEND_CONFIGS: list[type[BaseBackendConfig]] = [
    RedisBackendConfig,
    FileBackendConfig,
    CachekitIOBackendConfig,
    MemcachedBackendConfig,
]

# Required model_config settings from BaseBackendConfig
REQUIRED_MODEL_CONFIG_KEYS = {
    "env_nested_delimiter": "__",
    "case_sensitive": False,
    "extra": "forbid",
    "populate_by_name": True,
    "hide_input_in_errors": True,
}


def _assert_no_route_to(exc: ValidationError, secret: str) -> None:
    """CWE-532: nothing reachable from a config ValidationError may lead back to ``secret``."""
    for rendered in (str(exc), repr(exc), exc.json(), repr(exc.errors())):
        assert secret not in rendered
    assert exc.__context__ is None
    assert exc.__cause__ is None
    for err in exc.errors():
        assert err["input"] == "[REDACTED]"
        for value in (err.get("ctx") or {}).values():
            if isinstance(value, BaseException):
                # A validator's exception: its traceback frames hold the validator's locals (the raw
                # value, as a SecretStr or not) and its chain can quote the raw value too.
                assert (value.__traceback__, value.__context__, value.__cause__) == (None, None, None)


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

    @pytest.mark.parametrize("config_cls", BACKEND_CONFIGS)
    def test_validation_errors_redact_every_input(self, config_cls: type[BaseBackendConfig]) -> None:
        """CWE-532: backend configs hold credentials, so no surface of a ValidationError may carry a raw input.

        Pinned here, not per backend: the inherited RedactingSettings.__init__ does the redacting, and a
        subclass that overrides __init__ without calling it would silently lose it.
        """
        with pytest.raises(ValidationError) as exc_info:
            config_cls(totally_fake_field_that_doesnt_exist="SECRET_VALUE")  # type: ignore[call-arg]

        _assert_no_route_to(exc_info.value, "SECRET_VALUE")

    @pytest.mark.parametrize(
        "build",
        [
            lambda: CachekitIOBackendConfig(api_key="ck_live_SECRET_VALUE\n"),  # pragma: allowlist secret
            lambda: CachekitIOBackendConfig(
                api_key="ck_live_SECRET_VALUE",  # pragma: allowlist secret
                api_url="https://evil.example.com",
            ),
            lambda: CachekitConfig(previous_master_keys=[SecretStr("SECRET_VALUE")]),
            lambda: MemcachedBackendConfig(servers=["mc1:SECRET_VALUE"]),
            lambda: MemcachedBackendConfig(servers=["user:SECRET_VALUE@mc1"]),
        ],
        ids=["io-whitespace-key", "io-allowlist", "keyring-bad-hex", "memcached-port", "memcached-format"],
    )
    def test_validator_errors_leave_no_route_to_the_secret(self, build: Callable[[], object]) -> None:
        """A validator's own exception rides along in ctx["error"]: its message, its chain and its
        traceback's frame locals must not recover the value it rejected."""
        with pytest.raises(ValidationError) as exc_info:
            build()

        _assert_no_route_to(exc_info.value, "SECRET_VALUE")

    def test_non_builtin_error_types_are_redacted_too(self) -> None:
        """A type pydantic-core cannot rebuild by name must still come back as a redacted ValidationError.

        Rebuilding by name raised KeyError inside the except, chaining the raw original. pydantic's own
        Path fields raise "path_type"; a subclass validator may raise PydanticCustomError with a ctx.
        """
        from pydantic import field_validator
        from pydantic_core import PydanticCustomError

        class StrictURLConfig(BaseBackendConfig):
            url: str = ""

            @field_validator("url")
            @classmethod
            def reject(cls, v: str) -> str:
                raise PydanticCustomError("bad_url", "bad URL (port {port})", {"port": 6379})

        with pytest.raises(ValidationError) as file_info:
            FileBackendConfig(cache_dir=None, totally_fake_field_that_doesnt_exist="SECRET_VALUE")  # type: ignore[arg-type,call-arg]
        with pytest.raises(ValidationError) as custom_info:
            StrictURLConfig(url="redis://:SECRET_VALUE@host")

        assert [err["type"] for err in file_info.value.errors()] == ["path_type", "extra_forbidden"]
        [custom] = custom_info.value.errors()
        assert (custom["type"], custom["loc"], custom["msg"], custom["ctx"]) == (
            "bad_url",
            ("url",),
            "bad URL (port 6379)",
            {"port": 6379},
        )
        for exc in (file_info.value, custom_info.value):
            _assert_no_route_to(exc, "SECRET_VALUE")


class TestFromEnvClassmethod:
    """Ensure all configs have from_env() classmethod."""

    @pytest.mark.parametrize("config_cls", BACKEND_CONFIGS)
    def test_has_from_env_classmethod(self, config_cls: type[BaseBackendConfig]) -> None:
        """All backend configs must have from_env() classmethod."""
        assert hasattr(config_cls, "from_env"), f"{config_cls.__name__} must have from_env() classmethod"
        assert callable(config_cls.from_env), f"{config_cls.__name__}.from_env must be callable"
