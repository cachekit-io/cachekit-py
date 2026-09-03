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
from typing import Any, Optional

import pytest

from cachekit.backends.errors import BackendError, BackendErrorType
from cachekit.cache_handler import CacheInvalidator, StandardCacheHandler
from cachekit.decorators.orchestrator import FeatureOrchestrator
from cachekit.hash_utils import _SENTINEL_KEYS, redact_cache_key
from cachekit.key_generator import CacheKeyGenerator
from cachekit.logging import UltraOptimizedStructuredLogger

TENANT_KEY = "ns:tenant-42-alice-secret:func:app.get_user:args:deadbeef:v1"


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


class _FailingTTLBackend(_FailingBackend):
    """Adds TTL inspection so supports_ttl_inspection() passes; get_ttl raises."""

    async def get_ttl(self, key: str) -> Optional[int]:
        self.received_keys.append(key)
        raise self._error

    async def refresh_ttl(self, key: str, ttl: int) -> bool:
        raise self._error


def _assert_redacted(caplog: pytest.LogCaptureFixture, raw_key: str) -> None:
    """The digest must appear in some record; the raw key in none."""
    digest = redact_cache_key(raw_key)
    messages = [r.getMessage() for r in caplog.records]
    assert any(digest in m for m in messages), f"expected digest {digest!r} in logs; got {messages!r}"
    assert not any(raw_key in m for m in messages), f"raw key leaked into logs: {messages!r}"


class TestStandardCacheHandlerRedaction:
    """set/delete/TTL-refresh failures log the digest, never the raw key."""

    @pytest.mark.parametrize(
        "error",
        [BackendError("backend down", error_type=BackendErrorType.TRANSIENT), ValueError("unexpected")],
        ids=["backend_error", "unexpected_error"],
    )
    def test_set_failure_redacts_key(self, error: Exception, caplog: pytest.LogCaptureFixture) -> None:
        handler = StandardCacheHandler(backend=_FailingBackend(error))

        with caplog.at_level(logging.ERROR):
            assert handler.set(TENANT_KEY, b"value", ttl=60) is False

        _assert_redacted(caplog, TENANT_KEY)

    @pytest.mark.parametrize(
        "error",
        [BackendError("backend down", error_type=BackendErrorType.TRANSIENT), ValueError("unexpected")],
        ids=["backend_error", "unexpected_error"],
    )
    def test_delete_failure_redacts_key(self, error: Exception, caplog: pytest.LogCaptureFixture) -> None:
        handler = StandardCacheHandler(backend=_FailingBackend(error))

        with caplog.at_level(logging.ERROR):
            assert handler.delete(TENANT_KEY) is False

        _assert_redacted(caplog, TENANT_KEY)

    async def test_ttl_refresh_failure_redacts_key(self, caplog: pytest.LogCaptureFixture) -> None:
        """get_ttl raising must not fail the operation — and must log only the digest."""
        handler = StandardCacheHandler(backend=_FailingTTLBackend(ValueError("ttl probe failed")))

        with caplog.at_level(logging.DEBUG):
            await handler._maybe_refresh_ttl(TENANT_KEY, refresh_ttl=300)

        _assert_redacted(caplog, TENANT_KEY)


class TestCacheInvalidatorRedaction:
    """Invalidation failures (sync + async) log the digest of the generated key."""

    def _invalidator(self, error: Exception) -> tuple[CacheInvalidator, _FailingBackend]:
        backend = _FailingBackend(error)
        return CacheInvalidator(key_generator=CacheKeyGenerator(), backend=backend), backend

    @pytest.mark.parametrize(
        "error",
        [BackendError("backend down", error_type=BackendErrorType.TRANSIENT), ValueError("unexpected")],
        ids=["backend_error", "unexpected_error"],
    )
    def test_sync_invalidation_failure_redacts_key(self, error: Exception, caplog: pytest.LogCaptureFixture) -> None:
        invalidator, backend = self._invalidator(error)

        def cached_func(user: str) -> str:
            return user

        with caplog.at_level(logging.ERROR):
            invalidator.invalidate_cache(cached_func, ("alice",), {}, namespace="tenant-42-secret")

        assert len(backend.received_keys) == 1
        _assert_redacted(caplog, backend.received_keys[0])

    @pytest.mark.parametrize(
        "error",
        [BackendError("backend down", error_type=BackendErrorType.TRANSIENT), ValueError("unexpected")],
        ids=["backend_error", "unexpected_error"],
    )
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

    BackendError formats itself key-free (type name + redacted key digest), so it is
    logged verbatim; any other exception's ``str()`` has unknown provenance and may
    echo the raw key, so it collapses to the bare type name.
    """

    def test_backenderror_passes_through_key_free(self) -> None:
        from cachekit.hash_utils import redact_error_for_log

        err = BackendError(
            message="Redis timeout during get: TimeoutError",
            error_type=BackendErrorType.TIMEOUT,
            operation="get",
            key=TENANT_KEY,
        )
        rendered = redact_error_for_log(err)
        assert rendered == str(err)
        assert TENANT_KEY not in rendered
        assert redact_cache_key(TENANT_KEY) in rendered  # key present only as its digest

    def test_arbitrary_exception_reduced_to_type_name(self) -> None:
        from cachekit.hash_utils import redact_error_for_log

        # A raw provider exception whose text embeds the key must not leak it.
        rendered = redact_error_for_log(ValueError(f"bad key: {TENANT_KEY}"))
        assert rendered == "ValueError"
        assert TENANT_KEY not in rendered
