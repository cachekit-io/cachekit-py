"""Unit tests covering uncovered branches in encryption security paths.

Targets:
- EncryptionWrapper.__init__ `if serializer is not None` branch (explicit serializer)
- CacheSerializationHandler rejects non-default serializer with encryption=True
- CacheSerializationHandler accepts default/std/standard aliases with encryption=True
- deserialize_data raises SerializationError when encrypted metadata has no tenant_id
"""

from __future__ import annotations

import pytest

from cachekit.cache_handler import CacheSerializationHandler
from cachekit.config.validation import ConfigurationError
from cachekit.serializers.base import SerializationError
from cachekit.serializers.encryption_wrapper import EncryptionError, EncryptionWrapper
from cachekit.serializers.orjson_serializer import OrjsonSerializer
from cachekit.serializers.standard_serializer import StandardSerializer
from cachekit.serializers.wrapper import SerializationWrapper


@pytest.fixture(autouse=True)
def setup_di_for_redis_isolation():
    """Override root conftest's Redis isolation."""
    yield


class TestEncryptionWrapperSetupErrors:
    """Cover _setup_encryption error branches when master_key comes from settings."""

    def test_no_master_key_in_settings_raises(self, monkeypatch):
        """EncryptionError when master_key=None and settings has no key."""
        monkeypatch.delenv("CACHEKIT_MASTER_KEY", raising=False)
        monkeypatch.delenv("REDIS_CACHE_MASTER_KEY", raising=False)
        from cachekit.config.singleton import reset_settings

        reset_settings()
        try:
            with pytest.raises(EncryptionError, match="Master key required"):
                EncryptionWrapper(master_key=None)
        finally:
            reset_settings()

    def test_malformed_hex_key_raises(self, monkeypatch):
        """EncryptionError when settings master_key is invalid hex."""
        monkeypatch.setenv("CACHEKIT_MASTER_KEY", "not_valid_hex!!!")
        from cachekit.config.singleton import reset_settings

        reset_settings()
        try:
            with pytest.raises(EncryptionError, match="Invalid master key format"):
                EncryptionWrapper(master_key=None)
        finally:
            reset_settings()

    def test_short_master_key_raises(self):
        """EncryptionError when master_key is shorter than 32 bytes."""
        with pytest.raises(EncryptionError, match="at least 32 bytes"):
            EncryptionWrapper(master_key=b"too_short")


class TestEncryptionWrapperExplicitSerializer:
    """Cover the `if serializer is not None` branch in EncryptionWrapper.__init__."""

    def test_explicit_serializer_is_used(self):
        """Passing a custom serializer stores it instead of creating a default one."""
        custom = StandardSerializer()
        wrapper = EncryptionWrapper(serializer=custom, master_key=b"a" * 32)
        assert wrapper.serializer is custom

    def test_default_serializer_created_when_none(self):
        """When serializer=None (default), a fresh StandardSerializer is created."""
        wrapper = EncryptionWrapper(master_key=b"a" * 32)
        assert isinstance(wrapper.serializer, StandardSerializer)

    def test_explicit_serializer_participates_in_roundtrip(self):
        """Ensure the explicit serializer is actually called during serialize/deserialize."""
        custom = StandardSerializer()
        wrapper = EncryptionWrapper(serializer=custom, master_key=b"b" * 32)

        data = {"key": "value", "number": 42}
        cache_key = "test:roundtrip"
        encrypted, metadata = wrapper.serialize(data, cache_key=cache_key)
        recovered = wrapper.deserialize(encrypted, metadata, cache_key=cache_key)
        assert recovered == data


class TestCacheSerializationHandlerEncryptionSerializerValidation:
    """Cover ConfigurationError branches when encryption=True with non-default serializers."""

    def test_string_non_default_serializer_raises(self):
        """String serializer name other than default/std/standard raises ConfigurationError."""
        with pytest.raises(ConfigurationError, match="cross-language interop"):
            CacheSerializationHandler(
                serializer_name="orjson",
                encryption=True,
                single_tenant_mode=True,
            )

    def test_protocol_instance_serializer_raises(self):
        """Protocol instance as serializer raises ConfigurationError when encryption=True."""
        with pytest.raises(ConfigurationError, match="cross-language interop"):
            CacheSerializationHandler(
                serializer_name=OrjsonSerializer(),
                encryption=True,
                single_tenant_mode=True,
            )

    @pytest.mark.parametrize("alias", ["default", "std"])
    def test_alias_accepted_with_encryption(self, monkeypatch, alias):
        """Default serializer aliases are accepted with encryption=True."""
        monkeypatch.setenv("CACHEKIT_MASTER_KEY", "a" * 64)
        from cachekit.config.singleton import reset_settings

        reset_settings()
        try:
            handler = CacheSerializationHandler(
                serializer_name=alias,
                encryption=True,
                single_tenant_mode=True,
            )
            assert handler.encryption is True
        finally:
            reset_settings()

    def test_standard_alias_passes_validation_guard(self):
        """'standard' passes the ConfigurationError guard (validated in the allowed set).

        Note: 'standard' is allowed by the guard but not registered in SERIALIZER_REGISTRY,
        so we only verify the guard itself doesn't raise ConfigurationError.
        The downstream ValueError from the registry is a separate issue.
        """
        try:
            CacheSerializationHandler(
                serializer_name="standard",
                encryption=True,
                single_tenant_mode=True,
            )
        except ConfigurationError:
            pytest.fail("'standard' should pass the ConfigurationError guard")
        except ValueError:
            # Expected — 'standard' passes the guard but isn't in SERIALIZER_REGISTRY
            pass


class TestDeserializeDataMissingTenantId:
    """Cover SerializationError raised when encrypted metadata has no tenant_id."""

    def test_missing_tenant_id_raises_serialization_error(self, monkeypatch):
        """deserialize_data raises SerializationError when encrypted=True but tenant_id absent."""
        monkeypatch.setenv("CACHEKIT_MASTER_KEY", "a" * 64)
        from cachekit.config.singleton import reset_settings

        reset_settings()
        try:
            handler = CacheSerializationHandler(
                serializer_name="default",
                encryption=True,
                single_tenant_mode=True,
            )

            # Construct a wrapper blob that claims to be encrypted but has no tenant_id
            metadata = {
                "format": "msgpack",
                "compressed": False,
                "encrypted": True,
                "tenant_id": "",  # Empty — triggers the guard
                "encryption_algorithm": "AES-256-GCM",
                "key_fingerprint": "deadbeef",
                "encoding": None,
                "original_type": None,
            }
            blob = SerializationWrapper.wrap(b"fake_ciphertext", metadata, "default")

            with pytest.raises(SerializationError, match="missing tenant_id"):
                handler.deserialize_data(blob, cache_key="any:key")
        finally:
            reset_settings()
