"""Cache success paths leave the operation context unset (LAB-6357).

The operation context is read only by ``record_failure()``, and its one caller,
``handle_cache_error()``, sets its own context first. A write on a success path is
a dead store on the hot path, and a later failure record that skipped the setter
would inherit the success path's label. Every success path is driven here through
the real decorator stack: a miss that stores, an L1 hit and an L2 hit, sync and
async, and the async miss-store both under the lock and without it.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from typing import Any

import pytest

from cachekit import cache
from cachekit.decorators.orchestrator import _operation_context
from cachekit.l1_cache import get_l1_cache_manager

VALUE = {"answer": 42}


class _ByteStore:
    """Plain byte store without ``acquire_lock``: drives the no-lock miss path."""

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


class _LockableByteStore(_ByteStore):
    """Byte store whose lock always acquires: drives the async locked miss path."""

    @asynccontextmanager
    async def acquire_lock(self, key: str, timeout: float, blocking_timeout: float | None = None) -> AsyncIterator[bool]:
        yield True


@pytest.fixture(autouse=True)
def setup_di_for_redis_isolation() -> Iterator[None]:
    """Override the root conftest's Redis isolation: the backend is injected, no Redis needed.

    Start each case with no operation context (an earlier test on this thread may have
    set one), and keep L1 clear so a parametrized case never starts on an L1 hit.
    """
    token = _operation_context.set(None)
    yield
    _operation_context.reset(token)
    get_l1_cache_manager().clear_all()


def _leaked(step: str, leaks: list[tuple[str, Any]]) -> None:
    """Note a context the step left behind, then clear it so each step is judged alone."""
    if (ctx := _operation_context.get()) is not None:
        leaks.append((step, ctx["operation"]))
    _operation_context.set(None)


@pytest.mark.unit
def test_sync_success_paths_leave_operation_context_unset() -> None:
    backend = _ByteStore()

    @cache(backend=backend, ttl=60, namespace="op-context-sync")
    def compute() -> dict[str, int]:
        return dict(VALUE)

    before = compute.cache_info()  # type: ignore[attr-defined]  # stats are per function, shared across cases
    leaks: list[tuple[str, Any]] = []
    assert compute() == VALUE
    _leaked("miss-store", leaks)
    assert compute() == VALUE
    _leaked("l1-hit", leaks)

    l2_snapshot = dict(backend.store)
    compute.invalidate_cache()  # type: ignore[attr-defined]
    backend.store.update(l2_snapshot)
    assert compute() == VALUE
    _leaked("l2-hit", leaks)

    assert leaks == []
    after = compute.cache_info()  # type: ignore[attr-defined]
    assert (after.l1_hits - before.l1_hits, after.l2_hits - before.l2_hits) == (1, 1)  # each hit path ran once
    assert backend.store  # and the miss stored


@pytest.mark.unit
@pytest.mark.parametrize("backend_cls", [_ByteStore, _LockableByteStore], ids=["no-lock", "locked"])
async def test_async_success_paths_leave_operation_context_unset(backend_cls: type[_ByteStore]) -> None:
    backend = backend_cls()

    @cache(backend=backend, ttl=60, namespace="op-context-async")
    async def compute() -> dict[str, int]:
        return dict(VALUE)

    before = compute.cache_info()  # type: ignore[attr-defined]  # stats are per function, shared across cases
    leaks: list[tuple[str, Any]] = []
    assert await compute() == VALUE
    _leaked("miss-store", leaks)
    assert await compute() == VALUE
    _leaked("l1-hit", leaks)

    l2_snapshot = dict(backend.store)
    await compute.invalidate_cache()  # type: ignore[attr-defined]
    backend.store.update(l2_snapshot)
    assert await compute() == VALUE
    _leaked("l2-hit", leaks)

    assert leaks == []
    after = compute.cache_info()  # type: ignore[attr-defined]
    assert (after.l1_hits - before.l1_hits, after.l2_hits - before.l2_hits) == (1, 1)  # each hit path ran once
    assert backend.store  # and the miss stored
