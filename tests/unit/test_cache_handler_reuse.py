"""A decorated backed function builds its StandardCacheHandler once per resolved backend."""

from __future__ import annotations

from typing import Optional

import pytest

from cachekit import cache
from cachekit.cache_handler import StandardCacheHandler
from cachekit.decorators import wrapper as wrapper_module


class _Backend:
    def __init__(self) -> None:
        self.store: dict[str, bytes] = {}

    def get(self, key: str) -> Optional[bytes]:
        return self.store.get(key)

    def set(self, key: str, value: bytes, ttl: Optional[int] = None) -> None:
        self.store[key] = bytes(value)

    def delete(self, key: str) -> bool:
        return self.store.pop(key, None) is not None

    def exists(self, key: str) -> bool:
        return key in self.store


@pytest.fixture
def built(monkeypatch: pytest.MonkeyPatch) -> list[StandardCacheHandler]:
    """Every StandardCacheHandler the wrapper constructs, in order."""
    handlers: list[StandardCacheHandler] = []

    class Spy(StandardCacheHandler):
        def __init__(self, *args, **kwargs) -> None:
            super().__init__(*args, **kwargs)
            handlers.append(self)

    monkeypatch.setattr(wrapper_module, "StandardCacheHandler", Spy)
    return handlers


@pytest.fixture
def backend(monkeypatch: pytest.MonkeyPatch) -> _Backend:
    lazy = _Backend()
    monkeypatch.setattr(wrapper_module, "_resolve_lazy_backend", lambda: lazy)
    return lazy


@pytest.mark.unit
class TestHandlerBuiltOnce:
    def test_sync_explicit_backend(self, built):
        backend = _Backend()

        @cache(backend=backend, l1_enabled=False, namespace="lab7112_sync")
        def f(x: int) -> int:
            return x * 2

        for x in (1, 1, 2, 3):  # miss, hit, miss, miss
            assert f(x) == x * 2
        assert len(built) == 1
        assert built[0].backend is backend
        assert len(backend.store) == 3

    @pytest.mark.asyncio
    async def test_async_explicit_backend(self, built):
        backend = _Backend()

        @cache(backend=backend, l1_enabled=False, namespace="lab7112_async")
        async def f(x: int) -> int:
            return x * 2

        for x in (1, 1, 2, 3):
            assert await f(x) == x * 2
        assert len(built) == 1
        assert built[0].backend is backend

    def test_sync_lazy_backend_first_resolved_by_invalidate(self, built, backend):
        @cache(ttl=300, l1_enabled=False, namespace="lab7112_lazy_sync")
        def f(x: int) -> int:
            return x * 2

        f.invalidate_cache(1)  # resolves the backend without building a handler
        assert built == []
        for x in (1, 1, 2):
            assert f(x) == x * 2
        assert len(built) == 1
        assert built[0].backend is backend
        assert backend.store, "the first call after invalidate must still reach L2"

    @pytest.mark.asyncio
    async def test_async_lazy_backend_first_resolved_by_invalidate(self, built, backend):
        @cache(ttl=300, l1_enabled=False, namespace="lab7112_lazy_async")
        async def f(x: int) -> int:
            return x * 2

        await f.ainvalidate_cache(1)
        assert built == []
        for x in (1, 1, 2):
            assert await f(x) == x * 2
        assert len(built) == 1
        assert built[0].backend is backend
        assert backend.store
