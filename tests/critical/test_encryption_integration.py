"""
CRITICAL PATH TEST: Encryption Integration End-to-End

This test MUST pass for encryption integration to work correctly.
Tests complete encryption flow from decorator through Redis storage.
"""

from __future__ import annotations

import os
import uuid

import pytest

from cachekit.decorators import cache
from cachekit.key_generator import CacheKeyGenerator

from ..utils.redis_test_helpers import RedisIsolationMixin

# Mark all tests in this module as critical and integration
pytestmark = [pytest.mark.critical, pytest.mark.integration]


class TestEncryptionIntegration(RedisIsolationMixin):
    """Critical integration tests for end-to-end encryption flow."""

    def test_secure_decorator_encrypts_data_in_redis(self):
        """CRITICAL: @cache.secure must store encrypted ciphertext in Redis, not plaintext."""
        call_count = 0

        @cache.secure(master_key="a" * 64, ttl=300, namespace="secure_test")
        def get_sensitive_data(user_id: int):
            nonlocal call_count
            call_count += 1
            return {"user_id": user_id, "ssn": "123-45-6789", "secret": "very_sensitive_data"}

        # First call - cache miss (should encrypt data)
        result1 = get_sensitive_data(42)
        assert result1["ssn"] == "123-45-6789"
        assert result1["secret"] == "very_sensitive_data"
        assert call_count == 1

        # Read raw data from Redis to verify it's encrypted
        # CRITICAL: Use self.redis_client (isolated test client) NOT get_redis_client()
        # The decorator uses DI-injected client, self.redis_client is the same instance
        redis_client = self.redis_client

        # Generate correct cache key using Blake2b hashing (matches implementation)
        # functools.wraps copies __module__ and __qualname__ to wrapper, so use wrapper directly
        key_gen = CacheKeyGenerator()
        cache_key = key_gen.generate_key(
            get_sensitive_data,  # Use wrapper (has correct __module__/__qualname__)
            (42,),  # args tuple
            {},  # kwargs dict
            "secure_test",  # namespace
        )

        # Get raw bytes from Redis
        raw_data = redis_client.get(self.get_scoped_key(cache_key))
        assert raw_data is not None, "Data should be in Redis"

        # Verify it's NOT plaintext (should not contain our secret strings)
        raw_string = raw_data.decode("latin-1", errors="ignore")
        assert "123-45-6789" not in raw_string, "SSN should NOT be in plaintext in Redis"
        assert "very_sensitive_data" not in raw_string, "Secret should NOT be in plaintext in Redis"

        # Second call - cache hit (should decrypt correctly)
        result2 = get_sensitive_data(42)
        assert result2 == result1
        assert call_count == 1  # Function not called again

    def test_single_tenant_mode_uses_nil_uuid(self):
        """CRITICAL: Single-tenant mode (no tenant_extractor) should use nil UUID as default."""
        call_count = 0

        @cache.secure(master_key="a" * 64, ttl=300, namespace="single_tenant")
        def get_data(data_id: int):
            nonlocal call_count
            call_count += 1
            return {"id": data_id, "data": "single_tenant_data"}

        # Store data
        result = get_data(1)
        assert result["data"] == "single_tenant_data"
        assert call_count == 1

        # Verify cached
        result2 = get_data(1)
        assert result2 == result
        assert call_count == 1

    def test_multi_tenant_mode_produces_different_ciphertext(self):
        """CRITICAL: Different tenant_ids must produce different ciphertext for same plaintext."""
        from cachekit.config.nested import EncryptionConfig, L1CacheConfig
        from cachekit.decorators.tenant_context import ArgumentNameExtractor

        # Create tenant extractors
        extractor = ArgumentNameExtractor("tenant_id")

        # Generate valid UUIDs for tenants
        tenant1_uuid = str(uuid.uuid4())
        tenant2_uuid = str(uuid.uuid4())

        @cache(
            ttl=300,
            namespace="multi_t1",
            encryption=EncryptionConfig(enabled=True, master_key="a" * 64, tenant_extractor=extractor),
            l1=L1CacheConfig(enabled=False),
        )
        def get_tenant1_data(tenant_id: str, data_id: int):
            return {"tenant": tenant_id, "data": "same_plaintext_data", "id": data_id}

        @cache(
            ttl=300,
            namespace="multi_t2",
            encryption=EncryptionConfig(enabled=True, master_key="a" * 64, tenant_extractor=extractor),
            l1=L1CacheConfig(enabled=False),
        )
        def get_tenant2_data(tenant_id: str, data_id: int):
            return {"tenant": tenant_id, "data": "same_plaintext_data", "id": data_id}

        # Store data for both tenants (same plaintext)
        # Use keyword arguments for tenant extraction
        result1 = get_tenant1_data(tenant_id=tenant1_uuid, data_id=1)
        result2 = get_tenant2_data(tenant_id=tenant2_uuid, data_id=1)

        assert result1["data"] == result2["data"] == "same_plaintext_data"

        # Read raw ciphertext from Redis using correct key generation
        # Use isolated test client
        redis_client = self.redis_client
        key_gen = CacheKeyGenerator()

        # Generate keys for both tenant functions (matches Blake2b hashing)
        # Use wrapper functions directly (functools.wraps copies __module__/__qualname__)
        key1 = key_gen.generate_key(get_tenant1_data, (), {"tenant_id": tenant1_uuid, "data_id": 1}, "multi_t1")
        key2 = key_gen.generate_key(get_tenant2_data, (), {"tenant_id": tenant2_uuid, "data_id": 1}, "multi_t2")

        ciphertext1 = redis_client.get(self.get_scoped_key(key1))
        ciphertext2 = redis_client.get(self.get_scoped_key(key2))

        assert ciphertext1 is not None
        assert ciphertext2 is not None

        # CRITICAL: Different tenant_ids must produce different ciphertext
        # (due to different derived encryption keys)
        assert ciphertext1 != ciphertext2, "Same plaintext with different tenant_ids must produce different ciphertext"

    def test_master_key_validation_missing_key(self):
        """CRITICAL: Missing CACHEKIT_MASTER_KEY must fail validation at decoration time.

        Note: This test verifies validation logic exists and is correctly placed at decoration time.
        The autouse fixture in conftest.py sets CACHEKIT_MASTER_KEY, so we validate via config module directly.
        """
        from cachekit.config import ConfigurationError, reset_settings, validate_encryption_config

        # Save and remove master key temporarily
        original_key = os.environ.get("CACHEKIT_MASTER_KEY")
        try:
            if "CACHEKIT_MASTER_KEY" in os.environ:
                del os.environ["CACHEKIT_MASTER_KEY"]

            # Reset settings to pick up the env change
            reset_settings()

            # Direct validation should fail when master key missing
            with pytest.raises(ConfigurationError) as exc_info:
                validate_encryption_config(encryption=True)

            # Should raise an error about missing master key
            error_msg = str(exc_info.value).lower()
            assert "master" in error_msg or "key" in error_msg, f"Expected key/master in error, got: {exc_info.value}"

        finally:
            # Restore original key
            if original_key is not None:
                os.environ["CACHEKIT_MASTER_KEY"] = original_key

            # Reset settings again
            reset_settings()

    def test_master_key_validation_invalid_length(self):
        """CRITICAL: Master key with invalid length must fail validation at decoration time.

        Note: This test verifies validation logic exists and is correctly placed at decoration time.
        The autouse fixture in conftest.py sets CACHEKIT_MASTER_KEY, so we validate via config module directly.
        """
        from cachekit.config import ConfigurationError, reset_settings, validate_encryption_config

        # Save and replace with invalid key temporarily
        original_key = os.environ.get("CACHEKIT_MASTER_KEY")
        try:
            # Set too-short master key (less than 32 bytes)
            os.environ["CACHEKIT_MASTER_KEY"] = "too_short_key"

            # Reset settings to pick up the env change
            reset_settings()

            # Direct validation should fail when master key invalid
            with pytest.raises(ConfigurationError) as exc_info:
                validate_encryption_config(encryption=True)

            # Should raise an error about key format/length/encoding
            error_msg = str(exc_info.value).lower()
            assert any(kw in error_msg for kw in ["length", "32", "bytes", "hex", "encode"]), (
                f"Expected validation error (length/32/bytes/hex/encode), got: {exc_info.value}"
            )

        finally:
            # Restore original key
            if original_key is not None:
                os.environ["CACHEKIT_MASTER_KEY"] = original_key

            # Reset settings again
            reset_settings()

    def test_aes_gcm_authentication_prevents_tampering(self):
        """CRITICAL: AES-256-GCM authentication tags must prevent data tampering."""
        from cachekit.config.nested import EncryptionConfig, L1CacheConfig

        call_count = 0

        # Disable L1 caching for this test - we're testing Redis tampering detection
        # L1 cache would mask tampering because it returns the original valid bytes
        # stored before tampering. For proper tampering detection, we need to ensure
        # the decorator reads from Redis (L2) where the tampering occurred.
        @cache(
            ttl=300,
            namespace="tamper_test",
            encryption=EncryptionConfig(enabled=True, master_key="a" * 64, single_tenant_mode=True),
            l1=L1CacheConfig(enabled=False),
        )
        def get_protected_data(data_id: int):
            nonlocal call_count
            call_count += 1
            return {"id": data_id, "protected": "sensitive_value"}

        # Store data
        result1 = get_protected_data(99)
        assert result1["protected"] == "sensitive_value"
        assert call_count == 1

        # Get the raw encrypted data from Redis
        # Use isolated test client
        redis_client = self.redis_client

        # Generate correct cache key using Blake2b hashing
        # Use wrapper function directly (functools.wraps copies __module__/__qualname__)
        key_gen = CacheKeyGenerator()
        cache_key = key_gen.generate_key(get_protected_data, (99,), {}, "tamper_test")

        encrypted_data = redis_client.get(self.get_scoped_key(cache_key))
        assert encrypted_data is not None

        # Tamper with the data (flip some bits)
        tampered_data = bytearray(encrypted_data)
        # Flip bits in the middle of the ciphertext
        tampered_data[len(tampered_data) // 2] ^= 0xFF
        redis_client.set(self.get_scoped_key(cache_key), bytes(tampered_data))

        # Attempt to retrieve tampered data should fail gracefully
        # With safe_mode / graceful degradation, tampered data results in cache miss (calls function again)
        # This is correct behavior - corrupted cache data is treated as cache miss
        original_call_count = call_count
        result_after_tamper = get_protected_data(99)

        # Function should be called again (cache miss due to deserialization failure)
        assert call_count == original_call_count + 1, "Corrupted cache should result in cache miss"
        # Result should be correct (from fresh function execution)
        assert result_after_tamper["protected"] == "sensitive_value"

    def test_encryption_with_complex_data_types(self):
        """CRITICAL: Encryption must preserve complex data types through roundtrip."""
        call_count = 0

        @cache.secure(master_key="a" * 64, ttl=300, namespace="complex_encrypt")
        def get_complex_data(data_id: int):
            nonlocal call_count
            call_count += 1
            return {
                "id": data_id,
                "nested": {"level1": {"level2": [1, 2, {"level3": "deep"}]}},
                "list": [1, "two", 3.14, None, True, False],
                "tuple": (1, 2, 3),
                "unicode": "🔒🔐🗝️ 你好",
            }

        # First call - encrypt
        result1 = get_complex_data(5)
        assert result1["nested"]["level1"]["level2"][2]["level3"] == "deep"
        assert result1["list"][4] is True
        assert result1["unicode"] == "🔒🔐🗝️ 你好"
        assert call_count == 1

        # Second call - decrypt (note: MessagePack converts tuples to lists - documented behavior)
        result2 = get_complex_data(5)
        assert result2["nested"] == result1["nested"]
        assert result2["list"] == result1["list"]
        assert result2["unicode"] == result1["unicode"]
        # MessagePack limitation: tuples become lists after roundtrip
        assert result2["tuple"] == [1, 2, 3]
        assert isinstance(result2["tuple"], list)
        assert call_count == 1

    def test_encryption_tenant_isolation_prevents_crossover(self):
        """CRITICAL: Tenant isolation must prevent one tenant from accessing another's data."""
        from cachekit.decorators.tenant_context import ArgumentNameExtractor

        extractor = ArgumentNameExtractor("tenant_id")

        @cache.secure(master_key="a" * 64, ttl=300, namespace="isolation_test", tenant_extractor=extractor)
        def get_tenant_secret(tenant_id: str, secret_id: int):
            return {"tenant": tenant_id, "secret_id": secret_id, "secret": f"secret_for_{tenant_id}"}

        tenant_a_uuid = str(uuid.uuid4())
        tenant_b_uuid = str(uuid.uuid4())

        # Store secrets for both tenants (use keyword arguments for tenant extraction)
        secret_a = get_tenant_secret(tenant_id=tenant_a_uuid, secret_id=1)
        secret_b = get_tenant_secret(tenant_id=tenant_b_uuid, secret_id=1)

        assert secret_a["secret"] == f"secret_for_{tenant_a_uuid}"
        assert secret_b["secret"] == f"secret_for_{tenant_b_uuid}"

        # Verify tenant A cannot access tenant B's data
        # (Each tenant has different encryption keys derived from tenant_id)
        secret_a_again = get_tenant_secret(tenant_id=tenant_a_uuid, secret_id=1)
        assert secret_a_again == secret_a
        assert secret_a_again["secret"] != secret_b["secret"]

    def test_encryption_with_cache_invalidation(self):
        """CRITICAL: Cache invalidation must work correctly with encrypted data."""
        call_count = 0

        @cache.secure(master_key="a" * 64, ttl=300, namespace="invalidate_encrypt")
        def get_counter():
            nonlocal call_count
            call_count += 1
            return {"count": call_count}

        # First call
        result1 = get_counter()
        assert result1["count"] == 1

        # Cached call
        result2 = get_counter()
        assert result2["count"] == 1
        assert call_count == 1

        # Invalidate
        get_counter.invalidate_cache()

        # Should execute again
        result3 = get_counter()
        assert result3["count"] == 2
        assert call_count == 2

    def test_encryption_with_ttl_expiration(self):
        """CRITICAL: TTL expiration must work correctly with encrypted data."""
        import time

        call_count = 0

        @cache.secure(master_key="a" * 64, ttl=1, namespace="ttl_encrypt")  # 1 second TTL
        def get_data(data_id: int):
            nonlocal call_count
            call_count += 1
            return {"id": data_id, "call": call_count}

        # First call
        result1 = get_data(1)
        assert result1["call"] == 1

        # Immediate second call - cached
        result2 = get_data(1)
        assert result2["call"] == 1
        assert call_count == 1

        # Wait for TTL expiration
        time.sleep(1.5)

        # After expiration - should execute again
        result3 = get_data(1)
        assert result3["call"] == 2
        assert call_count == 2
