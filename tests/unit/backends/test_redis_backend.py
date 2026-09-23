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

Regression coverage for LAB-4773: a provider-issued backend scopes each operation to the
calling context's tenant (see ``TestProviderIssuedBackendFollowsTheCallingTenant``).
"""

from __future__ import annotations

import asyncio
import contextvars
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock, patch

import pytest
import redis
from redis.commands.core import Script
from redis.connection import Encoder
from redis.lock import Lock

from cachekit.backends.redis import RedisBackend
from cachekit.backends.redis.provider import PerRequestRedisBackend, RedisBackendProvider, tenant_context


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

        with patch("redis.ConnectionPool.from_url") as mock_from_url, patch("redis.Redis"):
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

        with patch("redis.ConnectionPool.from_url") as mock_from_url, patch("redis.Redis"):
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

        # Pre-existing wart (separate issue): when CACHEKIT_REDIS_URL/REDIS_URL
        # is set, pydantic-settings rejects the redis_url *constructor kwarg*
        # as extra_forbidden (env alias consumes the field, name key left
        # over). Clear env so this test exercises the RedisBackend(config)
        # contract, not that wart.
        monkeypatch.delenv("CACHEKIT_REDIS_URL", raising=False)
        monkeypatch.delenv("REDIS_URL", raising=False)

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


class _FakeRedis:
    """Just enough of ``redis.Redis`` for ``redis.lock.Lock`` (SET NX PX plus the release
    script) and for a decorator's reads, writes and deletes (TTLs are not modelled).

    Guarded by a mutex because ``acquire_lock`` runs each attempt on an executor thread.
    """

    def __init__(self) -> None:
        self._store: dict[str, bytes] = {}
        self._mutex = threading.Lock()
        self.nx_attempts: list[float] = []  # monotonic time of every SET NX, i.e. every acquire attempt

    def get_encoder(self) -> Encoder:
        return Encoder("utf-8", "strict", False)

    def register_script(self, script: str) -> Script:
        return Script(self, script)

    def set(self, name: str, value: bytes, nx: bool = False, px: int | None = None) -> bool | None:
        with self._mutex:
            if nx:
                self.nx_attempts.append(time.monotonic())
            if nx and name in self._store:
                return None
            self._store[name] = value
            return True

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


@pytest.mark.unit
class TestProviderIssuedBackendFollowsTheCallingTenant:
    """LAB-4773: the decorator keeps one backend for the life of the process, so a
    provider-issued backend must scope each operation to the calling context's tenant."""

    @staticmethod
    def _as_tenant(tenant, fn, *args):
        token = tenant_context.set(tenant)
        try:
            return fn(*args)
        finally:
            tenant_context.reset(token)

    @staticmethod
    def _tenants(fake: _FakeRedis) -> set[str]:
        return {key.split(":", 2)[1] for key in fake._store}

    @pytest.mark.parametrize(
        ("tenant", "wire"),
        [("org:1", "org%3A1"), (b"acme", "acme"), (7, "7"), (uuid.UUID(int=1), "00000000-0000-0000-0000-000000000001")],
    )
    def test_tenant_ids_with_a_canonical_str_are_accepted(self, tenant, wire):
        shared = PerRequestRedisBackend(Mock(), "default", follow_context=True)
        assert PerRequestRedisBackend(Mock(), tenant).key_prefix == f"t:{wire}:"
        assert self._as_tenant(tenant, lambda: shared.key_prefix) == f"t:{wire}:"

    def test_tenant_ids_whose_str_is_not_canonical_are_refused(self):
        """str() of an arbitrary object (default repr embeds id()) could merge two tenants."""
        client = Mock()
        with pytest.raises(TypeError):
            PerRequestRedisBackend(client, object())  # type: ignore[arg-type]
        shared = PerRequestRedisBackend(client, "default", follow_context=True)
        with pytest.raises(TypeError):
            self._as_tenant(object(), shared.get, "k")
        client.get.assert_not_called()

    def test_get_backend_falls_back_to_the_tenant_current_at_the_call(self):
        with patch.object(redis.Redis, "ping"):
            provider = RedisBackendProvider("redis://localhost:6379")
        try:
            backend = self._as_tenant("tenant-x", provider.get_backend)
            # An empty context (a fresh worker thread) has no tenant: the call-time tenant is the fallback.
            assert contextvars.Context().run(lambda: backend.key_prefix) == "t:tenant-x:"
            assert self._as_tenant("tenant-y", lambda: backend.key_prefix) == "t:tenant-y:"
        finally:
            provider.close()

    def test_whole_function_invalidate_deletes_only_the_callers_entries_and_keeps_others_tracked(self):
        from cachekit import cache

        fake = _FakeRedis()

        @cache(ttl=60, backend=PerRequestRedisBackend(fake, "default", follow_context=True), l1_enabled=False)
        def lookup(x):
            return x

        self._as_tenant("tenant-a", lookup, 1)
        self._as_tenant("tenant-b", lookup, 1)

        self._as_tenant("tenant-a", lookup.invalidate_cache)
        assert self._tenants(fake) == {"tenant-b"}
        self._as_tenant("tenant-b", lookup.invalidate_cache)  # tenant-b's entry stayed tracked
        assert fake._store == {}

    async def test_async_whole_function_invalidate_deletes_only_the_callers_entries_and_keeps_others_tracked(self, monkeypatch):
        from cachekit import cache

        monkeypatch.setattr(Lock, "lua_release", None)  # bind the release script to this fake (see above)
        fake = _FakeRedis()

        @cache(ttl=60, backend=PerRequestRedisBackend(fake, "default", follow_context=True), l1_enabled=False)
        async def lookup(x):
            return x

        async def as_tenant(tenant, fn, *args):
            tenant_context.set(tenant)  # each create_task below runs this in its own context copy
            await fn(*args)

        await asyncio.create_task(as_tenant("tenant-a", lookup, 1))
        await asyncio.create_task(as_tenant("tenant-b", lookup, 1))

        await asyncio.create_task(as_tenant("tenant-a", lookup.ainvalidate_cache))
        assert self._tenants(fake) == {"tenant-b"}
        await asyncio.create_task(as_tenant("tenant-b", lookup.ainvalidate_cache))
        assert fake._store == {}
