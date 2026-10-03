"""A decorated function runs at most once per call, whatever it raises.

The degradation handlers around a cache miss ("backend unavailable, run uncached",
"lock failed, run without the lock") must only ever see errors from cache, backend
or lock operations. A ``BackendError`` the function raised itself (from its own
backend client, or a nested cached call) used to land in them, so the function ran
a second time and the caller got the second run's exception. Side effects ran twice.

The lock path is covered on both lock shapes: a Redis-shaped ``acquire_lock`` that
re-raises lock-body errors through ``classify_redis_error``, and a CachekitIO-shaped
one that only releases in ``finally``.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import pytest

from cachekit import cache
from cachekit.backends.errors import BackendError, BackendErrorType
from cachekit.backends.redis.error_handler import classify_redis_error
from cachekit.interop import InteropError


class _MemoryBackend:
    """In-memory, non-lockable backend: every first call is a miss."""

    def __init__(self) -> None:
        self.store: dict[str, bytes] = {}

    def get(self, key: str) -> bytes | None:
        return self.store.get(key)

    def set(self, key: str, value: bytes, ttl: int | None = None, stale_ttl: int | None = None) -> None:
        self.store[key] = value

    def delete(self, key: str) -> bool:
        return self.store.pop(key, None) is not None

    def exists(self, key: str) -> bool:
        return key in self.store

    def health_check(self) -> tuple[bool, dict[str, Any]]:
        return True, {}


class _LockingBackend(_MemoryBackend):
    """Lockable. ``redis_shaped`` wraps lock-body errors as RedisBackend.acquire_lock does."""

    def __init__(self, *, redis_shaped: bool) -> None:
        super().__init__()
        self._redis_shaped = redis_shaped
        self.locks_released = 0

    @asynccontextmanager
    async def acquire_lock(self, key: str, timeout: float, blocking_timeout: float | None = None) -> AsyncIterator[bool]:
        try:
            yield True
        except Exception as exc:
            if not self._redis_shaped:
                raise
            raise classify_redis_error(exc, operation="acquire_lock", key=key) from exc
        finally:
            self.locks_released += 1


class _FailingReleaseLockBackend(_LockingBackend):
    """Its release raises, as an executor shut down mid-release would make RedisBackend's do.

    ``fill`` is written as the lock is taken, as another worker's entry, so the lock-acquired
    double-check hits.
    """

    def __init__(self, *, redis_shaped: bool, fill: dict[str, bytes] | None = None) -> None:
        super().__init__(redis_shaped=redis_shaped)
        self._fill = fill or {}

    @asynccontextmanager
    async def acquire_lock(self, key: str, timeout: float, blocking_timeout: float | None = None) -> AsyncIterator[bool]:
        self.store.update(self._fill)
        async with super().acquire_lock(key, timeout, blocking_timeout) as acquired:
            try:
                yield acquired
            finally:
                # Raised inside the parent lock's body, so a Redis-shaped parent wraps it once.
                raise RuntimeError("cannot schedule new futures after shutdown")


LOCK_SHAPES = [pytest.param(True, id="redis-lock"), pytest.param(False, id="finally-lock")]


def _function_error() -> BackendError:
    return BackendError("the function's own backend call failed", error_type=BackendErrorType.TRANSIENT)


@pytest.mark.unit
class TestFunctionBackendErrorRunsOnce:
    def test_sync_miss(self) -> None:
        raised: list[BackendError] = []

        @cache(backend=_MemoryBackend(), ttl=60, l1_enabled=False, namespace="lab5360-sync")
        def fn(x: int) -> int:
            raised.append(_function_error())
            raise raised[-1]

        with pytest.raises(BackendError) as excinfo:
            fn(1)

        assert len(raised) == 1
        assert excinfo.value is raised[0]

    @pytest.mark.asyncio
    async def test_async_lockless(self) -> None:
        raised: list[BackendError] = []

        @cache(backend=_MemoryBackend(), ttl=60, l1_enabled=False, namespace="lab5360-async-lockless")
        async def fn(x: int) -> int:
            raised.append(_function_error())
            raise raised[-1]

        with pytest.raises(BackendError) as excinfo:
            await fn(1)

        assert len(raised) == 1
        assert excinfo.value is raised[0]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("redis_shaped", LOCK_SHAPES)
    async def test_async_lock(self, redis_shaped: bool) -> None:
        backend = _LockingBackend(redis_shaped=redis_shaped)
        raised: list[BackendError] = []

        @cache(backend=backend, ttl=60, l1_enabled=False, namespace=f"lab5360-async-lock-{int(redis_shaped)}")
        async def fn(x: int) -> int:
            raised.append(_function_error())
            raise raised[-1]

        with pytest.raises(BackendError) as excinfo:
            await fn(1)

        assert len(raised) == 1
        assert excinfo.value is raised[0]
        assert backend.locks_released == 1
        assert backend.store == {}, "a failed call must not be cached"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("redis_shaped", LOCK_SHAPES)
    async def test_async_lock_release_failure_keeps_the_function_error(
        self, redis_shaped: bool, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A lock that fails while releasing neither replaces the function's exception nor its context.

        The release failure is logged instead: the lock may outlive the call until its timeout.
        """
        raised: list[BackendError] = []

        @cache(
            backend=_FailingReleaseLockBackend(redis_shaped=redis_shaped),
            ttl=60,
            l1_enabled=False,
            namespace=f"lab5360-async-lock-release-{int(redis_shaped)}",
        )
        async def fn(x: int) -> int:
            try:
                raise KeyError(x)
            except KeyError:
                raised.append(_function_error())
                raise raised[-1]  # noqa: B904 — the KeyError context is what the test pins

        with caplog.at_level(logging.WARNING, logger="cachekit"), pytest.raises(BackendError) as excinfo:
            await fn(1)

        assert len(raised) == 1
        assert excinfo.value is raised[0]
        assert isinstance(excinfo.value.__context__, KeyError), "the release error must not replace the context"
        assert any("Lock release failed" in r.getMessage() for r in caplog.records)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("redis_shaped", LOCK_SHAPES)
    async def test_async_lock_other_exception_is_unchanged(self, redis_shaped: bool) -> None:
        """A non-backend exception keeps reaching the caller as the same object."""
        raised: list[ValueError] = []

        @cache(
            backend=_LockingBackend(redis_shaped=redis_shaped),
            ttl=60,
            l1_enabled=False,
            namespace=f"lab5360-async-lock-value-{int(redis_shaped)}",
        )
        async def fn(x: int) -> int:
            raised.append(ValueError("bad input"))
            raise raised[-1]

        with pytest.raises(ValueError) as excinfo:
            await fn(1)

        assert len(raised) == 1
        assert excinfo.value is raised[0]


@pytest.mark.unit
@pytest.mark.asyncio
class TestLockReleaseFailureKeepsTheResult:
    """A release that fails after the lock body returned keeps that result: the function never reruns.

    The lock clause tells a release failure from an acquire failure by where it was raised, so
    it never takes one for the other and degrades to "run without the lock" a second time.
    """

    @staticmethod
    def _fn(backend: _LockingBackend, runs: list[int], namespace: str) -> Any:
        @cache(backend=backend, ttl=60, l1_enabled=False, namespace=namespace)
        async def fn(x: int) -> int:
            runs.append(x)
            return x * 2

        return fn

    @pytest.mark.parametrize("redis_shaped", LOCK_SHAPES)
    async def test_after_the_store(self, redis_shaped: bool, caplog: pytest.LogCaptureFixture) -> None:
        backend = _FailingReleaseLockBackend(redis_shaped=redis_shaped)
        runs: list[int] = []
        fn = self._fn(backend, runs, f"lab5346-release-store-{int(redis_shaped)}")

        with caplog.at_level(logging.WARNING, logger="cachekit"):
            assert await fn(1) == 2

        assert runs == [1]
        assert len(backend.store) == 1, "the result was cached before the release failed"
        assert any("Lock release failed" in r.getMessage() for r in caplog.records)

    @pytest.mark.parametrize("redis_shaped", LOCK_SHAPES)
    async def test_after_the_double_check_hit(self, redis_shaped: bool, caplog: pytest.LogCaptureFixture) -> None:
        namespace = f"lab5346-release-double-check-{int(redis_shaped)}"
        writer = _LockingBackend(redis_shaped=redis_shaped)
        assert await self._fn(writer, [], namespace)(1) == 2  # another worker's entry for fn(1)
        backend = _FailingReleaseLockBackend(redis_shaped=redis_shaped, fill=dict(writer.store))
        runs: list[int] = []

        with caplog.at_level(logging.WARNING, logger="cachekit"):
            assert await self._fn(backend, runs, namespace)(1) == 2

        assert runs == [], "the double-check hit is the result; nothing runs"
        assert any("Lock release failed" in r.getMessage() for r in caplog.records)


@pytest.mark.unit
@pytest.mark.asyncio
class TestLockBodyCacheErrorsStillReachTheCaller:
    """Cache errors raised inside the lock body are not the function's, and still propagate."""

    @pytest.mark.parametrize("redis_shaped", LOCK_SHAPES)
    async def test_interop_rejection_from_the_store(self, redis_shaped: bool) -> None:
        runs: list[int] = []

        @cache(
            backend=_LockingBackend(redis_shaped=redis_shaped),
            ttl=60,
            l1_enabled=False,
            namespace=f"lab5360-interop-{int(redis_shaped)}",
            interop="op",
        )
        async def fn(x: int) -> Any:
            runs.append(x)
            return {x}  # a set is outside the interop data model

        with pytest.raises(InteropError):
            await fn(1)

        assert runs == [1]
