"""Tests for CacheSerializationHandler encryption auto-detection.

When CACHEKIT_MASTER_KEY is set and encryption is not explicitly configured
(encryption=None), the handler auto-enables encryption with single_tenant_mode=True.

Encryption is tri-state (issue #128): None=auto-detect, True=force-on, False=hard
opt-out. An explicit False must survive fleet-wide CACHEKIT_MASTER_KEY auto-detection.
"""

from __future__ import annotations

from typing import Any

import pytest

from cachekit import cache
from cachekit.cache_handler import CacheSerializationHandler
from cachekit.config.singleton import reset_settings
from cachekit.serializers.base import SerializationMetadata
from cachekit.serializers.wrapper import SerializationWrapper

_FAKE_KEY = "ab" * 32  # pragma: allowlist secret
_DEPLOYMENT_UUID = "00000000-0000-0000-0000-000000000001"


def _envelope_is_encrypted(handler: CacheSerializationHandler, data: object, cache_key: str) -> bool:
    """Serialize and inspect the on-the-wire envelope's encrypted metadata flag."""
    blob = handler.serialize_data(data, cache_key=cache_key)
    _serialized, metadata_dict, _name = SerializationWrapper.unwrap(blob)
    return SerializationMetadata.from_dict(metadata_dict).encrypted


@pytest.mark.unit
class TestEncryptionAutoDetect:
    """CacheSerializationHandler auto-detects CACHEKIT_MASTER_KEY."""

    @pytest.fixture(autouse=True)
    def _reset(self):
        yield
        reset_settings()

    def test_auto_detect_enables_encryption(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Handler enables encryption when CACHEKIT_MASTER_KEY is set."""
        monkeypatch.setenv("CACHEKIT_MASTER_KEY", _FAKE_KEY)
        reset_settings()

        handler = CacheSerializationHandler(serializer_name="default")

        assert handler.encryption is True
        assert handler.master_key == _FAKE_KEY
        assert handler.single_tenant_mode is True

    def test_auto_detect_no_op_without_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Handler stays plaintext when CACHEKIT_MASTER_KEY is not set."""
        monkeypatch.delenv("CACHEKIT_MASTER_KEY", raising=False)
        reset_settings()

        handler = CacheSerializationHandler(serializer_name="default")

        assert handler.encryption is False
        assert handler.master_key is None

    def test_auto_detect_no_op_when_explicitly_enabled(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Explicit encryption=True is not overwritten by env var."""
        env_key = "ab" * 32  # pragma: allowlist secret
        explicit_key = "cc" * 32  # pragma: allowlist secret
        monkeypatch.setenv("CACHEKIT_MASTER_KEY", env_key)
        reset_settings()

        handler = CacheSerializationHandler(
            serializer_name="default",
            encryption=True,
            master_key=explicit_key,
            single_tenant_mode=True,
        )

        assert handler.master_key == explicit_key

    def test_auto_detect_no_op_when_tenant_extractor_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """If tenant_extractor is passed, auto-detect is skipped (user expressing intent)."""
        monkeypatch.setenv("CACHEKIT_MASTER_KEY", _FAKE_KEY)
        reset_settings()

        def extractor(*a, **kw):
            return "tenant-1"

        handler = CacheSerializationHandler(
            serializer_name="default",
            encryption=False,
            tenant_extractor=extractor,
        )

        # Explicit encryption=False is a hard opt-out — auto-detect never runs
        assert handler.encryption is False


@pytest.mark.unit
class TestEncryptionTriState:
    """Tri-state encryption: None=auto, True=force-on, False=explicit opt-out (issue #128)."""

    @pytest.fixture(autouse=True)
    def _reset(self):
        yield
        reset_settings()

    def test_default_param_is_none_auto_detects(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """encryption defaults to None (unset) and auto-detects from the env key."""
        monkeypatch.setenv("CACHEKIT_MASTER_KEY", _FAKE_KEY)
        monkeypatch.setenv("CACHEKIT_DEPLOYMENT_UUID", _DEPLOYMENT_UUID)
        reset_settings()

        handler = CacheSerializationHandler(serializer_name="default")

        assert handler.encryption is True
        assert _envelope_is_encrypted(handler, {"x": 1}, "ck:auto") is True

    def test_explicit_false_opts_out_despite_master_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """REGRESSION (issue #128): encryption=False must NOT auto-encrypt when a key is set.

        Before the tri-state fix, the auto-detect guard couldn't distinguish "unset" from
        "explicitly False" (both were the bool False), so a deliberate opt-out was silently
        promoted to encryption=True by fleet-wide CACHEKIT_MASTER_KEY.
        """
        monkeypatch.setenv("CACHEKIT_MASTER_KEY", _FAKE_KEY)
        monkeypatch.setenv("CACHEKIT_DEPLOYMENT_UUID", _DEPLOYMENT_UUID)
        reset_settings()

        handler = CacheSerializationHandler(serializer_name="default", encryption=False)

        assert handler.encryption is False
        assert handler.master_key is None
        # The on-the-wire envelope must be plaintext, not ciphertext.
        assert _envelope_is_encrypted(handler, {"x": 1}, "ck:optout") is False

    def test_explicit_true_forces_encryption_on(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """encryption=True forces encryption on (single-tenant) even via the env key."""
        monkeypatch.setenv("CACHEKIT_MASTER_KEY", _FAKE_KEY)
        monkeypatch.setenv("CACHEKIT_DEPLOYMENT_UUID", _DEPLOYMENT_UUID)
        reset_settings()

        handler = CacheSerializationHandler(
            serializer_name="default",
            encryption=True,
            single_tenant_mode=True,
        )

        assert handler.encryption is True
        assert _envelope_is_encrypted(handler, {"x": 1}, "ck:forced") is True

    def test_decorator_explicit_false_yields_plaintext_bare_encrypts(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """End-to-end via @cache: bare encrypts, encryption=False opts out, encryption=True forces on."""
        monkeypatch.setenv("CACHEKIT_MASTER_KEY", _FAKE_KEY)
        monkeypatch.setenv("CACHEKIT_DEPLOYMENT_UUID", _DEPLOYMENT_UUID)
        reset_settings()

        from cachekit.config.decorator import DecoratorConfig
        from cachekit.config.nested import EncryptionConfig

        # Bare @cache leaves encryption unset (None) -> auto-detect path stays available.
        # Construction runs full validation via __post_init__; no raise == valid.
        bare = DecoratorConfig(backend=None)
        assert bare.encryption.enabled is None

        # @cache(encryption=False) maps to an explicit opt-out that survives the env key.
        # Explicit False never requires a master key (validates on construction).
        opted_out = DecoratorConfig(backend=None, encryption=EncryptionConfig(enabled=False))
        assert opted_out.encryption.enabled is False

        # @cache(encryption=True) validates against the env-resolved key (force-on).
        # No inline key needed: __post_init__ resolves CACHEKIT_MASTER_KEY from env.
        forced = DecoratorConfig(
            backend=None,
            encryption=EncryptionConfig(enabled=True, single_tenant_mode=True),
        )
        assert forced.encryption.enabled is True


@pytest.mark.unit
class TestDecoratorEncryptionFlattening:
    """`@cache(...)` folds flat encryption kwargs into a nested EncryptionConfig (issue #128).

    Covers the bare-decorator mapping block in ``cachekit.decorators.intent.cache`` that turns
    flat ``encryption`` / ``master_key`` / ``tenant_extractor`` / ``single_tenant_mode`` /
    ``deployment_uuid`` kwargs into ``DecoratorConfig.encryption``. This is the path that lets a
    deliberate per-function ``encryption=False`` survive all the way to config resolution.

    The wrapper factory is patched out so we assert on the resolved DecoratorConfig directly,
    without constructing a real (Rust-backed) cache wrapper or touching a backend.
    """

    @pytest.fixture
    def captured_config(self, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
        import cachekit.decorators.intent as intent_mod

        captured: dict[str, Any] = {}

        def _fake_wrapper(func: Any, *, config: Any, _l1_only_mode: bool = False, **_kwargs: Any) -> Any:
            captured["config"] = config
            return func

        monkeypatch.setattr(intent_mod, "create_cache_wrapper", _fake_wrapper)
        return captured

    def test_flat_encryption_false_maps_to_opt_out(self, captured_config: dict[str, Any]) -> None:
        from cachekit.config.nested import EncryptionConfig

        @cache(encryption=False, backend=None)
        def fn() -> int:
            return 1

        enc = captured_config["config"].encryption
        assert isinstance(enc, EncryptionConfig)
        assert enc.enabled is False

    def test_flat_encryption_true_maps_to_force_on(self, captured_config: dict[str, Any]) -> None:
        from cachekit.config.nested import EncryptionConfig

        @cache(encryption=True, master_key=_FAKE_KEY, single_tenant_mode=True, backend=None)
        def fn() -> int:
            return 1

        enc = captured_config["config"].encryption
        assert isinstance(enc, EncryptionConfig)
        assert enc.enabled is True
        assert enc.single_tenant_mode is True

    def test_flat_key_params_fold_in_without_encryption_flag(self, captured_config: dict[str, Any]) -> None:
        """master_key / single_tenant_mode / deployment_uuid fold in even when `encryption`
        is omitted — `enabled` stays None (auto), exercising the per-key loop branch."""
        from cachekit.config.nested import EncryptionConfig

        @cache(master_key=_FAKE_KEY, single_tenant_mode=True, deployment_uuid=_DEPLOYMENT_UUID, backend=None)
        def fn() -> int:
            return 1

        enc = captured_config["config"].encryption
        assert isinstance(enc, EncryptionConfig)
        assert enc.enabled is None
        assert enc.master_key == _FAKE_KEY
        assert enc.single_tenant_mode is True
        assert enc.deployment_uuid == _DEPLOYMENT_UUID

    def test_prebuilt_encryption_config_passes_through_unwrapped(self, captured_config: dict[str, Any]) -> None:
        """An already-constructed EncryptionConfig is NOT re-wrapped (would nest a config in
        `.enabled`); the passthrough guard skips the mapping block."""
        from cachekit.config.nested import EncryptionConfig

        @cache(encryption=EncryptionConfig(enabled=False), backend=None)
        def fn() -> int:
            return 1

        enc = captured_config["config"].encryption
        assert isinstance(enc, EncryptionConfig)
        # If the guard failed, enabled would be an EncryptionConfig, not the bool False.
        assert enc.enabled is False
