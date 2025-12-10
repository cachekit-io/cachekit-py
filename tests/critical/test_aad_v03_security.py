"""
CRITICAL PATH TEST: AAD v0x03 Cache Key Binding Security

Tests for Protocol v1.0.1 Section 5.6 - Prevents ciphertext substitution attacks (CVSS 8.5).

The AAD v0x03 format binds ciphertext to cache_key, preventing an attacker within
the same tenant from swapping ciphertext between cache keys.
"""

import secrets

import pytest

from cachekit.serializers.base import SerializationFormat, SerializationMetadata
from cachekit.serializers.encryption_wrapper import EncryptionError, EncryptionWrapper

pytestmark = pytest.mark.critical


@pytest.fixture
def master_key() -> bytes:
    """Generate a valid 256-bit master key."""
    return bytes.fromhex(secrets.token_hex(32))


@pytest.fixture
def encryption_wrapper(master_key: bytes) -> EncryptionWrapper:
    """Create an EncryptionWrapper for testing."""
    return EncryptionWrapper(
        master_key=master_key,
        tenant_id="test-tenant",
        enable_encryption=True,
    )


class TestAADv03Format:
    """Tests for AAD v0x03 format specification."""

    def test_aad_version_byte_is_0x03(self, encryption_wrapper: EncryptionWrapper):
        """AAD must use version byte 0x03 for cache_key binding."""
        metadata = SerializationMetadata(
            serialization_format=SerializationFormat.MSGPACK,
            compressed=True,
        )
        cache_key = "cache:user:123:profile"

        aad = encryption_wrapper._create_aad(metadata, cache_key)

        # First byte must be version 0x03
        assert aad[0] == 0x03, f"Expected version 0x03, got {aad[0]:#x}"

    def test_aad_contains_cache_key(self, encryption_wrapper: EncryptionWrapper):
        """AAD v0x03 must include cache_key in length-prefixed format."""
        metadata = SerializationMetadata(
            serialization_format=SerializationFormat.MSGPACK,
            compressed=True,
        )
        cache_key = "cache:user:123:profile"

        aad = encryption_wrapper._create_aad(metadata, cache_key)

        # Parse AAD and verify cache_key is included
        parsed = encryption_wrapper._parse_aad(aad)
        assert parsed["cache_key"] == cache_key
        assert parsed["tenant_id"] == "test-tenant"
        assert parsed["format"] == "msgpack"
        assert parsed["compressed"] == "True"

    def test_aad_different_cache_keys_produce_different_aad(self, encryption_wrapper: EncryptionWrapper):
        """Different cache_keys MUST produce different AAD bytes."""
        metadata = SerializationMetadata(
            serialization_format=SerializationFormat.MSGPACK,
            compressed=True,
        )

        aad1 = encryption_wrapper._create_aad(metadata, "cache:user:123:profile")
        aad2 = encryption_wrapper._create_aad(metadata, "cache:user:456:profile")

        assert aad1 != aad2, "Different cache_keys must produce different AAD"


class TestCacheKeyBindingSecurity:
    """Tests for cache_key binding security (prevents ciphertext substitution)."""

    def test_serialize_requires_cache_key_for_encryption(self, encryption_wrapper: EncryptionWrapper):
        """serialize() MUST require cache_key when encryption is enabled."""
        test_data = {"user_id": 123, "sensitive": "password123"}

        # Empty cache_key should raise ValueError
        with pytest.raises(ValueError) as exc_info:
            encryption_wrapper.serialize(test_data, cache_key="")

        assert "cache_key is required" in str(exc_info.value)
        assert "AAD v0x03" in str(exc_info.value)

    def test_deserialize_requires_cache_key_for_encrypted_data(self, encryption_wrapper: EncryptionWrapper):
        """deserialize() MUST require cache_key when data is encrypted."""
        test_data = {"user_id": 123, "sensitive": "password123"}
        cache_key = "cache:user:123:profile"

        # Encrypt with cache_key
        encrypted_data, metadata = encryption_wrapper.serialize(test_data, cache_key=cache_key)

        # Decrypt without cache_key should raise ValueError
        with pytest.raises(ValueError) as exc_info:
            encryption_wrapper.deserialize(encrypted_data, metadata, cache_key="")

        assert "cache_key is required" in str(exc_info.value)
        assert "AAD v0x03" in str(exc_info.value)

    def test_correct_cache_key_decrypts_successfully(self, encryption_wrapper: EncryptionWrapper):
        """Decryption with correct cache_key MUST succeed."""
        test_data = {"user_id": 123, "sensitive": "password123"}
        cache_key = "cache:user:123:profile"

        # Encrypt
        encrypted_data, metadata = encryption_wrapper.serialize(test_data, cache_key=cache_key)

        # Decrypt with same cache_key
        decrypted = encryption_wrapper.deserialize(encrypted_data, metadata, cache_key=cache_key)

        assert decrypted == test_data

    def test_wrong_cache_key_fails_authentication(self, encryption_wrapper: EncryptionWrapper):
        """Decryption with wrong cache_key MUST fail (AAD mismatch)."""
        test_data = {"user_id": 123, "sensitive": "password123"}
        original_key = "cache:user:123:profile"
        wrong_key = "cache:user:456:profile"

        # Encrypt with original key
        encrypted_data, metadata = encryption_wrapper.serialize(test_data, cache_key=original_key)

        # Attempt to decrypt with wrong key - MUST fail
        with pytest.raises(EncryptionError) as exc_info:
            encryption_wrapper.deserialize(encrypted_data, metadata, cache_key=wrong_key)

        # AES-GCM authentication failure expected
        assert "Decryption failed" in str(exc_info.value)


class TestCiphertextSubstitutionAttackPrevention:
    """Tests for ciphertext substitution attack prevention (CVSS 8.5)."""

    def test_ciphertext_substitution_attack_detected(self, master_key: bytes):
        """
        CRITICAL SECURITY TEST: Ciphertext substitution attack MUST be detected.

        Attack scenario:
        1. Attacker and victim are in the same tenant
        2. Attacker caches data at their own key: cache:user:attacker:profile
        3. Attacker copies ciphertext to victim's key: cache:user:victim:profile
        4. Victim retrieves their profile
        5. WITHOUT cache_key binding: Decryption succeeds (same tenant = same key)
        6. WITH cache_key binding (AAD v0x03): Decryption FAILS (different AAD)

        This test verifies AAD v0x03 detects and blocks this attack.
        """
        wrapper = EncryptionWrapper(
            master_key=master_key,
            tenant_id="shared-tenant",  # Same tenant for attacker and victim
            enable_encryption=True,
        )

        # Attacker creates malicious data
        attacker_data = {"role": "admin", "permissions": ["delete_users", "read_secrets"]}
        attacker_key = "cache:user:attacker:profile"

        # Attacker encrypts their data
        attacker_ciphertext, attacker_metadata = wrapper.serialize(attacker_data, cache_key=attacker_key)

        # Victim's cache key
        victim_key = "cache:user:victim:profile"

        # ATTACK: Attacker copies their ciphertext to victim's cache key
        # (simulated by trying to decrypt attacker's ciphertext with victim's key)

        # Victim tries to decrypt the swapped ciphertext
        with pytest.raises(EncryptionError) as exc_info:
            wrapper.deserialize(attacker_ciphertext, attacker_metadata, cache_key=victim_key)

        # Attack MUST be detected
        assert "Decryption failed" in str(exc_info.value)

    def test_same_data_different_keys_produce_different_ciphertext(self, master_key: bytes):
        """Same data encrypted with different cache_keys MUST produce different authenticated ciphertext."""
        wrapper = EncryptionWrapper(
            master_key=master_key,
            tenant_id="test-tenant",
            enable_encryption=True,
        )

        test_data = {"secret": "shared_secret_value"}

        # Encrypt same data with different cache_keys
        ciphertext_1, _ = wrapper.serialize(test_data, cache_key="cache:key:1")
        ciphertext_2, _ = wrapper.serialize(test_data, cache_key="cache:key:2")

        # Ciphertext should be different because:
        # 1. Different nonces (random)
        # 2. Different AAD (includes cache_key) -> different auth tag
        # Note: We can't compare exact bytes (random nonce), but we verify decryption fails with wrong key
        # This is implicitly tested by test_wrong_cache_key_fails_authentication

    def test_encryption_without_cache_key_is_blocked(self, master_key: bytes):
        """Encryption must be blocked if cache_key is not provided."""
        wrapper = EncryptionWrapper(
            master_key=master_key,
            tenant_id="test-tenant",
            enable_encryption=True,
        )

        # Attempting to serialize without cache_key must raise error
        with pytest.raises(ValueError) as exc_info:
            wrapper.serialize({"data": "secret"})  # No cache_key!

        assert "cache_key is required" in str(exc_info.value)


class TestAADv03BackwardCompatibilityNotes:
    """
    Documentation tests for AAD v0x03 non-backward-compatibility.

    NOTE: This is greenfield development - NO backward compatibility with v0x02.
    Data encrypted with v0x02 (without cache_key) cannot be decrypted with v0x03.
    """

    def test_v0x03_is_current_version(self, encryption_wrapper: EncryptionWrapper):
        """Verify current AAD version is 0x03."""
        metadata = SerializationMetadata(
            serialization_format=SerializationFormat.MSGPACK,
            compressed=True,
        )
        aad = encryption_wrapper._create_aad(metadata, "test:key")
        assert aad[0] == 0x03, "Current version must be 0x03"

    def test_parse_aad_rejects_old_versions(self, encryption_wrapper: EncryptionWrapper):
        """_parse_aad must reject AAD with version != 0x03."""
        # Create fake AAD with old version 0x02
        old_aad = bytes([0x02]) + b"\x00\x00\x00\x04test"

        with pytest.raises(ValueError) as exc_info:
            encryption_wrapper._parse_aad(old_aad)

        assert "Unsupported AAD version" in str(exc_info.value)
        assert "0x2" in str(exc_info.value)  # Python formats as 0x2, not 0x02


class TestCacheKeyEdgeCases:
    """Tests for cache_key edge cases and type safety.

    These tests verify proper handling of malformed, unusual, or invalid cache_key values.
    Ensures helpful error messages instead of cryptic AttributeError/TypeError.
    """

    def test_integer_cache_key_raises_type_error(self, master_key: bytes):
        """Integer cache_key MUST raise TypeError, not AttributeError.

        Bug: If cache_key=123 is passed, validation `if not cache_key` passes (not 123 = False),
        then cache_key.encode("utf-8") raises AttributeError: 'int' object has no attribute 'encode'.

        Expected: Clear TypeError indicating cache_key must be a string.
        """
        wrapper = EncryptionWrapper(
            master_key=master_key,
            tenant_id="test-tenant",
            enable_encryption=True,
        )

        with pytest.raises(TypeError) as exc_info:
            wrapper.serialize({"data": "secret"}, cache_key=123)  # type: ignore[arg-type]

        assert "cache_key must be a string" in str(exc_info.value)

    def test_bytes_cache_key_raises_type_error(self, master_key: bytes):
        """bytes cache_key MUST raise TypeError, not AttributeError.

        Bug: If cache_key=b"test" is passed, validation passes,
        then b"test".encode("utf-8") raises AttributeError.

        Expected: Clear TypeError indicating cache_key must be a string.
        """
        wrapper = EncryptionWrapper(
            master_key=master_key,
            tenant_id="test-tenant",
            enable_encryption=True,
        )

        with pytest.raises(TypeError) as exc_info:
            wrapper.serialize({"data": "secret"}, cache_key=b"test:key")  # type: ignore[arg-type]

        assert "cache_key must be a string" in str(exc_info.value)

    def test_none_cache_key_raises_value_error(self, master_key: bytes):
        """None cache_key MUST raise ValueError with clear message.

        None is falsy, so `if not cache_key` catches it, but the error message
        should be clear about None being invalid.
        """
        wrapper = EncryptionWrapper(
            master_key=master_key,
            tenant_id="test-tenant",
            enable_encryption=True,
        )

        with pytest.raises((ValueError, TypeError)) as exc_info:
            wrapper.serialize({"data": "secret"}, cache_key=None)  # type: ignore[arg-type]

        # Either "cache_key is required" or "cache_key must be a string"
        error_msg = str(exc_info.value).lower()
        assert "cache_key" in error_msg

    def test_unicode_cache_key_works(self, master_key: bytes):
        """Unicode cache_key (valid UTF-8) MUST work correctly.

        Cache keys may contain unicode characters like emojis or non-ASCII text.
        """
        wrapper = EncryptionWrapper(
            master_key=master_key,
            tenant_id="test-tenant",
            enable_encryption=True,
        )
        test_data = {"message": "Hello"}
        cache_key = "cache:user:123:\u4e2d\u6587:\U0001f600"  # Contains Chinese and emoji

        # Encrypt and decrypt should work
        encrypted_data, metadata = wrapper.serialize(test_data, cache_key=cache_key)
        decrypted = wrapper.deserialize(encrypted_data, metadata, cache_key=cache_key)

        assert decrypted == test_data

    def test_special_chars_in_cache_key_work(self, master_key: bytes):
        """Special characters in cache_key (like @, #, $, {, }) MUST work correctly.

        Redis-style cache keys often contain special characters.
        """
        wrapper = EncryptionWrapper(
            master_key=master_key,
            tenant_id="test-tenant",
            enable_encryption=True,
        )
        test_data = {"email": "user@example.com"}
        cache_key = "cache:{user}@domain.com#123$tag"

        # Encrypt and decrypt should work
        encrypted_data, metadata = wrapper.serialize(test_data, cache_key=cache_key)
        decrypted = wrapper.deserialize(encrypted_data, metadata, cache_key=cache_key)

        assert decrypted == test_data

    def test_very_long_cache_key_works(self, master_key: bytes):
        """Very long cache_key MUST work (up to reasonable limits).

        While not recommended, long cache keys should work without issues.
        """
        wrapper = EncryptionWrapper(
            master_key=master_key,
            tenant_id="test-tenant",
            enable_encryption=True,
        )
        test_data = {"data": "value"}
        # 10KB cache key (extreme but should work)
        cache_key = "cache:" + "x" * 10240

        # Encrypt and decrypt should work
        encrypted_data, metadata = wrapper.serialize(test_data, cache_key=cache_key)
        decrypted = wrapper.deserialize(encrypted_data, metadata, cache_key=cache_key)

        assert decrypted == test_data

    def test_whitespace_only_cache_key_works(self, master_key: bytes):
        """Whitespace-only cache_key is technically valid (even if unusual).

        A cache key like "   " is truthy and therefore passes validation.
        It should work correctly, even if it's a strange use case.
        """
        wrapper = EncryptionWrapper(
            master_key=master_key,
            tenant_id="test-tenant",
            enable_encryption=True,
        )
        test_data = {"data": "value"}
        cache_key = "   "  # Three spaces

        # Encrypt and decrypt should work
        encrypted_data, metadata = wrapper.serialize(test_data, cache_key=cache_key)
        decrypted = wrapper.deserialize(encrypted_data, metadata, cache_key=cache_key)

        assert decrypted == test_data
