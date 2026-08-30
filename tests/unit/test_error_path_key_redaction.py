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
from cachekit.hash_utils import redact_cache_key
from cachekit.key_generator import CacheKeyGenerator

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
