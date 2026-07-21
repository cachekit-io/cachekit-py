"""Decorator-level stale-while-revalidate (LAB-381).

End-to-end behavior through the real decorator + serializer stack against a
fake SWR-capable backend: serve-stale-immediately, background revalidation
(async task / sync daemon thread), single-flight, lease handling, failure
degradation, and decoration-time validation.

Spec: protocol spec/saas-api.md#stale-while-revalidate.
"""

from __future__ import annotations

import asyncio
import threading
import time
from contextlib import asynccontextmanager
from typing import Any

import pytest

from cachekit import cache
from cachekit.config.decorator import DecoratorConfig
from cachekit.config.validation import ConfigurationError

_CAP = 2_592_000  # 30-day storage cap shared by ttl + stale_ttl


class FakeSWRBackend:
    """SWR-capable backend double: byte store + controllable freshness + lock log."""

    def __init__(self, grant_lock: bool = True) -> None:
        self.store: dict[str, bytes] = {}
        self.stale = False
        self.grant_lock = grant_lock
        self.set_calls: list[tuple[int | None, int | None]] = []
        self.lock_attempts: list[str] = []

    def get(self, key: str) -> bytes | None:
        return self.store.get(key)

    def get_with_freshness(self, key: str) -> tuple[bytes, bool] | None:
        value = self.store.get(key)
        return None if value is None else (value, self.stale)

    def set(self, key: str, value: bytes, ttl: int | None = None, stale_ttl: int | None = None) -> None:
        self.store[key] = value
        self.set_calls.append((ttl, stale_ttl))

    def delete(self, key: str) -> bool:
        return self.store.pop(key, None) is not None

    def exists(self, key: str) -> bool:
        return key in self.store

    def health_check(self) -> tuple[bool, dict[str, Any]]:
        return True, {}

    @asynccontextmanager
    async def acquire_lock(self, key: str, timeout: float, blocking_timeout: float | None = None):
        self.lock_attempts.append(key)
        yield self.grant_lock


def _wait_for(predicate, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


async def _await_for(predicate, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.02)
    return predicate()


class TestAsyncSWR:
    async def test_stale_hit_serves_immediately_and_revalidates_in_background(self) -> None:
        backend = FakeSWRBackend()
        calls = {"n": 0}

        @cache(backend=backend, ttl=60, stale_ttl=120, l1_enabled=False)
        async def compute(x: int) -> dict[str, Any]:
            calls["n"] += 1
            return {"x": x, "call": calls["n"]}

        assert (await compute(1))["call"] == 1
        assert backend.set_calls == [(60, 120)]  # miss-path store carries the window

        backend.stale = True
        stale_result = await compute(1)
        assert stale_result["call"] == 1  # stale value served, no synchronous recompute

        assert await _await_for(lambda: len(backend.set_calls) == 2)
        assert backend.set_calls[1] == (60, 120)  # revalidation PUT re-sends the window (spec)
        assert calls["n"] == 2
        assert backend.lock_attempts  # lease attempted

        backend.stale = False
        assert (await compute(1))["call"] == 2  # revalidated value now served fresh

    async def test_contested_lease_serves_stale_without_recompute(self) -> None:
        backend = FakeSWRBackend(grant_lock=False)
        calls = {"n": 0}

        @cache(backend=backend, ttl=60, stale_ttl=120, l1_enabled=False)
        async def compute() -> int:
            calls["n"] += 1
            return calls["n"]

        assert await compute() == 1
        backend.stale = True
        assert await compute() == 1  # stale served

        await asyncio.sleep(0.2)  # give a (wrong) recompute time to happen
        assert calls["n"] == 1  # contested lease: MUST NOT recompute (spec)
        assert len(backend.set_calls) == 1

    async def test_concurrent_stale_hits_single_flight(self) -> None:
        backend = FakeSWRBackend()
        calls = {"n": 0}
        started = asyncio.Event()

        @cache(backend=backend, ttl=60, stale_ttl=120, l1_enabled=False)
        async def compute() -> int:
            calls["n"] += 1
            started.set()
            await asyncio.sleep(0.1)  # keep the first revalidation in flight
            return calls["n"]

        assert await compute() == 1
        backend.stale = True
        results = await asyncio.gather(*(compute() for _ in range(5)))
        assert all(r == 1 for r in results)  # every caller got the stale value instantly

        assert await _await_for(lambda: len(backend.set_calls) == 2)
        await asyncio.sleep(0.15)
        assert calls["n"] == 2  # exactly ONE background recompute for 5 stale hits

    async def test_revalidation_failure_is_silent_and_leaves_entry(self) -> None:
        backend = FakeSWRBackend()
        calls = {"n": 0}

        @cache(backend=backend, ttl=60, stale_ttl=120, l1_enabled=False)
        async def compute() -> int:
            calls["n"] += 1
            if calls["n"] > 1:
                raise RuntimeError("recompute exploded")
            return calls["n"]

        assert await compute() == 1
        backend.stale = True
        assert await compute() == 1  # caller unaffected

        assert await _await_for(lambda: calls["n"] == 2)
        await asyncio.sleep(0.1)
        assert len(backend.set_calls) == 1  # failed revalidation stored nothing
        assert await compute() == 1  # stale keeps being served until evict_at


class TestSyncSWR:
    def test_stale_hit_serves_immediately_and_revalidates_on_daemon_thread(self) -> None:
        backend = FakeSWRBackend()
        calls = {"n": 0}

        @cache(backend=backend, ttl=60, stale_ttl=120, l1_enabled=False)
        def compute(x: int) -> dict[str, Any]:
            calls["n"] += 1
            return {"x": x, "call": calls["n"]}

        assert compute(1)["call"] == 1
        backend.stale = True
        assert compute(1)["call"] == 1  # stale served without blocking

        assert _wait_for(lambda: len(backend.set_calls) == 2)
        assert backend.set_calls[1] == (60, 120)
        assert calls["n"] == 2

    def test_concurrent_stale_hits_dedupe_in_process(self) -> None:
        backend = FakeSWRBackend()
        calls = {"n": 0}
        gate = threading.Event()

        @cache(backend=backend, ttl=60, stale_ttl=120, l1_enabled=False)
        def compute() -> int:
            calls["n"] += 1
            gate.wait(0.2)  # hold the first revalidation in flight
            return calls["n"]

        gate.set()
        assert compute() == 1
        gate.clear()
        backend.stale = True
        results = [compute() for _ in range(5)]
        gate.set()
        assert all(r == 1 for r in results)

        assert _wait_for(lambda: calls["n"] == 2)
        time.sleep(0.2)
        assert calls["n"] == 2  # per-process single-flight


class TestSWRConfig:
    def test_swr_by_default_resolves_stale_ttl_to_ttl(self) -> None:
        """io()-style preset: swr_by_default=True defaults the window to ttl."""
        backend = FakeSWRBackend()
        config = DecoratorConfig(backend=backend, ttl=60, swr_by_default=True)

        @cache(config=config)
        def compute() -> str:
            return "v"

        assert compute() == "v"
        assert backend.set_calls == [(60, 60)]  # window defaulted to ttl

    def test_stale_ttl_zero_opts_out_of_preset_default(self) -> None:
        backend = FakeSWRBackend()
        config = DecoratorConfig(backend=backend, ttl=60, stale_ttl=0, swr_by_default=True)

        @cache(config=config)
        def compute() -> str:
            return "v"

        assert compute() == "v"
        assert backend.set_calls == [(60, None)]  # no window: explicit opt-out

    def test_preset_default_caps_window_to_30_day_total(self) -> None:
        backend = FakeSWRBackend()
        ttl = _CAP - 100  # leaves only 100s of window headroom
        config = DecoratorConfig(backend=backend, ttl=ttl, swr_by_default=True)

        @cache(config=config)
        def compute() -> str:
            return "v"

        assert compute() == "v"
        assert backend.set_calls == [(ttl, 100)]

    def test_stale_ttl_without_ttl_raises(self) -> None:
        with pytest.raises(ConfigurationError, match="requires a positive ttl"):

            @cache(backend=FakeSWRBackend(), stale_ttl=120, l1_enabled=False)
            def f() -> None: ...

    def test_total_over_cap_raises(self) -> None:
        with pytest.raises(ConfigurationError, match="30-day"):

            @cache(backend=FakeSWRBackend(), ttl=2_000_000, stale_ttl=1_000_000, l1_enabled=False)
            def f() -> None: ...

    def test_non_swr_backend_raises(self) -> None:
        class PlainBackend:
            def get(self, k: str) -> None:
                return None

            def set(self, k: str, v: bytes, ttl: int | None = None) -> None: ...

            def delete(self, k: str) -> bool:
                return False

        with pytest.raises(ConfigurationError, match="SWR-capable"):

            @cache(backend=PlainBackend(), ttl=60, stale_ttl=120, l1_enabled=False)
            def f() -> None: ...

    def test_negative_stale_ttl_raises(self) -> None:
        with pytest.raises(ConfigurationError, match="non-negative"):

            @cache(backend=FakeSWRBackend(), ttl=60, stale_ttl=-5, l1_enabled=False)
            def f() -> None: ...
