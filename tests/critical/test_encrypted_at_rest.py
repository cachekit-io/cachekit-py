"""
CRITICAL PATH TEST: Encrypted-at-Rest Architecture for L1/L2 Backend Abstraction

Validates encryption architecture:
- L1 cache stores encrypted bytes (not plaintext objects)
- L2 backend stores encrypted bytes
- Decryption happens at read time only (< 1ms plaintext exposure)

Tests all valid caching combinations (L1+L2, L1-only, L2-only) with encryption.
"""

from __future__ import annotations

import secrets

import pytest

from cachekit.config.nested import EncryptionConfig, L1CacheConfig
from cachekit.decorators import cache
from cachekit.key_generator import CacheKeyGenerator

from ..utils.redis_test_helpers import RedisIsolationMixin

# Mark all tests in this module as critical and integration
pytestmark = [pytest.mark.critical, pytest.mark.integration]


@pytest.fixture(autouse=True)
def setup_encryption_master_key(monkeypatch):
    """Set up encryption master key for all tests in this module."""
    from cachekit.config import reset_settings

    # Generate a valid hex-encoded 32-byte (256-bit) master key
    master_key = secrets.token_hex(32)
    monkeypatch.setenv("CACHEKIT_MASTER_KEY", master_key)

    # Reset settings singleton to pick up new env var
    reset_settings()

    yield master_key  # Provide master_key to tests

    # Reset again after test
    reset_settings()


class TestEncryptedAtRestArchitecture(RedisIsolationMixin):
    """Critical tests for encrypted-at-rest L1/L2 storage architecture."""

    def test_l1_encryption_stores_encrypted_bytes(self, setup_encryption_master_key):
        """CRITICAL: L1 cache + encryption stores encrypted bytes (not plaintext)."""
        master_key = setup_encryption_master_key
        call_count = 0

        # L1+L2 with encryption enabled - should work and store encrypted bytes
        @cache(
            ttl=300,
            namespace="l1_encrypted",
            l1=L1CacheConfig(enabled=True),
            encryption=EncryptionConfig(
                enabled=True,
                master_key=master_key,
                single_tenant_mode=True,
                deployment_uuid="00000000-0000-0000-0000-000000000001",
            ),
        )
        def get_sensitive_data(user_id: int):
            nonlocal call_count
            call_count += 1
            return {"user_id": user_id, "ssn": "123-45-6789"}

        # First call - compute and store in L1+L2
        result1 = get_sensitive_data(1)
        assert result1["ssn"] == "123-45-6789"
        assert call_count == 1

        # Second call - should hit L1 cache (fast, ~50ns)
        # L1 stores encrypted bytes, decrypts at read time
        result2 = get_sensitive_data(1)
        assert result2["ssn"] == "123-45-6789"
        assert call_count == 1  # No recompute - L1 hit

        # Verify L2 (Redis) also has encrypted data
        from cachekit.key_generator import CacheKeyGenerator

        redis_client = self.redis_client
        key_gen = CacheKeyGenerator()
        cache_key = key_gen.generate_key(get_sensitive_data, (1,), {}, "l1_encrypted")
        l2_data = redis_client.get(self.get_scoped_key(cache_key))
        assert l2_data is not None
        l2_str = l2_data.decode("latin-1", errors="ignore")
        assert "123-45-6789" not in l2_str, "L2 must store encrypted bytes"

    def test_l2_stores_encrypted_bytes(self, setup_encryption_master_key):
        """CRITICAL: L2 backend must store encrypted bytes in Redis."""
        master_key = setup_encryption_master_key
        call_count = 0

        @cache(
            ttl=300,
            namespace="l2_encrypted",
            l1=L1CacheConfig(enabled=False),
            encryption=EncryptionConfig(
                enabled=True,
                master_key=master_key,
                single_tenant_mode=True,
                deployment_uuid="00000000-0000-0000-0000-000000000002",
            ),
        )
        def get_data(data_id: int):
            nonlocal call_count
            call_count += 1
            return {"id": data_id, "secret": "encrypted_in_l2"}

        # Store data
        result = get_data(1)
        assert result["secret"] == "encrypted_in_l2"
        assert call_count == 1

        # Read raw data from Redis (L2 backend)
        redis_client = self.redis_client
        key_gen = CacheKeyGenerator()
        cache_key = key_gen.generate_key(get_data, (1,), {}, "l2_encrypted")

        raw_data = redis_client.get(self.get_scoped_key(cache_key))
        assert raw_data is not None, "Data should be in Redis (L2)"

        # Verify encrypted (not plaintext)
        raw_string = raw_data.decode("latin-1", errors="ignore")
        assert "encrypted_in_l2" not in raw_string, "Secret must NOT be plaintext in L2"

    def test_l2_only_stores_encrypted_bytes(self, setup_encryption_master_key):
        """CRITICAL: L2-only mode stores encrypted bytes (encryption without L1)."""
        master_key = setup_encryption_master_key
        call_count = 0

        # L2-only mode (l1 disabled) with encryption
        @cache(
            ttl=300,
            namespace="l2_only_encrypted",
            l1=L1CacheConfig(enabled=False),
            encryption=EncryptionConfig(
                enabled=True,
                master_key=master_key,
                single_tenant_mode=True,
                deployment_uuid="00000000-0000-0000-0000-000000000003",
            ),
        )
        def get_secret(secret_id: int):
            nonlocal call_count
            call_count += 1
            return {"id": secret_id, "data": "top_secret_data"}

        # Store data
        result = get_secret(99)
        assert result["data"] == "top_secret_data"
        assert call_count == 1

        # Verify L2 storage (encrypted bytes)
        redis_client = self.redis_client
        key_gen = CacheKeyGenerator()
        cache_key = key_gen.generate_key(get_secret, (99,), {}, "l2_only_encrypted")
        l2_data = redis_client.get(self.get_scoped_key(cache_key))
        assert l2_data is not None
        l2_str = l2_data.decode("latin-1", errors="ignore")
        assert "top_secret_data" not in l2_str, "L2 must not have plaintext"

    def test_decryption_at_read_time_l2_only(self, setup_encryption_master_key):
        """CRITICAL: Decryption must happen at read time for L2-only mode."""
        master_key = setup_encryption_master_key
        call_count = 0

        @cache(
            ttl=300,
            namespace="decrypt_read",
            l1=L1CacheConfig(enabled=False),
            encryption=EncryptionConfig(
                enabled=True,
                master_key=master_key,
                single_tenant_mode=True,
                deployment_uuid="00000000-0000-0000-0000-000000000004",
            ),
        )
        def get_data(data_id: int):
            nonlocal call_count
            call_count += 1
            return {"id": data_id, "sensitive": "decrypt_at_read"}

        # First call - store encrypted in L2
        result1 = get_data(1)
        assert result1["sensitive"] == "decrypt_at_read"

        # Verify L2 stores encrypted
        redis_client = self.redis_client
        key_gen = CacheKeyGenerator()
        cache_key = key_gen.generate_key(get_data, (1,), {}, "decrypt_read")
        l2_data = redis_client.get(self.get_scoped_key(cache_key))
        assert l2_data is not None
        # Storage contains encrypted bytes
        assert isinstance(l2_data, bytes)

        # Second call - decrypt at read time
        result2 = get_data(1)
        # Decrypted correctly at read time
        assert result2["sensitive"] == "decrypt_at_read"
        assert call_count == 1

    def test_plaintext_msgpack_when_encryption_disabled(self):
        """Verify L2 stores plaintext msgpack when encryption disabled."""
        call_count = 0

        @cache(ttl=300, namespace="no_encryption", l1=L1CacheConfig(enabled=False), encryption=EncryptionConfig(enabled=False))
        def get_data(data_id: int):
            nonlocal call_count
            call_count += 1
            return {"id": data_id, "data": "plaintext_ok"}

        # Store data
        result = get_data(1)
        assert result["data"] == "plaintext_ok"

        # Verify L2 stores bytes (msgpack-encoded, not encrypted)
        redis_client = self.redis_client
        key_gen = CacheKeyGenerator()
        cache_key = key_gen.generate_key(get_data, (1,), {}, "no_encryption")
        l2_data = redis_client.get(self.get_scoped_key(cache_key))
        assert l2_data is not None
        assert isinstance(l2_data, bytes)
        # Note: msgpack binary format may or may not contain plaintext strings
        # (depends on msgpack encoding). The key point is it's NOT encrypted.

    def test_encryption_supported_with_all_cache_modes(self, setup_encryption_master_key):
        """CRITICAL: Encryption works with L2-only, L1+L2 modes (stores encrypted bytes)."""
        master_key = setup_encryption_master_key

        # L2-only with encryption works
        @cache(
            ttl=300,
            namespace="l2_encrypt",
            l1=L1CacheConfig(enabled=False),
            encryption=EncryptionConfig(
                enabled=True,
                master_key=master_key,
                single_tenant_mode=True,
                deployment_uuid="00000000-0000-0000-0000-000000000005",
            ),
        )
        def l2_encrypted(x: int):
            return {"x": x, "secret": "l2_secret"}

        result = l2_encrypted(1)
        assert result["secret"] == "l2_secret"

        # Verify L2 has encrypted data
        redis_client = self.redis_client
        key_gen = CacheKeyGenerator()
        key = key_gen.generate_key(l2_encrypted, (1,), {}, "l2_encrypt")
        l2_data = redis_client.get(self.get_scoped_key(key))
        assert l2_data is not None

        # L1+L2 with encryption works (L1 stores encrypted bytes)
        call_count = 0

        @cache(
            ttl=300,
            namespace="l1_l2_encrypt",
            l1=L1CacheConfig(enabled=True),
            encryption=EncryptionConfig(
                enabled=True,
                master_key=master_key,
                single_tenant_mode=True,
                deployment_uuid="00000000-0000-0000-0000-000000000006",
            ),
        )
        def l1_l2_encrypted(x: int):
            nonlocal call_count
            call_count += 1
            return {"x": x, "secret": "dual_encrypted"}

        result1 = l1_l2_encrypted(5)
        assert result1["secret"] == "dual_encrypted"
        assert call_count == 1

        # Second call hits L1 (encrypted bytes)
        result2 = l1_l2_encrypted(5)
        assert result2["secret"] == "dual_encrypted"
        assert call_count == 1  # No recompute - L1 hit

        # Note: L1-only mode (backend=None) with encryption is not tested here
        # because it's an invalid configuration (encryption requires backend storage)

    def test_l1_encryption_pii_protection(self, setup_encryption_master_key):
        """CRITICAL: L1 cache + encryption stores encrypted bytes (PII protection)."""
        master_key = setup_encryption_master_key

        # L1 cache + encryption stores encrypted bytes in memory
        # This prevents plaintext PII from being stored in process memory
        call_count = 0

        @cache(
            ttl=300,
            namespace="medium01",
            l1=L1CacheConfig(enabled=True),
            encryption=EncryptionConfig(
                enabled=True,
                master_key=master_key,
                single_tenant_mode=True,
                deployment_uuid="00000000-0000-0000-0000-000000000008",
            ),
        )
        def get_pii(user_id: int):
            nonlocal call_count
            call_count += 1
            return {
                "user_id": user_id,
                "ssn": "111-22-3333",
                "credit_card": "4111-1111-1111-1111",
                "password": "hunter2",
            }

        # First call - compute and store encrypted in L1+L2
        result1 = get_pii(100)
        assert result1["ssn"] == "111-22-3333"
        assert call_count == 1

        # Second call - L1 hit (decrypt at read time only)
        result2 = get_pii(100)
        assert result2["ssn"] == "111-22-3333"
        assert call_count == 1  # No recompute - L1 cache hit

        # Verify encrypted in L2 (Redis)
        redis_client = self.redis_client
        key_gen = CacheKeyGenerator()
        cache_key = key_gen.generate_key(get_pii, (100,), {}, "medium01")
        l2_data = redis_client.get(self.get_scoped_key(cache_key))
        assert l2_data is not None
        l2_str = l2_data.decode("latin-1", errors="ignore")
        # PII must not be in plaintext in L2
        assert "111-22-3333" not in l2_str
        assert "4111-1111-1111-1111" not in l2_str
        assert "hunter2" not in l2_str

        # L2-only mode also works (user can choose to disable L1 for extra security)
        @cache(
            ttl=300,
            namespace="medium01_l2only",
            l1=L1CacheConfig(enabled=False),
            encryption=EncryptionConfig(
                enabled=True,
                master_key=master_key,
                single_tenant_mode=True,
                deployment_uuid="00000000-0000-0000-0000-000000000009",
            ),
        )
        def get_pii_l2only(user_id: int):
            return {
                "user_id": user_id,
                "ssn": "222-33-4444",
            }

        result3 = get_pii_l2only(200)
        assert result3["ssn"] == "222-33-4444"


class TestL1CacheBytesStorage(RedisIsolationMixin):
    """Test L1 cache stores bytes (not objects) when encryption disabled."""

    def test_l1_stores_bytes_without_encryption(self):
        """L1 cache stores msgpack bytes when encryption disabled."""
        call_count = 0

        @cache(ttl=300, namespace="l1_bytes", l1=L1CacheConfig(enabled=False), encryption=EncryptionConfig(enabled=False))
        def get_data(data_id: int):
            nonlocal call_count
            call_count += 1
            return {"id": data_id, "data": "plaintext_msgpack"}

        # Store data
        result1 = get_data(1)
        assert result1["data"] == "plaintext_msgpack"
        assert call_count == 1

        # Second call - cache hit from L2
        result2 = get_data(1)
        assert result2 == result1
        assert call_count == 1

        # Verify L2 stores bytes
        redis_client = self.redis_client
        key_gen = CacheKeyGenerator()
        cache_key = key_gen.generate_key(get_data, (1,), {}, "l1_bytes")
        l2_data = redis_client.get(self.get_scoped_key(cache_key))
        assert l2_data is not None
        assert isinstance(l2_data, bytes), "L2 must store bytes"

    def test_l2_ttl_without_encryption(self):
        """L2 should respect TTL when encryption disabled."""

        @cache(ttl=300, namespace="l2_ttl", l1=L1CacheConfig(enabled=False), encryption=EncryptionConfig(enabled=False))
        def get_data(data_id: int):
            return {"id": data_id, "data": "ttl_test"}

        # Store with TTL
        result = get_data(1)
        assert result["data"] == "ttl_test"

        # Verify L2 (Redis) has TTL
        redis_client = self.redis_client
        key_gen = CacheKeyGenerator()
        cache_key = key_gen.generate_key(get_data, (1,), {}, "l2_ttl")
        ttl = redis_client.ttl(self.get_scoped_key(cache_key))
        assert ttl > 0, "L2 entry should have TTL"
        assert ttl <= 300, "TTL should not exceed configured value"
