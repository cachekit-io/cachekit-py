"""distributed_lock=False: an async miss skips the backend's distributed lock and takes the unlocked path.

The backend here has both lock methods the miss path can call (``acquire_fill_lock``, which it prefers, and
``acquire_lock``) and records every call to either. The request-level shapes over CachekitIO, and the in-process
single-flight that still holds with the lock off, are pinned in test_cachekitio_call_shape.py.
"""

from __future__ import annotations

import dataclasses
import inspect
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import pytest

from cachekit import cache
from cachekit.config.decorator import DecoratorConfig
from cachekit.config.validation import ConfigurationError
from cachekit.decorators.wrapper import create_cache_wrapper

pytestmark = pytest.mark.unit

_MASTER_KEY = "a" * 64  # pragma: allowlist secret — test fixture key


class _BothLocksBackend:
    """In-memory backend with both lock shapes. Records ``fill`` / ``lock`` per call."""

    def __init__(self) -> None:
        self.store: dict[str, bytes] = {}
        self.locks: list[str] = []

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

    @asynccontextmanager
    async def acquire_lock(self, key: str, timeout: float, blocking_timeout: float | None = None) -> AsyncIterator[bool]:
        self.locks.append("lock")
        yield True

    @asynccontextmanager
    async def acquire_fill_lock(
        self, key: str, timeout: float, blocking_timeout: float | None = None
    ) -> AsyncIterator[tuple[bool, bool]]:
        self.locks.append("fill")
        yield True, True


class TestConfig:
    def test_on_by_default_and_in_every_preset(self) -> None:
        assert DecoratorConfig().distributed_lock is True
        assert DecoratorConfig.io(api_key="ck_test_distributed_lock").distributed_lock is True  # pragma: allowlist secret
        assert all(
            getattr(DecoratorConfig, preset)(**kwargs).distributed_lock
            for preset, kwargs in {
                "minimal": {},
                "production": {},
                "dev": {},
                "test": {},
                "secure": {"master_key": _MASTER_KEY, "single_tenant_mode": True},
            }.items()
        )

    def test_must_be_a_bool(self) -> None:
        with pytest.raises(ConfigurationError, match="distributed_lock must be a bool"):
            DecoratorConfig(distributed_lock=0)  # type: ignore[arg-type]

    def test_direct_factory_call_must_pass_a_bool(self) -> None:
        async def fn(x: int) -> int:
            return x

        with pytest.raises(ConfigurationError, match="distributed_lock must be a bool"):
            create_cache_wrapper(fn, backend=_BothLocksBackend(), distributed_lock=0)  # type: ignore[arg-type]

    def test_adds_no_positional_parameter(self) -> None:
        """Appended keyword-only to the config and last to the factory: existing positional calls bind as before."""
        assert next(f for f in dataclasses.fields(DecoratorConfig) if f.name == "distributed_lock").kw_only
        assert list(inspect.signature(create_cache_wrapper).parameters)[-1] == "distributed_lock"


class TestAsyncMiss:
    async def test_default_takes_the_lock(self) -> None:
        backend = _BothLocksBackend()

        @cache(backend=backend, ttl=60)
        async def fn(x: int) -> int:
            return x * 2

        assert await fn(1) == 2
        assert backend.locks == ["fill"]

    async def test_off_requests_no_lock(self) -> None:
        backend = _BothLocksBackend()
        calls: list[int] = []

        @cache(backend=backend, ttl=60, distributed_lock=False)
        async def fn(x: int) -> int:
            calls.append(x)
            return x * 2

        assert await fn(1) == 2
        assert calls == [1]
        assert backend.locks == []
        assert len(backend.store) == 1  # the miss still wrote L2

    def test_sync_never_takes_the_lock(self) -> None:
        backend = _BothLocksBackend()

        @cache(backend=backend, ttl=60)
        def fn(x: int) -> int:
            return x * 2

        assert fn(1) == 2
        assert backend.locks == []
