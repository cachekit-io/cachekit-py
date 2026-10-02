"""A return value the cache write cannot serialize or encrypt never counts toward the breaker.

The value is rejected before anything reaches the backend, so the failure says nothing
about backend health. The sync write never counted it. The async write did, in both its
locked and lockless miss paths: five rejected values opened the breaker in front of a
healthy backend, and every L2-only entry then ran its function uncached until the
breaker recovered. The rejected write still logs its ERROR and WARNING, and the function's
result is still returned uncached.

These tests drive a real ``@cache`` function with L1 off, so a cached key is served from
L2 only, and capture the live breaker by spying on the orchestrator's ``CircuitBreaker``.
"""

from __future__ import annotations

import inspect
import logging
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager
from typing import Any

import pytest

from cachekit import cache
from cachekit.decorators import orchestrator as orchestrator_module
from cachekit.l1_cache import get_l1_cache_manager
from cachekit.reliability.circuit_breaker import CircuitBreaker, CircuitBreakerConfig, CircuitState
from cachekit.serializers.encryption_wrapper import EncryptionError, EncryptionWrapper

_THRESHOLD = CircuitBreakerConfig().failure_threshold
_MASTER_KEY = "a" * 64
_UNSERIALIZABLE = "unserializable"
_UNENCRYPTABLE = "unencryptable"


class _Backend:
    """Healthy in-memory backend without ``acquire_lock``: drives the lockless miss path."""

    def __init__(self) -> None:
        self.store: dict[str, bytes] = {}
        self.gets = 0
        self.sets = 0

    def get(self, key: str) -> bytes | None:
        self.gets += 1
        return self.store.get(key)

    def set(self, key: str, value: bytes, ttl: int | None = None) -> None:
        self.sets += 1
        self.store[key] = bytes(value)

    def delete(self, key: str) -> bool:
        return self.store.pop(key, None) is not None

    def exists(self, key: str) -> bool:
        return key in self.store

    def health_check(self) -> tuple[bool, dict[str, Any]]:
        return True, {}


class _LockableBackend(_Backend):
    """Backend whose lock always acquires: drives the locked miss path."""

    @asynccontextmanager
    async def acquire_lock(self, key: str, timeout: float, blocking_timeout: float | None = None) -> AsyncIterator[bool]:
        yield True


@pytest.fixture(autouse=True)
def setup_di_for_redis_isolation() -> Iterator[None]:
    """Override the root conftest's Redis isolation: the backend is injected, no Redis needed."""
    yield
    get_l1_cache_manager().clear_all()


@pytest.fixture
def live_breakers(monkeypatch: pytest.MonkeyPatch) -> list[CircuitBreaker]:
    """Capture every breaker a decorator builds (decorate AFTER requesting this)."""
    captured: list[CircuitBreaker] = []
    real = orchestrator_module.CircuitBreaker

    def spy(*args, **kwargs) -> CircuitBreaker:
        breaker = real(*args, **kwargs)
        captured.append(breaker)
        return breaker

    monkeypatch.setattr(orchestrator_module, "CircuitBreaker", spy)
    return captured


@pytest.fixture
def encryption_rejects(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make encryption fail for one marker value, as a real AES-GCM failure would surface."""
    real = EncryptionWrapper.serialize

    def serialize(self, obj: Any, cache_key: str = ""):
        if obj == _UNENCRYPTABLE:
            raise EncryptionError("Encryption failed")
        return real(self, obj, cache_key)

    monkeypatch.setattr(EncryptionWrapper, "serialize", serialize)


def _decorate(namespace: str, *, is_async: bool, secure: bool, backend: _Backend, executions: list[Any]):
    """``@cache`` a function that returns ``v:<x>``, an unserializable object, or the encryption marker."""

    def body(x: str) -> Any:
        executions.append(x)
        if x == _UNSERIALIZABLE:
            return object()
        if x == _UNENCRYPTABLE:
            return _UNENCRYPTABLE
        return f"v:{x}"

    async def async_body(x: str) -> Any:
        return body(x)

    fn = async_body if is_async else body
    if secure:
        return cache.secure(master_key=_MASTER_KEY, backend=backend, ttl=300, l1_enabled=False, namespace=namespace)(fn)
    return cache(backend=backend, ttl=300, l1_enabled=False, namespace=namespace)(fn)


async def _call(fn: Callable[[str], Any], x: str) -> Any:
    result = fn(x)
    return await result if inspect.isawaitable(result) else result


_PATHS = [
    pytest.param(True, _Backend, id="async-no-lock"),
    pytest.param(True, _LockableBackend, id="async-locked"),
    pytest.param(False, _Backend, id="sync"),
]
_REJECTIONS = [
    pytest.param(False, _UNSERIALIZABLE, id="serialize"),
    pytest.param(True, _UNENCRYPTABLE, id="encrypt"),
]


@pytest.mark.unit
@pytest.mark.parametrize(("secure", "rejected"), _REJECTIONS)
@pytest.mark.parametrize(("is_async", "backend_cls"), _PATHS)
class TestRejectedWriteLeavesBreakerAlone:
    async def test_breaker_stays_closed_and_l2_entry_still_served(
        self, is_async, backend_cls, secure, rejected, live_breakers, request
    ):
        if secure:
            request.getfixturevalue("encryption_rejects")
        backend = backend_cls()
        executions: list[Any] = []
        fn = _decorate(
            f"serialize-breaker-{request.node.callspec.id}",
            is_async=is_async,
            secure=secure,
            backend=backend,
            executions=executions,
        )
        (breaker,) = live_breakers

        assert await _call(fn, "cached") == "v:cached"
        assert backend.sets == 1

        for _ in range(_THRESHOLD + 2):
            result = await _call(fn, rejected)
            assert result is not None  # the function's value comes back, uncached
        assert executions.count(rejected) == _THRESHOLD + 2
        assert backend.sets == 1  # no rejected value reached the backend

        assert breaker.state == CircuitState.CLOSED
        assert breaker.failure_count == 0

        gets_before = backend.gets
        assert await _call(fn, "cached") == "v:cached"
        assert executions.count("cached") == 1  # served from L2, not recomputed
        assert backend.gets == gets_before + 1

    async def test_logs_unchanged(self, is_async, backend_cls, secure, rejected, live_breakers, request, caplog):
        if secure:
            request.getfixturevalue("encryption_rejects")
        backend = backend_cls()
        fn = _decorate(
            f"serialize-breaker-log-{request.node.callspec.id}", is_async=is_async, secure=secure, backend=backend, executions=[]
        )

        with caplog.at_level(logging.WARNING):
            await _call(fn, rejected)

        assert any(r.levelno == logging.ERROR and "Serialization failed" in r.getMessage() for r in caplog.records)
        assert any(r.levelno == logging.WARNING and "SerializationError" in r.getMessage() for r in caplog.records)
