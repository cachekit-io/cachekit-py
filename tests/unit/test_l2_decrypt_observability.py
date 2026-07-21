"""Tests for L2 decrypt/integrity failure observability.

When L2 cached data cannot be deserialized (decrypt failure, integrity check
failure, corrupt data), the decorator must:
  1. Log a warning identifying the failure as decrypt/integrity related.
  2. Treat it as a miss and recompute (fail-open — existing behavior).
  3. Evict the poisoned entry and emit the cache_get_deserialize metric on
     both sync and async decorator paths (#159).
"""

from __future__ import annotations

import logging
from typing import Any
from unittest import mock

import pytest

from cachekit import cache
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
        # Instance attr set in __init__, invisible to spec= — and it must be a real
        # bool: a bare MagicMock here is truthy, which would silently flip the read
        # path to fail-closed (cachekit-py#170).
        mock_serialization.encryption_fail_closed = False

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
        mock_serialization.encryption_fail_closed = False  # real bool: MagicMock is truthy
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
        mock_serialization.encryption_fail_closed = False  # real bool: MagicMock is truthy
        handler = CacheOperationHandler(mock_serialization, CacheKeyGenerator())
        mock_ch = mock.MagicMock()
        mock_ch.get_async = mock.AsyncMock(return_value=b"corrupted-bytes")
        mock_ch.delete_async = mock.AsyncMock(side_effect=RuntimeError("backend down"))
        handler.set_cache_handler(mock_ch)

        with caplog.at_level(logging.WARNING):
            result = await handler.get_cached_value_async("poison:key")

        assert result is None
        assert any("Failed to evict poisoned" in r.message for r in caplog.records)

    async def test_async_hit_returns_value_and_raw_bytes(self) -> None:
        """get_cached_value_async returns (True, value, raw_bytes) so the async
        decorator can backfill L1 with the serialized envelope without re-serializing."""
        sentinel = object()
        mock_serialization = mock.MagicMock(spec=CacheSerializationHandler)
        mock_serialization.deserialize_data.return_value = sentinel
        handler = CacheOperationHandler(mock_serialization, CacheKeyGenerator())
        mock_ch = mock.MagicMock()
        mock_ch.get_async = mock.AsyncMock(return_value=b"serialized-envelope")
        handler.set_cache_handler(mock_ch)

        result = await handler.get_cached_value_async("hit:key")

        assert result == (True, sentinel, b"serialized-envelope")


@pytest.mark.unit
class TestOnDeserializeErrorHook:
    """The on_deserialize_error hook fires once per corrupt L2 read on both paths.

    The decorator wires this hook to features.handle_cache_error so the
    cache_get_deserialize metric fires from a single place (#159).
    """

    def _make_handler(self) -> CacheOperationHandler:
        mock_serialization = mock.MagicMock(spec=CacheSerializationHandler)
        mock_serialization.deserialize_data.side_effect = SerializationError("integrity check failed")
        # Instance attrs set in __init__ are invisible to spec= and MUST be real values:
        # a bare MagicMock is truthy, which would flip the read path to fail-closed
        # (encryption_fail_closed) or route into the mmap branch (supports_mmap_read).
        mock_serialization.encryption_fail_closed = False
        mock_serialization.supports_mmap_read.return_value = False
        handler = CacheOperationHandler(mock_serialization, CacheKeyGenerator())
        mock_ch = mock.MagicMock()
        mock_ch.get.return_value = b"corrupted-bytes"
        mock_ch.get_async = mock.AsyncMock(return_value=b"corrupted-bytes")
        mock_ch.delete_async = mock.AsyncMock(return_value=True)
        handler.set_cache_handler(mock_ch)
        return handler

    def test_sync_corruption_invokes_hook(self) -> None:
        handler = self._make_handler()
        calls: list[tuple[Exception, str]] = []
        handler.on_deserialize_error = lambda error, key: calls.append((error, key))

        assert handler.get_cached_value("poison:key") is None
        assert len(calls) == 1
        assert isinstance(calls[0][0], SerializationError)
        assert calls[0][1] == "poison:key"

    async def test_async_corruption_invokes_hook(self) -> None:
        handler = self._make_handler()
        calls: list[tuple[Exception, str]] = []
        handler.on_deserialize_error = lambda error, key: calls.append((error, key))

        assert await handler.get_cached_value_async("poison:key") is None
        assert len(calls) == 1
        assert isinstance(calls[0][0], SerializationError)
        assert calls[0][1] == "poison:key"

    def test_hook_failure_does_not_mask_miss(self, caplog: pytest.LogCaptureFixture) -> None:
        """Observability must never break the miss/recompute path."""
        handler = self._make_handler()
        handler.on_deserialize_error = mock.MagicMock(side_effect=RuntimeError("metrics down"))

        with caplog.at_level(logging.WARNING):
            assert handler.get_cached_value("poison:key") is None

        handler.on_deserialize_error.assert_called_once()

    def test_generic_backend_error_does_not_invoke_hook(self) -> None:
        """Network/backend errors are not corruption; the hook must stay silent."""
        mock_serialization = mock.MagicMock(spec=CacheSerializationHandler)
        mock_serialization.deserialize_data.side_effect = ConnectionError("redis down")
        handler = CacheOperationHandler(mock_serialization, CacheKeyGenerator())
        mock_ch = mock.MagicMock()
        mock_ch.get.return_value = b"bytes"
        handler.set_cache_handler(mock_ch)
        hook = mock.MagicMock()
        handler.on_deserialize_error = hook

        assert handler.get_cached_value("some:key") is None
        hook.assert_not_called()


class DictBackend:
    """Minimal sync backend with a delete spy for poison-eviction tests."""

    def __init__(self) -> None:
        self._store: dict[str, bytes] = {}
        self.deleted: list[str] = []

    def get(self, key: str) -> bytes | None:
        return self._store.get(key)

    def set(self, key: str, value: bytes, ttl: int | None = None) -> None:
        self._store[key] = value

    def delete(self, key: str) -> bool:
        self.deleted.append(key)
        return self._store.pop(key, None) is not None

    def exists(self, key: str) -> bool:
        return key in self._store

    def health_check(self) -> tuple[bool, dict[str, Any]]:
        return True, {"backend_type": "dict_test"}


def _tamper(envelope: bytes) -> bytes:
    """Flip a payload byte near the end of a stored envelope.

    Keeps the CK frame + JSON header parseable so deserialization fails the
    integrity/decode stage with SerializationError. (A fully garbage frame
    raises ValueError instead and is not evicted — that detection gap is #156.)
    """
    poisoned = bytearray(envelope)
    poisoned[-3] ^= 0xFF
    return bytes(poisoned)


@pytest.mark.unit
class TestDecoratorPoisonEviction:
    """End-to-end regression for #159: a poisoned L2 entry observed by a decorated
    call is evicted, the value recomputed and re-stored, and the
    cache_get_deserialize metric fires — on both sync and async paths."""

    async def test_async_decorated_call_evicts_poisoned_l2_entry(self, caplog: pytest.LogCaptureFixture) -> None:
        backend = DictBackend()
        calls = 0

        @cache(backend=backend, ttl=300, l1_enabled=False)
        async def fn(x: int) -> dict:
            nonlocal calls
            calls += 1
            return {"result": x * 2}

        assert (await fn(21))["result"] == 42  # populate L2
        (key,) = backend._store  # exactly one entry
        poison = _tamper(backend._store[key])
        backend._store[key] = poison

        with caplog.at_level(logging.WARNING):
            result = await fn(21)

        assert result == {"result": 42}
        assert calls == 2  # corruption treated as miss → recomputed
        assert key in backend.deleted  # poisoned entry evicted (#159)
        assert backend._store.get(key) not in (None, poison)  # healed with fresh envelope
        assert any("cache_get_deserialize" in r.message for r in caplog.records)  # metric fired

    def test_sync_decorated_call_evicts_poisoned_l2_entry(self, caplog: pytest.LogCaptureFixture) -> None:
        backend = DictBackend()
        calls = 0

        @cache(backend=backend, ttl=300, l1_enabled=False)
        def fn(x: int) -> dict:
            nonlocal calls
            calls += 1
            return {"result": x * 2}

        assert fn(21)["result"] == 42  # populate L2
        (key,) = backend._store
        poison = _tamper(backend._store[key])
        backend._store[key] = poison

        with caplog.at_level(logging.WARNING):
            result = fn(21)

        assert result == {"result": 42}
        assert calls == 2
        assert key in backend.deleted
        assert backend._store.get(key) not in (None, poison)
        assert any("cache_get_deserialize" in r.message for r in caplog.records)
