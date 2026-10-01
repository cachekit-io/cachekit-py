"""One cache operation, one metrics record (LAB-3761).

Every success site used to call ``features.record_success()``, which forwarded an
unlabelled record to the collector, and then its own labelled
``features.record_cache_operation(...)``. So ``cache_operations_total`` counted each
operation twice, once under ``serializer="unknown"``. Pinned at the collector, the
real sink, over sync and async, for an L1 hit, an L2 hit and a miss.
"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable, Iterator
from typing import Any, TypeVar

import pytest

from cachekit import cache
from cachekit.l1_cache import get_l1_cache_manager
from cachekit.reliability.async_metrics import AsyncMetricsCollector, _metrics_cache

T = TypeVar("T")


class _ByteStore:
    def __init__(self) -> None:
        self.store: dict[str, bytes] = {}

    def get(self, key: str) -> bytes | None:
        return self.store.get(key)

    def set(self, key: str, value: bytes, ttl: int | None = None) -> None:
        self.store[key] = value

    def delete(self, key: str) -> bool:
        return self.store.pop(key, None) is not None

    def exists(self, key: str) -> bool:
        return key in self.store

    def health_check(self) -> tuple[bool, dict[str, Any]]:
        return True, {}


@pytest.fixture(autouse=True)
def setup_di_for_redis_isolation() -> Iterator[None]:
    """The backend is injected, no Redis needed; clear L1 so cases sharing a key stay independent."""
    yield
    get_l1_cache_manager().clear_all()


@pytest.fixture
def recorded(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(AsyncMetricsCollector, "record_cache_operation", lambda self, **kw: calls.append(kw))
    return calls


async def _call(fn: Callable[[], T | Awaitable[T]]) -> T:
    result = fn()
    if inspect.isawaitable(result):
        return await result
    return result


@pytest.mark.unit
@pytest.mark.parametrize("is_async", [False, True], ids=["sync", "async"])
@pytest.mark.parametrize(
    ("scenario", "expected"),
    [("miss", ("set", "rust")), ("l2-hit", ("get", "rust")), ("l1-hit", ("get", "l1_memory"))],
)
async def test_one_operation_one_record(
    recorded: list[dict[str, Any]], is_async: bool, scenario: str, expected: tuple[str, str]
) -> None:
    backend = _ByteStore()

    if is_async:

        @cache(backend=backend, ttl=60, namespace=f"single-record-{scenario}-async")
        async def compute() -> dict[str, int]:
            return {"answer": 42}

    else:

        @cache(backend=backend, ttl=60, namespace=f"single-record-{scenario}-sync")
        def compute() -> dict[str, int]:
            return {"answer": 42}

    if scenario != "miss":
        assert await _call(compute) == {"answer": 42}  # prime L2 and L1
        if scenario == "l2-hit":
            get_l1_cache_manager().clear_all()
        recorded.clear()

    assert await _call(compute) == {"answer": 42}

    assert len(recorded) == 1, recorded
    assert (recorded[0]["operation"], recorded[0]["serializer"]) == expected


@pytest.mark.unit
@pytest.mark.parametrize("sync_mode", [True, False], ids=["sync-sink", "batched-sink"])
def test_collector_counts_one_record_once(sync_mode: bool) -> None:
    """Both collector modes turn one record into one counter increment, with no hit field queued."""
    namespace = f"single-record-sink-{sync_mode}"
    collector = AsyncMetricsCollector(sync_mode=sync_mode, auto_detect_mode=False)
    if not sync_mode:
        # Stop the batched worker first and flush by hand: a running worker could take the record before this
        # reads it. Not via shutdown(), which also returns the collector to sync mode.
        assert collector._stopped is not None and collector._worker_thread is not None
        collector._stopped.set()
        collector._worker_thread.join(5)

    collector.record_cache_operation(
        operation="get", namespace=namespace, success=True, duration_ms=1.0, serializer="rust", size_bytes=8
    )
    if not sync_mode:
        assert collector._queue is not None
        batch = [collector._queue.get_nowait()]
        assert collector._queue.empty()
        assert "hit" not in batch[0]
        collector._flush_batch(batch)

    counter = _metrics_cache["cache_operations_total"]  # process-wide: shared by every collector
    assert counter.labels(operation="get", namespace=namespace, success="True", serializer="rust")._value.get() == 1
