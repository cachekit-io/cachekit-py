"""Error-path log redaction for backend operations (CWE-532, LAB-304).

Companion to ``tests/unit/test_orchestrator_error_handling.py``'s
``TestCacheKeyRedaction``: that file pins the decorator error sink; this file
pins the direct logger calls in ``cache_handler.py`` — backend set/delete
failures, invalidation failures, and TTL-refresh failures. Each test drives a
real failure and asserts the tenant-identifying key appears only as its
blake2b digest, never verbatim.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, Optional
from unittest.mock import MagicMock

import pytest

from cachekit import cache
from cachekit.backends.errors import BackendError, BackendErrorType
from cachekit.cache_handler import (
    CacheInvalidator,
    CacheOperationHandler,
    CacheSerializationHandler,
    StandardCacheHandler,
    _get_cached_serializer_class,
)
from cachekit.decorators.orchestrator import FeatureOrchestrator
from cachekit.hash_utils import _SENTINEL_KEYS, redact_cache_key
from cachekit.key_generator import CacheKeyGenerator
from cachekit.logging import UltraOptimizedStructuredLogger
from cachekit.serializers.base import SerializationError

TENANT_KEY = "ns:tenant-42-alice-secret:func:app.get_user:args:deadbeef:v1"

# Every sink is driven with four exception shapes. The first two prove the KEY field is
# redacted; the last two prove the EXCEPTION TEXT is too — a BackendError whose free-form
# ``message`` embeds the key (``_format_message`` preserves it verbatim) and a provider
# exception that echoes it (redis ResponseError style). A sink that interpolates ``{e}``
# raw passes the first two and fails the last two.
ERRORS = [
    BackendError("backend down", error_type=BackendErrorType.TRANSIENT),
    ValueError("unexpected"),
    BackendError(f"WRONGTYPE for {TENANT_KEY}", error_type=BackendErrorType.TRANSIENT, operation="get"),
    ValueError(f"illegal input: {TENANT_KEY}"),
]
ERROR_IDS = ["backend_error", "unexpected_error", "backenderror_key_in_message", "provider_key_in_text"]


class _FailingBackend:
    """Minimal BaseBackend whose mutating operations raise a configured error."""

    def __init__(self, error: Exception) -> None:
        self._error = error
        self.received_keys: list[str] = []

    def get(self, key: str) -> Optional[bytes]:
        self.received_keys.append(key)
        raise self._error

    def set(self, key: str, value: bytes, ttl: Optional[int] = None) -> None:
        self.received_keys.append(key)
        raise self._error

    def delete(self, key: str) -> bool:
        self.received_keys.append(key)
        raise self._error

    def exists(self, key: str) -> bool:
        return False

    def health_check(self) -> tuple[bool, dict[str, Any]]:
        return True, {"backend_type": "failing"}


class _RaisingCacheHandler:
    """CacheHandlerStrategy stand-in whose async reads raise (see _operation_handler)."""

    def __init__(self, error: Exception) -> None:
        self._error = error

    async def get_async(self, key: str, refresh_ttl: Optional[int] = None) -> Optional[bytes]:
        raise self._error

    async def get_with_freshness_async(self, key: str) -> Optional[tuple[bytes, bool, Optional[int]]]:
        raise self._error


class _DictBackend:
    """Transparent in-memory BaseBackend; ``delete_error`` makes delete raise."""

    key_prefix = ""  # interop compatibility contract (ensure_interop_backend_compatible)

    def __init__(self) -> None:
        self.store: dict[str, bytes] = {}
        self.delete_error: Optional[Exception] = None

    def get(self, key: str) -> Optional[bytes]:
        return self.store.get(key)

    def set(self, key: str, value: bytes, ttl: Optional[int] = None) -> None:
        self.store[key] = bytes(value)

    def delete(self, key: str) -> bool:
        if self.delete_error is not None:
            raise self.delete_error
        return self.store.pop(key, None) is not None

    def exists(self, key: str) -> bool:
        return key in self.store

    def health_check(self) -> tuple[bool, dict[str, Any]]:
        return True, {"backend_type": "dict"}


class _LockingDictBackend(_DictBackend):
    """Adds the LockableBackend protocol so the async wrapper takes the stampede-lock branch."""

    @asynccontextmanager
    async def acquire_lock(
        self, key: str, timeout: float = 10.0, blocking_timeout: Optional[float] = None
    ) -> AsyncIterator[bool]:
        yield True


class _FailingTTLBackend(_FailingBackend):
    """Adds TTL inspection so supports_ttl_inspection() passes; get_ttl raises."""

    async def get_ttl(self, key: str) -> Optional[int]:
        self.received_keys.append(key)
        raise self._error

    async def refresh_ttl(self, key: str, ttl: int) -> bool:
        raise self._error


def _assert_error_text_redacted(caplog: pytest.LogCaptureFixture, error: Exception) -> None:
    """The exception renders as its type name only; its free-form text (which may echo a key) never does."""
    messages = [r.getMessage() for r in caplog.records]
    assert str(error), "test bug: a blank message would match every record"
    assert any(type(error).__name__ in m for m in messages), f"expected {type(error).__name__} in logs; got {messages!r}"
    assert not any(str(error) in m for m in messages), f"exception text leaked into logs: {messages!r}"


def _assert_redacted(caplog: pytest.LogCaptureFixture, raw_key: str) -> None:
    """The digest must appear in some record; the raw key in none."""
    digest = redact_cache_key(raw_key)
    messages = [r.getMessage() for r in caplog.records]
    assert any(digest in m for m in messages), f"expected digest {digest!r} in logs; got {messages!r}"
    assert not any(raw_key in m for m in messages), f"raw key leaked into logs: {messages!r}"
    assert not any(TENANT_KEY in m for m in messages), f"key-bearing exception text leaked into logs: {messages!r}"


class TestStandardCacheHandlerRedaction:
    """set/delete/TTL-refresh failures log the digest, never the raw key."""

    @pytest.mark.parametrize("error", ERRORS, ids=ERROR_IDS)
    def test_set_failure_redacts_key(self, error: Exception, caplog: pytest.LogCaptureFixture) -> None:
        handler = StandardCacheHandler(backend=_FailingBackend(error))

        with caplog.at_level(logging.ERROR):
            assert handler.set(TENANT_KEY, b"value", ttl=60) is False

        _assert_redacted(caplog, TENANT_KEY)

    @pytest.mark.parametrize("error", ERRORS, ids=ERROR_IDS)
    def test_delete_failure_redacts_key(self, error: Exception, caplog: pytest.LogCaptureFixture) -> None:
        handler = StandardCacheHandler(backend=_FailingBackend(error))

        with caplog.at_level(logging.ERROR):
            assert handler.delete(TENANT_KEY) is False

        _assert_redacted(caplog, TENANT_KEY)

    @staticmethod
    def _operation_handler(error: Exception) -> CacheOperationHandler:
        # StandardCacheHandler swallows backend errors at its OWN sink and returns None, so a
        # failing backend never reaches the CacheOperationHandler sinks these tests pin. The
        # exception has to come from the cache handler itself.
        return CacheOperationHandler(MagicMock(), CacheKeyGenerator(), cache_handler=_RaisingCacheHandler(error))  # type: ignore[arg-type]

    @pytest.mark.parametrize("error", ERRORS, ids=ERROR_IDS)
    async def test_async_get_failure_redacts_key(self, error: Exception, caplog: pytest.LogCaptureFixture) -> None:
        """The async L2 read sink (CacheOperationHandler.get_cached_value_async)."""
        with caplog.at_level(logging.WARNING):
            assert await self._operation_handler(error).get_cached_value_async(TENANT_KEY) is None

        _assert_redacted(caplog, TENANT_KEY)

    @pytest.mark.parametrize("error", ERRORS, ids=ERROR_IDS)
    async def test_async_freshness_get_failure_redacts_key(self, error: Exception, caplog: pytest.LogCaptureFixture) -> None:
        """The SWR freshness read sink (CacheOperationHandler.get_cached_value_with_freshness_async)."""
        with caplog.at_level(logging.WARNING):
            assert await self._operation_handler(error).get_cached_value_with_freshness_async(TENANT_KEY) is None

        _assert_redacted(caplog, TENANT_KEY)

    @pytest.mark.parametrize("error", ERRORS, ids=ERROR_IDS)
    async def test_ttl_refresh_failure_redacts_key(self, error: Exception, caplog: pytest.LogCaptureFixture) -> None:
        """get_ttl raising must not fail the operation — and must log only the digest."""
        handler = StandardCacheHandler(backend=_FailingTTLBackend(error))

        with caplog.at_level(logging.DEBUG):
            await handler._maybe_refresh_ttl(TENANT_KEY, refresh_ttl=300)

        _assert_redacted(caplog, TENANT_KEY)


class TestCacheInvalidatorRedaction:
    """Invalidation failures (sync + async) log the digest of the generated key."""

    def _invalidator(self, error: Exception) -> tuple[CacheInvalidator, _FailingBackend]:
        backend = _FailingBackend(error)
        return CacheInvalidator(key_generator=CacheKeyGenerator(), backend=backend), backend

    @pytest.mark.parametrize("error", ERRORS, ids=ERROR_IDS)
    def test_sync_invalidation_failure_redacts_key(self, error: Exception, caplog: pytest.LogCaptureFixture) -> None:
        invalidator, backend = self._invalidator(error)

        def cached_func(user: str) -> str:
            return user

        with caplog.at_level(logging.ERROR):
            invalidator.invalidate_cache(cached_func, ("alice",), {}, namespace="tenant-42-secret")

        assert len(backend.received_keys) == 1
        _assert_redacted(caplog, backend.received_keys[0])

    @pytest.mark.parametrize("error", ERRORS, ids=ERROR_IDS)
    async def test_async_invalidation_failure_redacts_key(self, error: Exception, caplog: pytest.LogCaptureFixture) -> None:
        invalidator, backend = self._invalidator(error)

        def cached_func(user: str) -> str:
            return user

        with caplog.at_level(logging.ERROR):
            await invalidator.invalidate_cache_async(cached_func, ("alice",), {}, namespace="tenant-42-secret")

        assert len(backend.received_keys) == 1
        _assert_redacted(caplog, backend.received_keys[0])


class TestKeyCarryingBackendErrorRedaction:
    """A BackendError that carries the raw key must not leak it through ``{e}``.

    ``BackendError.__str__`` includes a ``key=`` segment; the get() sinks
    interpolate the exception verbatim, so the exception text itself must be
    redacted (CodeRabbit PR #264).
    """

    def _key_carrying_error(self) -> BackendError:
        return BackendError(
            "backend down",
            error_type=BackendErrorType.TRANSIENT,
            operation="get",
            key=TENANT_KEY,
        )

    def test_sync_get_failure_redacts_key_in_exception_text(self, caplog: pytest.LogCaptureFixture) -> None:
        handler = StandardCacheHandler(backend=_FailingBackend(self._key_carrying_error()))

        with caplog.at_level(logging.ERROR):
            assert handler.get(TENANT_KEY) is None

        _assert_redacted(caplog, TENANT_KEY)

    async def test_async_get_failure_redacts_key_in_exception_text(self, caplog: pytest.LogCaptureFixture) -> None:
        handler = StandardCacheHandler(backend=_FailingBackend(self._key_carrying_error()))

        with caplog.at_level(logging.ERROR):
            assert await handler.get_async(TENANT_KEY) is None

        _assert_redacted(caplog, TENANT_KEY)


class TestStructuredLoggerCacheOperationRedaction:
    """``UltraOptimizedStructuredLogger.cache_operation`` is a direct sink.

    ``cache_hit``/``cache_miss``/``cache_stored`` all funnel through it, so this
    one method is the whole surface. It must apply the *same* pass-through policy
    as the orchestrator sink: a value that arrives already redacted, or is a known
    sentinel, is emitted verbatim. Hashing it a second time would mint a different
    digest for the same key and break correlation between the two sinks
    (CodeRabbit PR #264).
    """

    def _emit(self, caplog: pytest.LogCaptureFixture, cache_key: str) -> str:
        logger = UltraOptimizedStructuredLogger("test.cache_operation")

        with caplog.at_level(logging.INFO, logger="test.cache_operation"):
            logger.cache_operation("get", cache_key, hit=True)

        records = [r for r in caplog.records if hasattr(r, "structured")]
        assert records, "cache_operation emitted no structured record"
        return records[-1].structured["cache_key"]

    def test_raw_key_is_redacted(self, caplog: pytest.LogCaptureFixture) -> None:
        assert self._emit(caplog, TENANT_KEY) == redact_cache_key(TENANT_KEY)

    def test_already_redacted_key_passes_through(self, caplog: pytest.LogCaptureFixture) -> None:
        """The digest must survive a second hop unchanged — this is the correlation contract."""
        pre_redacted = redact_cache_key(TENANT_KEY)

        assert self._emit(caplog, pre_redacted) == pre_redacted

    @pytest.mark.parametrize("sentinel", sorted(_SENTINEL_KEYS))
    def test_sentinels_stay_readable(self, sentinel: str, caplog: pytest.LogCaptureFixture) -> None:
        """Covers ``system`` too — health.py logs under that label, and hashing it
        turned a readable operator field into an opaque digest."""
        assert self._emit(caplog, sentinel) == sentinel

    def test_digest_matches_the_orchestrator_sink(self, caplog: pytest.LogCaptureFixture) -> None:
        """Both sinks must render one key as one digest, or logs cannot be joined.

        Drives the orchestrator sink for real rather than re-calling the shared
        helper — comparing the helper against itself would pass even if the two
        sinks diverged, which is the only thing this test exists to catch.
        """
        from_logging_sink = self._emit(caplog, TENANT_KEY)

        caplog.clear()
        orchestrator = FeatureOrchestrator(
            namespace="test",
            circuit_breaker_enabled=False,
            backpressure_enabled=False,
        )
        with caplog.at_level(logging.WARNING):
            orchestrator.handle_cache_error(
                error=ValueError("boom"),
                operation="get",
                cache_key=TENANT_KEY,
            )

        orchestrator_messages = " ".join(r.getMessage() for r in caplog.records)
        assert from_logging_sink in orchestrator_messages, (
            f"sinks disagree: logging emitted {from_logging_sink!r}, orchestrator logged {orchestrator_messages!r}"
        )
        assert TENANT_KEY not in orchestrator_messages

    def test_falsy_key_emits_empty_string(self, caplog: pytest.LogCaptureFixture) -> None:
        """No key means nothing to redact — must not become a digest of ``""``."""
        assert self._emit(caplog, "") == ""


class TestRedactErrorForLog:
    """Pin the two-branch contract of ``redact_error_for_log`` (CWE-532).

    It logs NO free-form exception text: a BackendError renders from allow-listed
    non-key fields (type + BackendErrorType classification), never its .message; every
    other exception collapses to its bare type name.
    """

    def test_backenderror_renders_type_and_classification(self) -> None:
        from cachekit.hash_utils import redact_error_for_log

        err = BackendError(
            message="Redis timeout during get: TimeoutError",
            error_type=BackendErrorType.TIMEOUT,
            operation="get",
            key=TENANT_KEY,
        )
        assert redact_error_for_log(err) == "BackendError(timeout)"

    def test_backenderror_with_key_bearing_message_does_not_leak(self) -> None:
        """Defense-in-depth: even a BackendError whose .message embeds the raw key
        (a construction-site mistake) must not leak it — the message is never read."""
        from cachekit.hash_utils import redact_error_for_log

        err = BackendError(
            message=f"provider failure for {TENANT_KEY}",  # poisoned message
            error_type=BackendErrorType.UNKNOWN,
            key=TENANT_KEY,
        )
        rendered = redact_error_for_log(err)
        assert TENANT_KEY not in rendered
        assert rendered == "BackendError(unknown)"

    def test_arbitrary_exception_reduced_to_type_name(self) -> None:
        from cachekit.hash_utils import redact_error_for_log

        # A raw provider exception whose text embeds the key must not leak it.
        rendered = redact_error_for_log(ValueError(f"bad key: {TENANT_KEY}"))
        assert rendered == "ValueError"
        assert TENANT_KEY not in rendered


class TestClassifierMessagesAreKeyFree:
    """Every backend classifier must build a key-free BackendError.message (CWE-532).

    This is the invariant ``redact_error_for_log`` relies on when it logs a BackendError
    verbatim: provider exception text (redis ACL/WRONGTYPE, httpx URL, pymemcache) can
    echo the raw key, so no classifier may interpolate ``str(exc)`` into the message —
    only ``type(exc).__name__``. Detail stays on ``original_exception``; the key rides
    the ``.key`` attribute, which ``_format_message`` redacts. Guards against the wrapped
    path the logger-call architecture test cannot see (a BackendError construction, not a
    logger call).
    """

    def test_redis_classifier_does_not_leak_key(self) -> None:
        redis_exc = pytest.importorskip("redis.exceptions")
        from cachekit.backends.redis.error_handler import classify_redis_error

        # redis-py ResponseError text echoes the offending key verbatim (ACL/WRONGTYPE);
        # ResponseError classifies PERMANENT — a real branch, not the UNKNOWN fallback.
        exc = redis_exc.ResponseError(f"WRONGTYPE Operation against key {TENANT_KEY}")
        err = classify_redis_error(exc, operation="get", key=TENANT_KEY)
        assert TENANT_KEY not in str(err)
        assert redact_cache_key(TENANT_KEY) in str(err)  # key present only as its digest

    def test_http_classifier_does_not_leak_key(self) -> None:
        import httpx

        from cachekit.backends.cachekitio.error_handler import classify_http_error

        # httpx exception text carries the request URL, which embeds the raw key in its path.
        exc = httpx.ConnectError(f"Connection refused to https://api.cachekit.io/v1/cache/{TENANT_KEY}")
        err = classify_http_error(exc, operation="get", key=TENANT_KEY)
        assert TENANT_KEY not in str(err)
        assert redact_cache_key(TENANT_KEY) in str(err)

    def test_memcached_classifier_does_not_leak_key(self) -> None:
        from cachekit.backends.memcached.error_handler import classify_memcached_error

        # TIMEOUT/TRANSIENT branches previously interpolated raw {exc}. socket.timeout is
        # an alias of TimeoutError (3.10+), which the TIMEOUT branch matches.
        exc = TimeoutError(f"timed out serving key {TENANT_KEY}")
        err = classify_memcached_error(exc, operation="get", key=TENANT_KEY)
        assert TENANT_KEY not in str(err)
        assert redact_cache_key(TENANT_KEY) in str(err)


class TestSerializationSinksRedaction:
    """cache_handler.py serialization sinks: exception text renders as a type name only."""

    @pytest.mark.parametrize(
        "import_path",
        ["cachekit.no_such_module.Nope", "cachekit.cache_handler.NoSuchClass"],
        ids=["import_error", "attribute_error"],
    )
    def test_serializer_import_failure(self, import_path: str, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.WARNING), pytest.raises((ImportError, AttributeError)) as exc_info:
            _get_cached_serializer_class("lab304-bogus", import_path)

        _assert_error_text_redacted(caplog, exc_info.value)

    def test_serialize_failure(self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
        handler = CacheSerializationHandler(serializer_name="default", encryption=False)
        error = RuntimeError(f"serializer exploded on {TENANT_KEY}")
        monkeypatch.setattr(handler._base_serializer, "serialize", MagicMock(side_effect=error))

        with caplog.at_level(logging.ERROR), pytest.raises(SerializationError):
            handler.serialize_data({"a": 1}, cache_key=TENANT_KEY)

        _assert_error_text_redacted(caplog, error)

    def test_interop_deserialize_failure_redacts_key(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        handler = CacheSerializationHandler(serializer_name="default", encryption=False, interop_mode=True)
        error = RuntimeError(f"decoder exploded on {TENANT_KEY}")
        monkeypatch.setattr(handler._base_serializer, "deserialize", MagicMock(side_effect=error))

        with caplog.at_level(logging.ERROR), pytest.raises(SerializationError):
            handler.deserialize_data(b"\x81\xa1a\x01", TENANT_KEY)

        _assert_redacted(caplog, TENANT_KEY)
        _assert_error_text_redacted(caplog, error)

    @pytest.mark.parametrize("error", ERRORS, ids=ERROR_IDS)
    async def test_async_streaming_failure_redacts_key(self, error: Exception, caplog: pytest.LogCaptureFixture) -> None:
        """set_streaming_async: the BackendError sink and the producer-failure sink."""
        backend = MagicMock()
        backend.set_streaming.side_effect = error
        handler = StandardCacheHandler(backend=backend)

        with caplog.at_level(logging.ERROR):
            assert await handler.set_streaming_async(TENANT_KEY, lambda sink: None) is False

        _assert_redacted(caplog, TENANT_KEY)


class TestDecoratorWrapperRedaction:
    """Direct logger calls in decorators/wrapper.py that bypass the orchestrator sink."""

    @staticmethod
    def _poison_deserialize(monkeypatch: pytest.MonkeyPatch, error: Exception) -> None:
        # Class-level patch: the wrapper reaches deserialize_data through the handler instance
        # it built at decoration time, so an instance patch has nothing to attach to.
        monkeypatch.setattr(CacheSerializationHandler, "deserialize_data", MagicMock(side_effect=error))

    def test_sync_l1_deserialization_failure_redacts_key(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        backend = _DictBackend()

        @cache(backend=backend, ttl=300, l1_enabled=True, namespace="lab304-l1-sync")
        def get_user(user_id: int) -> dict[str, int]:
            return {"id": user_id}

        assert get_user(1) == {"id": 1}  # populates L1 and L2
        (cache_key,) = backend.store
        error = RuntimeError(f"corrupt entry for {cache_key}")
        self._poison_deserialize(monkeypatch, error)

        with caplog.at_level(logging.WARNING, logger="cachekit"):
            assert get_user(1) == {"id": 1}  # L1 hit fails, L2 fails, function recomputes

        _assert_redacted(caplog, cache_key)
        _assert_error_text_redacted(caplog, error)

    async def test_async_l1_deserialization_failure_redacts_key(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        backend = _DictBackend()

        @cache(backend=backend, ttl=300, l1_enabled=True, namespace="lab304-l1-async")
        async def get_user(user_id: int) -> dict[str, int]:
            return {"id": user_id}

        assert await get_user(1) == {"id": 1}
        (cache_key,) = backend.store
        error = RuntimeError(f"corrupt entry for {cache_key}")
        self._poison_deserialize(monkeypatch, error)

        with caplog.at_level(logging.WARNING, logger="cachekit"):
            assert await get_user(1) == {"id": 1}

        _assert_redacted(caplog, cache_key)
        _assert_error_text_redacted(caplog, error)

    async def test_async_double_check_failure_redacts_key(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Post-lock double-check read raises: logged at debug, function still recomputes."""
        backend = _LockingDictBackend()
        error = RuntimeError(f"double-check exploded on {TENANT_KEY}")
        real_get = CacheOperationHandler.get_cached_value_async
        calls: list[str] = []

        async def second_call_raises(self: CacheOperationHandler, cache_key: str, *args: Any, **kwargs: Any) -> Any:
            calls.append(cache_key)
            if len(calls) == 2:  # 1st = pre-lock read (miss), 2nd = post-lock double-check
                raise error
            return await real_get(self, cache_key, *args, **kwargs)

        monkeypatch.setattr(CacheOperationHandler, "get_cached_value_async", second_call_raises)

        @cache(backend=backend, ttl=300, l1_enabled=False, namespace="lab304-dc")
        async def get_user(user_id: int) -> dict[str, int]:
            return {"id": user_id}

        with caplog.at_level(logging.DEBUG, logger="cachekit"):
            assert await get_user(1) == {"id": 1}

        assert len(calls) == 2
        _assert_redacted(caplog, calls[1])
        _assert_error_text_redacted(caplog, error)

    @staticmethod
    def _failing_provider(monkeypatch: pytest.MonkeyPatch, error: Exception) -> None:
        provider = MagicMock()
        provider.get_backend.side_effect = error
        monkeypatch.setattr("cachekit.decorators.wrapper.get_backend_provider", lambda: provider)

    def test_sync_invalidate_provider_failure(self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
        @cache(ttl=300, namespace="lab304-inv-sync")
        def get_user(user_id: int) -> dict[str, int]:
            return {"id": user_id}

        error = RuntimeError(f"provider exploded for {TENANT_KEY}")
        self._failing_provider(monkeypatch, error)

        with caplog.at_level(logging.DEBUG, logger="cachekit"):
            get_user.invalidate_cache(1)  # no L2 to clear; must not raise

        _assert_error_text_redacted(caplog, error)

    async def test_async_invalidate_provider_failure(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        @cache(ttl=300, namespace="lab304-inv-async")
        async def get_user(user_id: int) -> dict[str, int]:
            return {"id": user_id}

        error = RuntimeError(f"provider exploded for {TENANT_KEY}")
        self._failing_provider(monkeypatch, error)

        with caplog.at_level(logging.DEBUG, logger="cachekit"):
            await get_user.invalidate_cache(1)

        _assert_error_text_redacted(caplog, error)

    @pytest.mark.parametrize("error", ERRORS, ids=ERROR_IDS)
    def test_sync_interop_delete_failure_redacts_key(self, error: Exception, caplog: pytest.LogCaptureFixture) -> None:
        backend = _DictBackend()

        @cache(backend=backend, l1_enabled=False, interop="get_user", namespace="users")
        def get_user(user_id: int) -> dict[str, int]:
            return {"id": user_id}

        get_user(1)
        (interop_key,) = backend.store
        backend.delete_error = error

        with caplog.at_level(logging.ERROR):
            get_user.invalidate_cache(1)

        _assert_redacted(caplog, interop_key)

    @pytest.mark.parametrize("error", ERRORS, ids=ERROR_IDS)
    async def test_async_interop_delete_failure_redacts_key(self, error: Exception, caplog: pytest.LogCaptureFixture) -> None:
        backend = _DictBackend()

        @cache(backend=backend, l1_enabled=False, interop="get_user", namespace="users")
        async def get_user(user_id: int) -> dict[str, int]:
            return {"id": user_id}

        await get_user(1)
        (interop_key,) = backend.store
        backend.delete_error = error

        with caplog.at_level(logging.ERROR):
            await get_user.invalidate_cache(1)

        _assert_redacted(caplog, interop_key)
