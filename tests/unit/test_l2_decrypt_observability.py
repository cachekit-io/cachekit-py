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
