"""Tests for L2 decrypt/integrity failure observability.

When L2 cached data cannot be deserialized (decrypt failure, integrity check
failure, corrupt data), the decorator must:
  1. Log a warning identifying the failure as decrypt/integrity related.
  2. Treat it as a miss and recompute (fail-open — existing behavior).
"""

from __future__ import annotations

import logging
from unittest import mock

import pytest

from cachekit.cache_handler import CacheOperationHandler, CacheSerializationHandler
from cachekit.key_generator import CacheKeyGenerator
from cachekit.serializers.base import SerializationError
from cachekit.serializers.encryption_wrapper import EncryptionError


@pytest.mark.unit
class TestL2DecryptFailureWarning:
    """CacheOperationHandler.get_cached_value logs on SerializationError."""

    def _make_handler(self, *, deserialize_side_effect: Exception) -> CacheOperationHandler:
        """Build a CacheOperationHandler whose serialization_handler.deserialize_data raises."""
        mock_serialization = mock.MagicMock(spec=CacheSerializationHandler)
        mock_serialization.deserialize_data.side_effect = deserialize_side_effect

        handler = CacheOperationHandler(mock_serialization, CacheKeyGenerator())

        mock_cache_handler = mock.MagicMock()
        mock_cache_handler.get.return_value = b"corrupted-bytes"
        handler.set_cache_handler(mock_cache_handler)
        return handler

    def test_serialization_error_logs_warning(self, caplog: pytest.LogCaptureFixture) -> None:
        """SerializationError at L2 deserialize logs a specific warning."""
        handler = self._make_handler(deserialize_side_effect=SerializationError("integrity check failed"))

        with caplog.at_level(logging.WARNING):
            result = handler.get_cached_value("test:key")

        # Fail-open: returns None (miss)
        assert result is None
        # Must contain the decrypt/integrity warning
        assert any("decrypt/integrity failure" in r.message for r in caplog.records)

    def test_encryption_error_logs_warning(self, caplog: pytest.LogCaptureFixture) -> None:
        """EncryptionError (subclass of SerializationError) also triggers the warning."""
        handler = self._make_handler(deserialize_side_effect=EncryptionError("Decryption failed: GCM tag mismatch"))

        with caplog.at_level(logging.WARNING):
            result = handler.get_cached_value("test:key")

        assert result is None
        assert any("decrypt/integrity failure" in r.message for r in caplog.records)
        assert any("GCM tag mismatch" in r.message for r in caplog.records)

    def test_generic_exception_does_not_trigger_decrypt_warning(self, caplog: pytest.LogCaptureFixture) -> None:
        """Non-SerializationError (e.g. ConnectionError) uses the generic warning."""
        handler = self._make_handler(deserialize_side_effect=ConnectionError("Redis connection lost"))

        with caplog.at_level(logging.WARNING):
            result = handler.get_cached_value("test:key")

        assert result is None
        # Generic path, not decrypt/integrity
        assert not any("decrypt/integrity failure" in r.message for r in caplog.records)
        assert any("Backend operation failed" in r.message for r in caplog.records)

    def test_recompute_on_decrypt_failure(self) -> None:
        """After decrypt failure, get_cached_value returns None so caller recomputes."""
        handler = self._make_handler(deserialize_side_effect=EncryptionError("Decryption failed: wrong key"))

        result = handler.get_cached_value("test:key")
        assert result is None  # Caller will recompute

    def test_serialization_error_evicts_poisoned_entry(self) -> None:
        """On corruption, the poisoned L2 entry is deleted so reads stop re-failing (#159)."""
        handler = self._make_handler(deserialize_side_effect=SerializationError("integrity check failed"))

        result = handler.get_cached_value("poison:key")

        assert result is None
        handler._cache_handler.delete.assert_called_once_with("poison:key")

    def test_eviction_failure_does_not_mask_miss(self, caplog: pytest.LogCaptureFixture) -> None:
        """If eviction itself fails, get_cached_value still returns None (best-effort)."""
        handler = self._make_handler(deserialize_side_effect=SerializationError("corrupt"))
        handler._cache_handler.delete.side_effect = RuntimeError("backend down")

        with caplog.at_level(logging.WARNING):
            result = handler.get_cached_value("poison:key")

        assert result is None
        assert any("Failed to evict poisoned" in r.message for r in caplog.records)

    async def test_async_serialization_error_evicts_poisoned_entry(self) -> None:
        """Async corruption path evicts the poisoned entry via delete_async (#159)."""
        mock_serialization = mock.MagicMock(spec=CacheSerializationHandler)
        mock_serialization.deserialize_data.side_effect = SerializationError("integrity check failed")
        handler = CacheOperationHandler(mock_serialization, CacheKeyGenerator())
        mock_ch = mock.MagicMock()
        mock_ch.get_async = mock.AsyncMock(return_value=b"corrupted-bytes")
        mock_ch.delete_async = mock.AsyncMock(return_value=True)
        handler.set_cache_handler(mock_ch)

        result = await handler.get_cached_value_async("poison:key")

        assert result is None
        mock_ch.delete_async.assert_awaited_once_with("poison:key")

    async def test_async_eviction_failure_does_not_mask_miss(self, caplog: pytest.LogCaptureFixture) -> None:
        """Async eviction failure must not propagate; still a miss."""
        mock_serialization = mock.MagicMock(spec=CacheSerializationHandler)
        mock_serialization.deserialize_data.side_effect = SerializationError("corrupt")
        handler = CacheOperationHandler(mock_serialization, CacheKeyGenerator())
        mock_ch = mock.MagicMock()
        mock_ch.get_async = mock.AsyncMock(return_value=b"corrupted-bytes")
        mock_ch.delete_async = mock.AsyncMock(side_effect=RuntimeError("backend down"))
        handler.set_cache_handler(mock_ch)

        with caplog.at_level(logging.WARNING):
            result = await handler.get_cached_value_async("poison:key")

        assert result is None
        assert any("Failed to evict poisoned" in r.message for r in caplog.records)
