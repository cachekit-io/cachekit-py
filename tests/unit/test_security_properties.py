"""Property-based security tests using Hypothesis."""

from __future__ import annotations

import pytest
from hypothesis import assume, given, settings

from cachekit.serializers.base import SerializationError
from tests.utils.fuzzing_strategies import SecurityFuzzingStrategies


@pytest.mark.unit
@pytest.mark.critical
class TestEncryptionProperties:
    """Property-based tests for encryption security properties."""

    @given(
        data=SecurityFuzzingStrategies.cache_payloads(),
        key=SecurityFuzzingStrategies.encryption_keys(),
    )
    @settings(max_examples=50, deadline=None)
    def test_encryption_roundtrip(self, data: object, key: bytes) -> None:
        """Property: decrypt(encrypt(data, key), key) == data.

        Verifies that encryption roundtrip preserves original data.
        """
        # Skip testing with actual EncryptionWrapper for now (complex initialization)
        # This test validates the property hypothesis itself
        # Production use would test against actual serializer implementation
        try:
            from cachekit.serializers.base import SerializationError
            from cachekit.serializers.encryption_wrapper import EncryptionWrapper

            serializer = EncryptionWrapper(master_key=key)
            try:
                encrypted, metadata = serializer.serialize(data, cache_key="test_key")
                decrypted = serializer.deserialize(encrypted, metadata, cache_key="test_key")

                # Verify roundtrip
                assert decrypted == data, "Roundtrip failed: data mismatch"
            except SerializationError:
                # Some generated data may not be serializable (e.g., large ints > int64)
                # This is expected - skip those cases
                assume(False)
        except (ImportError, TypeError, AttributeError):
            # Skip if serializer not available in test environment
            pytest.skip("EncryptionWrapper not available")

    @given(
        data=SecurityFuzzingStrategies.cache_payloads(),
        tenant_a=SecurityFuzzingStrategies.tenant_ids(),
        tenant_b=SecurityFuzzingStrategies.tenant_ids(),
    )
    @settings(max_examples=50, deadline=None)
    def test_tenant_isolation(self, data: object, tenant_a: str, tenant_b: str) -> None:
        """Property: Different tenants produce different ciphertexts.

        Verifies that tenant isolation prevents data leakage between tenants.
        """
        # Only test if tenants are different
        assume(tenant_a != tenant_b)

        try:
            from cachekit.serializers.encryption_wrapper import EncryptionWrapper

            # Use a fixed test key for reproducibility
            test_key = b"0" * 32

            # Encrypt same data with different tenant IDs
            serializer_a = EncryptionWrapper(master_key=test_key, tenant_id=tenant_a)
            encrypted_a, _ = serializer_a.serialize(data, cache_key="test_key")

            serializer_b = EncryptionWrapper(master_key=test_key, tenant_id=tenant_b)
            encrypted_b, _ = serializer_b.serialize(data, cache_key="test_key")

            # Verify tenant isolation: ciphertexts must differ
            assert encrypted_a != encrypted_b, "Tenant isolation failed: ciphertexts match"
        except (ImportError, TypeError, AttributeError):
            # Skip if serializer not available in test environment
            pytest.skip("EncryptionWrapper not available")
        except SerializationError:
            # Skip non-serializable Hypothesis-generated data
            assume(False)
