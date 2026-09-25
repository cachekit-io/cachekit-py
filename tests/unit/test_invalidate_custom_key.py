"""LAB-4387: invalidate_cache(*args) must delete the key the write path wrote.

Bug: the read/write paths derived the key four ways (interop / key= / fast_mode /
auto) but invalidate_cache only two (interop / auto), so with a custom ``key=``
function or ``fast_mode`` it deleted a key nothing ever wrote and the stale
value kept being served.

The assertions go through a recording backend and the wrapped function's call
count — never by re-deriving the key in the test.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any

import pytest

from cachekit import cache
from cachekit.decorators.wrapper import create_cache_wrapper


class RecordingBackend:
    """Stores keys and bytes exactly as given; records every delete."""

    def __init__(self) -> None:
        self.store: dict[str, bytes] = {}
        self.deleted: list[str] = []

    def get(self, key: str) -> bytes | None:
        return self.store.get(key)

    def set(self, key: str, value: bytes, ttl: int | None = None) -> None:
        self.store[key] = bytes(value)

    def delete(self, key: str) -> bool:
        self.deleted.append(key)
        return self.store.pop(key, None) is not None

    def exists(self, key: str) -> bool:
        return key in self.store

    def health_check(self) -> tuple[bool, dict[str, Any]]:
        return True, {"latency_ms": 0.0, "backend_type": "recording"}


def _user_key(user_id: int) -> str:
    return f"user:{user_id}"


def _decorate(
    backend: RecordingBackend,
    namespace: str,
    *,
    key: Callable[..., str] | None = None,
    fast_mode: bool = False,
) -> Callable[[Callable[..., Any]], Any]:
    """L1 disabled so every read/write/delete hits the RecordingBackend.

    key= must go through the public @cache path (create_cache_wrapper only reads
    it from DecoratorConfig); fast_mode is internal-only and not a DecoratorConfig
    field, so it goes through create_cache_wrapper directly.
    """

    def apply(fn: Callable[..., Any]) -> Any:
        if fast_mode:
            return create_cache_wrapper(fn, backend=backend, l1_enabled=False, namespace=namespace, fast_mode=True)
        return cache(backend=backend, l1_enabled=False, namespace=namespace, key=key)(fn)

    return apply


@pytest.mark.unit
class TestInvalidateCustomKey:
    @pytest.mark.parametrize("mode", [{"key": _user_key}, {"fast_mode": True}], ids=["custom_key", "fast_mode"])
    def test_sync_invalidate_args_deletes_written_entry(self, mode: dict[str, Any]):
        backend = RecordingBackend()
        calls = 0

        @_decorate(backend, "lab4387", **mode)
        def get_user(user_id: int) -> int:
            nonlocal calls
            calls += 1
            return calls

        assert get_user(1) == 1
        assert get_user(1) == 1  # hit
        (written_key,) = backend.store

        get_user.invalidate_cache(1)

        assert backend.deleted == [written_key], "invalidate must delete the key the write path wrote"
        assert not backend.store
        assert get_user(1) == 2, "stale value still served after invalidate_cache(args)"

    @pytest.mark.parametrize("mode", [{"key": _user_key}, {"fast_mode": True}], ids=["custom_key", "fast_mode"])
    @pytest.mark.asyncio
    async def test_async_invalidate_args_deletes_written_entry(self, mode: dict[str, Any]):
        backend = RecordingBackend()
        calls = 0

        @_decorate(backend, "lab4387", **mode)
        async def get_user(user_id: int) -> int:
            nonlocal calls
            calls += 1
            return calls

        assert await get_user(1) == 1
        assert await get_user(1) == 1  # hit
        (written_key,) = backend.store

        await get_user.ainvalidate_cache(1)

        assert backend.deleted == [written_key]
        assert not backend.store
        assert await get_user(1) == 2

    def test_sync_invalidate_l1_only_custom_key(self):
        """L1-only mode (backend=None): the L1 delete must also use the custom key."""
        calls = 0

        @cache(backend=None, key=_user_key, namespace="lab4387_l1_only")
        def get_user(user_id: int) -> int:
            nonlocal calls
            calls += 1
            return calls

        assert get_user(1) == 1
        assert get_user(1) == 1
        get_user.invalidate_cache(1)
        assert get_user(1) == 2


class ThreadRecordingBackend(RecordingBackend):
    """Records which thread each sync delete ran on."""

    def __init__(self) -> None:
        super().__init__()
        self.delete_threads: list[int] = []

    def delete(self, key: str) -> bool:
        self.delete_threads.append(threading.get_ident())
        return super().delete(key)


class AsyncDeleteBackend(RecordingBackend):
    """Offers a native delete_async; the sync delete must not be called from async code."""

    def __init__(self) -> None:
        super().__init__()
        self.async_deleted: list[str] = []

    def delete(self, key: str) -> bool:
        raise AssertionError("sync delete called from ainvalidate_cache")

    async def delete_async(self, key: str) -> bool:
        self.async_deleted.append(key)
        return self.store.pop(key, None) is not None


_MODES = pytest.mark.parametrize("mode", [{"key": _user_key}, {"fast_mode": True}], ids=["custom_key", "fast_mode"])
_NO_ARGS = pytest.mark.parametrize("no_args", [False, True], ids=["args", "no_args"])


@pytest.mark.unit
class TestAsyncInvalidateNonBlocking:
    """ainvalidate_cache must never run a blocking L2 delete on the event loop."""

    @_MODES
    @_NO_ARGS
    @pytest.mark.asyncio
    async def test_sync_backend_delete_runs_in_worker_thread(self, mode: dict[str, Any], no_args: bool):
        backend = ThreadRecordingBackend()

        @_decorate(backend, "nonblocking_thread", **mode)
        async def get_user(user_id: int) -> int:
            return user_id

        await get_user(1)
        (written_key,) = backend.store

        await (get_user.ainvalidate_cache() if no_args else get_user.ainvalidate_cache(1))

        assert backend.deleted == [written_key]
        assert backend.delete_threads
        assert threading.get_ident() not in backend.delete_threads, "sync delete ran on the event loop thread"

    @_MODES
    @_NO_ARGS
    @pytest.mark.asyncio
    async def test_native_delete_async_preferred(self, mode: dict[str, Any], no_args: bool):
        backend = AsyncDeleteBackend()

        @_decorate(backend, "nonblocking_native", **mode)
        async def get_user(user_id: int) -> int:
            return user_id

        await get_user(1)
        (written_key,) = backend.store

        await (get_user.ainvalidate_cache() if no_args else get_user.ainvalidate_cache(1))

        assert backend.async_deleted == [written_key]
        assert not backend.store
