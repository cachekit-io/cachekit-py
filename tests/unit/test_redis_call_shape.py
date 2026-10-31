"""Call shape of the async miss over the lockable Redis backend: which Redis round trips block the caller.

Each test runs against a real Redis through a redis-py client that reports every command, and every pipeline,
to the gate (tests/utils/call_shape_gate.py) before sending it. The pinned list is the round trips the caller
waits on, in order; the background list is what runs after it returns. A path that gains a blocking round
trip, or a background release that becomes blocking, changes the list and fails here.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
import redis
from redis.client import Pipeline
from redis.lock import Lock

from cachekit import cache
from cachekit.backends.redis.provider import PerRequestRedisBackend
from tests.utils.call_shape_gate import CallShapeGate, Shape

pytestmark = pytest.mark.unit

_HOLDER = b"another-process"


def _label(args: tuple[Any, ...]) -> str:
    """``SET`` for a write, ``SET NX`` for a lock attempt, else the command name."""
    name = str(args[0]).upper()
    return f"{name} NX" if name == "SET" and "NX" in args[3:] else name  # options follow name and value


class _GatedPipeline(Pipeline):
    gate: CallShapeGate | None = None

    def execute(self, raise_on_error: bool = True) -> list[Any]:
        if self.gate is not None:
            self.gate.observe("PIPELINE " + ",".join(_label(args) for args, _ in self.command_stack))
        return super().execute(raise_on_error)


class _GatedRedis(redis.Redis):
    """A redis-py client that reports each round trip to ``gate``, and can play another lock holder."""

    gate: CallShapeGate | None = None
    lock_held_for = 0  # lock attempts answered "held elsewhere"
    filled_by_holder: bytes | None = None  # stored under the key when that holder is first reported
    fail_next_reads = 0  # GETs that fail with a ConnectionError, as a dropped connection would

    def execute_command(self, *args: Any, **options: Any) -> Any:
        label = _label(args)
        if self.gate is not None:
            self.gate.observe(label)
        if label == "GET" and self.fail_next_reads:
            self.fail_next_reads -= 1
            raise redis.ConnectionError("dropped")
        if label == "SET NX" and self.lock_held_for:
            self.lock_held_for -= 1
            lock_name = args[1]
            super().execute_command("SET", lock_name, _HOLDER)
            if self.filled_by_holder is not None:
                super().execute_command("SET", lock_name.removesuffix(":lock"), self.filled_by_holder)
            try:
                return super().execute_command(*args, **options)
            finally:
                super().execute_command("DEL", lock_name)  # the holder finishes before the next attempt
        return super().execute_command(*args, **options)

    def pipeline(self, transaction: bool = True, shard_hint: Any = None) -> Pipeline:
        pipe = _GatedPipeline(self.connection_pool, self.response_callbacks, transaction, shard_hint)
        pipe.gate = self.gate
        return pipe


@pytest.fixture
def client(redis_backend: PerRequestRedisBackend) -> _GatedRedis:
    # redis_backend owns the Redis process and closes the shared pool; this client only rides it.
    client = _GatedRedis(connection_pool=redis_backend._client.connection_pool)
    client.script_load(Lock.LUA_RELEASE_SCRIPT)  # so a release is one EVALSHA, never a NOSCRIPT retry
    return client


@pytest.fixture
def backend(client: _GatedRedis) -> Iterator[PerRequestRedisBackend]:
    tenant = uuid.uuid4().hex
    yield PerRequestRedisBackend(client, tenant)
    client.gate = None
    if keys := list(client.scan_iter(match=f"t:{tenant}:*")):
        client.delete(*keys)


@pytest.fixture
def gate(client: _GatedRedis) -> CallShapeGate:
    client.gate = CallShapeGate()
    return client.gate


def _value_key(client: _GatedRedis, backend: PerRequestRedisBackend) -> str:
    (key,) = (k.decode() for k in client.scan_iter(match=f"{backend.key_prefix}*") if b":ck:reg:" not in k)
    return key


@pytest.mark.asyncio
class TestAsyncCallShape:
    async def test_cold_miss(self, backend: PerRequestRedisBackend, gate: CallShapeGate) -> None:
        """Uncontended, after a clean miss: read, lock, write. No double-check read; the release does not block."""

        @cache(backend=backend, ttl=60)
        async def fn(x: int) -> int:
            return x * 2

        shape = await gate.run_async(fn(1))
        assert shape == Shape(blocking=["GET", "SET NX", "PIPELINE SET,SADD,EXPIRE"], background=["EVALSHA"])

    async def test_locked_miss(self, backend: PerRequestRedisBackend, gate: CallShapeGate, client: _GatedRedis) -> None:
        """The lock is held elsewhere; its holder stores the value, then the caller wins the lock and reads it."""
        calls = 0

        @cache(backend=backend, ttl=60, l1_enabled=False)
        async def fn(x: int) -> int:
            nonlocal calls
            calls += 1
            return x * 2

        await gate.run_async(fn(1))
        client.gate = None  # the setup's own commands are no part of the call
        key = _value_key(client, backend)
        client.filled_by_holder = client.get(key)
        client.delete(key)
        client.gate, client.lock_held_for = gate, 1
        shape = await gate.run_async(fn(1))
        # A waited grant keeps the double-check read, which finds the holder's value: no recompute.
        assert shape == Shape(blocking=["GET", "SET NX", "SET NX", "GET"], background=["EVALSHA"])
        assert calls == 1

    async def test_failed_read_keeps_the_double_check(
        self, backend: PerRequestRedisBackend, gate: CallShapeGate, client: _GatedRedis
    ) -> None:
        """A failed read reports a miss, but the entry may be live: the post-lock read is its only retry."""

        @cache(backend=backend, ttl=60)
        async def fn(x: int) -> int:
            return x * 2

        client.fail_next_reads = 1
        shape = await gate.run_async(fn(1))
        assert shape == Shape(blocking=["GET", "SET NX", "GET", "PIPELINE SET,SADD,EXPIRE"], background=["EVALSHA"])

    async def test_acquire_lock_still_awaits_its_release(self, backend: PerRequestRedisBackend, gate: CallShapeGate) -> None:
        """The LockableBackend protocol's lock is unchanged: its release is done when the ``async with`` exits."""

        async def hold() -> None:
            async with backend.acquire_lock("k", timeout=5) as acquired:
                assert acquired

        shape = await gate.run_async(hold())
        assert shape == Shape(blocking=["SET NX", "EVALSHA"])


class TestBackgroundReleaseLands:
    """The fill lock's release runs after the caller returns, so it must survive the loop ending."""

    def test_when_the_miss_is_the_last_await_of_asyncio_run(self, backend: PerRequestRedisBackend, client: _GatedRedis) -> None:
        @cache(backend=backend, ttl=60)
        async def fn(x: int) -> int:
            return x * 2

        assert asyncio.run(fn(1)) == 2
        assert list(client.scan_iter(match=f"{backend.key_prefix}*:lock")) == []

    def test_inline_when_the_default_executor_is_already_shut_down(
        self, backend: PerRequestRedisBackend, client: _GatedRedis
    ) -> None:
        async def hold() -> tuple[bool, bool]:
            async with backend.acquire_fill_lock("k", timeout=5) as grant:
                await asyncio.get_running_loop().shutdown_default_executor()
            return grant

        assert asyncio.run(hold()) == (True, True)
        assert client.exists(f"{backend.key_prefix}k:lock") == 0
