"""LAB-4387: invalidate_cache(*args) must delete the key the write path wrote.

Bug: the read/write paths derived the key four ways (interop / key= / fast_mode /
auto) but invalidate_cache only two (interop / auto), so with a custom ``key=``
function or ``fast_mode`` it deleted a key nothing ever wrote and the stale
value kept being served.

The assertions go through a recording backend and the wrapped function's call
count — never by re-deriving the key in the test.
"""

from __future__ import annotations

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


def _decorate(backend: RecordingBackend, namespace: str, **mode: Any):
    """L1 disabled so every read/write/delete hits the RecordingBackend.

    key= must go through the public @cache path (create_cache_wrapper only reads
    it from DecoratorConfig); fast_mode is internal-only and not a DecoratorConfig
    field, so it goes through create_cache_wrapper directly.
    """

    def apply(fn):
        if mode.get("fast_mode"):
            return create_cache_wrapper(fn, backend=backend, l1_enabled=False, namespace=namespace, **mode)
        return cache(backend=backend, l1_enabled=False, namespace=namespace, **mode)(fn)

    return apply


@pytest.mark.unit
class TestInvalidateCustomKey:
    @pytest.mark.parametrize("mode", [{"key": _user_key}, {"fast_mode": True}], ids=["custom_key", "fast_mode"])
    def test_sync_invalidate_args_deletes_written_entry(self, mode: dict[str, Any]):
        backend = RecordingBackend()
        calls = 0

        @_decorate(backend, f"lab4387_sync_{next(iter(mode))}", **mode)
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

        @_decorate(backend, f"lab4387_async_{next(iter(mode))}", **mode)
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
