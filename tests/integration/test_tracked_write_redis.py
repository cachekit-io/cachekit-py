"""The fused tracked write on real Redis: a decorated miss sends its SET and its key-registry SADD + EXPIRE
in one round trip (PerRequestRedisBackend._set_tracked), and on async in one thread hop.

Every count is compared with the separate-call path, reached by removing ``_set_tracked`` from the class: the
path a backend without it keeps, and the path every Redis-backed write took before the fused write.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable, Iterator
from typing import Any

import pytest
import redis

from cachekit import cache
from cachekit.backends.errors import BackendError
from cachekit.backends.redis.provider import PerRequestRedisBackend
from cachekit.cache_handler import StandardCacheHandler
from cachekit.decorators import write_behind as wb

pytestmark = pytest.mark.integration

REG = "ck:reg:ns:0123456789abcdef"
N = 5


@pytest.fixture
def client(redis_test_client: redis.Redis) -> redis.Redis:
    return redis_test_client


@pytest.fixture(params=["fused", "separate"])
def fused(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> bool:
    """Run a test on the fused path, then again on the separate-call path."""
    if request.param == "separate":
        monkeypatch.delattr(PerRequestRedisBackend, "_set_tracked")
    return request.param == "fused"


class _Unlockable(PerRequestRedisBackend):
    """No distributed lock: an async miss takes the unlocked write (the only one write-behind defers)."""

    acquire_lock = None  # type: ignore[assignment]


def _write_flushes(monkeypatch: pytest.MonkeyPatch) -> list[bytes]:
    """Every send to Redis that carries a cache write (a SET that is not a lock's SET NX) or a registry SADD.

    One send is one round trip: a pipeline packs all its commands into one send_packed_command.
    """
    flushes: list[bytes] = []
    send = redis.connection.AbstractConnection.send_packed_command

    def counting_send(self: Any, command: Any, check_health: bool = True) -> None:
        packed = b"".join(command) if isinstance(command, list) else command
        if b"\r\nSADD\r\n" in packed or (b"\r\nSET\r\n" in packed and b"\r\nNX\r\n" not in packed):
            flushes.append(packed)
        send(self, command, check_health)

    monkeypatch.setattr(redis.connection.AbstractConnection, "send_packed_command", counting_send)
    return flushes


def _tracked(client: redis.Redis) -> set[bytes]:
    """Members of every tracking set of tenant ``default``."""
    return {member for reg in client.keys("t:default:ck:reg:*") for member in client.smembers(reg)}


def _cache_keys(client: redis.Redis) -> set[bytes]:
    return {k[len(b"t:default:") :] for k in client.keys("t:default:*") if b":ck:reg:" not in k}


def _drain_writers(created: list[wb.WriteBehind]) -> None:
    for writer in created:
        assert writer.close(time.monotonic() + 5.0) == [], "a deferred write never finished"


@pytest.fixture
def writers(monkeypatch: pytest.MonkeyPatch) -> list[wb.WriteBehind]:
    """The write-behind writers the test creates, so it can wait for their writes."""
    created: list[wb.WriteBehind] = []
    init = wb.WriteBehind.__init__

    def recording_init(self: wb.WriteBehind, *args: Any, **kwargs: Any) -> None:
        init(self, *args, **kwargs)
        created.append(self)

    monkeypatch.setattr(wb.WriteBehind, "__init__", recording_init)
    return created


class TestOneRoundTripPerTrackedWrite:
    """N tracked writes: N round trips fused, 2N separate. Every key is stored and tracked either way."""

    def _check(self, client: redis.Redis, flushes: list[bytes], fused: bool) -> None:
        assert len(flushes) == (N if fused else 2 * N)
        assert len(_cache_keys(client)) == N
        assert _tracked(client) == _cache_keys(client)

    def test_sync_miss(self, client: redis.Redis, monkeypatch: pytest.MonkeyPatch, fused: bool) -> None:
        fn = cache(backend=PerRequestRedisBackend(client, "default"), ttl=300, l1_enabled=False)(lambda x: x)
        flushes = _write_flushes(monkeypatch)
        for x in range(N):
            fn(x)
        self._check(client, flushes, fused)

    def test_write_behind_writer(
        self, client: redis.Redis, monkeypatch: pytest.MonkeyPatch, fused: bool, writers: list[wb.WriteBehind]
    ) -> None:
        fn = cache(backend=PerRequestRedisBackend(client, "default"), ttl=300, l1_enabled=False, write_behind=True)(lambda x: x)
        flushes = _write_flushes(monkeypatch)
        for x in range(N):
            fn(x)
        _drain_writers(writers)
        self._check(client, flushes, fused)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("backend_class", [PerRequestRedisBackend, _Unlockable], ids=["locked", "unlocked"])
    async def test_async_miss(
        self, client: redis.Redis, monkeypatch: pytest.MonkeyPatch, fused: bool, backend_class: type
    ) -> None:
        async def lookup(x: int) -> int:
            return x

        fn = cache(backend=backend_class(client, "default"), ttl=300, l1_enabled=False)(lookup)
        flushes = _write_flushes(monkeypatch)
        for x in range(N):
            await fn(x)
        self._check(client, flushes, fused)


class _CappedTTL(PerRequestRedisBackend):
    """A subclass whose ``set`` caps every TTL at 60 s: the fused write must not bypass it."""

    calls = 0

    def set(self, key: str, value: bytes, ttl: int | None = None) -> None:
        self.calls += 1
        super().set(key, value, 60)


class _OwnTracking(PerRequestRedisBackend):
    """A subclass whose ``track_key`` does more than the registry write: the fused write must not bypass it."""

    calls = 0

    def track_key(self, registry_id: str, key: str) -> None:
        self.calls += 1
        super().track_key(registry_id, key)


class TestSubclassOverridesKeepTheSeparateCalls:
    """A subclass's own set or track_key runs on every write: N override calls, 2N round trips, its TTL kept."""

    @pytest.mark.parametrize(
        ("backend_class", "max_ttl"), [(_CappedTTL, 60), (_OwnTracking, 300)], ids=["own-set", "own-track-key"]
    )
    def test_sync_miss(self, client: redis.Redis, monkeypatch: pytest.MonkeyPatch, backend_class: Any, max_ttl: int) -> None:
        backend = backend_class(client, "default")
        fn = cache(backend=backend, ttl=300, l1_enabled=False)(lambda x: x)
        flushes = _write_flushes(monkeypatch)
        for x in range(N):
            fn(x)
        assert backend.calls == N
        assert len(flushes) == 2 * N
        assert _tracked(client) == _cache_keys(client)
        assert all(max_ttl - 10 < client.ttl(f"t:default:{key.decode()}") <= max_ttl for key in _cache_keys(client))

    @pytest.mark.asyncio
    async def test_async_miss(self, client: redis.Redis) -> None:
        async def lookup(x: int) -> int:
            return x

        backend = _CappedTTL(client, "default")
        fn = cache(backend=backend, ttl=300, l1_enabled=False)(lookup)
        for x in range(N):
            await fn(x)
        assert backend.calls == N
        assert all(50 < client.ttl(f"t:default:{key.decode()}") <= 60 for key in _cache_keys(client))


class TestOneThreadHopPerAsyncTrackedWrite:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("backend_class", [PerRequestRedisBackend, _Unlockable], ids=["locked", "unlocked"])
    async def test_fused_saves_one_hop_per_write(
        self, client: redis.Redis, monkeypatch: pytest.MonkeyPatch, backend_class: type
    ) -> None:
        """The same N misses, fused then separate: the separate path takes exactly N more hops, the track's."""

        async def lookup(x: int) -> int:
            return x

        hops: list[Callable[..., Any]] = []
        to_thread = asyncio.to_thread

        async def counting_to_thread(func: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Any:
            hops.append(func)
            return await to_thread(func, *args, **kwargs)

        monkeypatch.setattr(asyncio, "to_thread", counting_to_thread)

        async def hops_for(namespace: str) -> int:
            fn = cache(backend=backend_class(client, "default"), ttl=300, l1_enabled=False, namespace=namespace)(lookup)
            hops.clear()
            for x in range(N):
                await fn(x)
            return len(hops)

        fused_hops = await hops_for("fused")
        with monkeypatch.context() as m:
            m.delattr(PerRequestRedisBackend, "_set_tracked")
            separate_hops = await hops_for("separate")
        assert separate_hops - fused_hops == N
        assert len(_tracked(client)) == 2 * N


@pytest.fixture
def set_denied(client: redis.Redis) -> Iterator[redis.Redis]:
    """A client whose ACL user may run everything but SET: the SET of a fused write gets an error reply while
    its SADD and EXPIRE run."""
    client.acl_setuser("no-set", enabled=True, nopass=True, keys=["*"], commands=["+@all", "-set"])
    pool = client.connection_pool
    denied = redis.Redis(
        connection_pool=redis.ConnectionPool(
            connection_class=pool.connection_class, **{**pool.connection_kwargs, "username": "no-set", "password": None}
        )
    )
    yield denied
    denied.close()
    client.acl_deluser("no-set")


class TestFusedWriteErrors:
    def test_set_error_reply_is_not_stored_and_never_raises(self, client: redis.Redis, set_denied: redis.Redis) -> None:
        handler = StandardCacheHandler(PerRequestRedisBackend(set_denied, "default"))
        track_errors: list[Exception] = []
        stored = handler.set_tracked("k", b"v", 60, REG, lambda _key, e: track_errors.append(e))
        assert stored is False
        assert track_errors == []
        assert not client.exists("t:default:k")

    def test_set_error_reply_through_the_decorator(self, client: redis.Redis, set_denied: redis.Redis) -> None:
        fn = cache(backend=PerRequestRedisBackend(set_denied, "default"), ttl=300, l1_enabled=False)(lambda x: x * 2)
        assert fn(3) == 6  # the write failed; the caller still gets its value
        assert _cache_keys(client) == set()

    def test_registry_error_reply_is_stored_and_warns(
        self, client: redis.Redis, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fn = cache(backend=PerRequestRedisBackend(client, "default"), ttl=300, l1_enabled=False)(lambda x: x)
        fn(0)  # creates the registry set; turn it into a string so the next SADD gets WRONGTYPE
        [reg] = client.keys("t:default:ck:reg:*")
        client.delete(reg)
        client.set(reg, b"not a set")
        flushes = _write_flushes(monkeypatch)
        with caplog.at_level(logging.WARNING, logger="cachekit.decorators.wrapper"):
            assert fn(1) == 1
        assert len(flushes) == 1  # the SADD error came back from the fused pipeline, not a separate track_key
        assert len(_cache_keys(client)) == 2  # stored despite the failed SADD
        assert any("Key tracking failed" in r.getMessage() for r in caplog.records)

    def test_backend_returns_the_registry_error(self, client: redis.Redis) -> None:
        client.set(f"t:default:{REG}", b"not a set")
        error = PerRequestRedisBackend(client, "default")._set_tracked("k", b"v", 60, REG)
        assert isinstance(error, BackendError)
        assert client.get("t:default:k") == b"v"

    def test_connection_drop_is_not_stored(self, client: redis.Redis, monkeypatch: pytest.MonkeyPatch) -> None:
        def dropped(self: Any, raise_on_error: bool = True) -> list[Any]:
            raise redis.exceptions.ConnectionError("Connection reset by peer")

        monkeypatch.setattr(redis.client.Pipeline, "execute", dropped)
        handler = StandardCacheHandler(PerRequestRedisBackend(client, "default"))
        track_errors: list[Exception] = []
        assert handler.set_tracked("k", b"v", 60, REG, lambda _key, e: track_errors.append(e)) is False
        assert track_errors == []

    @pytest.mark.parametrize(("ttl", "expected"), [(None, -1), (0, -1), (-5, -1), (60, 60)])
    def test_ttl_none_or_not_positive_sends_no_expiry(self, client: redis.Redis, ttl: int | None, expected: int) -> None:
        assert PerRequestRedisBackend(client, "default")._set_tracked("k", b"v", ttl, REG) is None
        assert client.ttl("t:default:k") == expected
        assert 604_000 < client.ttl(f"t:default:{REG}") <= 604_800


class TestPhantomMember:
    def test_member_left_by_a_failed_set_is_drained_without_error(self, client: redis.Redis, set_denied: redis.Redis) -> None:
        """A fused write whose SET failed still ran its SADD: the next drain unlinks the missing key and empties the set."""
        with pytest.raises(BackendError):
            PerRequestRedisBackend(set_denied, "default")._set_tracked("ghost", b"v", 60, REG)
        assert client.smembers(f"t:default:{REG}") == {b"ghost"}
        assert PerRequestRedisBackend(client, "default").drain_tracked(REG, set()) == {"ghost"}
        assert not client.exists(f"t:default:{REG}")
