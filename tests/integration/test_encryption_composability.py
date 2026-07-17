"""Integration tests for EncryptionWrapper composability with any SerializerProtocol.

Tests that EncryptionWrapper can wrap any serializer (OrjsonSerializer, ArrowSerializer)
to enable zero-knowledge caching for any data type.
"""

import numpy as np
import pandas as pd
import pytest

from cachekit.serializers import ArrowSerializer, AutoSerializer, EncryptionWrapper, OrjsonSerializer


class TestEncryptionWrapperComposability:
    """Test EncryptionWrapper can wrap any SerializerProtocol."""

    @pytest.fixture
    def master_key(self):
        """32-byte master key for testing."""
        return b"a" * 32

    def test_encryption_wrapper_with_default_serializer(self, master_key):
        """EncryptionWrapper with default StandardSerializer (MessagePack) works."""
        wrapper = EncryptionWrapper(master_key=master_key, tenant_id="test-tenant")

        data = {"user": "alice", "ssn": "123-45-6789"}
        cache_key = "test:encryption:auto"
        encrypted, metadata = wrapper.serialize(data, cache_key=cache_key)

        # Verify encrypted
        assert metadata.encrypted is True
        assert metadata.encryption_algorithm == "AES-256-GCM"
        assert b"alice" not in encrypted  # No plaintext in ciphertext

        # Verify roundtrip
        decrypted = wrapper.deserialize(encrypted, metadata, cache_key=cache_key)
        assert decrypted == data

    def test_encryption_wrapper_with_orjson_serializer(self, master_key):
        """EncryptionWrapper with OrjsonSerializer (JSON) works for zero-knowledge API caching."""
        orjson_wrapper = EncryptionWrapper(serializer=OrjsonSerializer(), master_key=master_key, tenant_id="api-tenant")

        # API response with PII
        api_response = {
            "status": "success",
            "user": {"email": "user@example.com", "phone": "+1-555-0100"},
            "session_token": "secret-token-12345",
        }

        cache_key = "test:encryption:orjson"
        encrypted, metadata = orjson_wrapper.serialize(api_response, cache_key=cache_key)

        # Verify encrypted JSON
        assert metadata.encrypted is True
        assert metadata.format.value == "orjson"  # Underlying format preserved
        assert b"user@example.com" not in encrypted  # No PII in ciphertext
        assert b"secret-token" not in encrypted

        # Verify roundtrip preserves structure
        decrypted = orjson_wrapper.deserialize(encrypted, metadata, cache_key=cache_key)
        assert decrypted == api_response

    def test_encryption_wrapper_with_arrow_serializer(self, master_key):
        """EncryptionWrapper with ArrowSerializer (DataFrames) works for zero-knowledge ML caching."""
        arrow_wrapper = EncryptionWrapper(serializer=ArrowSerializer(), master_key=master_key, tenant_id="ml-tenant")

        # Sensitive ML features
        df = pd.DataFrame(
            {"patient_id": [101, 102, 103], "diagnosis": ["diabetes", "hypertension", "healthy"], "risk_score": [0.8, 0.6, 0.1]}
        )

        cache_key = "test:encryption:arrow"
        encrypted, metadata = arrow_wrapper.serialize(df, cache_key=cache_key)

        # Verify encrypted DataFrame
        assert metadata.encrypted is True
        assert metadata.format.value == "arrow"  # Arrow format preserved
        assert b"diabetes" not in encrypted  # No sensitive data in ciphertext
        assert b"hypertension" not in encrypted

        # Verify roundtrip preserves DataFrame
        decrypted = arrow_wrapper.deserialize(encrypted, metadata, cache_key=cache_key)
        pd.testing.assert_frame_equal(decrypted, df)

    def test_tenant_isolation_across_different_serializers(self, master_key):
        """Different tenants with different serializers cannot decrypt each other's data."""
        tenant_a = EncryptionWrapper(serializer=OrjsonSerializer(), master_key=master_key, tenant_id="tenant-a")
        tenant_b = EncryptionWrapper(serializer=OrjsonSerializer(), master_key=master_key, tenant_id="tenant-b")

        data = {"secret": "confidential"}
        cache_key = "test:tenant:isolation"

        # Tenant A encrypts
        encrypted_a, metadata_a = tenant_a.serialize(data, cache_key=cache_key)

        # Tenant B cannot decrypt
        with pytest.raises(Exception) as exc_info:
            tenant_b.deserialize(encrypted_a, metadata_a, cache_key=cache_key)

        assert "Tenant mismatch" in str(exc_info.value)

    def test_zero_knowledge_opaque_storage_workflow(self, master_key):
        """Simulate zero-knowledge pattern: client encrypts, backend stores opaque blobs."""
        # Client encrypts JSON API response
        client_wrapper = EncryptionWrapper(serializer=OrjsonSerializer(), master_key=master_key, tenant_id="customer-123")

        sensitive_data = {"api_key": "sk_live_abc123", "webhook_secret": "whsec_xyz789"}
        cache_key = "test:zero-knowledge:secrets"

        # Client-side encryption
        encrypted_blob, metadata = client_wrapper.serialize(sensitive_data, cache_key=cache_key)

        # Backend receives encrypted blob (zero-knowledge - cannot decrypt)
        # Backend stores blob without seeing plaintext
        assert b"sk_live" not in encrypted_blob
        assert b"whsec" not in encrypted_blob

        # Only client can decrypt
        decrypted = client_wrapper.deserialize(encrypted_blob, metadata, cache_key=cache_key)
        assert decrypted == sensitive_data

    def test_auto_serializer_binds_true_compressed_flag_into_aad(self, master_key):
        """Regression (#166): AutoSerializer's ByteStorage paths must bind compressed="True" into AAD v0x03.

        AutoSerializer LZ4-compresses via ByteStorage but never set compressed=True in its
        metadata, so EncryptionWrapper authenticated "False" while a conformant reader
        (protocol spec/encryption.md: create_aad(..., compressed=true) for enveloped
        payloads) computes "True" — AES-GCM authentication failed on legitimate entries.
        """
        wrapper = EncryptionWrapper(serializer=AutoSerializer(), master_key=master_key, tenant_id="test-tenant")
        cache_key = "test:encryption:auto:compressed"
        data = {"user": "alice", "score": 100}

        encrypted, metadata = wrapper.serialize(data, cache_key=cache_key)

        # Writer metadata reflects the ByteStorage LZ4 envelope (parity with StandardSerializer)
        assert metadata.compressed is True

        # Conformant reader: hand-built AAD v0x03 per spec (compressed="True") must authenticate.
        # AutoSerializer's msgpack path sets original_type="msgpack", appended as 5th component.
        components = [b"test-tenant", cache_key.encode(), b"msgpack", b"True", b"msgpack"]
        spec_aad = bytes([0x03]) + b"".join(len(c).to_bytes(4, "big") + c for c in components)
        envelope = wrapper.encryptor.decrypt_with_keys(encrypted, spec_aad, wrapper.tenant_keys)

        # Recovered ByteStorage envelope round-trips to the original object
        assert AutoSerializer().deserialize(bytes(envelope), metadata) == data

        # Full wrapper round-trip still works with the stored metadata
        assert wrapper.deserialize(encrypted, metadata, cache_key=cache_key) == data

    @pytest.mark.parametrize("integrity", [True, False])
    def test_compressed_flag_reflects_bytestorage_codec(self, integrity):
        """compressed=True iff the ByteStorage LZ4 envelope wrapped the payload (#166).

        Covers every AutoSerializer path: msgpack default, Series, the DataFrame
        msgpack-columnar fallback (pyarrow absent), and the checksum-only numpy path
        (never LZ4, so False regardless of integrity checking).
        """
        serializer = AutoSerializer(enable_integrity_checking=integrity)

        _, msgpack_meta = serializer.serialize({"a": 1})
        assert msgpack_meta.compressed is integrity

        _, series_meta = serializer.serialize(pd.Series([1, 2, 3]))
        assert series_meta.compressed is integrity

        serializer._arrow_serializer = None  # force the msgpack-columnar fallback path
        _, df_meta = serializer.serialize(pd.DataFrame({"a": [1, 2, 3]}))
        assert df_meta.original_type == "dataframe"
        assert df_meta.compressed is integrity

        _, numpy_meta = serializer.serialize(np.array([1, 2, 3]))
        assert numpy_meta.compressed is False

    def test_encryption_metadata_preserves_format_information(self, master_key):
        """Encryption metadata correctly preserves underlying serialization format."""
        serializers = [
            (AutoSerializer(), "msgpack"),
            (OrjsonSerializer(), "orjson"),
            (ArrowSerializer(), "arrow"),
        ]

        for serializer, expected_format in serializers:
            wrapper = EncryptionWrapper(serializer=serializer, master_key=master_key, tenant_id="test")
            cache_key = f"test:metadata:{expected_format}"

            # Test with appropriate data for each serializer
            if expected_format == "arrow":
                data = pd.DataFrame({"a": [1, 2, 3]})
            else:
                data = {"test": "data"}

            encrypted, metadata = wrapper.serialize(data, cache_key=cache_key)

            assert metadata.encrypted is True
            assert metadata.format.value == expected_format
            assert metadata.encryption_algorithm == "AES-256-GCM"
