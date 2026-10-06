"""Unit tests for DecoratorConfig core functionality.

Tests DecoratorConfig:
- __post_init__ validation (ttl_refresh_threshold 0.0-1.0)
- Nested config validation delegation
- Frozen immutability
- Defaults
- to_dict() method (temporary backward compatibility)
"""

from __future__ import annotations

import inspect
from collections.abc import Callable
from dataclasses import fields, replace
from typing import Any

import pytest
from pydantic import SecretStr

from cachekit import cache
from cachekit.backends.cachekitio import CachekitIOBackend
from cachekit.config.decorator import _ENCRYPTION_FLAT_KWARGS, _SECURE_ENCRYPTION_KWARGS, DecoratorConfig
from cachekit.config.nested import (
    BackpressureConfig,
    CircuitBreakerConfig,
    EncryptionConfig,
    L1CacheConfig,
    MonitoringConfig,
)
from cachekit.config.singleton import reset_settings
from cachekit.config.validation import ConfigurationError


@pytest.fixture
def resolved(monkeypatch: pytest.MonkeyPatch) -> list[DecoratorConfig]:
    """Capture the DecoratorConfig the decorator resolves, instead of building a wrapper."""
    seen: list[DecoratorConfig] = []

    def spy(f, config, **_kwargs):
        seen.append(config)
        return f

    monkeypatch.setattr("cachekit.decorators.intent.create_cache_wrapper", spy)
    return seen


@pytest.mark.unit
class TestDecoratorConfigDefaults:
    """Test DecoratorConfig default values."""

    def test_core_defaults(self) -> None:
        """Test core field defaults."""
        config = DecoratorConfig()
        assert config.ttl is None
        assert config.namespace is None
        assert config.serializer == "default"

    def test_performance_defaults(self) -> None:
        """Test performance field defaults."""
        config = DecoratorConfig()
        assert config.refresh_ttl_on_get is False
        assert config.ttl_refresh_threshold == 0.5

    def test_backend_default(self) -> None:
        """The default is UNSET (resolve at first call), not None (L1-only)."""
        from cachekit.config.decorator import UNSET

        config = DecoratorConfig()
        assert config.backend is UNSET

    def test_nested_config_defaults(self) -> None:
        """Test nested config groups use default factory."""
        config = DecoratorConfig()
        assert isinstance(config.l1, L1CacheConfig)
        assert isinstance(config.circuit_breaker, CircuitBreakerConfig)
        assert isinstance(config.backpressure, BackpressureConfig)
        assert isinstance(config.monitoring, MonitoringConfig)
        assert isinstance(config.encryption, EncryptionConfig)


@pytest.mark.unit
class TestDecoratorConfigFrozen:
    """Test frozen dataclass immutability."""

    def test_frozen_core_field(self) -> None:
        """Test frozen dataclass prevents mutation of core fields."""
        config = DecoratorConfig()
        with pytest.raises(AttributeError, match="cannot assign to field"):
            config.ttl = 100  # type: ignore[misc]

    def test_frozen_nested_config(self) -> None:
        """Test frozen dataclass prevents mutation of nested configs."""
        config = DecoratorConfig()
        with pytest.raises(AttributeError, match="cannot assign to field"):
            config.l1 = L1CacheConfig(enabled=False)  # type: ignore[misc]


@pytest.mark.unit
class TestDecoratorConfigValidation:
    """Test DecoratorConfig validation logic."""

    def test_validate_ttl_refresh_threshold_valid(self) -> None:
        """Test validation passes for valid ttl_refresh_threshold (0.0-1.0)."""
        config = DecoratorConfig(ttl_refresh_threshold=0.0)
        assert config.ttl_refresh_threshold == 0.0

        config = DecoratorConfig(ttl_refresh_threshold=0.5)
        assert config.ttl_refresh_threshold == 0.5

        config = DecoratorConfig(ttl_refresh_threshold=1.0)
        assert config.ttl_refresh_threshold == 1.0

    def test_validate_ttl_refresh_threshold_negative(self) -> None:
        """Test validation fails for negative ttl_refresh_threshold."""
        with pytest.raises(ConfigurationError, match="ttl_refresh_threshold must be 0.0-1.0, got -0.1"):
            DecoratorConfig(ttl_refresh_threshold=-0.1)

    @pytest.mark.parametrize("bad_ttl", [float("nan"), float("inf"), float("-inf")])
    def test_non_finite_ttl_rejected(self, bad_ttl: float) -> None:
        """Non-finite TTL (NaN/inf) must fail validation (#158: would create an immortal entry)."""
        with pytest.raises(ValueError, match="finite"):
            DecoratorConfig(ttl=bad_ttl)

    def test_validate_ttl_refresh_threshold_above_one(self) -> None:
        """Test validation fails for ttl_refresh_threshold > 1.0."""
        with pytest.raises(ConfigurationError, match="ttl_refresh_threshold must be 0.0-1.0, got 1.5"):
            DecoratorConfig(ttl_refresh_threshold=1.5)

    def test_validate_delegates_to_l1_config(self) -> None:
        """Test validation delegates to L1CacheConfig."""
        with pytest.raises(ConfigurationError, match="L1 max_size_mb must be >= 1, got 0"):
            DecoratorConfig(l1=L1CacheConfig(max_size_mb=0))

    def test_validate_delegates_to_circuit_breaker_config(self) -> None:
        """Test validation delegates to CircuitBreakerConfig."""
        with pytest.raises(ConfigurationError, match="failure_threshold must be >= 1, got 0"):
            DecoratorConfig(circuit_breaker=CircuitBreakerConfig(failure_threshold=0))

    def test_top_level_circuit_breaker_config_rejected_naming_the_nested_class(self) -> None:
        """cachekit.CircuitBreakerConfig is the reliability class; circuit_breaker= takes the nested one (LAB-5340)."""
        import cachekit

        with pytest.raises(TypeError, match=r"cachekit\.config\.nested\.CircuitBreakerConfig"):
            DecoratorConfig(circuit_breaker=cachekit.CircuitBreakerConfig(failure_threshold=1))  # type: ignore[arg-type]

    def test_validate_delegates_to_backpressure_config(self) -> None:
        """Test validation delegates to BackpressureConfig."""
        with pytest.raises(ConfigurationError, match="max_concurrent_requests must be >= 1, got 0"):
            DecoratorConfig(backpressure=BackpressureConfig(max_concurrent_requests=0))

    def test_validate_delegates_to_monitoring_config(self) -> None:
        """Test validation delegates to MonitoringConfig (no constraints)."""
        config = DecoratorConfig(monitoring=MonitoringConfig(collect_stats=False))
        assert config.monitoring.collect_stats is False

    def test_validate_delegates_to_encryption_config(self) -> None:
        """Test validation delegates to EncryptionConfig."""
        with pytest.raises(ConfigurationError, match="encryption.enabled=True requires encryption.master_key"):
            DecoratorConfig(encryption=EncryptionConfig(enabled=True, single_tenant_mode=True))


@pytest.mark.unit
class TestDecoratorConfigBoolEncryptionKwarg:
    """`encryption=True/False` is the explicit encryption spelling on every preset.

    Presets forward kwargs raw to the dataclass, so the bool must be coerced to an
    EncryptionConfig before validate() — previously it died with AttributeError.
    """

    def test_false_constructs_as_explicit_opt_out(self) -> None:
        off = DecoratorConfig.production(backend=None, encryption=False)
        assert off.encryption == EncryptionConfig(enabled=False)

    def test_true_is_rejected_with_a_configuration_error_not_attribute_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from cachekit.config.singleton import reset_settings

        monkeypatch.delenv("CACHEKIT_MASTER_KEY", raising=False)
        reset_settings()
        with pytest.raises(ConfigurationError):
            DecoratorConfig.production(backend=None, encryption=True)


@pytest.mark.unit
class TestDecoratorConfigToDict:
    """Test to_dict() method for backward compatibility."""

    def test_to_dict_core_fields(self) -> None:
        """Test to_dict() includes core fields."""
        config = DecoratorConfig(ttl=300, namespace="test", serializer="msgpack")
        d = config.to_dict()
        assert d["ttl"] == 300
        assert d["namespace"] == "test"
        assert d["serializer"] == "msgpack"

    def test_to_dict_performance_fields(self) -> None:
        """Test to_dict() includes performance fields."""
        config = DecoratorConfig(refresh_ttl_on_get=True, ttl_refresh_threshold=0.8)
        d = config.to_dict()
        assert d["refresh_ttl_on_get"] is True
        assert d["ttl_refresh_threshold"] == 0.8

    def test_to_dict_backend_field(self) -> None:
        """Test to_dict() includes backend field."""
        config = DecoratorConfig(backend=None)
        d = config.to_dict()
        assert d["backend"] is None

    def test_to_dict_flattens_l1_config(self) -> None:
        """Test to_dict() flattens L1CacheConfig."""
        config = DecoratorConfig(l1=L1CacheConfig(enabled=False, max_size_mb=200))
        d = config.to_dict()
        assert d["l1_enabled"] is False
        assert d["l1_max_size_mb"] == 200

    def test_to_dict_flattens_circuit_breaker_config(self) -> None:
        """Test to_dict() flattens CircuitBreakerConfig."""
        config = DecoratorConfig(
            circuit_breaker=CircuitBreakerConfig(
                enabled=True,
                failure_threshold=10,
                success_threshold=5,
                recovery_timeout=60,
                half_open_requests=7,
            )
        )
        d = config.to_dict()
        assert d["circuit_breaker"] is True
        assert d["failure_threshold"] == 10
        assert d["success_threshold"] == 5
        assert d["recovery_timeout"] == 60
        assert d["half_open_requests"] == 7
        assert "excluded_exceptions" not in d

    def test_to_dict_flattens_backpressure_config(self) -> None:
        """Test to_dict() flattens BackpressureConfig."""
        config = DecoratorConfig(
            backpressure=BackpressureConfig(enabled=True, max_concurrent_requests=50, queue_size=500, timeout=0.5)
        )
        d = config.to_dict()
        assert d["backpressure"] is True
        assert d["max_concurrent_requests"] == 50
        assert d["queue_size"] == 500
        assert d["backpressure_timeout"] == 0.5

    def test_to_dict_flattens_monitoring_config(self) -> None:
        """Test to_dict() flattens MonitoringConfig."""
        config = DecoratorConfig(
            monitoring=MonitoringConfig(
                collect_stats=False,
                enable_tracing=False,
                enable_structured_logging=False,
                enable_prometheus_metrics=False,
            )
        )
        d = config.to_dict()
        assert d["collect_stats"] is False
        assert d["enable_tracing"] is False
        assert d["enable_structured_logging"] is False
        assert d["enable_prometheus_metrics"] is False

    def test_to_dict_flattens_encryption_config(self) -> None:
        """Test to_dict() flattens EncryptionConfig."""

        def tenant_extractor() -> str:
            return "tenant-123"

        config = DecoratorConfig(
            encryption=EncryptionConfig(enabled=True, master_key="a" * 64, tenant_extractor=tenant_extractor)
        )
        d = config.to_dict()
        assert d["encryption"] is True
        assert d["master_key"] == "[REDACTED]"  # master_key masked in to_dict (CWE-200)
        assert d["tenant_extractor"] is tenant_extractor

    def test_to_dict_complete_config(self) -> None:
        """Test to_dict() on complete config with all fields."""
        config = DecoratorConfig(
            ttl=600,
            namespace="prod",
            serializer="msgpack",
            refresh_ttl_on_get=True,
            ttl_refresh_threshold=0.8,
            backend=None,
            l1=L1CacheConfig(enabled=True, max_size_mb=150),
            circuit_breaker=CircuitBreakerConfig(enabled=True, failure_threshold=3),
            backpressure=BackpressureConfig(enabled=True, max_concurrent_requests=75),
            monitoring=MonitoringConfig(collect_stats=True, enable_prometheus_metrics=False),
            encryption=EncryptionConfig(enabled=True, master_key="a" * 64, single_tenant_mode=True),
        )
        d = config.to_dict()

        # Core fields
        assert d["ttl"] == 600
        assert d["namespace"] == "prod"
        assert d["serializer"] == "msgpack"

        # Performance
        assert d["refresh_ttl_on_get"] is True
        assert d["ttl_refresh_threshold"] == 0.8

        # Backend
        assert d["backend"] is None

        # Nested configs
        assert d["l1_enabled"] is True
        assert d["l1_max_size_mb"] == 150
        assert d["circuit_breaker"] is True
        assert d["failure_threshold"] == 3
        assert d["backpressure"] is True
        assert d["max_concurrent_requests"] == 75
        assert d["collect_stats"] is True
        assert d["enable_prometheus_metrics"] is False
        assert d["encryption"] is True
        assert d["master_key"] == "[REDACTED]"  # masked (CWE-200)


@pytest.mark.unit
class TestIoPreset:
    """DecoratorConfig.io / @cache.io credentials and argument rejection (LAB-4643).

    Contract (protocol spec, intent-presets.md § io Credentials / § Explicit Configuration):
    api_key argument OR CACHEKIT_API_KEY, argument wins, neither -> ConfigurationError at
    construction; an unsupported argument (backend=) is rejected, never silently dropped.
    """

    @staticmethod
    def _key_of(config: DecoratorConfig) -> str:
        assert isinstance(config.backend, CachekitIOBackend)
        return config.backend._config.api_key.get_secret_value()

    @pytest.mark.parametrize("key", ["ck_arg", b"ck_arg"], ids=["str", "bytes"])  # pragma: allowlist secret
    def test_api_key_argument_builds_backend_with_that_key(self, monkeypatch: pytest.MonkeyPatch, key: str | bytes) -> None:
        monkeypatch.delenv("CACHEKIT_API_KEY", raising=False)
        assert self._key_of(DecoratorConfig.io(api_key=key)) == "ck_arg"  # type: ignore[arg-type]  # pragma: allowlist secret

    def test_api_key_argument_beats_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CACHEKIT_API_KEY", "ck_env")  # pragma: allowlist secret
        assert self._key_of(DecoratorConfig.io(api_key="ck_arg")) == "ck_arg"  # pragma: allowlist secret

    def test_env_fallback_when_no_argument(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CACHEKIT_API_KEY", "ck_env")  # pragma: allowlist secret
        assert self._key_of(DecoratorConfig.io()) == "ck_env"

    def test_env_key_resolves_exactly_as_the_backend_does(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Regression: io() read CACHEKIT_API_KEY itself, case-sensitively, and rejected a key the
        backend's case-insensitive pydantic-settings config would have loaded."""
        monkeypatch.delenv("CACHEKIT_API_KEY", raising=False)
        monkeypatch.setenv("cachekit_api_key", "ck_env")  # pragma: allowlist secret
        assert self._key_of(DecoratorConfig.io()) == "ck_env"

    def test_missing_both_raises_at_construction(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("CACHEKIT_API_KEY", raising=False)
        with pytest.raises(ConfigurationError, match=r"api_key=.*CACHEKIT_API_KEY"):
            DecoratorConfig.io()

    @pytest.mark.parametrize(
        "build",
        [DecoratorConfig.io, lambda api_key: cache.io(api_key=api_key)(lambda: 1)],
        ids=["config", "decorator"],
    )
    @pytest.mark.parametrize("key", ["", b""], ids=["str", "bytes"])
    def test_empty_argument_is_an_error_not_an_env_fallback(
        self, monkeypatch: pytest.MonkeyPatch, build: Callable[..., object], key: str | bytes
    ) -> None:
        """api_key=settings.tenant_key yielding "" must not silently cache under the env tenant's key, nor b"", which
        is falsy wrapped as a secret too."""
        monkeypatch.setenv("CACHEKIT_API_KEY", "ck_env")  # pragma: allowlist secret
        with pytest.raises(ConfigurationError, match="requires an API key"):
            build(api_key=key)

    def test_two_keys_in_one_process_reach_the_wire_separately(self) -> None:
        """Regression: the per-thread HTTP client was first-wins, so a second key's backend
        carried the right _config but every request left under the FIRST key's header.
        Async methods send on the same client, through asyncio.to_thread."""
        a = DecoratorConfig.io(api_key="ck_tenant_a").backend  # pragma: allowlist secret
        b = DecoratorConfig.io(api_key="ck_tenant_b").backend  # pragma: allowlist secret
        assert isinstance(a, CachekitIOBackend) and isinstance(b, CachekitIOBackend)
        assert a._lease.client.headers["Authorization"] == "Bearer ck_tenant_a"
        assert b._lease.client.headers["Authorization"] == "Bearer ck_tenant_b"

    @pytest.mark.parametrize("backend", [None, object()], ids=["none", "instance"])
    def test_backend_kwarg_rejected(self, backend: object) -> None:
        with pytest.raises(ConfigurationError, match="does not accept backend="):
            DecoratorConfig.io(api_key="ck_arg", backend=backend)  # pragma: allowlist secret

    def test_decorator_api_key_reaches_backend(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """@cache.io(api_key=...) hands the key to CachekitIOBackend (the decorator path, not just the classmethod)."""
        monkeypatch.setenv("CACHEKIT_API_KEY", "ck_env")  # pragma: allowlist secret
        seen: dict[str, object] = {}

        class Spy(CachekitIOBackend):
            def __init__(self, **kwargs: object) -> None:
                seen.update(kwargs)
                super().__init__(**kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr("cachekit.backends.cachekitio.CachekitIOBackend", Spy)

        @cache.io(api_key="ck_arg")  # pragma: allowlist secret
        def fn() -> int:
            return 1

        # Handed down wrapped, so no frame between the decorator and the backend holds it raw
        assert isinstance(seen["api_key"], SecretStr)
        assert seen["api_key"].get_secret_value() == "ck_arg"  # pragma: allowlist secret

    def test_decorator_config_kwarg_rejected(self) -> None:
        """config= would swap the whole preset (and its backend) in silently — reject it like backend=."""
        with pytest.raises(ConfigurationError, match="does not accept config="):

            @cache.io(config=DecoratorConfig.production(backend=None))
            def fn() -> int:
                return 1

    @pytest.mark.parametrize("backend", [None, object()], ids=["none", "instance"])
    def test_decorator_backend_kwarg_rejected(self, backend: object) -> None:
        """@cache.io(backend=...) is a ConfigurationError at decoration, not a silent drop."""
        with pytest.raises(ConfigurationError, match="does not accept backend="):

            @cache.io(api_key="ck_arg", backend=backend)  # pragma: allowlist secret
            def fn() -> int:
                return 1


_SECURE_KEY = "a" * 64


@pytest.mark.unit
class TestSecureIntegrityChecking:
    """.secure forces integrity_checking on. Asking to turn it off is a ConfigurationError on every
    path, never a silent drop or a silent pass (protocol intent-presets.md § Explicit Configuration)."""

    # Each form hands integrity_checking to .secure a different way.
    FORMS = {
        "config": lambda v: cache(config=DecoratorConfig.secure(master_key=_SECURE_KEY), integrity_checking=v),
        "secure-kwarg": lambda v: cache.secure(master_key=_SECURE_KEY, integrity_checking=v),
    }

    @pytest.mark.parametrize("form", FORMS, ids=list(FORMS))
    @pytest.mark.parametrize("value", [False, None], ids=["false", "none"])
    def test_disable_rejected_at_decoration(self, resolved: list[DecoratorConfig], form: str, value: object) -> None:
        decorator = self.FORMS[form](value)
        with pytest.raises(ConfigurationError, match="integrity_checking"):

            @decorator
            def fn() -> int:
                return 1

        assert resolved == []

    @pytest.mark.parametrize("value", [False, None], ids=["false", "none"])
    def test_classmethod_disable_rejected(self, value: object) -> None:
        with pytest.raises(ConfigurationError, match="integrity_checking"):
            DecoratorConfig.secure(master_key=_SECURE_KEY, integrity_checking=value)

    @pytest.mark.parametrize("overrides", [{}, {"integrity_checking": False}], ids=["bare", "integrity-off"])
    @pytest.mark.parametrize(
        "config",
        [DecoratorConfig.minimal(backend=None), DecoratorConfig.secure(master_key=_SECURE_KEY)],
        ids=["unencrypted", "secure"],
    )
    def test_secure_config_rejected(
        self, resolved: list[DecoratorConfig], config: DecoratorConfig, overrides: dict[str, object]
    ) -> None:
        """config= would replace the secure preset wholesale (an unencrypted one caches plaintext), as with .io."""
        with pytest.raises(ConfigurationError, match="does not accept config="):

            @cache.secure(config=config, **overrides)
            def fn() -> int:
                return 1

        assert resolved == []

    @pytest.mark.parametrize("form", FORMS, ids=list(FORMS))
    def test_explicit_true_accepted(self, resolved: list[DecoratorConfig], form: str) -> None:
        @self.FORMS[form](True)
        def fn() -> int:
            return 1

        assert resolved[0].integrity_checking is True
        assert resolved[0].encryption.enabled is True

    def test_classmethod_explicit_true_accepted(self) -> None:
        config = DecoratorConfig.secure(master_key=_SECURE_KEY, integrity_checking=True)
        assert config.integrity_checking is True
        assert config.encryption.enabled is True

    def test_non_secure_config_override_unchanged(self, resolved: list[DecoratorConfig]) -> None:
        """The config= guard keys on encryption, so an unencrypted preset keeps its RORO override."""

        @cache(config=DecoratorConfig.production(backend=None), integrity_checking=False)
        def fn() -> int:
            return 1

        assert resolved[0].integrity_checking is False


# Dummy credentials, as the secure()/io() doctests use.
_PRESET_KWARGS: dict[str, dict[str, str]] = {
    "minimal": {},
    "production": {},
    "secure": {"master_key": "a" * 64},  # pragma: allowlist secret
    "dev": {},
    "test": {},
    "io": {"api_key": "ck_test_key"},  # pragma: allowlist secret
}


@pytest.mark.unit
class TestL1EnabledFlag:
    """``l1_enabled=`` flips only ``l1.enabled`` on the config each decorator form would use (LAB-4828).

    Disabling L1 is the tenant-safety escape hatch while L1 is tenant-blind, so every form must
    accept it — and must keep the rest of the preset's L1 tuning (minimal/test ``swr_enabled=False``).
    """

    @staticmethod
    def _decorate(decorator, **kwargs) -> None:
        @decorator(ttl=60, **kwargs)
        def fn() -> int:
            return 1

    @pytest.mark.parametrize("l1_enabled", [False, True])
    @pytest.mark.parametrize("preset", list(_PRESET_KWARGS))
    def test_preset_flips_only_enabled(self, resolved: list[DecoratorConfig], preset: str, l1_enabled: bool) -> None:
        creds = _PRESET_KWARGS[preset]
        self._decorate(getattr(cache, preset), l1_enabled=l1_enabled, **creds)
        assert resolved[0].l1 == replace(getattr(DecoratorConfig, preset)(**creds).l1, enabled=l1_enabled)

    def test_config_form_keeps_config_l1(self, resolved: list[DecoratorConfig]) -> None:
        self._decorate(cache, config=DecoratorConfig.minimal(), l1_enabled=False)
        assert resolved[0].l1.enabled is False
        assert resolved[0].l1.swr_enabled is False

    def test_bare_form_uses_l1_defaults(self, resolved: list[DecoratorConfig]) -> None:
        self._decorate(cache, l1_enabled=False)
        assert resolved[0].l1 == L1CacheConfig(enabled=False)


# One override per field a preset sets itself, each unequal to every preset's default for that field.
_NESTED_OVERRIDES: dict[str, object] = {
    "circuit_breaker": CircuitBreakerConfig(failure_threshold=3),
    "l1": L1CacheConfig(max_size_mb=200, swr_enabled=False),
    "backpressure": BackpressureConfig(max_concurrent_requests=7),
    "monitoring": MonitoringConfig(collect_stats=False, enable_tracing=True),
}
_PRESET_OVERRIDES: dict[str, dict[str, object]] = {
    "minimal": {**_NESTED_OVERRIDES, "integrity_checking": True},
    "production": {**_NESTED_OVERRIDES, "integrity_checking": False},
    "dev": {**_NESTED_OVERRIDES, "integrity_checking": False},
    "test": {**_NESTED_OVERRIDES, "integrity_checking": True},
    "io": {**_NESTED_OVERRIDES, "integrity_checking": False, "swr_by_default": False},
    # secure: integrity_checking is forced (falsy rejected), encryption= is not an override.
    "secure": _NESTED_OVERRIDES,
}
_PRESET_FIELD_CASES = [(preset, name) for preset, overrides in _PRESET_OVERRIDES.items() for name in overrides]


def _field_values(config: DecoratorConfig) -> dict[str, object]:
    # backend is excluded: io builds a fresh CachekitIOBackend per call, which compares by identity.
    return {f.name: getattr(config, f.name) for f in fields(config) if f.name != "backend"}


@pytest.mark.unit
class TestPresetFieldOverrides:
    """A preset accepts an override for every field it sets itself, and the caller's value wins (LAB-5361).

    protocol spec/intent-presets.md § Explicit Configuration rule 1: an explicit argument MUST override
    the preset default. Every other field keeps the preset's default.
    """

    @staticmethod
    def _assert_only_field_overridden(config: DecoratorConfig, preset: str, name: str) -> None:
        expected = _field_values(getattr(DecoratorConfig, preset)(**_PRESET_KWARGS[preset]))
        expected[name] = _PRESET_OVERRIDES[preset][name]
        assert _field_values(config) == expected

    @pytest.mark.parametrize(("preset", "name"), _PRESET_FIELD_CASES)
    def test_classmethod_override_wins(self, preset: str, name: str) -> None:
        value = _PRESET_OVERRIDES[preset][name]
        config = getattr(DecoratorConfig, preset)(**_PRESET_KWARGS[preset], **{name: value})
        assert getattr(config, name) is value
        self._assert_only_field_overridden(config, preset, name)

    @pytest.mark.parametrize(("preset", "name"), _PRESET_FIELD_CASES)
    def test_decorator_override_wins(self, resolved: list[DecoratorConfig], preset: str, name: str) -> None:
        value = _PRESET_OVERRIDES[preset][name]

        @getattr(cache, preset)(**_PRESET_KWARGS[preset], **{name: value})
        def fn() -> int:
            return 1

        assert getattr(resolved[0], name) is value
        self._assert_only_field_overridden(resolved[0], preset, name)

    @pytest.mark.parametrize("name", list(_NESTED_OVERRIDES))
    def test_secure_override_keeps_encryption_invariants(self, resolved: list[DecoratorConfig], name: str) -> None:
        classmethod_config = DecoratorConfig.secure(master_key=_SECURE_KEY, **{name: _NESTED_OVERRIDES[name]})

        @cache.secure(master_key=_SECURE_KEY, **{name: _NESTED_OVERRIDES[name]})
        def fn() -> int:
            return 1

        for config in (classmethod_config, resolved[0]):
            assert config.encryption.enabled is True
            assert config.encryption.master_key == _SECURE_KEY
            assert config.integrity_checking is True

    def test_secure_rejects_encryption_override(self, resolved: list[DecoratorConfig]) -> None:
        plaintext = EncryptionConfig(enabled=False)
        with pytest.raises(ConfigurationError, match="sets its own encryption"):
            DecoratorConfig.secure(master_key=_SECURE_KEY, encryption=plaintext)
        with pytest.raises(ConfigurationError, match="sets its own encryption"):

            @cache.secure(master_key=_SECURE_KEY, encryption=plaintext)
            def fn() -> int:
                return 1

        assert resolved == []

    def test_bare_and_secure_fold_every_flat_encryption_keyword(self, resolved: list[DecoratorConfig]) -> None:
        # One sample per flat keyword: a keyword added to the shared list fails here until both forms carry it.
        class Extractor:
            def extract(self, args: tuple[Any, ...], kwargs: dict[str, Any]) -> str:
                return "tenant"

        samples: dict[str, Any] = {
            "master_key": _SECURE_KEY,
            "tenant_extractor": Extractor(),
            "single_tenant_mode": True,
            "deployment_uuid": "00000000-0000-4000-8000-000000000001",
            "fail_closed": True,
        }
        assert samples.keys() == _ENCRYPTION_FLAT_KWARGS
        # secure()'s named parameters are the flat keywords it does not take through **kwargs. One added to its signature
        # alone would work on DecoratorConfig.secure() and be refused by @cache.secure and bare @cache.
        assert set(inspect.signature(DecoratorConfig.secure).parameters) - {"kwargs"} == (
            _ENCRYPTION_FLAT_KWARGS - _SECURE_ENCRYPTION_KWARGS
        )
        for name, value in samples.items():
            secure_kwargs = {"master_key": _SECURE_KEY, name: value}

            @cache(**{name: value})
            def fn() -> int:
                return 1

            for config in (resolved.pop(), DecoratorConfig.secure(**secure_kwargs)):
                assert getattr(config.encryption, name) == value, (name, config)
        # An explicit single_tenant_mode reaches EncryptionConfig rather than being re-derived from tenant_extractor:
        # each of these contradicts the derived value, so EncryptionConfig refuses it.
        for bad in ({"tenant_extractor": Extractor(), "single_tenant_mode": True}, {"single_tenant_mode": False}):
            with pytest.raises(ConfigurationError, match="tenant"):
                DecoratorConfig.secure(master_key=_SECURE_KEY, **bad)
        with pytest.raises(ConfigurationError) as excinfo:
            DecoratorConfig.secure(master_key=_SECURE_KEY, encryption=EncryptionConfig())
        assert all(f"{k}=" in str(excinfo.value) for k in samples.keys() - {"master_key"})

    def test_l1_enabled_applies_on_top_of_l1_override(self, resolved: list[DecoratorConfig]) -> None:
        @cache.production(l1=L1CacheConfig(max_size_mb=200), l1_enabled=False)
        def fn() -> int:
            return 1

        assert resolved[0].l1.enabled is False
        assert resolved[0].l1.max_size_mb == 200


_OTHER_KEY = "b" * 64  # pragma: allowlist secret


@pytest.mark.unit
class TestConfigFormGuards:
    """A keyword beside config= cannot change what the config fixes, as the preset's own keywords cannot (LAB-8223).

    An encrypted config keeps its encryption: its key, its tenant mode and its being on. An io config keeps the
    CachekitIOBackend it built. Each override is a ConfigurationError when the decorator is applied (protocol
    intent-presets.md § Explicit Configuration rule 2).
    """

    ENCRYPTED_CONFIGS = {
        "secure": lambda: DecoratorConfig.secure(master_key=_SECURE_KEY),
        "production-encrypted": lambda: DecoratorConfig.production(
            encryption=EncryptionConfig(enabled=True, single_tenant_mode=True, master_key=_SECURE_KEY)
        ),
    }
    # Each would replace the config's whole EncryptionConfig.
    ENCRYPTION_OVERRIDES = {
        "fail-closed": EncryptionConfig(fail_closed=True),
        "enabled-no-key": EncryptionConfig(enabled=True, single_tenant_mode=True),
        "other-key": EncryptionConfig(enabled=True, single_tenant_mode=True, master_key=_OTHER_KEY),
        "disabled": EncryptionConfig(enabled=False),
        "false": False,
        "true": True,
    }

    @pytest.mark.parametrize("env_key", [False, True], ids=["no-env-key", "env-key"])
    @pytest.mark.parametrize("override", ENCRYPTION_OVERRIDES.values(), ids=list(ENCRYPTION_OVERRIDES))
    @pytest.mark.parametrize("config", ENCRYPTED_CONFIGS.values(), ids=list(ENCRYPTED_CONFIGS))
    def test_encryption_override_rejected(
        self,
        resolved: list[DecoratorConfig],
        monkeypatch: pytest.MonkeyPatch,
        config: Callable[[], DecoratorConfig],
        override: object,
        env_key: bool,
    ) -> None:
        if env_key:
            monkeypatch.setenv("CACHEKIT_MASTER_KEY", _OTHER_KEY)
            reset_settings()
        decorator = cache(config=config(), encryption=override)
        with pytest.raises(ConfigurationError, match="cannot override an encrypted config="):

            @decorator
            def fn() -> int:
                return 1

        assert resolved == []

    @pytest.mark.parametrize("config", ENCRYPTED_CONFIGS.values(), ids=list(ENCRYPTED_CONFIGS))
    def test_other_overrides_keep_the_encryption(
        self, resolved: list[DecoratorConfig], config: Callable[[], DecoratorConfig]
    ) -> None:
        built = config()

        @cache(config=built, ttl=5, namespace="guarded")
        def fn() -> int:
            return 1

        assert (resolved[0].ttl, resolved[0].namespace) == (5, "guarded")
        assert resolved[0].encryption == built.encryption

    def test_unencrypted_config_takes_an_encryption_override(self, resolved: list[DecoratorConfig]) -> None:
        """The guard keys on an encrypted config: opting an unencrypted one in through an override still works."""
        on = EncryptionConfig(enabled=True, single_tenant_mode=True, master_key=_SECURE_KEY)

        @cache(config=DecoratorConfig.production(), encryption=on)
        def fn() -> int:
            return 1

        assert resolved[0].encryption is on

    @pytest.mark.parametrize("backend", [None, object()], ids=["none", "instance"])
    @pytest.mark.parametrize(
        "derive", [lambda config: config, lambda config: replace(config, ttl=5)], ids=["io", "derived-from-io"]
    )
    def test_io_config_rejects_backend(
        self, resolved: list[DecoratorConfig], derive: Callable[[DecoratorConfig], DecoratorConfig], backend: object
    ) -> None:
        config = derive(DecoratorConfig.io(api_key="ck_test_key"))  # pragma: allowlist secret
        with pytest.raises(ConfigurationError, match="does not accept backend="):

            @cache(config=config, backend=backend)
            def fn() -> int:
                return 1

        assert resolved == []

    def test_cachekitio_backend_in_another_preset_stays_overridable(self, resolved: list[DecoratorConfig]) -> None:
        """The io rule follows the preset, not the backend's type: a production config holding a CachekitIOBackend
        keeps the rule that a backend= keyword beats the config's backend."""
        other = object()
        config = DecoratorConfig.production(backend=CachekitIOBackend(api_key="ck_test_key"))  # pragma: allowlist secret

        @cache(config=config, backend=other)
        def fn() -> int:
            return 1

        assert resolved[0].backend is other


@pytest.mark.unit
class TestUnsupportedKeywords:
    """A keyword the form does not accept is a ConfigurationError at construction: never the dataclass's TypeError,
    never a silent drop (protocol intent-presets.md § Explicit Configuration rule 2, LAB-8223)."""

    @pytest.mark.parametrize("preset", list(_PRESET_KWARGS))
    def test_classmethod_rejects_unknown_keyword(self, preset: str) -> None:
        with pytest.raises(ConfigurationError, match="does not accept bogus"):
            getattr(DecoratorConfig, preset)(**_PRESET_KWARGS[preset], bogus=1)

    @pytest.mark.parametrize("preset", list(_PRESET_KWARGS))
    def test_decorator_rejects_unknown_keyword(self, resolved: list[DecoratorConfig], preset: str) -> None:
        decorator = getattr(cache, preset)(**_PRESET_KWARGS[preset], bogus=1)
        with pytest.raises(ConfigurationError, match="does not accept bogus"):

            @decorator
            def fn() -> int:
                return 1

        assert resolved == []

    def test_bare_decorator_rejects_unknown_keyword(self, resolved: list[DecoratorConfig]) -> None:
        with pytest.raises(ConfigurationError, match="does not accept bogus"):

            @cache(bogus=1)
            def fn() -> int:
                return 1

        assert resolved == []

    @pytest.mark.parametrize("preset", list(_PRESET_KWARGS))
    def test_config_form_rejects_unknown_keyword(self, resolved: list[DecoratorConfig], preset: str) -> None:
        decorator = cache(config=getattr(DecoratorConfig, preset)(**_PRESET_KWARGS[preset]), bogus=1)
        with pytest.raises(ConfigurationError, match="does not accept bogus"):

            @decorator
            def fn() -> int:
                return 1

        assert resolved == []

    # Bare @cache folds the encryption ones into its EncryptionConfig and @cache.io takes api_key; beside config=
    # each names nothing.
    @pytest.mark.parametrize("name", sorted(_ENCRYPTION_FLAT_KWARGS | {"api_key"}))
    def test_config_form_rejects_a_keyword_only_another_form_takes(self, resolved: list[DecoratorConfig], name: str) -> None:
        decorator = cache(config=DecoratorConfig.minimal(), **{name: object()})
        with pytest.raises(ConfigurationError, match=f"does not accept {name}"):

            @decorator
            def fn() -> int:
                return 1

        assert resolved == []

    @pytest.mark.parametrize("preset", list(_PRESET_KWARGS))
    def test_every_field_the_preset_does_not_fix_is_accepted(self, preset: str) -> None:
        fixed = {"secure": {"encryption"}, "io": {"backend"}}.get(preset, set())
        defaults = DecoratorConfig()
        for f in fields(DecoratorConfig):
            if f.name.startswith("_") or f.name in fixed:
                continue
            getattr(DecoratorConfig, preset)(**_PRESET_KWARGS[preset], **{f.name: getattr(defaults, f.name)})

    def test_every_unknown_keyword_is_named(self) -> None:
        with pytest.raises(ConfigurationError, match="does not accept bogus, tll"):
            DecoratorConfig.minimal(tll=300, bogus=1)

    def test_io_rejects_before_building_its_backend(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """No API key anywhere, so a check after the backend was built would report the missing key instead."""
        monkeypatch.delenv("CACHEKIT_API_KEY", raising=False)
        with pytest.raises(ConfigurationError, match="does not accept bogus"):
            DecoratorConfig.io(bogus=1)

    def test_secure_decorator_rejects_before_looking_up_its_key(
        self, resolved: list[DecoratorConfig], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No master key anywhere, so a check after the key lookup would report the missing key instead."""
        monkeypatch.delenv("CACHEKIT_MASTER_KEY", raising=False)
        reset_settings()
        decorator = cache.secure(bogus=1)
        with pytest.raises(ConfigurationError, match="The secure preset does not accept bogus"):

            @decorator
            def fn() -> int:
                return 1

        assert resolved == []

    def test_misplaced_key_is_named_not_quoted(self) -> None:
        with pytest.raises(ConfigurationError, match="does not accept master_key") as exc_info:
            DecoratorConfig.minimal(master_key=_OTHER_KEY)
        assert _OTHER_KEY not in str(exc_info.value)


class TestIntegrityCheckingIsABool:
    """integrity_checking becomes the encryption AAD's `compressed` token, frozen as exactly True or
    False (spec/encryption.md): a truthy non-bool is refused when the config is built, on every path."""

    @pytest.mark.parametrize("value", [1, 0, "yes", None], ids=["one", "zero", "str", "none"])
    def test_constructor_rejects_non_bool(self, value: object) -> None:
        with pytest.raises(ConfigurationError, match="integrity_checking must be a bool"):
            DecoratorConfig(integrity_checking=value)  # type: ignore[arg-type]

    def test_replace_rejects_non_bool(self) -> None:
        with pytest.raises(ConfigurationError, match="integrity_checking must be a bool"):
            replace(DecoratorConfig(), integrity_checking=1)

    @pytest.mark.parametrize("preset", list(_PRESET_KWARGS), ids=list(_PRESET_KWARGS))
    def test_presets_reject_non_bool(self, preset: str) -> None:
        with pytest.raises(ConfigurationError, match="integrity_checking"):
            getattr(DecoratorConfig, preset)(integrity_checking=1, **_PRESET_KWARGS[preset])

    def test_decorator_rejects_non_bool(self) -> None:
        with pytest.raises(ConfigurationError, match="integrity_checking must be a bool"):

            @cache(backend=None, integrity_checking=1)
            def fn() -> int:
                return 1
