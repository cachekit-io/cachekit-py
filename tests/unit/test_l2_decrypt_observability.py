"""Tests for L2 decrypt/integrity failure observability.

When L2 cached data cannot be deserialized (decrypt failure, integrity check
failure, corrupt data), the decorator must:
  1. Log a warning identifying the failure as decrypt/integrity related.
  2. Treat it as a miss and recompute (fail-open — existing behavior).
  3. Evict the poisoned entry and emit the cache_get_deserialize metric on
     both sync and async decorator paths (#159).
  4. Leave the circuit breaker untouched: the failure is a miss, not a backend failure.
"""

from __future__ import annotations

import inspect
import json
import logging
from collections.abc import Iterator
from typing import Any
from unittest import mock

import pytest

from cachekit import cache
from cachekit.cache_handler import CacheHit, CacheOperationHandler, CacheSerializationHandler
from cachekit.decorators.orchestrator import FeatureOrchestrator
from cachekit.key_generator import CacheKeyGenerator
from cachekit.l1_cache import get_l1_cache
from cachekit.reliability import AsyncMetricsCollector
from cachekit.serializers.base import SerializationError
from cachekit.serializers.encryption_wrapper import DecryptionAuthenticationError, EncryptionError
from cachekit.serializers.wrapper import _PREFIX_LEN, SerializationWrapper


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
        # The exception is rendered by redact_error_for_log (CWE-532, LAB-304): the log
        # names the type, never the provider's free-form message text.
        assert any("EncryptionError" in r.message for r in caplog.records)
        assert not any("GCM tag mismatch" in r.message for r in caplog.records)

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
        """get_cached_value_async returns a CacheHit so the async decorator can
        backfill L1 with the serialized envelope without re-serializing."""
        sentinel = object()
        mock_serialization = mock.MagicMock(spec=CacheSerializationHandler)
        mock_serialization.deserialize_data.return_value = sentinel
        handler = CacheOperationHandler(mock_serialization, CacheKeyGenerator())
        mock_ch = mock.MagicMock()
        mock_ch.get_async = mock.AsyncMock(return_value=b"serialized-envelope")
        handler.set_cache_handler(mock_ch)

        result = await handler.get_cached_value_async("hit:key")

        assert result == CacheHit(sentinel, b"serialized-envelope", len(b"serialized-envelope"))


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


@pytest.mark.unit
class TestCorruptFrameHeaderEvicts:
    """A corrupt CK frame *header* must evict like a corrupt payload (LAB-4075).

    Drives a REAL CacheSerializationHandler (no deserialize mock) so the exception class
    under test is the one the code actually raises, not one a mock was told to raise.
    """

    KEY = "hdr:key"

    @pytest.fixture
    def plain_handler(self, monkeypatch: pytest.MonkeyPatch) -> Iterator[CacheSerializationHandler]:
        monkeypatch.delenv("CACHEKIT_MASTER_KEY", raising=False)
        yield CacheSerializationHandler(serializer_name="default")

    @pytest.fixture
    def enc_handler(self, monkeypatch: pytest.MonkeyPatch) -> Iterator[CacheSerializationHandler]:
        monkeypatch.setenv("CACHEKIT_MASTER_KEY", "a" * 64)  # test-only placeholder, not a secret
        yield CacheSerializationHandler(
            serializer_name="default",
            encryption=True,
            single_tenant_mode=True,
            deployment_uuid="00000000-0000-0000-0000-000000000001",
        )

    @staticmethod
    def _corrupt_header(blob: bytes, variant: str) -> bytes:
        """Mutate only the header region of a valid CK v3 frame; the payload is untouched."""
        hdr_len = int.from_bytes(blob[3:_PREFIX_LEN], "big")
        payload = blob[_PREFIX_LEN + hdr_len :]
        if variant == "truncated":
            return blob[: _PREFIX_LEN - 2]
        if variant == "bad_version":
            return blob[:2] + b"\x09" + blob[3:]
        if variant == "bad_header_len":
            return blob[:3] + (0xFFFFFFFF).to_bytes(4, "big") + blob[_PREFIX_LEN:]
        if variant == "undecodable_header":
            return blob[:_PREFIX_LEN] + b"\xff" * hdr_len + payload
        if variant == "non_json_header":
            return blob[:_PREFIX_LEN] + b"{" * hdr_len + payload
        if variant == "unknown_format_enum":
            doc = json.loads(blob[_PREFIX_LEN : _PREFIX_LEN + hdr_len])
            doc["m"]["format"] = "not-a-format"
            new_header = json.dumps(doc).encode()
            return blob[:3] + len(new_header).to_bytes(4, "big") + new_header + payload
        raise AssertionError(variant)

    VARIANTS = ["truncated", "bad_version", "bad_header_len", "undecodable_header", "non_json_header", "unknown_format_enum"]

    @pytest.mark.parametrize("variant", VARIANTS)
    def test_header_corruption_raises_serialization_error(self, plain_handler: CacheSerializationHandler, variant: str) -> None:
        blob = plain_handler.serialize_data({"k": "v"}, cache_key=self.KEY)
        assert plain_handler.deserialize_data(blob, cache_key=self.KEY) == {"k": "v"}  # baseline is valid

        with pytest.raises(SerializationError):  # not a bare ValueError subclass
            plain_handler.deserialize_data(self._corrupt_header(blob, variant), cache_key=self.KEY)

    def test_header_corruption_evicts_and_notifies(self, plain_handler: CacheSerializationHandler) -> None:
        """Sync get_cached_value: corrupt header -> miss, backend delete, on_deserialize_error hook."""
        blob = plain_handler.serialize_data({"k": "v"}, cache_key=self.KEY)
        handler = CacheOperationHandler(plain_handler, CacheKeyGenerator())
        mock_ch = mock.MagicMock()
        mock_ch.get.return_value = self._corrupt_header(blob, "truncated")
        handler.set_cache_handler(mock_ch)
        calls: list[tuple[Exception, str]] = []
        handler.on_deserialize_error = lambda error, key: calls.append((error, key))

        assert handler.get_cached_value(self.KEY) is None
        mock_ch.delete.assert_called_once_with(self.KEY)
        assert len(calls) == 1 and isinstance(calls[0][0], SerializationError)

    def test_parser_fault_outside_the_narrowed_catch_still_evicts(self, plain_handler: CacheSerializationHandler) -> None:
        """The inner catch lists only (AttributeError, KeyError, TypeError, ValueError).

        Anything else a parser raises must still reach SerializationError via the outer arm,
        or narrowing that tuple would silently restore the no-eviction bug this ticket fixes.
        Raised through a patched parser rather than crafted bytes on purpose: no header the
        CPython JSON scanner accepts produces a non-ValueError on 3.12-3.14, so bytes cannot
        pin this invariant without depending on interpreter internals.
        """
        blob = plain_handler.serialize_data({"k": "v"}, cache_key=self.KEY)
        handler = CacheOperationHandler(plain_handler, CacheKeyGenerator())
        mock_ch = mock.MagicMock()
        mock_ch.get.return_value = blob
        handler.set_cache_handler(mock_ch)

        with mock.patch.object(SerializationWrapper, "unwrap", side_effect=RecursionError("parser blew the stack")):
            with pytest.raises(SerializationError):
                plain_handler.deserialize_data(blob, cache_key=self.KEY)
            assert handler.get_cached_value(self.KEY) is None

        mock_ch.delete.assert_called_once_with(self.KEY)

    def test_missing_cache_key_on_encrypted_entry_still_fails_closed_without_evicting(
        self, enc_handler: CacheSerializationHandler
    ) -> None:
        """Guard: the wrap must not swallow the deliberate ValueError for a missing cache_key."""
        blob = enc_handler.serialize_data({"k": "v"}, cache_key=self.KEY)

        with pytest.raises(ValueError, match="cache_key is required"):
            enc_handler.deserialize_data(blob, cache_key="")

        handler = CacheOperationHandler(enc_handler, CacheKeyGenerator())
        mock_ch = mock.MagicMock()
        mock_ch.get.return_value = blob
        handler.set_cache_handler(mock_ch)

        assert handler.get_cached_value("") is None
        mock_ch.delete.assert_not_called()  # a caller bug, not a poisoned entry


_SECURE_KEY = "a" * 64  # test-only placeholder, not a secret
_BREAKER_THRESHOLD = 5  # the @cache.secure breaker's default failure_threshold


def _refused_plaintext(keys: list[str], envelopes: list[bytes]) -> list[bytes]:
    """Pre-encryption entries: plaintext envelopes an encrypting reader refuses."""
    writer = CacheSerializationHandler(serializer_name="default")
    return [writer.serialize_data({"result": -1}, cache_key=key) for key in keys]


def _substituted(keys: list[str], envelopes: list[bytes]) -> list[bytes]:
    """Each key serves another key's ciphertext, so the AAD check fails."""
    return envelopes[1:] + envelopes[:1]


def _corrupt(keys: list[str], envelopes: list[bytes]) -> list[bytes]:
    """An unknown CK frame version: corruption, not tamper evidence."""
    return [blob[:2] + b"\x09" + blob[3:] for blob in envelopes]


def _secure_fn(backend: DictBackend, namespace: str, calls: list[int], *, is_async: bool, **kwargs: Any) -> Any:
    decorator = cache.secure(master_key=_SECURE_KEY, backend=backend, namespace=namespace, ttl=300, **kwargs)
    if is_async:

        @decorator
        async def afn(x: int) -> dict:
            calls.append(x)
            return {"result": x}

        return afn

    @decorator
    def fn(x: int) -> dict:
        calls.append(x)
        return {"result": x}

    return fn


async def _call(fn: Any, x: int) -> Any:
    result = fn(x)
    return await result if inspect.isawaitable(result) else result


async def _populate_then_poison(fn: Any, backend: DictBackend, namespace: str, poison: Any) -> list[str]:
    """Cache fn(0..N-1) in L2, replace every entry with ``poison``, and drop L1."""
    for x in range(_BREAKER_THRESHOLD):
        assert await _call(fn, x) == {"result": x}
    keys = sorted(backend._store)
    assert len(keys) == _BREAKER_THRESHOLD
    for key, blob in zip(keys, poison(keys, [backend._store[k] for k in keys]), strict=True):
        backend._store[key] = blob
    get_l1_cache(namespace).clear()  # force the next reads through L2
    return keys


@pytest.mark.unit
@pytest.mark.parametrize("is_async", [False, True], ids=["sync", "async"])
class TestL2ReadFailureLeavesBreakerAlone:
    """A decrypt/integrity failure on an L2 read is a miss, never a backend failure.

    It must not count toward the circuit breaker. Otherwise enabling encryption over
    N >= failure_threshold live plaintext entries (lazy migration), or N entries planted
    by anyone who can write to the backend, opens the breaker and silently disables
    caching for the function.
    """

    @pytest.mark.parametrize(
        ("poison", "reason"),
        [(_refused_plaintext, "suspicious_envelope"), (_substituted, "auth_tamper"), (_corrupt, "corruption")],
        ids=["refused-plaintext", "auth-tamper", "corruption"],
    )
    async def test_fail_open_read_failure_is_miss_and_breaker_stays_closed(
        self,
        is_async: bool,
        poison: Any,
        reason: str,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        monkeypatch.delenv("CACHEKIT_MASTER_KEY", raising=False)
        backend = DictBackend()
        calls: list[int] = []
        namespace = f"breaker-{reason}-{is_async}"
        fn = _secure_fn(backend, namespace, calls, is_async=is_async)
        keys = await _populate_then_poison(fn, backend, namespace, poison)

        metrics: list[dict[str, Any]] = []
        record_metric = AsyncMetricsCollector.record_cache_operation
        monkeypatch.setattr(
            AsyncMetricsCollector,
            "record_cache_operation",
            lambda self, **kw: (metrics.append(kw), record_metric(self, **kw))[1],
        )
        logged: list[str] = []
        log_operation = FeatureOrchestrator.log_cache_operation
        monkeypatch.setattr(
            FeatureOrchestrator,
            "log_cache_operation",
            lambda self, **kw: (logged.append(kw.get("operation", "")), log_operation(self, **kw))[1],
        )

        calls.clear()
        with caplog.at_level(logging.WARNING):
            for x in range(_BREAKER_THRESHOLD):
                assert await _call(fn, x) == {"result": x}

        # Each read was a miss plus a recompute, of the failure class under test.
        assert calls == list(range(_BREAKER_THRESHOLD))
        assert sum(f"({reason})" in r.message for r in caplog.records) == _BREAKER_THRESHOLD
        assert set(backend.deleted) == set(keys)
        # The breaker never saw them.
        breaker = fn.get_health_status()["circuit_breaker"]
        assert (breaker["state"], breaker["failure_count"]) == ("closed", 0)
        # Observability is kept: one failure record and one structured log per read.
        deserialize_records = [m for m in metrics if m["operation"] == "cache_get_deserialize"]
        assert len(deserialize_records) == _BREAKER_THRESHOLD
        assert not any(m["success"] for m in deserialize_records)
        assert logged.count("cache_get_deserialize_failed") == _BREAKER_THRESHOLD

    async def test_fail_closed_tamper_raises_and_breaker_stays_closed(
        self, is_async: bool, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("CACHEKIT_MASTER_KEY", raising=False)
        backend = DictBackend()
        calls: list[int] = []
        namespace = f"breaker-fail-closed-{is_async}"
        fn = _secure_fn(backend, namespace, calls, is_async=is_async, fail_closed=True)
        await _populate_then_poison(fn, backend, namespace, _substituted)

        for x in range(_BREAKER_THRESHOLD):
            with pytest.raises(DecryptionAuthenticationError):
                await _call(fn, x)

        breaker = fn.get_health_status()["circuit_breaker"]
        assert (breaker["state"], breaker["failure_count"]) == ("closed", 0)
