"""The decorator's duration samples time the operation their label names.

A fake clock replaces ``time`` as ``cachekit.decorators.wrapper`` reads it, and only the stub
backend and the decorated function move it: GET +5 ms, SET +7 ms, ``track_key`` +3 ms, the
function +20 ms, a lock grant +11 ms. Every sample is then an exact sum of those steps, so the
assertions need no tolerance beyond float rounding and the tests never sleep.

``set`` covers serialization plus the store and nothing else: not the miss read, the function,
the lock wait, the post-lock double-check read, the L1 put or the key-registry write. An L1 hit
covers the L1 get plus deserialization, read from the clock.
"""

from __future__ import annotations

import threading
import time
import types
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from typing import Any

import pytest

from cachekit import cache
from cachekit.l1_cache import L1Cache, get_l1_cache_manager
from cachekit.reliability.async_metrics import AsyncMetricsCollector

GET_MS, SET_MS, TRACK_MS, FUNC_MS, LOCK_MS, L1_MS = 5.0, 7.0, 3.0, 20.0, 11.0, 0.05


class _FakeClock:
    """A perf_counter that moves only when advanced. Thread-safe: track_key runs in a worker thread."""

    def __init__(self) -> None:
        self._now = 1000.0
        self._lock = threading.Lock()

    def advance(self, ms: float) -> None:
        with self._lock:
            self._now += ms / 1000

    def perf_counter(self) -> float:
        with self._lock:
            return self._now


class _TimedStore:
    """Key-tracking byte store without a lock: drives the sync miss and the async unlocked miss."""

    def __init__(self, clock: _FakeClock) -> None:
        self.clock = clock
        self.store: dict[str, bytes] = {}

    def get(self, key: str) -> bytes | None:
        self.clock.advance(GET_MS)
        return self.store.get(key)

    def set(self, key: str, value: bytes, ttl: int | None = None) -> None:
        self.clock.advance(SET_MS)
        self.store[key] = value

    def delete(self, key: str) -> bool:
        return self.store.pop(key, None) is not None

    def exists(self, key: str) -> bool:
        return key in self.store

    def health_check(self) -> tuple[bool, dict[str, Any]]:
        return True, {}

    def track_key(self, registry_id: str, key: str) -> None:
        self.clock.advance(TRACK_MS)

    def drain_tracked(self, registry_id: str, keys: set[str]) -> int:
        return 0


class _TimedLockableStore(_TimedStore):
    """Implements only ``acquire_lock``, so every grant is contended and the double-check GET runs."""

    @asynccontextmanager
    async def acquire_lock(self, key: str, timeout: float, blocking_timeout: float | None = None) -> AsyncIterator[bool]:
        self.clock.advance(LOCK_MS)
        yield True


@pytest.fixture(autouse=True)
def setup_di_for_redis_isolation() -> Iterator[None]:
    """Override the root conftest's Redis isolation: the backend is injected. Keep L1 clear between cases."""
    get_l1_cache_manager().clear_all()
    yield
    get_l1_cache_manager().clear_all()


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> _FakeClock:
    """Swap the wrapper module's ``time``; the async trip runs in its own task, so a task-local patch would miss it."""
    fake = _FakeClock()
    monkeypatch.setattr(
        "cachekit.decorators.wrapper.time",
        types.SimpleNamespace(perf_counter=fake.perf_counter, time=time.time, monotonic=time.monotonic),
    )
    return fake


@pytest.fixture
def recorded(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Every cache-operation metric record, captured at the collector (the single observation per operation)."""
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(AsyncMetricsCollector, "record_cache_operation", lambda self, **kw: calls.append(kw))
    return calls


@pytest.fixture
def slow_l1(monkeypatch: pytest.MonkeyPatch, clock: _FakeClock) -> None:
    """Make every L1 get cost L1_MS on the fake clock."""
    original = L1Cache.get

    def get(self: L1Cache, key: str) -> Any:
        clock.advance(L1_MS)
        return original(self, key)

    monkeypatch.setattr(L1Cache, "get", get)


def _durations(recorded: list[dict[str, Any]], operation: str, serializer: str) -> list[float]:
    return [r["duration_ms"] for r in recorded if r["operation"] == operation and r.get("serializer") == serializer]


@pytest.mark.unit
def test_sync_miss_set_times_only_the_store(clock: _FakeClock, recorded: list[dict[str, Any]]) -> None:
    backend = _TimedStore(clock)

    @cache(backend=backend, ttl=60, namespace="latency-sync")
    def compute() -> dict[str, int]:
        clock.advance(FUNC_MS)
        return {"answer": 42}

    assert compute() == {"answer": 42}

    assert _durations(recorded, "set", "rust") == [pytest.approx(SET_MS, abs=1e-6)]


@pytest.mark.unit
@pytest.mark.parametrize("backend_cls", [_TimedStore, _TimedLockableStore], ids=["unlocked", "locked"])
async def test_async_miss_set_times_only_the_store(
    clock: _FakeClock, recorded: list[dict[str, Any]], backend_cls: type[_TimedStore]
) -> None:
    backend = backend_cls(clock)

    @cache(backend=backend, ttl=60, namespace=f"latency-async-{backend_cls.__name__}")
    async def compute() -> dict[str, int]:
        clock.advance(FUNC_MS)
        return {"answer": 42}

    assert await compute() == {"answer": 42}

    assert _durations(recorded, "set", "rust") == [pytest.approx(SET_MS, abs=1e-6)]


@pytest.mark.unit
@pytest.mark.usefixtures("slow_l1")
def test_sync_l1_hit_duration_comes_from_the_clock(clock: _FakeClock, recorded: list[dict[str, Any]]) -> None:
    backend = _TimedStore(clock)

    @cache(backend=backend, ttl=60, namespace="latency-sync-l1")
    def compute() -> dict[str, int]:
        return {"answer": 42}

    compute()
    recorded.clear()
    assert compute() == {"answer": 42}

    assert _durations(recorded, "get", "l1_memory") == [pytest.approx(L1_MS, abs=1e-6)]


@pytest.mark.unit
@pytest.mark.usefixtures("slow_l1")
async def test_async_l1_hit_duration_comes_from_the_clock(clock: _FakeClock, recorded: list[dict[str, Any]]) -> None:
    backend = _TimedStore(clock)

    @cache(backend=backend, ttl=60, namespace="latency-async-l1")
    async def compute() -> dict[str, int]:
        return {"answer": 42}

    await compute()
    recorded.clear()
    assert await compute() == {"answer": 42}

    assert _durations(recorded, "get", "l1_memory") == [pytest.approx(L1_MS, abs=1e-6)]
