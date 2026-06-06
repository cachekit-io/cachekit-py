"""Wrapper-boundary regression test for the bare-cache-key lock contract.

Companion to ``tests/unit/backends/test_cachekitio_lock_url.py``: that file
pins the URL shape downstream of ``CachekitIOBackend.acquire_lock``; this file
pins the upstream contract — what the wrapper itself passes to
``LockableBackend.acquire_lock``.

The production bug at ``decorators/wrapper.py:1064`` was:

    lock_key = f"{cache_key}:lock"
    async with _backend.acquire_lock(lock_key, ...):

Two boundaries are sensitive to the cache-key shape, and each needs its own
test so a future regression at either layer is caught independently:

- **Wrapper → backend** (this file): the wrapper must hand the backend the
  bare cache key, not a ``:lock``-suffixed variant.
- **Backend → SaaS HTTP** (the sibling file): given the bare cache key, the
  ``CachekitIOBackend`` must compose a 7-segment URL path.

A Redis-side sanity test also lives here to confirm that fixing the wrapper
does not regress the on-wire Redis lock name — the Redis backend now owns the
``:lock`` suffix internally.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from typing import Any, Optional
from unittest.mock import MagicMock

import pytest

from cachekit import cache


class _RecordingLockableBackend:
    """Minimal BaseBackend + LockableBackend that records what acquire_lock receives.

    Forces a cache miss on the first ``get`` so the wrapper takes the
    distributed-lock branch — which is the only path that calls ``acquire_lock``.
    """

    def __init__(self) -> None:
        """Initialise empty in-memory store and an empty list of received lock keys."""
        self._store: dict[str, bytes] = {}
        self.lock_keys: list[str] = []  # everything the wrapper passed to acquire_lock

    def get(self, key: str) -> bytes | None:
        """Return stored bytes for ``key`` or ``None`` for a cache miss."""
        return self._store.get(key)

    def set(self, key: str, value: bytes, ttl: int | None = None) -> None:
        """Store ``value`` at ``key``; ``ttl`` is accepted but ignored (test backend)."""
        self._store[key] = value

    def delete(self, key: str) -> bool:
        """Remove ``key`` from the store; return True iff the key was present."""
        return self._store.pop(key, None) is not None

    def exists(self, key: str) -> bool:
        """Return True iff ``key`` is present in the store."""
        return key in self._store

    def health_check(self) -> tuple[bool, dict[str, Any]]:
        """Return ``(True, info)`` — test backend is always healthy."""
        return True, {"backend_type": "recording_lockable"}

    @asynccontextmanager
    async def acquire_lock(
        self,
        key: str,
        timeout: float = 10.0,
        blocking_timeout: Optional[float] = None,
    ) -> AsyncIterator[bool]:
        """Record ``key`` and yield ``True`` — the happy-path lock acquisition."""
        self.lock_keys.append(key)
        yield True


@pytest.fixture(autouse=True)
def setup_di_for_redis_isolation() -> Iterator[None]:
    """Override the root conftest's Redis isolation — these tests are pure unit."""
    yield


@pytest.mark.unit
@pytest.mark.asyncio
class TestWrapperPassesBareCacheKeyToAcquireLock:
    """The wrapper must hand the backend the bare cache key — no ``:lock`` suffix."""

    async def test_async_wrapper_passes_bare_key(self) -> None:
        """On a cache miss, the wrapper's stampede-prevention lock acquisition
        must use the bare cache key, identical to what ``get``/``set`` see."""
        backend = _RecordingLockableBackend()

        @cache(backend=backend, ttl=300, l1_enabled=False)
        async def my_func(x: int) -> dict[str, int]:
            """Trivial cached coroutine used to drive the wrapper through one cache miss."""
            return {"result": x * 2}

        result = await my_func(42)
        assert result == {"result": 84}

        assert len(backend.lock_keys) == 1, (
            f"wrapper must call acquire_lock exactly once on a cache miss; got {backend.lock_keys!r}"
        )
        lock_arg = backend.lock_keys[0]

        # Primary regression: no ``:lock`` suffix smuggled in via the wrapper.
        assert not lock_arg.endswith(":lock"), (
            f"wrapper appended ':lock' to cache_key before calling acquire_lock — "
            f"violates LockableBackend protocol contract. Got {lock_arg!r}"
        )

        # Shape sanity: the key the wrapper passes to acquire_lock must be the
        # SAME key it stored under ``set`` (which is what the cache-hit double-check
        # path uses). If those drift, lock-vs-cache key skew silently breaks
        # stampede protection.
        stored_keys = list(backend._store.keys())
        assert stored_keys == [lock_arg], (
            f"wrapper stored under {stored_keys!r} but locked under {lock_arg!r} — "
            f"lock/cache key skew would silently defeat stampede protection"
        )


class _LockDeniedBackend(_RecordingLockableBackend):
    """Variant of the recording backend whose acquire_lock yields False, simulating
    a held lock that didn't release within ``blocking_timeout``. Drives the
    wrapper into the lock-timeout fallback branch (``wrapper.py:1105-1108``)."""

    @asynccontextmanager
    async def acquire_lock(
        self,
        key: str,
        timeout: float = 10.0,
        blocking_timeout: Optional[float] = None,
    ) -> AsyncIterator[bool]:
        """Record ``key`` and yield ``False`` — simulates a held lock not released in time."""
        self.lock_keys.append(key)
        yield False


class _LockBackendErrorBackend(_RecordingLockableBackend):
    """Variant whose acquire_lock raises ``BackendError`` before yielding. Drives
    the wrapper into the ``except Exception`` lock-operation-failed branch
    (``wrapper.py:1195-1212``) where the warning at line 1211 fires and the
    function falls through to execute without a lock."""

    @asynccontextmanager
    async def acquire_lock(
        self,
        key: str,
        timeout: float = 10.0,
        blocking_timeout: Optional[float] = None,
    ) -> AsyncIterator[bool]:
        """Record ``key`` and raise ``BackendError`` — simulates a lock-op failure (HTTP 5xx, dropped conn)."""
        from cachekit.backends.errors import BackendError, BackendErrorType

        self.lock_keys.append(key)
        # No original_exception → wrapper takes the line-1211 branch (not the
        # function-exception re-raise at line 1208).
        raise BackendError(
            "simulated lock-acquisition failure",
            error_type=BackendErrorType.TRANSIENT,
            operation="acquire_lock",
            key=key,
        )
        yield True  # unreachable, satisfies asynccontextmanager typing


@pytest.mark.unit
@pytest.mark.asyncio
class TestWrapperLockTimeoutFallback:
    """When ``acquire_lock`` yields False (contended lock didn't release in
    time), the wrapper must log a warning naming the bare ``cache_key`` —
    not a ``:lock``-suffixed variant — and double-check the cache before
    falling through to execute the function."""

    async def test_async_wrapper_logs_warning_with_bare_key_on_lock_timeout(self, caplog: pytest.LogCaptureFixture) -> None:
        """Drive the lock-contended branch and assert the warning names the bare cache_key."""
        import logging

        backend = _LockDeniedBackend()

        @cache(backend=backend, ttl=300, l1_enabled=False)
        async def my_func(x: int) -> dict[str, int]:
            """Trivial cached coroutine — exercises the lock-timeout fallback path."""
            return {"result": x * 2}

        with caplog.at_level(logging.WARNING, logger="cachekit"):
            result = await my_func(42)
        assert result == {"result": 84}

        assert len(backend.lock_keys) == 1
        bare_key = backend.lock_keys[0]

        # The warning must reference the bare cache_key (no ``:lock`` smuggled in)
        # so operators reading logs see the same key shape that ``get``/``set`` use.
        timeout_warnings = [r for r in caplog.records if "Failed to acquire lock" in r.message]
        assert len(timeout_warnings) == 1, (
            f"expected exactly one lock-timeout warning; got {[r.message for r in caplog.records]!r}"
        )
        msg = timeout_warnings[0].message
        assert bare_key in msg, f"warning must name the bare cache_key {bare_key!r}; got {msg!r}"
        assert ":lock" not in msg, f"warning leaked ':lock' suffix: {msg!r}"


@pytest.mark.unit
@pytest.mark.asyncio
class TestWrapperLockOperationFailureFallback:
    """When ``acquire_lock`` raises ``BackendError`` (e.g. SaaS HTTP 5xx, Redis
    connection dropped), the wrapper must catch, log a warning naming the bare
    ``cache_key``, and fall through to execute the function without a lock."""

    async def test_async_wrapper_logs_warning_and_falls_through_on_lock_error(self, caplog: pytest.LogCaptureFixture) -> None:
        """Raise ``BackendError`` from acquire_lock; assert warning fires and function still runs."""
        import logging

        backend = _LockBackendErrorBackend()
        call_count = 0

        @cache(backend=backend, ttl=300, l1_enabled=False)
        async def my_func(x: int) -> dict[str, int]:
            """Trivial cached coroutine — exercises the lock-operation-failed fallback path."""
            nonlocal call_count
            call_count += 1
            return {"result": x * 3}

        with caplog.at_level(logging.WARNING, logger="cachekit"):
            result = await my_func(7)

        # Function executed despite lock failure (fell through to no-lock path)
        assert result == {"result": 21}
        assert call_count == 1, "function must execute when lock acquisition raises"

        assert len(backend.lock_keys) == 1
        bare_key = backend.lock_keys[0]

        # The lock-operation-failed warning must reference the bare cache_key —
        # not a ``:lock``-suffixed variant — matching the protocol contract.
        lock_failed_warnings = [r for r in caplog.records if "Lock operation failed" in r.message]
        assert len(lock_failed_warnings) == 1, (
            f"expected one lock-operation-failed warning; got {[r.message for r in caplog.records]!r}"
        )
        msg = lock_failed_warnings[0].message
        assert bare_key in msg, f"warning must name the bare cache_key {bare_key!r}; got {msg!r}"
        assert ":lock" not in msg, f"warning leaked ':lock' suffix: {msg!r}"


@pytest.mark.unit
@pytest.mark.asyncio
class TestRedisBackendOwnsLockSuffixOnWire:
    """Wire compatibility: removing the wrapper's ``:lock`` suffix must NOT change
    the on-wire Redis lock name. The Redis backend owns the suffix internally."""

    async def test_redis_lock_name_keeps_lock_suffix_on_wire(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Given the wrapper passes a bare cache_key, the Redis backend must
        still construct the underlying ``redis.lock.Lock`` with ``<key>:lock``
        — preserving zero-migration compatibility for existing Redis deployments."""
        import redis.lock

        from cachekit.backends.redis.provider import PerRequestRedisBackend

        # Mock redis client — we only need to capture Lock(name=...) construction.
        mock_redis = MagicMock()
        backend = PerRequestRedisBackend(mock_redis, tenant_id="t1")

        captured_lock_names: list[str] = []

        class _FakeLock:
            """Stand-in for ``redis.lock.Lock`` that captures the ``name=`` kwarg passed at construction."""

            def __init__(self, _client: Any, *, name: str, **_kwargs: Any) -> None:
                """Record the lock name (the on-wire key) used to construct the lock."""
                captured_lock_names.append(name)

            def acquire(self, blocking: bool = True) -> bool:
                """Pretend acquisition always succeeds (no real Redis round-trip)."""
                return True

            def release(self) -> None:
                """No-op release — no real lock state to clear."""
                return None

        monkeypatch.setattr(redis.lock, "Lock", _FakeLock)

        async with backend.acquire_lock("user:123", timeout=10.0) as acquired:
            assert acquired is True

        assert len(captured_lock_names) == 1
        wire_name = captured_lock_names[0]

        # Wire compatibility: the Redis lock name MUST still carry ``:lock``.
        # The tenant scope prefix is irrelevant to this contract — what matters
        # is that the cache_key portion is suffixed with ``:lock`` so the on-wire
        # key matches existing Redis deployments. Zero migration required.
        assert wire_name.endswith(":lock"), (
            f"Redis lock wire-name lost the ':lock' suffix; got {wire_name!r}. "
            f"This would invalidate existing Redis deployments' locks (key skew)."
        )
        # Sanity: the bare key was scoped + suffixed, not double-suffixed.
        assert ":lock:lock" not in wire_name, (
            f"double ':lock' suffix in Redis wire name: {wire_name!r} — both wrapper and backend appended the suffix"
        )
