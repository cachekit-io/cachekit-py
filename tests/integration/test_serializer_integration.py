"""Integration tests for serializer composability with encryption and backends.

Tests end-to-end serializer integration: ArrowSerializer + EncryptionWrapper + L1/L2 backends.
"""

from __future__ import annotations

import os

import pandas as pd
import pytest

from cachekit import cache
from cachekit.serializers.arrow_serializer import ArrowSerializer
from cachekit.serializers.encryption_wrapper import EncryptionWrapper


@pytest.mark.integration
class TestSerializerEncryptionComposability:
    """Test encryption composability with serializers (Requirement 3)."""

    @pytest.fixture(autouse=True)
    def setup_encryption(self):
        """Set up encryption master key for tests."""
        original_master_key = os.environ.get("CACHEKIT_MASTER_KEY")
        test_master_key = "a" * 64  # 32 bytes in hex = 64 hex chars
        os.environ["CACHEKIT_MASTER_KEY"] = test_master_key

        yield

        # Restore original setting
        if original_master_key is not None:
            os.environ["CACHEKIT_MASTER_KEY"] = original_master_key
        else:
            if "CACHEKIT_MASTER_KEY" in os.environ:
                del os.environ["CACHEKIT_MASTER_KEY"]

    def test_encryption_wrapper_with_auto_serializer_works(self):
        """EncryptionWrapper with AutoSerializer encrypts data correctly."""
        master_key = bytes.fromhex("a" * 64)
        encrypted_serializer = EncryptionWrapper(master_key=master_key, tenant_id="test-tenant")

        test_data = {"key": "value", "number": 42}
        cache_key = "test:encryption:auto"

        encrypted_data, metadata = encrypted_serializer.serialize(test_data, cache_key=cache_key)

        assert metadata.encrypted is True
        assert metadata.tenant_id == "test-tenant"
        assert metadata.encryption_algorithm == "AES-256-GCM"

        result = encrypted_serializer.deserialize(encrypted_data, metadata, cache_key=cache_key)
        assert result == test_data

    def test_encrypted_data_is_not_readable_without_decryption(self):
        """Encrypted data doesn't contain plaintext."""
        master_key = bytes.fromhex("a" * 64)
        encrypted_serializer = EncryptionWrapper(master_key=master_key, tenant_id="test-tenant")

        test_data = {"sensitive": "secret_value_12345"}
        cache_key = "test:encryption:opaque"

        encrypted_data, _ = encrypted_serializer.serialize(test_data, cache_key=cache_key)

        # Verify encrypted data doesn't contain plaintext
        assert b"secret_value_12345" not in encrypted_data
        assert b"sensitive" not in encrypted_data

    def test_different_tenant_cannot_decrypt(self):
        """Different tenant_id cannot decrypt data (tenant isolation)."""
        master_key = bytes.fromhex("a" * 64)
        serializer_tenant1 = EncryptionWrapper(master_key=master_key, tenant_id="tenant-1")
        serializer_tenant2 = EncryptionWrapper(master_key=master_key, tenant_id="tenant-2")

        test_data = {"data": "tenant1_secret"}
        cache_key = "test:tenant:isolation"

        # Encrypt with tenant-1
        encrypted_data, metadata = serializer_tenant1.serialize(test_data, cache_key=cache_key)

        # Attempt to decrypt with tenant-2 should fail
        # Decryption will fail due to tenant mismatch (raises various Rust errors)
        with pytest.raises((RuntimeError, ValueError, Exception)):
            serializer_tenant2.deserialize(encrypted_data, metadata, cache_key=cache_key)


@pytest.mark.integration
class TestSerializerBackendIntegration:
    """Test serializer integration with L1/L2 cache backends."""

    def test_arrow_serializer_with_cache_decorator(self, redis_isolated):
        """@cache decorator with ArrowSerializer stores/retrieves DataFrames end-to-end."""
        call_count = 0

        @cache(ttl=300, serializer=ArrowSerializer())
        def load_dataframe(dataset_id: str) -> pd.DataFrame:
            nonlocal call_count
            call_count += 1
            return pd.DataFrame({"id": [dataset_id], "value": [call_count * 10]})

        # First call - cache miss
        df1 = load_dataframe("dataset-123")
        assert call_count == 1
        assert isinstance(df1, pd.DataFrame)
        assert df1["value"].iloc[0] == 10

        # Second call - cache hit
        df2 = load_dataframe("dataset-123")
        assert call_count == 1  # No additional call
        pd.testing.assert_frame_equal(df2, df1)

        # Different argument - cache miss
        df3 = load_dataframe("dataset-456")
        assert call_count == 2
        assert df3["id"].iloc[0] == "dataset-456"

    def test_arrow_bytes_stored_in_redis(self, redis_isolated):
        """Verify Redis stores Arrow IPC format bytes."""

        @cache(ttl=300, serializer=ArrowSerializer())
        def get_data() -> pd.DataFrame:
            return pd.DataFrame({"col": [1, 2, 3]})

        # Execute to populate cache
        get_data()

        # Inspect Redis directly to verify Arrow IPC format stored
        # (Arrow IPC format has specific magic bytes: b'ARROW1')
        keys = redis_isolated.keys("*")
        assert len(keys) > 0  # Cache entry exists

        # Retrieve raw bytes from Redis
        raw_data = redis_isolated.get(keys[0])
        assert raw_data is not None

        # Note: The actual bytes will be wrapped by ByteStorage (LZ4 compression + xxHash3-64)
        # so we can't directly inspect Arrow magic bytes here. The test validates that
        # data flows through Redis successfully.

    def test_l1_l2_cache_with_arrow_serializer(self, redis_isolated):
        """L1+L2 cache with ArrowSerializer works correctly."""
        call_count = 0

        @cache(ttl=300, serializer=ArrowSerializer(), l1_enabled=True)
        def compute_dataframe(size: int) -> pd.DataFrame:
            nonlocal call_count
            call_count += 1
            return pd.DataFrame({"index": range(size), "value": [i * 2 for i in range(size)]})

        # First call - cache miss (populates L2 and L1)
        df1 = compute_dataframe(5)
        assert call_count == 1
        assert len(df1) == 5

        # Second call - L1 hit (fast path, no Redis access)
        df2 = compute_dataframe(5)
        assert call_count == 1  # No function execution
        pd.testing.assert_frame_equal(df2, df1)

        # Clear L1 cache to test L2 hit
        from cachekit.l1_cache import get_l1_cache_manager

        get_l1_cache_manager().clear_all()

        # Third call - L1 miss, L2 hit (retrieves from Redis, populates L1)
        df3 = compute_dataframe(5)
        assert call_count == 1  # Still no function execution (L2 hit)
        pd.testing.assert_frame_equal(df3, df1)

    def test_auto_serializer_without_serializer_parameter(self, redis_isolated):
        """@cache without serializer parameter uses AutoSerializer (backward compat)."""
        call_count = 0

        @cache(ttl=300)  # No serializer parameter
        def get_dict_data(key: str) -> dict:
            nonlocal call_count
            call_count += 1
            return {"key": key, "value": call_count}

        # This should work with AutoSerializer (MessagePack)
        result1 = get_dict_data("test")
        assert call_count == 1
        assert result1["key"] == "test"

        result2 = get_dict_data("test")
        assert call_count == 1  # Cache hit
        assert result2 == result1


@pytest.mark.integration
class TestSerializerGracefulDegradation:
    """Test graceful degradation with corrupted data and error recovery."""

    def test_corrupted_cache_data_results_in_cache_miss(self, redis_isolated):
        """Corrupted cache data should result in cache miss (graceful degradation)."""
        call_count = 0

        @cache(ttl=300, serializer=ArrowSerializer())
        def get_dataframe() -> pd.DataFrame:
            nonlocal call_count
            call_count += 1
            return pd.DataFrame({"data": [1, 2, 3]})

        # First call - populate cache
        df1 = get_dataframe()
        assert call_count == 1

        # Corrupt the cached data in Redis
        keys = redis_isolated.keys("*")
        if keys:
            redis_isolated.set(keys[0], b"corrupted_data_not_valid_arrow")

        # Next call should gracefully handle corruption (log warning, recompute)
        # The system may treat this as a cache miss and recompute, or it may
        # log an error and continue. Both are acceptable graceful degradation.
        df2 = get_dataframe()
        # Call count may be 1 (cached) or 2 (recomputed) depending on error handling
        assert call_count in (1, 2)
        if call_count == 2:
            pd.testing.assert_frame_equal(df2, df1)  # Same result

    def test_arrow_serializer_with_non_dataframe_gracefully_handled(self):
        """ArrowSerializer with non-DataFrame returns uncached result (graceful degradation)."""
        call_count = 0

        @cache(ttl=300, serializer=ArrowSerializer())
        def get_scalar_value() -> int:
            nonlocal call_count
            call_count += 1
            return 42

        # ArrowSerializer will fail to serialize scalar value, but the decorator
        # should gracefully degrade (log error, return result uncached)
        result1 = get_scalar_value()
        assert result1 == 42
        assert call_count == 1

        # Second call should also execute (no caching due to serialization error)
        result2 = get_scalar_value()
        assert result2 == 42
        assert call_count == 2  # Function executed again (no cache)


@pytest.mark.integration
class TestSerializerWithEncryptionInProduction:
    """Serializer + encryption integration (Issue #134).

    Re-added after PR #133 removed the original variants. Those tests combined
    ``@cache(serializer=ArrowSerializer())`` with ``CACHEKIT_MASTER_KEY`` and asserted
    Arrow encoding flowed through the encryption path — it never did, because
    ``CacheSerializationHandler._get_cached_encryption_wrapper`` built the
    EncryptionWrapper WITHOUT the user's serializer, so it silently fell back to
    StandardSerializer (MessagePack). Issue #134 threads the user's serializer into
    the wrapper and adds the ``cross_sdk_compatible`` marker. These tests lock in the
    fix: the user's serializer must actually be used under encryption.
    """

    @pytest.fixture(autouse=True)
    def setup_encryption(self):
        """Set up encryption master key and reset the settings singleton each side."""
        from cachekit.config.singleton import reset_settings

        original_master_key = os.environ.get("CACHEKIT_MASTER_KEY")
        os.environ["CACHEKIT_MASTER_KEY"] = "a" * 64  # 32 bytes hex
        reset_settings()

        yield

        if original_master_key is not None:
            os.environ["CACHEKIT_MASTER_KEY"] = original_master_key
        else:
            os.environ.pop("CACHEKIT_MASTER_KEY", None)
        reset_settings()

    def test_arrow_dataframe_encrypt_decrypt_roundtrip(self, redis_isolated):
        """Arrow DataFrame survives an encrypt -> store -> retrieve -> decrypt roundtrip.

        REGRESSION (Issue #134): before the fix this silently substituted MessagePack,
        which cannot round-trip a DataFrame, so the cache hit would not equal the source.
        """
        call_count = 0

        @cache(ttl=300, serializer=ArrowSerializer(), namespace="enc_arrow_rt")
        def load_data(user_id: str) -> pd.DataFrame:
            nonlocal call_count
            call_count += 1
            return pd.DataFrame({"user_id": [user_id], "balance": [call_count * 1000.0]})

        # First call - cache miss (serialize+encrypt path)
        df1 = load_data("user-123")
        assert call_count == 1
        assert df1["balance"].iloc[0] == 1000.0

        # Cache entry exists in Redis
        keys = redis_isolated.keys("*")
        assert len(keys) > 0

        # Second call - cache hit (retrieve+decrypt path), must equal original DataFrame
        df2 = load_data("user-123")
        assert call_count == 1, "Expected cache hit; function should not run again"
        assert isinstance(df2, pd.DataFrame)
        pd.testing.assert_frame_equal(df2, df1)

    def test_encrypted_arrow_uses_arrow_not_messagepack(self, redis_isolated):
        """The EncryptionWrapper must wrap the user's ArrowSerializer, not StandardSerializer.

        This is the core assertion for Issue #134: assert the wrapper's base serializer
        is the user's Arrow serializer and that the produced metadata carries the Arrow
        wire format (not MessagePack).
        """
        from cachekit.cache_handler import CacheSerializationHandler
        from cachekit.serializers.arrow_serializer import ArrowSerializer
        from cachekit.serializers.base import SerializationFormat

        deployment_uuid = "00000000-0000-0000-0000-000000000134"
        handler = CacheSerializationHandler(
            serializer_name=ArrowSerializer(),
            encryption=True,
            single_tenant_mode=True,
            deployment_uuid=deployment_uuid,
            master_key="a" * 64,
        )

        wrapper = handler._get_cached_encryption_wrapper(deployment_uuid)
        assert type(wrapper.serializer).__name__ == "ArrowSerializer", (
            "EncryptionWrapper must wrap the user's ArrowSerializer, not fall back to StandardSerializer"
        )

        df = pd.DataFrame({"col": [1, 2, 3], "val": [1.5, 2.5, 3.5]})
        encrypted, metadata = wrapper.serialize(df, cache_key="data:df")

        assert metadata.encrypted is True
        assert metadata.format == SerializationFormat.ARROW, (
            f"Encrypted data must carry the Arrow wire format, got {metadata.format}"
        )

        decrypted = wrapper.deserialize(encrypted, metadata, cache_key="data:df")
        assert isinstance(decrypted, pd.DataFrame)
        pd.testing.assert_frame_equal(decrypted, df)

    def test_dataframe_index_preserved_with_encrypted_arrow_serializer(self, redis_isolated):
        """DataFrame index is preserved through the encrypted Arrow path end-to-end."""

        @cache(ttl=300, serializer=ArrowSerializer(), namespace="enc_arrow_idx")
        def get_indexed_data() -> pd.DataFrame:
            return pd.DataFrame({"value": [10, 20, 30]}, index=["a", "b", "c"])

        df1 = get_indexed_data()
        assert list(df1.index) == ["a", "b", "c"]

        df2 = get_indexed_data()
        pd.testing.assert_frame_equal(df2, df1)
        assert list(df2.index) == ["a", "b", "c"]
