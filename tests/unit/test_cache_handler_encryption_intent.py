"""Tests for CacheSerializationHandler's encryption tri-state and the no-intent construction error.

Encryption is tri-state (issue #128): None=unset, True=force-on, False=hard opt-out. A master key
is a key source, never an activation switch (protocol intent-presets.md § Encryption Activation):
encryption=None with a key present, from CACHEKIT_MASTER_KEY or master_key=, raises
ConfigurationError at construction; with no key it stays plaintext. An explicit False must survive
a present key and still decrypt stale ciphertext on read.
"""

from __future__ import annotations

from typing import Any

import pytest

import cachekit.cache_handler as cache_handler_mod
from cachekit import cache
from cachekit.cache_handler import CacheSerializationHandler
from cachekit.config import ConfigurationError
from cachekit.config.nested import EncryptionConfig
from cachekit.config.singleton import reset_settings
from cachekit.serializers.base import SerializationMetadata
from cachekit.serializers.encryption_wrapper import DecryptionAuthenticationError
from cachekit.serializers.wrapper import SerializationWrapper

_FAKE_KEY = "ab" * 32  # pragma: allowlist secret
_DEPLOYMENT_UUID = "00000000-0000-0000-0000-000000000001"


def _envelope_is_encrypted(handler: CacheSerializationHandler, data: object, cache_key: str) -> bool:
    """Serialize and inspect the on-the-wire envelope's encrypted metadata flag."""
    blob = handler.serialize_data(data, cache_key=cache_key)
    _serialized, metadata_dict, _name = SerializationWrapper.unwrap(blob)
    return SerializationMetadata.from_dict(metadata_dict).encrypted


def _extractor(*_args: Any, **_kwargs: Any) -> str:
    return "tenant-1"


def _assert_names_explicit_spellings(
    error: pytest.ExceptionInfo[ConfigurationError], key_source: str, *, interop: bool = False
) -> None:
    """The error must name the key's source and every spelling that constructs, so the fix is copy-pasteable."""
    message = str(error.value)
    assert f"A master key is present ({key_source})" in message
    assert "@cache.secure(...)" in message
    assert "encryption=True with single_tenant_mode=True" in message
    assert "encryption=EncryptionConfig(enabled=True, single_tenant_mode=True)" in message
    assert "encryption=False" in message
    # Interop already rejects backend=None upstream, so only a non-interop message may mention it.
    assert ("the only choice with backend=None" in message) is not interop


@pytest.mark.unit
class TestNoIntentWithKeyRaises:
    """encryption=None + a master key from either source -> ConfigurationError at construction."""

    def test_env_key_without_intent_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CACHEKIT_MASTER_KEY", _FAKE_KEY)
        reset_settings()

        with pytest.raises(ConfigurationError) as error:
            CacheSerializationHandler(serializer_name="default")

        _assert_names_explicit_spellings(error, "CACHEKIT_MASTER_KEY")
        assert "or a tenant_extractor" in str(error.value)
        assert "still decrypts stale ciphertext on read" in str(error.value)

    def test_passed_master_key_without_intent_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A passed key is a key source exactly like the env var: no exemption for it."""
        monkeypatch.delenv("CACHEKIT_MASTER_KEY", raising=False)
        reset_settings()

        with pytest.raises(ConfigurationError) as error:
            CacheSerializationHandler(serializer_name="default", master_key=_FAKE_KEY)

        _assert_names_explicit_spellings(error, "master_key=")

    def test_interop_cache_message_fits_interop(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Interop rejects tenant_extractor and backend=None and never legacy-decrypts, so the message offers none."""
        monkeypatch.setenv("CACHEKIT_MASTER_KEY", _FAKE_KEY)
        reset_settings()

        with pytest.raises(ConfigurationError) as error:
            CacheSerializationHandler(serializer_name="default", interop_mode=True)

        _assert_names_explicit_spellings(error, "CACHEKIT_MASTER_KEY", interop=True)
        assert "tenant_extractor" not in str(error.value)
        assert "never decrypts stale ciphertext" in str(error.value)

    def test_tenant_extractor_with_env_key_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A tenant_extractor states no encryption intent: with the key set it must raise, not store plaintext."""
        monkeypatch.setenv("CACHEKIT_MASTER_KEY", _FAKE_KEY)
        reset_settings()

        with pytest.raises(ConfigurationError) as error:
            CacheSerializationHandler(serializer_name="default", tenant_extractor=_extractor)

        _assert_names_explicit_spellings(error, "CACHEKIT_MASTER_KEY")


@pytest.mark.unit
class TestEncryptionTriState:
    """Tri-state encryption: None=unset, True=force-on, False=explicit opt-out (issue #128)."""

    @pytest.mark.parametrize("tenant_extractor", [None, _extractor], ids=["no-extractor", "extractor"])
    def test_no_key_stays_plaintext(self, monkeypatch: pytest.MonkeyPatch, tenant_extractor: Any) -> None:
        """Zero-config: no key from either source and no intent -> plaintext, no error."""
        monkeypatch.delenv("CACHEKIT_MASTER_KEY", raising=False)
        reset_settings()

        handler = CacheSerializationHandler(serializer_name="default", tenant_extractor=tenant_extractor)

        assert handler.encryption is False
        assert handler.master_key is None
        assert _envelope_is_encrypted(handler, {"x": 1}, "ck:no-key") is False

    def test_explicit_true_encrypts_under_passed_key_not_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A passed master_key= wins over CACHEKIT_MASTER_KEY: only a holder of the passed key can decrypt."""
        explicit_key = "cc" * 32  # pragma: allowlist secret
        monkeypatch.setenv("CACHEKIT_MASTER_KEY", _FAKE_KEY)
        monkeypatch.setenv("CACHEKIT_DEPLOYMENT_UUID", _DEPLOYMENT_UUID)
        reset_settings()

        def build(**kwargs: Any) -> CacheSerializationHandler:
            return CacheSerializationHandler(serializer_name="default", encryption=True, single_tenant_mode=True, **kwargs)

        blob = build(master_key=explicit_key).serialize_data({"x": 1}, cache_key="ck:explicit")

        assert build(master_key=explicit_key).deserialize_data(blob, cache_key="ck:explicit") == {"x": 1}
        with pytest.raises(DecryptionAuthenticationError):
            build().deserialize_data(blob, cache_key="ck:explicit")

    def test_explicit_false_with_tenant_extractor_and_env_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CACHEKIT_MASTER_KEY", _FAKE_KEY)
        reset_settings()

        handler = CacheSerializationHandler(serializer_name="default", encryption=False, tenant_extractor=_extractor)

        assert handler.encryption is False

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

    def test_explicit_false_still_decrypts_stale_ciphertext(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Legacy-decrypt: an entry written under encryption=True reads back under encryption=False.

        The reader holds no key of its own; EncryptionWrapper resolves CACHEKIT_MASTER_KEY itself on
        the config-drift read path, which counts every such read.
        """
        monkeypatch.setenv("CACHEKIT_MASTER_KEY", _FAKE_KEY)
        monkeypatch.setenv("CACHEKIT_DEPLOYMENT_UUID", _DEPLOYMENT_UUID)
        reset_settings()
        counters: list[tuple[str, dict[str, str]]] = []
        monkeypatch.setattr(cache_handler_mod, "_record_security_counter", lambda name, labels: counters.append((name, labels)))

        writer = CacheSerializationHandler(serializer_name="default", encryption=True, single_tenant_mode=True)
        stale = writer.serialize_data({"x": 1}, cache_key="ck:drift")
        reader = CacheSerializationHandler(serializer_name="default", encryption=False)

        assert _envelope_is_encrypted(writer, {"x": 1}, "ck:drift") is True
        assert _envelope_is_encrypted(reader, {"x": 1}, "ck:drift") is False
        assert reader.deserialize_data(stale, cache_key="ck:drift") == {"x": 1}
        assert counters == [("cachekit_config_drift_reads_total", {"reason": "encryption_disabled"})]

    @pytest.mark.parametrize("preset", [None, "minimal", "production", "io", "dev", "test"])
    def test_no_intent_raises_at_decoration(self, monkeypatch: pytest.MonkeyPatch, preset: str | None) -> None:
        """Bare @cache and every preset but .secure / .local raise; None is bare @cache."""
        monkeypatch.setenv("CACHEKIT_MASTER_KEY", _FAKE_KEY)
        monkeypatch.setenv("CACHEKIT_API_KEY", "ck_test_placeholder")  # @cache.io builds its backend first
        reset_settings()
        decorator = cache if preset is None else getattr(cache, preset)

        with pytest.raises(ConfigurationError) as error:
            decorator(lambda: None)

        _assert_names_explicit_spellings(error, "CACHEKIT_MASTER_KEY")

    def test_flat_master_key_without_intent_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("CACHEKIT_MASTER_KEY", raising=False)
        reset_settings()

        with pytest.raises(ConfigurationError) as error:
            cache(ttl=60, master_key=_FAKE_KEY)(lambda: None)

        _assert_names_explicit_spellings(error, "master_key=")

    def test_preset_encryption_config_key_without_enabled_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """EncryptionConfig(master_key=K) with enabled unset is the same no-intent case as the flat kwarg."""
        monkeypatch.delenv("CACHEKIT_MASTER_KEY", raising=False)
        reset_settings()

        with pytest.raises(ConfigurationError) as error:
            cache.production(encryption=EncryptionConfig(master_key=_FAKE_KEY))(lambda: None)

        _assert_names_explicit_spellings(error, "master_key=")

    @pytest.mark.parametrize(
        "decorate",
        [
            pytest.param(lambda: cache(ttl=60, encryption=False), id="bare-false"),
            pytest.param(lambda: cache(ttl=60, encryption=True, single_tenant_mode=True), id="bare-true"),
            pytest.param(lambda: cache.production(encryption=False), id="preset-false"),
            pytest.param(
                lambda: cache.production(encryption=EncryptionConfig(enabled=True, single_tenant_mode=True)),
                id="preset-true",
            ),
            pytest.param(lambda: cache.secure(), id="secure"),
            pytest.param(lambda: cache.local(), id="local"),
        ],
    )
    def test_explicit_intent_constructs_with_env_key(self, monkeypatch: pytest.MonkeyPatch, decorate: Any) -> None:
        """Every explicit spelling the error names constructs; @cache.local never builds the handler."""
        monkeypatch.setenv("CACHEKIT_MASTER_KEY", _FAKE_KEY)
        monkeypatch.setenv("CACHEKIT_DEPLOYMENT_UUID", _DEPLOYMENT_UUID)
        reset_settings()

        assert callable(decorate()(lambda: None))


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

        def _fake_wrapper(func: Any, *, config: Any, **_kwargs: Any) -> Any:
            captured["config"] = config
            return func

        monkeypatch.setattr(intent_mod, "create_cache_wrapper", _fake_wrapper)
        return captured

    def test_flat_encryption_false_maps_to_opt_out(self, captured_config: dict[str, Any]) -> None:
        @cache(encryption=False, backend=None)
        def fn() -> int:
            return 1

        enc = captured_config["config"].encryption
        assert isinstance(enc, EncryptionConfig)
        assert enc.enabled is False

    def test_flat_encryption_true_maps_to_force_on(self, captured_config: dict[str, Any]) -> None:
        @cache(encryption=True, master_key=_FAKE_KEY, single_tenant_mode=True, backend=None)
        def fn() -> int:
            return 1

        enc = captured_config["config"].encryption
        assert isinstance(enc, EncryptionConfig)
        assert enc.enabled is True
        assert enc.single_tenant_mode is True

    def test_flat_key_params_fold_in_without_encryption_flag(self, captured_config: dict[str, Any]) -> None:
        """master_key / single_tenant_mode / deployment_uuid fold in even when `encryption`
        is omitted — `enabled` stays None (unset), exercising the per-key loop branch. The handler
        then refuses the key (TestNoIntentWithKeyRaises); the wrapper is patched out here."""

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

        @cache(encryption=EncryptionConfig(enabled=False), backend=None)
        def fn() -> int:
            return 1

        enc = captured_config["config"].encryption
        assert isinstance(enc, EncryptionConfig)
        # If the guard failed, enabled would be an EncryptionConfig, not the bool False.
        assert enc.enabled is False
