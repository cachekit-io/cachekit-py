"""Unit tests for the Redis connection-pool configuration and RedisBackend.get() contract.

These are mocked (no real Redis) and live under tests/unit/ so they run on pull
requests — unlike the real-Redis suite in tests/integration/test_redis_backend.py,
which CI only runs on push-to-main.

Regression coverage for #154: the shared pools must use decode_responses=False so
binary payloads (LZ4 / Arrow IPC / AES-256-GCM ciphertext) are never UTF-8 decoded,
and RedisBackend.get() must return those raw bytes (or None) without coercion.

Regression coverage for the distributed-lock executor stall: ``acquire_lock`` must
not hold an executor thread while a waiter polls (see
``TestRedisLockWaitersDoNotPinExecutorThreads``).

Key registry control flow (``track_key`` / ``drain_tracked``) against a mocked client:
see ``TestKeyRegistryControlFlow``. The drain script itself runs on real Redis in
tests/integration/test_key_registry_redis.py.

Regression coverage for LAB-4773: a provider-issued backend scopes each operation to the
calling context's tenant (see ``TestProviderIssuedBackendFollowsTheCallingTenant``).

Regression coverage for LAB-5713: an unsupported tenant id type raises through ``@cache`` on both
paths, never degraded or counted by the circuit breaker
(see ``TestUnsupportedTenantIdRaisesThroughTheDecorator``).
"""

from __future__ import annotations

import asyncio
import contextvars
import enum
import inspect
import logging
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import Mock, call, patch

import pytest
import redis
from redis.commands.core import Script
from redis.connection import Encoder
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import LockNotOwnedError
from redis.lock import Lock

from cachekit import cache
from cachekit.backends.errors import BackendError
from cachekit.backends.redis import RedisBackend
from cachekit.backends.redis import provider as provider_module
from cachekit.backends.redis.provider import PerRequestRedisBackend, RedisBackendProvider
from tests.fixtures.tenant import as_tenant


@pytest.mark.unit
class TestRedisPoolDecodeResponses:
    """The shared sync and async pools must be created with decode_responses=False."""

    @staticmethod
    def _reset(monkeypatch):
        import cachekit.backends.redis.client as rc
        from cachekit.config.singleton import reset_settings

        monkeypatch.setenv("CACHEKIT_REDIS_URL", "redis://localhost:6379")
        rc._pool_instance = None
        rc._async_pool_instance = None
        # get_cached_redis_client() short-circuits on the thread-local client,
        # so a client cached by an earlier test would skip pool creation.
        if hasattr(rc._thread_local, "sync_client"):
            rc._thread_local.sync_client = None
        reset_settings()
        return rc

    def test_sync_pool_uses_decode_responses_false(self, monkeypatch):
        rc = self._reset(monkeypatch)
        from cachekit.config.singleton import reset_settings

        with patch("redis.BlockingConnectionPool.from_url") as mock_from_url, patch("redis.Redis"):
            try:
                rc.get_cached_redis_client()
            finally:
                rc._pool_instance = None
                reset_settings()
        assert mock_from_url.call_args.kwargs["decode_responses"] is False

    async def test_async_pool_uses_decode_responses_false(self, monkeypatch):
        rc = self._reset(monkeypatch)
        from cachekit.config.singleton import reset_settings

        with patch("redis.asyncio.ConnectionPool.from_url") as mock_from_url, patch("redis.asyncio.Redis"):
            try:
                await rc.get_async_redis_client()
            finally:
                rc._async_pool_instance = None
                reset_settings()
        assert mock_from_url.call_args.kwargs["decode_responses"] is False

    def test_sync_pool_has_finite_socket_timeouts(self, monkeypatch):
        """#222: an unreachable Redis must fail fast, not block on the OS TCP timeout."""
        rc = self._reset(monkeypatch)
        from cachekit.config.singleton import reset_settings

        with patch("redis.BlockingConnectionPool.from_url") as mock_from_url, patch("redis.Redis"):
            try:
                rc.get_cached_redis_client()
            finally:
                rc._pool_instance = None
                reset_settings()
        assert mock_from_url.call_args.kwargs["socket_timeout"] == 5.0
        assert mock_from_url.call_args.kwargs["socket_connect_timeout"] == 5.0

    async def test_async_pool_has_finite_socket_timeouts(self, monkeypatch):
        rc = self._reset(monkeypatch)
        from cachekit.config.singleton import reset_settings

        with patch("redis.asyncio.ConnectionPool.from_url") as mock_from_url, patch("redis.asyncio.Redis"):
            try:
                await rc.get_async_redis_client()
            finally:
                rc._async_pool_instance = None
                reset_settings()
        assert mock_from_url.call_args.kwargs["socket_timeout"] == 5.0
        assert mock_from_url.call_args.kwargs["socket_connect_timeout"] == 5.0

    def test_socket_timeouts_configurable_via_env(self, monkeypatch):
        from cachekit.backends.redis.config import RedisBackendConfig

        monkeypatch.setenv("CACHEKIT_SOCKET_TIMEOUT", "1.5")
        monkeypatch.setenv("CACHEKIT_SOCKET_CONNECT_TIMEOUT", "0.7")
        config = RedisBackendConfig.from_env()
        assert config.socket_timeout == 1.5
        assert config.socket_connect_timeout == 0.7


@pytest.mark.unit
@pytest.mark.parametrize("keepalive", [True, False])
@pytest.mark.parametrize("build", ["create_connection_pool", "create_async_connection_pool"])
class TestRedisPoolSocketKeepalive:
    """RedisBackendConfig.socket_keepalive must reach every TCP pool, and never a unix:// one.

    redis-py's UnixDomainSocketConnection rejects socket_keepalive with a TypeError (with
    either value) when the pool makes a connection, so passing it unconditionally would break
    every unix-socket user. Pools connect lazily, so none of this needs a Redis server.
    """

    @staticmethod
    def _pool(build, url, keepalive):
        import cachekit.backends.redis.client as rc
        from cachekit.backends.redis.config import RedisBackendConfig

        return getattr(rc, build)(url, RedisBackendConfig(socket_keepalive=keepalive))

    @pytest.mark.parametrize("url", ["redis://localhost:6379/0", "rediss://localhost:6380/0"])
    def test_tcp_pool_carries_configured_keepalive(self, build, keepalive, url):
        pool = self._pool(build, url, keepalive)
        assert pool.connection_kwargs["socket_keepalive"] is keepalive

    def test_url_query_option_overrides_config(self, build, keepalive):
        """Documented precedence: redis-py's from_url lets querystring options win over kwargs."""
        pool = self._pool(build, f"redis://localhost:6379/0?socket_keepalive={int(not keepalive)}", keepalive)
        assert pool.connection_kwargs["socket_keepalive"] is (not keepalive)

    def test_unix_pool_makes_connections(self, build, keepalive):
        pool = self._pool(build, "unix:///tmp/cachekit-no-such.sock?db=0", keepalive)
        assert "socket_keepalive" not in pool.connection_kwargs
        pool.make_connection()  # raised TypeError when the kwarg leaked through


@pytest.mark.unit
class TestRedisPoolSizing:
    """Every Redis pool path is sized by RedisBackendConfig.connection_pool_size (default 50).

    The default executor runs min(32, cpu_count + 4) L2 operations at once, so a smaller
    pool raised on ordinary async load. The live wait/timeout behaviour is covered against
    a real Redis in tests/integration/test_redis_backend.py.
    """

    @pytest.fixture(autouse=True)
    def _env(self, monkeypatch):
        for var in (
            "CACHEKIT_CONNECTION_POOL_SIZE",
            "CACHEKIT_API_KEY",
            "CACHEKIT_MEMCACHED_SERVERS",
            "CACHEKIT_FILE_CACHE_DIR",
        ):
            monkeypatch.delenv(var, raising=False)

    def test_default_pool_size_is_50(self):
        from cachekit.backends.redis.config import RedisBackendConfig

        assert RedisBackendConfig().connection_pool_size == 50

    def test_explicit_backend_default_pool(self):
        assert RedisBackend(redis_url="redis://localhost:6379")._client_provider._pool.max_connections == 50

    @pytest.mark.parametrize(("env_size", "expected"), [(None, 50), ("3", 3)])
    def test_env_resolved_backend_pool_follows_config(self, monkeypatch, env_size, expected):
        from cachekit.backends.provider import DefaultBackendProvider

        monkeypatch.setenv("CACHEKIT_REDIS_URL", "redis://localhost:6379")
        if env_size is not None:
            monkeypatch.setenv("CACHEKIT_CONNECTION_POOL_SIZE", env_size)
        with patch.object(redis.Redis, "ping"):
            backend = DefaultBackendProvider().get_backend()
        assert backend._client.connection_pool.max_connections == expected

    def test_provider_pool_size_argument_overrides_config(self, monkeypatch):
        monkeypatch.setenv("CACHEKIT_CONNECTION_POOL_SIZE", "3")
        with patch.object(redis.Redis, "ping"):
            provider = RedisBackendProvider("redis://localhost:6379", pool_size=7)
        assert provider._pool.max_connections == 7

    @pytest.mark.parametrize(
        ("url", "expected"),
        [("redis://localhost:6379", 1.5), ("redis://localhost:6379?socket_timeout=0.1", 0.1)],
    )
    def test_sync_pool_waits_up_to_the_effective_socket_timeout(self, url, expected):
        """A URL query option overrides the config's socket_timeout, and so bounds the pool wait too."""
        from cachekit.backends.redis.client import create_connection_pool
        from cachekit.backends.redis.config import RedisBackendConfig

        pool = create_connection_pool(url, RedisBackendConfig(socket_timeout=1.5))
        assert isinstance(pool, redis.BlockingConnectionPool)
        assert pool.timeout == pool.connection_kwargs["socket_timeout"] == expected

    @pytest.mark.parametrize(("method", "args", "command"), [("get_ttl", ("k",), "ttl"), ("refresh_ttl", ("k", 60), "expire")])
    async def test_ttl_commands_run_off_the_event_loop(self, method, args, command):
        """A full pool makes a sync command wait up to socket_timeout; on the loop thread it would stall every coroutine."""
        threads = []
        client = Mock()
        getattr(client, command).side_effect = lambda *a: threads.append(threading.get_ident()) or 1
        await getattr(PerRequestRedisBackend(client, "tenant"), method)(*args)
        assert threads and threads[0] != threading.get_ident()

    def test_cachekitio_keeps_its_own_default(self, monkeypatch):
        """CACHEKIT_CONNECTION_POOL_SIZE also sizes the CachekitIO HTTP pool, whose default stays 10."""
        from cachekit.backends.cachekitio.config import CachekitIOBackendConfig

        monkeypatch.setenv("CACHEKIT_API_KEY", "ck_test_123")  # pragma: allowlist secret
        assert CachekitIOBackendConfig.from_env().connection_pool_size == 10


@pytest.mark.unit
class TestRedisBackendProviderResolution:
    """#222 regression: RedisBackend honours redis_url and works zero-config.

    Construction never touches the network (pools connect lazily), so these
    run without a real Redis.
    """

    def test_explicit_url_wins_over_env(self, monkeypatch):
        """A URL argument that differs from env must connect to the argument."""
        from cachekit.backends.provider import PooledClientProvider

        monkeypatch.setenv("CACHEKIT_REDIS_URL", "redis://env-host:6379")
        backend = RedisBackend(redis_url="redis://arg-host:6390/3")

        provider = backend._client_provider
        assert isinstance(provider, PooledClientProvider)
        kwargs = provider._pool.connection_kwargs
        assert kwargs["host"] == "arg-host"
        assert kwargs["port"] == 6390
        assert kwargs["db"] == 3

    def test_explicit_url_never_consults_di_container(self):
        with patch("cachekit.backends.redis.backend.DIContainer") as mock_di:
            RedisBackend(redis_url="redis://arg-host:6379")
        mock_di.assert_not_called()

    def test_zero_config_without_registered_provider(self, monkeypatch):
        """The documented RedisBackend() example must not crash (#222 defect 2)."""
        from cachekit.backends.provider import CacheClientProvider, PooledClientProvider
        from cachekit.di import DIContainer

        monkeypatch.delenv("CACHEKIT_REDIS_URL", raising=False)
        monkeypatch.delenv("REDIS_URL", raising=False)

        # Simulate a fresh process: no CacheClientProvider registered
        # (the test conftest registers one; production code never does).
        container = DIContainer()
        saved_service = container._services.pop(CacheClientProvider, None)
        saved_singleton = container._singletons.pop(CacheClientProvider, None)
        try:
            backend = RedisBackend()
            assert isinstance(backend._client_provider, PooledClientProvider)
            assert backend._client_provider._pool.connection_kwargs["host"] == "localhost"
        finally:
            if saved_service is not None:
                container._services[CacheClientProvider] = saved_service
            if saved_singleton is not None:
                container._singletons[CacheClientProvider] = saved_singleton

    def test_zero_config_honours_di_registered_provider(self):
        """Back-compat: a DI-registered provider still wins for RedisBackend()."""
        from cachekit.backends.provider import CacheClientProvider
        from cachekit.di import DIContainer

        class _SentinelProvider(CacheClientProvider):
            pass

        container = DIContainer()
        saved_service = container._services.get(CacheClientProvider)
        saved_singleton = container._singletons.get(CacheClientProvider)
        container.register(CacheClientProvider, _SentinelProvider, singleton=False)
        try:
            backend = RedisBackend()
            assert isinstance(backend._client_provider, _SentinelProvider)
        finally:
            container._singletons.pop(CacheClientProvider, None)
            if saved_service is not None:
                container._services[CacheClientProvider] = saved_service
            else:
                container._services.pop(CacheClientProvider, None)
            if saved_singleton is not None:
                container._singletons[CacheClientProvider] = saved_singleton

    def test_explicit_client_provider_wins_over_url(self):
        """The radar workaround pattern: an injected provider is used as-is."""
        from cachekit.backends.provider import CacheClientProvider

        provider = Mock(spec=CacheClientProvider)
        backend = RedisBackend(redis_url="redis://ignored:6379", client_provider=provider)
        assert backend._client_provider is provider

    def test_config_object_accepted_positionally(self, monkeypatch):
        """docs/backends/redis.md promises RedisBackend(config) — honour it."""
        from cachekit.backends.provider import PooledClientProvider
        from cachekit.backends.redis.config import RedisBackendConfig

        monkeypatch.setenv("REDIS_URL", "redis://env-host:6379")

        config = RedisBackendConfig(
            redis_url="redis://cfg-host:7000",
            connection_pool_size=3,
            socket_timeout=1.5,
        )
        backend = RedisBackend(config)

        provider = backend._client_provider
        assert isinstance(provider, PooledClientProvider)
        assert provider._pool.connection_kwargs["host"] == "cfg-host"
        assert provider._pool.max_connections == 3
        assert provider._pool.connection_kwargs["socket_timeout"] == 1.5

    def test_per_instance_pool_has_finite_socket_timeouts(self):
        from cachekit.backends.provider import PooledClientProvider

        backend = RedisBackend(redis_url="redis://arg-host:6379")
        provider = backend._client_provider
        assert isinstance(provider, PooledClientProvider)
        kwargs = provider._pool.connection_kwargs
        assert kwargs["socket_timeout"] == 5.0
        assert kwargs["socket_connect_timeout"] == 5.0


@pytest.mark.unit
class TestRedisBackendGetContract:
    """get() returns raw bytes (or None) — never str, never UTF-8 decoded.

    Uses explicit client_provider injection (no DIContainer / env patching) so the
    tests are independent of REDIS_URL vs CACHEKIT_REDIS_URL alias resolution.
    """

    @staticmethod
    def _backend_returning(value):
        from cachekit.backends.provider import CacheClientProvider

        mock_client = Mock()
        mock_client.get.return_value = value
        provider = Mock(spec=CacheClientProvider)
        provider.get_sync_client.return_value = mock_client
        return RedisBackend("redis://localhost:6379", client_provider=provider)

    def test_get_returns_non_utf8_bytes_unchanged(self):
        backend = self._backend_returning(b"\x82\xa3val\xff\xfe")
        result = backend.get("k")
        assert result == b"\x82\xa3val\xff\xfe"
        assert isinstance(result, bytes)

    def test_get_returns_none_for_missing_key(self):
        backend = self._backend_returning(None)
        assert backend.get("missing") is None

    def test_get_returns_none_for_non_bytes_response(self):
        # decode_responses=False means this never happens in practice, but the
        # bytes|None narrowing guard must hold defensively (no str coercion).
        backend = self._backend_returning("unexpected-str")
        assert backend.get("k") is None


@pytest.mark.unit
class TestRedisTtlOpsRunOffTheLoop:
    """get_ttl and refresh_ttl call the sync client from a worker thread, so a Redis round trip
    never stalls the event loop (LAB-7074)."""

    @staticmethod
    def _backend_with(client: Mock) -> PerRequestRedisBackend:
        return PerRequestRedisBackend(client, tenant_id="default")

    async def test_get_ttl_and_refresh_ttl_leave_the_loop_thread(self):
        loop_thread = threading.current_thread()
        seen: list[threading.Thread] = []
        client = Mock()
        client.ttl.side_effect = lambda _k: seen.append(threading.current_thread()) or 42
        client.expire.side_effect = lambda _k, _t: seen.append(threading.current_thread()) or 1
        backend = self._backend_with(client)

        assert await backend.get_ttl("k") == 42
        assert await backend.refresh_ttl("k", 60) is True
        assert len(seen) == 2
        assert loop_thread not in seen


class _FakeRedis:
    """Just enough of ``redis.Redis`` for ``redis.lock.Lock`` (SET NX PX plus the release
    script) and for a decorator's reads, writes and deletes (TTLs are not modelled).

    Guarded by a mutex because ``acquire_lock`` runs each attempt on an executor thread.
    """

    def __init__(self) -> None:
        self._store: dict[str, bytes] = {}
        self._mutex = threading.Lock()
        self.nx_attempts: list[float] = []  # monotonic time of every SET NX, i.e. every acquire attempt
        # Test hooks for the cancellation-mid-attempt race: when set, an NX SET call
        # signals nx_entered (so the test knows the executor thread is inside the call),
        # blocks on block_nx until the test releases it, then signals nx_done once the
        # store write has actually landed — independent of whatever asyncio did with the
        # coroutine that was awaiting it.
        self.nx_entered: threading.Event | None = None
        self.block_nx: threading.Event | None = None
        self.nx_done: threading.Event | None = None
        self.nx_error: Exception | None = None  # raised by the NX SET once unblocked, in place of a result

    def get_encoder(self) -> Encoder:
        return Encoder("utf-8", "strict", False)

    def register_script(self, script: str) -> Script:
        return Script(self, script)

    def set(self, name: str, value: bytes, nx: bool = False, px: int | None = None) -> bool | None:
        if nx and self.nx_entered is not None:
            self.nx_entered.set()
        if nx and self.block_nx is not None:
            self.block_nx.wait()
        if nx and self.nx_error is not None:
            raise self.nx_error
        with self._mutex:
            if nx:
                self.nx_attempts.append(time.monotonic())
            if nx and name in self._store:
                result = None
            else:
                self._store[name] = value
                result = True
        if nx and self.nx_done is not None:
            self.nx_done.set()
        return result

    def setex(self, name: str, _ttl: int, value: bytes) -> bool:
        return bool(self.set(name, value))

    def get(self, name: str) -> bytes | None:
        with self._mutex:
            return self._store.get(name)

    def delete(self, name: str) -> int:
        with self._mutex:
            return int(self._store.pop(name, None) is not None)

    def evalsha(self, _sha: str, _numkeys: int, name: str, token: bytes) -> int:
        """The only script ``Lock`` runs here is LUA_RELEASE: delete iff the token still matches."""
        with self._mutex:
            if self._store.get(name) != token:
                return 0
            del self._store[name]
            return 1


async def _entered(event: threading.Event, what: str) -> None:
    """Poll a thread-side event from the loop; ``to_thread(event.wait)`` would take the very executor thread under test."""
    deadline = time.monotonic() + 2.0
    while not event.is_set():
        assert time.monotonic() < deadline, what
        await asyncio.sleep(0.01)


async def _acquire_once(backend: PerRequestRedisBackend) -> None:
    async with backend.acquire_lock("k", timeout=30.0, blocking_timeout=None):
        pass


@pytest.mark.unit
class TestRedisLockWaitersDoNotPinExecutorThreads:
    """A lock waiter must not hold an executor thread while it waits.

    ``acquire_lock`` used to run redis-py's *blocking* ``Lock.acquire`` inside
    ``asyncio.to_thread``. With more concurrent misses on one key than the default
    executor has threads (``min(32, cpu_count + 4)``, 8 when ``cpu_count`` is 4), every
    thread sat in a polling loop, the holder's own ``get``/``set``/``release`` (also
    ``to_thread`` calls) queued behind them, every waiter hit ``blocking_timeout`` and
    recomputed — a stampede from the feature that exists to prevent one. This pins the
    executor at 2 threads and runs 4 contenders: red on the blocking implementation
    (two waiters time out), green when the wait happens on the event loop.
    """

    @pytest.fixture(autouse=True)
    def _fresh_release_script(self, monkeypatch):
        # Lock caches its Script objects class-wide. An earlier test may have registered
        # lua_release against a MagicMock client, whose "release" never deletes our key;
        # reset it so register_scripts() binds it to this test's fake.
        monkeypatch.setattr(Lock, "lua_release", None)

    async def test_all_contenders_acquire_when_executor_is_smaller_than_contention(self):
        asyncio.get_running_loop().set_default_executor(ThreadPoolExecutor(max_workers=2))
        backend = PerRequestRedisBackend(_FakeRedis(), tenant_id="t")

        async def contend() -> bool:
            async with backend.acquire_lock("k", timeout=30.0, blocking_timeout=2.0) as acquired:
                await asyncio.sleep(0.05)  # the holder's compute
                return acquired

        results = await asyncio.gather(*(contend() for _ in range(4)))
        assert results == [True] * 4, f"waiters starved the executor and timed out: {results}"

    async def test_waiter_gives_up_with_false_when_lock_is_held_past_its_window(self):
        fake = _FakeRedis()
        backend = PerRequestRedisBackend(fake, tenant_id="t")
        holder_acquired = asyncio.Event()
        holder_released = asyncio.Event()
        blocking_timeout = 0.45

        async def hold() -> None:
            async with backend.acquire_lock("k", timeout=30.0, blocking_timeout=None) as acquired:
                assert acquired is True
                holder_acquired.set()
                await asyncio.sleep(0.8)  # longer than the waiter's window
            holder_released.set()

        async def wait() -> tuple[bool, bool, float]:
            await holder_acquired.wait()
            started = time.monotonic()
            async with backend.acquire_lock("k", timeout=30.0, blocking_timeout=blocking_timeout) as acquired:
                return acquired, holder_released.is_set(), started

        holder = asyncio.create_task(hold())
        acquired, holder_had_released, started = await wait()
        await holder

        assert acquired is False
        assert holder_had_released is False, "waiter must give up on its own deadline, not wait for the release"
        waiter_attempts = [t - started for t in fake.nx_attempts if t >= started]
        assert len(waiter_attempts) >= 2, f"a blocking waiter must retry before giving up: {waiter_attempts}"
        # Contract: no attempt lands past the deadline. Scheduling jitter only ever delays an
        # attempt, so the tolerance can hide a slightly late legitimate attempt but never an
        # extra one — that would land a full lock.sleep (0.1 s) later.
        assert max(waiter_attempts) <= blocking_timeout + 0.03, f"attempt past the deadline: {waiter_attempts}"

    async def test_non_blocking_acquire_makes_exactly_one_attempt(self):
        fake = _FakeRedis()
        backend = PerRequestRedisBackend(fake, tenant_id="t")

        async with backend.acquire_lock("k", timeout=30.0, blocking_timeout=None) as held:
            assert held is True
            async with backend.acquire_lock("k", timeout=30.0, blocking_timeout=None) as contended:
                assert contended is False

        assert len(fake.nx_attempts) == 2, "blocking_timeout=None must be a single SET NX per acquire_lock"

    async def test_cancellation_mid_attempt_releases_a_lock_it_goes_on_to_win(self):
        """Cancelling the awaiter while the SET NX round-trip is in flight must not orphan the key.

        ``asyncio.to_thread`` can't be interrupted once the executor thread starts the
        round-trip, so cancellation only stops the awaiting coroutine from seeing the
        result — not the thread from winning the lock. Red on the pre-fix code (the
        `try`/`finally` release block is never reached because the cancellation
        propagates straight out of the `while True` loop); green once the attempt is awaited uninterrupted.
        """
        fake = _FakeRedis()
        fake.nx_entered = threading.Event()
        fake.block_nx = threading.Event()
        fake.nx_done = threading.Event()
        backend = PerRequestRedisBackend(fake, tenant_id="t")

        task = asyncio.create_task(_acquire_once(backend))
        await _entered(fake.nx_entered, "executor thread never entered the SET NX call")

        task.cancel()
        fake.block_nx.set()  # let the executor thread finish the SET NX (it wins the lock)

        with pytest.raises(asyncio.CancelledError):
            await task

        # The executor thread runs independently of the cancelled coroutine, so wait for
        # its write to actually land before checking the store — otherwise the assertion
        # below races the background thread instead of testing the fix.
        assert fake.nx_done.wait(2.0), "executor thread never finished the SET NX"

        lock_name = backend._scoped_key("k") + ":lock"
        assert lock_name not in fake._store, "lock won after cancellation must still be released"

    async def test_second_cancellation_while_draining_the_attempt_still_releases_the_lock(self):
        """A cancel landing while the first one waits out the in-flight SET NX must not orphan the key.

        A plain ``asyncio.shield`` hands the *next* ``task.cancel()`` straight to the attempt
        itself: its result is lost and the release skipped.
        """
        fake = _FakeRedis()
        fake.nx_entered = threading.Event()
        fake.block_nx = threading.Event()
        fake.nx_done = threading.Event()
        backend = PerRequestRedisBackend(fake, tenant_id="t")

        task = asyncio.create_task(_acquire_once(backend))
        await _entered(fake.nx_entered, "executor thread never entered the SET NX call")

        task.cancel()
        await asyncio.sleep(0)  # first cancellation lands; acquire_lock is now waiting out the attempt
        task.cancel()
        fake.block_nx.set()  # the executor thread finishes the SET NX and wins the lock

        with pytest.raises(asyncio.CancelledError):
            await task
        assert fake.nx_done.wait(2.0), "executor thread never finished the SET NX"
        assert backend._scoped_key("k") + ":lock" not in fake._store, "lock won under repeated cancellation must be released"

    async def test_all_tasks_sweep_mid_attempt_still_releases_the_lock(self):
        """``asyncio.run()`` teardown cancels everything in ``all_tasks()``: a round-trip run as a Task dies under the drain.

        A plain executor future is invisible to that sweep, so the win is still read and released.
        """
        fake = _FakeRedis()
        fake.nx_entered = threading.Event()
        fake.block_nx = threading.Event()
        fake.nx_done = threading.Event()
        backend = PerRequestRedisBackend(fake, tenant_id="t")

        task = asyncio.create_task(_acquire_once(backend))
        await _entered(fake.nx_entered, "executor thread never entered the SET NX call")

        me = asyncio.current_task()
        for t in asyncio.all_tasks():  # what asyncio.run()'s _cancel_all_tasks does
            if t is not me:
                t.cancel()
        fake.block_nx.set()  # the executor thread finishes the SET NX and wins the lock

        with pytest.raises(asyncio.CancelledError):
            await task
        assert fake.nx_done.wait(2.0), "executor thread never finished the SET NX"
        assert backend._scoped_key("k") + ":lock" not in fake._store, "lock won during a shutdown sweep must be released"

    async def test_second_cancellation_while_the_release_is_queued_still_releases_the_lock(self):
        """A cancel landing while ``lock.release`` still waits for an executor thread must not orphan the key.

        With every executor thread busy — the saturation this class exists for — the release
        sits in the pool's queue, and a bare ``await to_thread(lock.release)`` lets the next
        ``task.cancel()`` cancel that queued work item, so the release never runs.
        """
        fake = _FakeRedis()
        backend = PerRequestRedisBackend(fake, tenant_id="t")
        pool = ThreadPoolExecutor(max_workers=1)
        asyncio.get_running_loop().set_default_executor(pool)
        holding = asyncio.Event()

        async def hold() -> None:
            async with backend.acquire_lock("k", timeout=30.0, blocking_timeout=None) as acquired:
                assert acquired
                holding.set()
                await asyncio.Event().wait()  # hold the lock until cancelled

        task = asyncio.create_task(hold())
        await asyncio.wait_for(holding.wait(), 2.0)

        busy = threading.Event()
        pool.submit(busy.wait)  # the only executor thread is now taken; the release will queue behind it
        task.cancel()
        await asyncio.sleep(0)  # first cancellation lands; the release is queued for the pool
        task.cancel()
        busy.set()

        with pytest.raises(asyncio.CancelledError):
            await task
        pool.shutdown(wait=True)  # whatever survived in the queue has run by now
        assert backend._scoped_key("k") + ":lock" not in fake._store, "lock must be released despite repeated cancellation"

    async def test_attempt_failing_during_cancellation_is_logged_not_raised(self, caplog):
        """A Redis error from the in-flight attempt must not replace the ``CancelledError``; it is logged instead."""
        fake = _FakeRedis()
        fake.nx_entered = threading.Event()
        fake.block_nx = threading.Event()
        fake.nx_error = RedisConnectionError("redis went away")
        backend = PerRequestRedisBackend(fake, tenant_id="t")

        task = asyncio.create_task(_acquire_once(backend))
        await _entered(fake.nx_entered, "executor thread never entered the SET NX call")
        task.cancel()
        fake.block_nx.set()  # the executor thread now fails the SET NX

        with pytest.raises(asyncio.CancelledError), caplog.at_level(logging.WARNING, logger="cachekit.backends.redis.provider"):
            await task
        # LAB-304: the log names the error by type only — never its text, never a traceback.
        assert any(
            r.levelno == logging.WARNING
            and RedisConnectionError.__name__ in r.getMessage()
            and "redis went away" not in r.getMessage()
            and r.exc_info is None
            for r in caplog.records
        ), "a failed attempt swallowed by cancellation must be logged, by type"

    async def test_cancellation_mid_attempt_that_loses_leaves_the_holders_lock_alone(self, caplog):
        """A cancelled attempt that loses to an existing holder has nothing to release: no release call, nothing logged."""
        fake = _FakeRedis()
        fake.nx_entered = threading.Event()
        fake.block_nx = threading.Event()
        backend = PerRequestRedisBackend(fake, tenant_id="t")
        lock_name = backend._scoped_key("k") + ":lock"
        fake._store[lock_name] = b"someone-else"

        task = asyncio.create_task(_acquire_once(backend))
        await _entered(fake.nx_entered, "executor thread never entered the SET NX call")
        task.cancel()
        fake.block_nx.set()  # the executor thread finishes the SET NX and loses

        with pytest.raises(asyncio.CancelledError), caplog.at_level(logging.DEBUG, logger="cachekit.backends.redis.provider"):
            await task
        assert not caplog.records, "a lost attempt must not try to release (a release without a token logs)"

    @pytest.mark.parametrize(
        ("error", "level"),
        [
            (RedisConnectionError("redis went away"), logging.WARNING),  # key orphaned until its TTL: worth a warning
            (LockNotOwnedError("expired"), logging.DEBUG),  # already gone or taken over: nothing to orphan
        ],
    )
    async def test_release_failing_in_redis_is_logged_not_raised(self, caplog, monkeypatch, error, level):
        """Redis failing the release is the one gap left: the caller sees no error, the key lives until its TTL."""
        fake = _FakeRedis()
        monkeypatch.setattr(fake, "evalsha", Mock(side_effect=error))
        backend = PerRequestRedisBackend(fake, tenant_id="t")

        with caplog.at_level(logging.DEBUG, logger="cachekit.backends.redis.provider"):
            async with backend.acquire_lock("k", timeout=30.0, blocking_timeout=None) as acquired:
                assert acquired

        assert backend._scoped_key("k") + ":lock" in fake._store, "a failed release leaves the key for its TTL"
        assert [r.levelno for r in caplog.records if "release" in r.getMessage()] == [level]

    async def test_decorator_runs_uncached_when_redis_goes_away_before_the_lock(self, caplog):
        """A ``SET NX`` failing after a successful call degrades the next miss: the function runs once,
        uncached, and the raw ``redis.ConnectionError`` never reaches the caller (LAB-5346)."""
        fake = _FakeRedis()
        runs: list[int] = []

        @cache(backend=PerRequestRedisBackend(fake, tenant_id="t"), ttl=60, l1_enabled=False, namespace="lab5346-redis")
        async def compute(x: int) -> int:
            runs.append(x)
            return x * 2

        assert await compute(1) == 2  # warm-up: Redis works
        fake.nx_error = RedisConnectionError("redis went away")
        with caplog.at_level(logging.DEBUG, logger="cachekit"):
            assert await compute(2) == 4

        assert runs == [1, 2]
        assert any("Lock operation failed" in r.getMessage() for r in caplog.records)
        cache_keys = [k for k in fake._store if not k.endswith(":lock")]
        print("KEYS", cache_keys, [k.split(":", 1)[-1] for k in cache_keys])
        formatter = logging.Formatter()
        for record in caplog.records:
            text = formatter.format(record)
            assert not any(k.split(":", 1)[-1] in text for k in cache_keys), f"{record.name} logged a cache key: {text}"


REG = "ck:reg:ns:0123456789abcdef"


def _drain_backend(*replies: list[bytes | str]) -> tuple[PerRequestRedisBackend, Mock, Mock]:
    """A tenant-``self`` backend on a Mock client whose drain script returns ``replies`` in turn."""
    client = Mock()
    script = Mock(side_effect=list(replies))
    client.register_script.return_value = script
    return PerRequestRedisBackend(client, "self"), client, script


@pytest.mark.unit
class TestKeyRegistryControlFlow:
    """What ``drain_tracked`` does with the script's replies: rounds, stragglers, logging."""

    def test_track_key_failure_is_classified(self):
        client = Mock()
        client.pipeline.side_effect = RedisConnectionError("down")
        with pytest.raises(BackendError) as exc_info:
            PerRequestRedisBackend(client, "self").track_key(REG, "k")
        assert exc_info.value.is_transient

    def test_rounds_until_short_chunk_then_unlinks_stragglers_in_batches(self, monkeypatch, caplog):
        monkeypatch.setattr(provider_module, "_DRAIN_CHUNK", 2)
        backend, client, script = _drain_backend([b"a", b"b"], ["c"], [])
        with caplog.at_level(logging.INFO, logger=provider_module.__name__):
            out = backend.drain_tracked(REG, ["a", "s1", "s2", "s3"])
        assert out == {"a", "b", "c", "s1", "s2", "s3"}  # str replies (decode_responses clients) pass through
        assert script.call_args_list == [call(keys=[f"t:self:{REG}"], args=["t:self:", 2])] * 2
        assert client.unlink.call_args_list == [call("t:self:s1", "t:self:s2"), call("t:self:s3")]
        assert [r.levelno for r in caplog.records if "drained 6 keys" in r.getMessage()] == [logging.INFO]

        assert backend.drain_tracked(REG, []) == set()
        client.register_script.assert_called_once_with(provider_module._DRAIN_SCRIPT)

    def test_undecodable_members_are_one_warning_without_bytes(self, caplog):
        backend, _, _ = _drain_backend([b"\xff\xfe", b"\xc3\x28", b"good"])
        with caplog.at_level(logging.WARNING, logger=provider_module.__name__):
            assert backend.drain_tracked(REG, []) == {"good"}
        (warning,) = [r for r in caplog.records if "undecodable" in r.getMessage()]
        assert "unlinked 2 undecodable members" in warning.getMessage()
        assert "\\xff" not in caplog.text and REG not in caplog.text

    def test_round_guard_stops_with_warning(self, monkeypatch, caplog):
        monkeypatch.setattr(provider_module, "_DRAIN_CHUNK", 1)
        monkeypatch.setattr(provider_module, "_DRAIN_MAX_ROUNDS", 2)
        monkeypatch.setattr(provider_module, "_DRAIN_WARN_KEYS", 1)
        backend, _, script = _drain_backend([b"a"], [b"b"])
        with caplog.at_level(logging.WARNING, logger=provider_module.__name__):
            assert backend.drain_tracked(REG, []) == {"a", "b"}
        assert script.call_count == 2
        assert "stopped after 2 rounds" in caplog.text
        assert [r.levelno for r in caplog.records if "drained 2 keys" in r.getMessage()] == [logging.WARNING]

    def test_script_failure_is_classified_and_skips_stragglers(self):
        backend, client, _ = _drain_backend(RedisConnectionError("lost"))
        with pytest.raises(BackendError) as exc_info:
            backend.drain_tracked(REG, ["s1"])
        assert exc_info.value.is_transient
        client.unlink.assert_not_called()


@pytest.mark.unit
class TestClassifyRedisErrorClusterDown:
    """ClusterDownError subclasses ResponseError, so its TRANSIENT branch must run before PERMANENT."""

    def test_cluster_down_is_transient(self):
        from redis.exceptions import ClusterDownError

        from cachekit.backends.errors import BackendErrorType
        from cachekit.backends.redis.error_handler import classify_redis_error

        error = classify_redis_error(ClusterDownError("CLUSTERDOWN The cluster is down"), operation="get")

        assert error.error_type == BackendErrorType.TRANSIENT

    def test_plain_response_error_stays_permanent(self):
        from redis.exceptions import ResponseError

        from cachekit.backends.errors import BackendErrorType
        from cachekit.backends.redis.error_handler import classify_redis_error

        error = classify_redis_error(
            ResponseError("WRONGTYPE Operation against a key holding the wrong kind of value"), operation="get"
        )

        assert error.error_type == BackendErrorType.PERMANENT


@pytest.mark.unit
class TestProviderIssuedBackendFollowsTheCallingTenant:
    """LAB-4773: the decorator keeps one backend for the life of the process, so a
    provider-issued backend must scope each operation to the calling context's tenant."""

    @staticmethod
    def _tenants(fake: _FakeRedis) -> set[str]:
        return {key.split(":", 2)[1] for key in fake._store}

    @pytest.mark.parametrize(
        ("tenant", "wire"),
        [
            ("org:1", "org%3A1"),
            (b"acme", "acme"),
            (7, "7"),
            (uuid.UUID(int=1), "00000000-0000-0000-0000-000000000001"),
            # asyncpg / uuid6 hand out uuid.UUID subclasses
            (type("DriverUUID", (uuid.UUID,), {})(int=1), "00000000-0000-0000-0000-000000000001"),
            # a subclass's __str__ override is ignored, so it cannot merge two tenants' prefixes
            (type("OpaqueUUID", (uuid.UUID,), {"__str__": lambda _: "same"})(int=1), "00000000-0000-0000-0000-000000000001"),
        ],
    )
    def test_accepted_tenant_ids_encode_to_their_canonical_form(self, tenant, wire):
        shared = PerRequestRedisBackend(Mock(), "default", follow_context=True)
        assert PerRequestRedisBackend(Mock(), tenant).key_prefix == f"t:{wire}:"
        with as_tenant(tenant):
            assert shared.key_prefix == f"t:{wire}:"

    @pytest.mark.parametrize(
        "tenant", [object(), True, False, enum.IntEnum("Org", "A").A], ids=["object", "True", "False", "IntEnum"]
    )
    def test_tenant_ids_whose_str_is_not_canonical_are_refused(self, tenant):
        """str() of an arbitrary object (default repr embeds id()) could merge two tenants; a bool or
        IntEnum tenant is a caller bug whose str() is not canonical (str(True) is 'True', an IntEnum's
        varies by Python version)."""
        client = Mock()
        with pytest.raises(TypeError):
            PerRequestRedisBackend(client, tenant)
        shared = PerRequestRedisBackend(client, "default", follow_context=True)
        with as_tenant(tenant), pytest.raises(TypeError):
            shared.get("k")
        client.get.assert_not_called()

    def test_a_context_without_a_tenant_falls_back_to_default_or_the_call_time_tenant(self):
        with patch.object(redis.Redis, "ping"):
            provider = RedisBackendProvider("redis://localhost:6379")
        try:
            # An empty context (e.g. a thread that inherited none) has no tenant: get_shared_backend()
            # falls back to "default", get_backend() to the tenant current at the call.
            with as_tenant("tenant-x"):
                shared = provider.get_shared_backend()
            assert contextvars.Context().run(lambda: shared.key_prefix) == "t:default:"
            with as_tenant("tenant-y"):
                assert shared.key_prefix == "t:tenant-y:"
            with as_tenant("tenant-x"):
                backend = provider.get_backend()
            assert contextvars.Context().run(lambda: backend.key_prefix) == "t:tenant-x:"
            with as_tenant("tenant-y"):
                assert backend.key_prefix == "t:tenant-y:"
        finally:
            provider.close()

    def test_whole_function_invalidate_deletes_only_the_callers_entries_and_keeps_others_tracked(self):
        from cachekit import cache

        fake = _FakeRedis()

        @cache(ttl=60, backend=PerRequestRedisBackend(fake, "default", follow_context=True), l1_enabled=False)
        def lookup(x):
            return x

        with as_tenant("tenant-a"):
            lookup(1)
        with as_tenant("tenant-b"):
            lookup(1)

        with as_tenant("tenant-a"):
            lookup.invalidate_cache()
        assert self._tenants(fake) == {"tenant-b"}
        with as_tenant("tenant-b"):
            lookup.invalidate_cache()  # tenant-b's entry stayed tracked
        assert fake._store == {}

    async def test_async_whole_function_invalidate_deletes_only_the_callers_entries_and_keeps_others_tracked(self, monkeypatch):
        from cachekit import cache

        monkeypatch.setattr(Lock, "lua_release", None)  # bind the release script to this fake (see above)
        fake = _FakeRedis()

        @cache(ttl=60, backend=PerRequestRedisBackend(fake, "default", follow_context=True), l1_enabled=False)
        async def lookup(x):
            return x

        with as_tenant("tenant-a"):
            await lookup(1)
        with as_tenant("tenant-b"):
            await lookup(1)

        with as_tenant("tenant-a"):
            await lookup.ainvalidate_cache()
        assert self._tenants(fake) == {"tenant-b"}
        with as_tenant("tenant-b"):
            await lookup.ainvalidate_cache()
        assert fake._store == {}


@pytest.mark.unit
class TestClassifyRedisErrorMisfiles:
    """TryAgainError subclasses ResponseError; InvalidResponse and LockError match no base branch."""

    def test_try_again_is_transient(self):
        exceptions = pytest.importorskip("redis.exceptions")
        if not hasattr(exceptions, "TryAgainError"):
            pytest.skip("redis-py lacks TryAgainError")

        from cachekit.backends.errors import BackendErrorType
        from cachekit.backends.redis.error_handler import classify_redis_error

        error = classify_redis_error(
            exceptions.TryAgainError("TRYAGAIN Multiple keys request during rehashing"), operation="get"
        )

        assert error.error_type == BackendErrorType.TRANSIENT

    @pytest.mark.parametrize("exc_name", ["InvalidResponse", "LockError"])
    def test_protocol_and_lock_errors_are_permanent(self, exc_name):
        import redis.exceptions

        from cachekit.backends.errors import BackendErrorType
        from cachekit.backends.redis.error_handler import classify_redis_error

        error = classify_redis_error(getattr(redis.exceptions, exc_name)("boom"), operation="get")

        assert error.error_type == BackendErrorType.PERMANENT


def _decorate(is_async: bool, calls: list, **options):
    """One @cache function per test, sync or async, recording each real execution in ``calls``."""
    from cachekit import cache

    if is_async:

        @cache(ttl=60, **options)
        async def lookup(x):
            calls.append(x)
            return x

    else:

        @cache(ttl=60, **options)
        def lookup(x):
            calls.append(x)
            return x

    return lookup


async def _call(fn, *args):
    result = fn(*args)
    return await result if inspect.isawaitable(result) else result


_BOTH_PATHS = pytest.mark.parametrize("is_async", [False, True], ids=["sync", "async"])


@pytest.fixture(params=["backend=", "shared-provider", "call-time-provider"])
def tenant_backend(request, monkeypatch):
    """How the function gets its tenant-scoped backend, a _FakeRedis behind each: ``backend=`` at
    decoration, or at its first call from a provider handing out RedisBackendProvider's
    get_shared_backend() (as env auto-detection does; the tenant is checked after resolving it) or
    get_backend() (which checks the tenant while building it). Yields (decorator options, client).

    tests/unit's conftest resets neither L1 nor the DI container, so this does both itself."""
    from cachekit.backends.provider import BackendProviderInterface
    from cachekit.config import decorator as decorator_config
    from cachekit.di import DIContainer
    from cachekit.l1_cache import get_l1_cache_manager

    monkeypatch.setattr(Lock, "lua_release", None)  # async misses take the lock: bind its script to this fake
    fake = _FakeRedis()
    options = {}
    if request.param == "backend=":
        options["backend"] = PerRequestRedisBackend(fake, "default", follow_context=True)
    else:
        with patch.object(redis.Redis, "ping"):
            provider = RedisBackendProvider("redis://localhost:6379")
        provider._client = fake
        get_backend = provider.get_shared_backend if request.param == "shared-provider" else provider.get_backend
        monkeypatch.setattr(decorator_config, "_default_backend", None)
        monkeypatch.setitem(DIContainer()._singletons, BackendProviderInterface, SimpleNamespace(get_backend=get_backend))
    get_l1_cache_manager().clear_all()
    yield options, fake
    get_l1_cache_manager().clear_all()


@pytest.fixture
def live_breakers(monkeypatch):
    """Every circuit breaker a decorator builds from here on, so a test can open it."""
    from cachekit.decorators import orchestrator

    built = []

    class Recorded(orchestrator.CircuitBreaker):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            built.append(self)

    monkeypatch.setattr(orchestrator, "CircuitBreaker", Recorded)
    return built


@pytest.mark.unit
class TestUnsupportedTenantIdRaisesThroughTheDecorator:
    """LAB-5713: a tenant id of a type _encode_tenant rejects is a caller bug, not a cache fault.

    Both wrappers must raise it before the function runs, however the function got its backend and
    whatever the breaker state. Degraded to an uncached call it counted a failure on the
    per-function breaker every tenant shares, so one bad caller turned caching off for all of them."""

    @_BOTH_PATHS
    @pytest.mark.parametrize("tenant", [1.5, True, object()], ids=["float", "bool", "object"])
    async def test_raises_before_the_function_runs(self, tenant_backend, caplog, is_async, tenant):
        options, fake = tenant_backend
        calls: list = []
        lookup = _decorate(is_async, calls, l1_enabled=False, **options)

        with as_tenant(tenant), pytest.raises(TypeError, match=f"not {type(tenant).__name__}$"):
            await _call(lookup, 1)

        assert calls == []
        assert fake._store == {}
        # Raised before any cache operation, so none is logged as a failed get / set.
        assert [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING] == []

    @_BOTH_PATHS
    async def test_breaker_other_tenants_share_stays_closed(self, tenant_backend, is_async):
        options, fake = tenant_backend
        calls: list = []
        lookup = _decorate(is_async, calls, l1_enabled=False, **options)

        for _ in range(6):  # one past the default failure threshold
            with as_tenant(1.5), pytest.raises(TypeError):
                await _call(lookup, 1)

        breaker = lookup.get_health_status()["circuit_breaker"]
        assert (breaker["state"], breaker["failure_count"]) == ("closed", 0)
        with as_tenant("tenant-b"):
            assert await _call(lookup, 1) == 1
        assert calls == [1]
        assert {key.split(":", 2)[1] for key in fake._store} == {"tenant-b"}

    @_BOTH_PATHS
    async def test_raises_while_the_breaker_is_open(self, tenant_backend, live_breakers, is_async):
        options, _ = tenant_backend
        calls: list = []
        lookup = _decorate(is_async, calls, l1_enabled=False, **options)
        with as_tenant("tenant-b"):
            await _call(lookup, 1)  # the function has its backend from here on
        (breaker,) = live_breakers
        for _ in range(breaker.config.failure_threshold):
            breaker.record_failure()
        assert lookup.get_health_status()["circuit_breaker"]["state"] == "open"

        with as_tenant(1.5), pytest.raises(TypeError):
            await _call(lookup, 2)

        assert calls == [1]

    @_BOTH_PATHS
    async def test_raises_on_an_l1_hit(self, tenant_backend, is_async):
        """L1 is shared by every tenant, so an entry tenant-b cached is there for any caller: the
        check runs before the L1 lookup once the function has its backend."""
        options, _ = tenant_backend
        calls: list = []
        lookup = _decorate(is_async, calls, **options)
        with as_tenant("tenant-b"):
            await _call(lookup, 1)

        with as_tenant(1.5), pytest.raises(TypeError):
            await _call(lookup, 1)

        assert calls == [1]

    @pytest.mark.parametrize("tenant_backend", ["shared-provider", "call-time-provider"], indirect=True)
    async def test_async_interop_call_raises_rather_than_degrading(self, tenant_backend):
        """The async interop path resolves the backend on a branch of its own. A tenant-scoped backend
        is refused under interop anyway (at decoration, given as ``backend=``); an unsupported tenant
        must not turn that into an uncached call."""
        options, _ = tenant_backend
        calls: list = []
        lookup = _decorate(True, calls, l1_enabled=False, interop="lookup", namespace="users", **options)

        with as_tenant(1.5), pytest.raises(TypeError):
            await _call(lookup, 1)

        assert calls == []
        breaker = lookup.get_health_status()["circuit_breaker"]
        assert (breaker["state"], breaker["failure_count"]) == ("closed", 0)


@pytest.mark.unit
class TestRedisDeleteMany:
    """RedisBackend._delete_many: one UNLINK per call, raises as a whole."""

    @staticmethod
    def _backend(client: Mock) -> RedisBackend:
        provider = Mock()
        provider.get_sync_client.return_value = client
        return RedisBackend(redis_url="redis://localhost:6379", client_provider=provider)

    def test_one_unlink_for_all_keys(self) -> None:
        client = Mock()
        client.unlink.return_value = 1  # absent keys are still deleted
        assert self._backend(client)._delete_many(["a", "b", "c"]) == set()
        client.unlink.assert_called_once_with("a", "b", "c")
        client.delete.assert_not_called()

    def test_empty_sends_nothing(self) -> None:
        client = Mock()
        assert self._backend(client)._delete_many([]) == set()
        client.unlink.assert_not_called()

    def test_failure_raises_backend_error(self) -> None:
        client = Mock()
        client.unlink.side_effect = ConnectionError("reset")
        with pytest.raises(BackendError):
            self._backend(client)._delete_many(["a"])
