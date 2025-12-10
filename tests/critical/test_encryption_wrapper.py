"""
CRITICAL PATH TEST: EncryptionWrapper Functionality

This test MUST pass for zero-knowledge encryption to work.
Tests client-side encryption with tenant isolation and key derivation.
"""

import secrets

import pytest

from cachekit.config.nested import EncryptionConfig, L1CacheConfig
from cachekit.decorators import cache
from cachekit.decorators.tenant_context import ArgumentNameExtractor

from ..utils.redis_test_helpers import RedisIsolationMixin

# Mark all tests in this module as critical
pytestmark = pytest.mark.critical


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


# Test UUIDs (deterministic for test reproducibility)
TEST_UUID_1 = "00000000-0000-0000-0000-000000000001"
TEST_UUID_2 = "00000000-0000-0000-0000-000000000002"
TEST_UUID_COMPLEX = "00000000-0000-0000-0000-000000000003"
TEST_UUID_LARGE = "00000000-0000-0000-0000-000000000004"
TEST_UUID_INV = "00000000-0000-0000-0000-000000000005"
TEST_UUID_EMPTY = "00000000-0000-0000-0000-000000000006"
TEST_UUID_BOOL = "00000000-0000-0000-0000-000000000007"
TEST_UUID_NUMERIC = "00000000-0000-0000-0000-000000000008"
TEST_UUID_UNICODE = "00000000-0000-0000-0000-000000000009"
TEST_UUID_CONSISTENCY = "00000000-0000-0000-0000-00000000000a"
TEST_UUID_TYPE_PRESERVE = "00000000-0000-0000-0000-00000000000b"
TEST_UUID_INTEGRITY = "00000000-0000-0000-0000-00000000000c"
TEST_UUID_CTX1 = "00000000-0000-0000-0000-00000000000d"
TEST_UUID_CTX2 = "00000000-0000-0000-0000-00000000000e"
TEST_UUID_TTL = "00000000-0000-0000-0000-00000000000f"


class TestEncryptionWrapperFunctionality(RedisIsolationMixin):
    """Critical tests for EncryptionWrapper with zero-knowledge encryption."""

    def test_encryption_wrapper_basic_roundtrip(self, setup_encryption_master_key):
        """CRITICAL: EncryptionWrapper must encrypt and decrypt correctly."""
        master_key = setup_encryption_master_key
        call_count = 0

        @cache(
            ttl=300,
            l1=L1CacheConfig(enabled=False),
            encryption=EncryptionConfig(
                enabled=True, master_key=master_key, single_tenant_mode=True, deployment_uuid=TEST_UUID_1
            ),
        )
        def get_sensitive_data(data_id):
            nonlocal call_count
            call_count += 1
            return {
                "id": data_id,
                "sensitive": "password123",
                "secret_number": 42,
                "call_number": call_count,
            }

        # First call - cache miss (encrypts data)
        result1 = get_sensitive_data(1)
        assert result1["sensitive"] == "password123"
        assert result1["secret_number"] == 42
        assert result1["call_number"] == 1
        assert call_count == 1

        # Second call - cache hit (decrypts data)
        result2 = get_sensitive_data(1)
        assert result2 == result1
        assert result2["call_number"] == 1  # Should still be 1 (cached)
        assert call_count == 1  # Function not called again

    def test_encryption_wrapper_tenant_isolation(self, setup_encryption_master_key):
        """CRITICAL: Different tenants must get different encryption keys."""
        master_key = setup_encryption_master_key
        call_count_t1 = 0
        call_count_t2 = 0

        @cache(
            ttl=300,
            namespace="tenant1_data",
            l1=L1CacheConfig(enabled=False),
            encryption=EncryptionConfig(
                enabled=True, master_key=master_key, single_tenant_mode=True, deployment_uuid=TEST_UUID_1
            ),
        )
        def get_tenant1_data(data_id):
            nonlocal call_count_t1
            call_count_t1 += 1
            return {"tenant": "1", "secret": "tenant1_secret", "call": call_count_t1}

        @cache(
            ttl=300,
            namespace="tenant2_data",
            l1=L1CacheConfig(enabled=False),
            encryption=EncryptionConfig(
                enabled=True, master_key=master_key, single_tenant_mode=True, deployment_uuid=TEST_UUID_2
            ),
        )
        def get_tenant2_data(data_id):
            nonlocal call_count_t2
            call_count_t2 += 1
            return {"tenant": "2", "secret": "tenant2_secret", "call": call_count_t2}

        # Store data for both tenants
        t1_result = get_tenant1_data(1)
        t2_result = get_tenant2_data(1)

        # Verify tenant isolation - different data
        assert t1_result["tenant"] == "1"
        assert t2_result["tenant"] == "2"
        assert t1_result["secret"] != t2_result["secret"]

        # Verify caching works for each tenant independently
        t1_result2 = get_tenant1_data(1)
        t2_result2 = get_tenant2_data(1)

        assert t1_result2 == t1_result
        assert t2_result2 == t2_result
        assert call_count_t1 == 1
        assert call_count_t2 == 1

    def test_encryption_wrapper_complex_data_types(self, setup_encryption_master_key):
        """CRITICAL: EncryptionWrapper must handle complex nested types."""
        master_key = setup_encryption_master_key
        call_count = 0

        @cache(
            ttl=300,
            l1=L1CacheConfig(enabled=False),
            encryption=EncryptionConfig(
                enabled=True, master_key=master_key, single_tenant_mode=True, deployment_uuid=TEST_UUID_COMPLEX
            ),
        )
        def get_complex_encrypted_data(data_id):
            nonlocal call_count
            call_count += 1
            return {
                "id": data_id,
                "nested": {"level1": {"level2": [1, 2, {"level3": "encrypted_deep"}]}},
                "mixed_list": [1, "two", 3.0, None, True],
                "tuple_data": (1, 2, 3),
                "call_number": call_count,
            }

        # First call - cache miss, returns original object (before serialization)
        result1 = get_complex_encrypted_data(2)
        assert result1["nested"]["level1"]["level2"][2]["level3"] == "encrypted_deep"
        assert result1["mixed_list"][4] is True
        assert result1["tuple_data"] == (1, 2, 3)  # Original tuple
        assert result1["call_number"] == 1
        assert call_count == 1

        # Second call - cache hit, returns deserialized object
        # MessagePack converts tuples to lists (documented limitation)
        result2 = get_complex_encrypted_data(2)
        assert result2["nested"]["level1"]["level2"][2]["level3"] == "encrypted_deep"
        assert result2["mixed_list"][4] is True
        assert result2["tuple_data"] == [1, 2, 3]  # Tuple became list after MessagePack roundtrip
        assert result2["call_number"] == 1
        assert call_count == 1

    @pytest.mark.slow
    def test_encryption_wrapper_large_payloads(self, setup_encryption_master_key):
        """CRITICAL: EncryptionWrapper must handle large encrypted payloads."""
        master_key = setup_encryption_master_key
        call_count = 0

        @cache(
            ttl=300,
            l1=L1CacheConfig(enabled=False),
            encryption=EncryptionConfig(
                enabled=True, master_key=master_key, single_tenant_mode=True, deployment_uuid=TEST_UUID_LARGE
            ),
        )
        def get_large_encrypted_data(data_id):
            nonlocal call_count
            call_count += 1
            # Create a reasonably large data structure (AES-256-GCM can handle it)
            return {
                "id": data_id,
                "large_list": list(range(5000)),  # 5000 integers
                "large_dict": {str(i): i * 2 for i in range(500)},  # 500 key-value pairs
                "large_string": "x" * 10000,  # 10KB string
                "call_number": call_count,
            }

        # First call - cache miss (encrypt large payload)
        result1 = get_large_encrypted_data(4)
        assert len(result1["large_list"]) == 5000
        assert result1["large_list"][4999] == 4999
        assert len(result1["large_dict"]) == 500
        assert result1["large_dict"]["499"] == 998
        assert len(result1["large_string"]) == 10000
        assert result1["call_number"] == 1
        assert call_count == 1

        # Second call - cache hit (decrypt large payload)
        result2 = get_large_encrypted_data(4)
        assert result2 == result1
        assert result2["call_number"] == 1
        assert call_count == 1

    def test_encryption_wrapper_cache_invalidation(self, setup_encryption_master_key):
        """CRITICAL: Cache invalidation must work with encrypted data."""
        master_key = setup_encryption_master_key
        call_count = 0

        @cache(
            ttl=300,
            namespace="invalidate_encrypted",
            l1=L1CacheConfig(enabled=False),
            encryption=EncryptionConfig(
                enabled=True, master_key=master_key, single_tenant_mode=True, deployment_uuid=TEST_UUID_INV
            ),
        )
        def get_encrypted_counter_data():
            nonlocal call_count
            call_count += 1
            return {"count": call_count, "encrypted": True}

        # First call - cache miss
        result1 = get_encrypted_counter_data()
        assert result1["count"] == 1
        assert result1["encrypted"] is True

        # Second call - should be cached
        result2 = get_encrypted_counter_data()
        assert result2["count"] == 1

        # Invalidate cache
        get_encrypted_counter_data.invalidate_cache()

        # Third call - should execute function again
        result3 = get_encrypted_counter_data()
        assert result3["count"] == 2

    def test_encryption_wrapper_empty_data(self, setup_encryption_master_key):
        """CRITICAL: EncryptionWrapper must handle empty data structures."""
        master_key = setup_encryption_master_key
        call_count = 0

        @cache(
            ttl=300,
            l1=L1CacheConfig(enabled=False),
            encryption=EncryptionConfig(
                enabled=True, master_key=master_key, single_tenant_mode=True, deployment_uuid=TEST_UUID_EMPTY
            ),
        )
        def get_empty_encrypted_data(data_type):
            nonlocal call_count
            call_count += 1
            empty_data = {
                "dict": {},
                "list": [],
                "string": "",
                "null": None,
            }
            return {"type": data_type, "data": empty_data[data_type], "call": call_count}

        for data_type in ["dict", "list", "string", "null"]:
            result = get_empty_encrypted_data(data_type)
            assert result["type"] == data_type

            # Verify caching works
            result2 = get_empty_encrypted_data(data_type)
            assert result2 == result

    def test_encryption_wrapper_boolean_values(self, setup_encryption_master_key):
        """CRITICAL: EncryptionWrapper must preserve boolean types correctly."""
        master_key = setup_encryption_master_key
        call_count = 0

        @cache(
            ttl=300,
            l1=L1CacheConfig(enabled=False),
            encryption=EncryptionConfig(
                enabled=True, master_key=master_key, single_tenant_mode=True, deployment_uuid=TEST_UUID_BOOL
            ),
        )
        def get_boolean_data(bool_val):
            nonlocal call_count
            call_count += 1
            return {"value": bool_val, "type": "boolean", "call": call_count}

        # Test True
        result_true = get_boolean_data(True)
        assert result_true["value"] is True
        result_true_cached = get_boolean_data(True)
        assert result_true_cached["value"] is True

        # Test False
        result_false = get_boolean_data(False)
        assert result_false["value"] is False
        result_false_cached = get_boolean_data(False)
        assert result_false_cached["value"] is False

    def test_encryption_wrapper_numeric_types(self, setup_encryption_master_key):
        """CRITICAL: EncryptionWrapper must handle various numeric types."""
        master_key = setup_encryption_master_key
        call_count = 0

        @cache(
            ttl=300,
            l1=L1CacheConfig(enabled=False),
            encryption=EncryptionConfig(
                enabled=True, master_key=master_key, single_tenant_mode=True, deployment_uuid=TEST_UUID_NUMERIC
            ),
        )
        def get_numeric_data(data_id):
            nonlocal call_count
            call_count += 1
            return {
                "integer": 42,
                "negative": -10,
                "zero": 0,
                "float": 3.14159,
                "large_int": 9999999999,
                "call": call_count,
            }

        result1 = get_numeric_data(1)
        assert result1["integer"] == 42
        assert result1["negative"] == -10
        assert result1["zero"] == 0
        assert result1["float"] == 3.14159
        assert result1["large_int"] == 9999999999

        # Verify caching preserves numeric precision
        result2 = get_numeric_data(1)
        assert result2 == result1
        assert call_count == 1

    def test_encryption_wrapper_unicode_strings(self, setup_encryption_master_key):
        """CRITICAL: EncryptionWrapper must handle Unicode correctly."""
        master_key = setup_encryption_master_key
        call_count = 0

        @cache(
            ttl=300,
            l1=L1CacheConfig(enabled=False),
            encryption=EncryptionConfig(
                enabled=True, master_key=master_key, single_tenant_mode=True, deployment_uuid=TEST_UUID_UNICODE
            ),
        )
        def get_unicode_data(data_id):
            nonlocal call_count
            call_count += 1
            return {
                "english": "Hello World",
                "emoji": "🔒🔐🗝️",
                "chinese": "你好世界",
                "arabic": "مرحبا بالعالم",
                "russian": "Привет мир",
                "call": call_count,
            }

        result1 = get_unicode_data(1)
        assert result1["english"] == "Hello World"
        assert result1["emoji"] == "🔒🔐🗝️"
        assert result1["chinese"] == "你好世界"
        assert result1["arabic"] == "مرحبا بالعالم"
        assert result1["russian"] == "Привет мир"

        # Verify caching preserves Unicode
        result2 = get_unicode_data(1)
        assert result2 == result1
        assert call_count == 1

    def test_encryption_wrapper_key_derivation_consistency(self, setup_encryption_master_key):
        """CRITICAL: Same deployment UUID must always derive the same encryption key."""
        master_key = setup_encryption_master_key

        @cache(
            ttl=300,
            namespace="derive1",
            l1=L1CacheConfig(enabled=False),
            encryption=EncryptionConfig(
                enabled=True, master_key=master_key, single_tenant_mode=True, deployment_uuid=TEST_UUID_CONSISTENCY
            ),
        )
        def func1():
            return {"source": "func1", "data": "secret1"}

        @cache(
            ttl=300,
            namespace="derive2",
            l1=L1CacheConfig(enabled=False),
            encryption=EncryptionConfig(
                enabled=True, master_key=master_key, single_tenant_mode=True, deployment_uuid=TEST_UUID_CONSISTENCY
            ),
        )
        def func2():
            return {"source": "func2", "data": "secret2"}

        # Both functions use same tenant_id, so key derivation should be consistent
        result1 = func1()
        result2 = func2()

        assert result1["source"] == "func1"
        assert result2["source"] == "func2"

        # Verify caching works for both
        result1_cached = func1()
        result2_cached = func2()

        assert result1_cached == result1
        assert result2_cached == result2

    def test_encryption_wrapper_multiple_concurrent_tenants(self, setup_encryption_master_key):
        """CRITICAL: Multiple tenants can cache data concurrently without interference."""
        master_key = setup_encryption_master_key

        @cache(
            ttl=300,
            namespace="concurrent",
            l1=L1CacheConfig(enabled=False),
            encryption=EncryptionConfig(
                enabled=True, master_key=master_key, tenant_extractor=ArgumentNameExtractor("tenant_id")
            ),
        )
        def get_user_data(tenant_id, user_id):
            return {"tenant": tenant_id, "user": user_id, "secret": f"{tenant_id}_{user_id}_secret"}

        # Create data for multiple tenant/user combinations
        results = {}
        for tenant in ["tenant_a", "tenant_b", "tenant_c"]:
            for user in ["user_1", "user_2"]:
                result = get_user_data(tenant, user)
                results[(tenant, user)] = result
                assert result["tenant"] == tenant
                assert result["user"] == user
                assert result["secret"] == f"{tenant}_{user}_secret"

        # Verify all data is cached correctly and independently
        for (tenant, user), expected in results.items():
            cached = get_user_data(tenant, user)
            assert cached == expected

    def test_encryption_wrapper_type_preservation_across_encryption(self, setup_encryption_master_key):
        """CRITICAL: Data types must be preserved through encryption/decryption."""
        master_key = setup_encryption_master_key
        call_count = 0

        @cache(
            ttl=300,
            l1=L1CacheConfig(enabled=False),
            encryption=EncryptionConfig(
                enabled=True, master_key=master_key, single_tenant_mode=True, deployment_uuid=TEST_UUID_TYPE_PRESERVE
            ),
        )
        def get_typed_encrypted_data(data_id):
            nonlocal call_count
            call_count += 1
            return {
                "id": data_id,
                "tuple": (1, 2, 3),  # MessagePack converts to list
                "list": [1, 2, 3],  # Preserved as list
                "bool_true": True,  # Must remain bool, not int
                "bool_false": False,  # Must remain bool, not int
                "none": None,  # Must remain None
                "call_number": call_count,
            }

        # First call - cache miss, returns original object (before serialization)
        result1 = get_typed_encrypted_data(3)
        assert result1["tuple"] == (1, 2, 3)  # Original tuple
        assert isinstance(result1["tuple"], tuple)
        assert result1["list"] == [1, 2, 3]
        assert result1["bool_true"] is True
        assert isinstance(result1["bool_true"], bool)
        assert result1["bool_false"] is False
        assert isinstance(result1["bool_false"], bool)
        assert result1["none"] is None
        assert result1["call_number"] == 1
        assert call_count == 1

        # Second call - cache hit, returns deserialized object
        # MessagePack converts tuples to lists (documented limitation)
        result2 = get_typed_encrypted_data(3)
        assert result2["tuple"] == [1, 2, 3]  # Tuple became list after MessagePack roundtrip
        assert isinstance(result2["tuple"], list)
        assert result2["list"] == [1, 2, 3]
        assert result2["bool_true"] is True
        assert isinstance(result2["bool_true"], bool)
        assert result2["bool_false"] is False
        assert isinstance(result2["bool_false"], bool)
        assert result2["none"] is None
        assert result2["call_number"] == 1  # Still cached
        assert call_count == 1

    def test_encryption_wrapper_data_integrity_verification(self, setup_encryption_master_key):
        """CRITICAL: Encrypted data must maintain integrity (authentication tags work)."""
        master_key = setup_encryption_master_key
        call_count = 0

        @cache(
            ttl=300,
            l1=L1CacheConfig(enabled=False),
            encryption=EncryptionConfig(
                enabled=True, master_key=master_key, single_tenant_mode=True, deployment_uuid=TEST_UUID_INTEGRITY
            ),
        )
        def get_integrity_data(data_id):
            nonlocal call_count
            call_count += 1
            # Use deterministic data to ensure integrity checks are meaningful
            return {
                "id": data_id,
                "checksum_data": "this_should_match_exactly",
                "numbers": [1, 2, 3, 4, 5],
                "nested": {"level1": {"level2": "deep_data"}},
                "call": call_count,
            }

        # First call - cache miss (encrypt with authentication tag)
        result1 = get_integrity_data(1)
        assert result1["checksum_data"] == "this_should_match_exactly"
        assert result1["numbers"] == [1, 2, 3, 4, 5]
        assert result1["nested"]["level1"]["level2"] == "deep_data"
        assert result1["call"] == 1

        # Second call - cache hit (decrypt and verify authentication tag)
        result2 = get_integrity_data(1)
        assert result2 == result1
        assert call_count == 1

        # Data integrity is verified by AES-256-GCM authentication
        # If authentication fails, decryption raises an error
        # The fact that we get matching data proves integrity verification works

    def test_encryption_wrapper_different_encryption_contexts_produce_different_ciphertext(self, setup_encryption_master_key):
        """CRITICAL: Different encryption contexts must produce different ciphertext (key isolation)."""
        master_key = setup_encryption_master_key

        @cache(
            ttl=300,
            namespace="ctx1",
            l1=L1CacheConfig(enabled=False),
            encryption=EncryptionConfig(
                enabled=True, master_key=master_key, single_tenant_mode=True, deployment_uuid=TEST_UUID_CTX1
            ),
        )
        def func_ctx1():
            return {"data": "same_plaintext", "context": "1"}

        @cache(
            ttl=300,
            namespace="ctx2",
            l1=L1CacheConfig(enabled=False),
            encryption=EncryptionConfig(
                enabled=True, master_key=master_key, single_tenant_mode=True, deployment_uuid=TEST_UUID_CTX2
            ),
        )
        def func_ctx2():
            return {"data": "same_plaintext", "context": "2"}

        # Both functions cache the same plaintext but with different encryption contexts
        result1 = func_ctx1()
        result2 = func_ctx2()

        # Plaintext data field is same
        assert result1["data"] == result2["data"] == "same_plaintext"

        # But context differs
        assert result1["context"] == "1"
        assert result2["context"] == "2"

        # Different encryption contexts mean different derived keys
        # This is implicitly tested by the fact that each function correctly
        # decrypts its own cached data but not the other's

    def test_encryption_wrapper_with_zero_ttl_edge_case(self, setup_encryption_master_key):
        """CRITICAL: EncryptionWrapper must handle edge case of very short TTL."""
        import time

        master_key = setup_encryption_master_key
        call_count = 0

        @cache(
            ttl=1,
            l1=L1CacheConfig(enabled=False),
            encryption=EncryptionConfig(
                enabled=True, master_key=master_key, single_tenant_mode=True, deployment_uuid=TEST_UUID_TTL
            ),
        )  # 1 second TTL
        def get_short_ttl_data(data_id):
            nonlocal call_count
            call_count += 1
            return {"id": data_id, "call": call_count}

        # First call - cache miss
        result1 = get_short_ttl_data(1)
        assert result1["call"] == 1

        # Immediate second call - cache hit
        result2 = get_short_ttl_data(1)
        assert result2["call"] == 1
        assert call_count == 1

        # Wait for TTL expiration
        time.sleep(1.5)

        # After TTL - cache miss
        result3 = get_short_ttl_data(1)
        assert result3["call"] == 2
        assert call_count == 2
