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
        assert wrapper.is_encryption_enabled is True

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
    """Cover validation branches when encryption=True (Issue #134 cross-SDK contract).

    Under the cross-SDK contract, encryption ALLOWS any serializer that produces a
    language-agnostic wire format (default/std/standard/orjson/arrow strings and
    instances marked cross_sdk_compatible=True), and REJECTS single-SDK serializers
    ('auto' and unmarked custom instances) so the encrypted bytes stay decodable by
    other-language SDKs.
    """

    def test_auto_string_serializer_raises(self, monkeypatch):
        """String serializer 'auto' (single-SDK) raises ConfigurationError under encryption."""
        monkeypatch.setenv("CACHEKIT_MASTER_KEY", "a" * 64)
        from cachekit.config.singleton import reset_settings

        reset_settings()
        try:
            with pytest.raises(ConfigurationError, match="cross-SDK-compatible"):
                CacheSerializationHandler(
                    serializer_name="auto",
                    encryption=True,
                    single_tenant_mode=True,
                )
        finally:
            reset_settings()

    def test_unmarked_custom_instance_serializer_raises(self, monkeypatch):
        """Custom serializer instance without cross_sdk_compatible=True raises under encryption."""
        monkeypatch.setenv("CACHEKIT_MASTER_KEY", "a" * 64)
        from cachekit.config.singleton import reset_settings

        class UnmarkedSerializer:
            # No cross_sdk_compatible attribute -> treated as single-SDK
            def serialize(self, obj):
                return b"", None

            def deserialize(self, data, metadata=None):
                return None

        reset_settings()
        try:
            with pytest.raises(ConfigurationError, match="cross_sdk_compatible"):
                CacheSerializationHandler(
                    serializer_name=UnmarkedSerializer(),
                    encryption=True,
                    single_tenant_mode=True,
                )
        finally:
            reset_settings()

    def test_orjson_string_accepted_with_encryption(self, monkeypatch):
        """String serializer 'orjson' (cross-SDK) is accepted under encryption (Issue #134)."""
        monkeypatch.setenv("CACHEKIT_MASTER_KEY", "a" * 64)
        from cachekit.config.singleton import reset_settings

        reset_settings()
        try:
            handler = CacheSerializationHandler(
                serializer_name="orjson",
                encryption=True,
                single_tenant_mode=True,
            )
            assert handler.encryption is True
        finally:
            reset_settings()

    def test_cross_sdk_instance_accepted_and_threaded_into_wrapper(self, monkeypatch):
        """A cross_sdk_compatible serializer instance is accepted AND used by the wrapper (Issue #134)."""
        monkeypatch.setenv("CACHEKIT_MASTER_KEY", "a" * 64)
        from cachekit.config.singleton import reset_settings

        reset_settings()
        try:
            serializer = OrjsonSerializer()
            assert OrjsonSerializer.cross_sdk_compatible is True
            handler = CacheSerializationHandler(
                serializer_name=serializer,
                encryption=True,
                single_tenant_mode=True,
                deployment_uuid="00000000-0000-0000-0000-0000000000aa",
                master_key="a" * 64,
            )
            assert handler.encryption is True
            wrapper = handler._get_cached_encryption_wrapper("00000000-0000-0000-0000-0000000000aa")
            assert wrapper.serializer is serializer, "User's serializer must be threaded into the EncryptionWrapper"
        finally:
            reset_settings()

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


class TestEncryptedRoundTrip:
    """Cover serialize_data/deserialize_data success paths with encryption enabled."""

    def test_single_tenant_encrypt_decrypt_roundtrip(self, monkeypatch):
        """Full round-trip through CacheSerializationHandler with encryption.

        Covers:
        - serialize_data lines 559-578 (encryption branch, single-tenant UUID)
        - deserialize_data lines 667-678 (encrypted metadata with valid tenant_id)
        """
        monkeypatch.setenv("CACHEKIT_MASTER_KEY", "a" * 64)
        from cachekit.config.singleton import reset_settings

        reset_settings()
        try:
            handler = CacheSerializationHandler(
                serializer_name="default",
                encryption=True,
                single_tenant_mode=True,
            )

            data = {"user_id": 42, "email": "test@example.com"}
            cache_key = "cache:user:42:profile"

            serialized = handler.serialize_data(data, cache_key=cache_key)
            recovered = handler.deserialize_data(serialized, cache_key=cache_key)

            assert recovered == data
        finally:
            reset_settings()
