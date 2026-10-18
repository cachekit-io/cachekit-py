"""Zero-config: with nothing configured, a plaintext decorator caches in L1 only, as backend=None does.

Nothing configured means no backend= (keyword or config=), no set_default_backend(), no non-default
backend provider, no CACHEKIT_* selector and no REDIS_URL. Such a decorator never dials Redis: it is
switched to L1-only at its first call. Every configured setup resolves its backend as before, and
encrypted and interop decorators keep resolving one with nothing configured.
"""

from __future__ import annotations

import inspect
import logging
import time
from collections.abc import Callable, Iterator
from typing import Any
from unittest import mock

import pytest

from cachekit import cache
from cachekit.backends.errors import BackendError, BackendErrorType
from cachekit.backends.provider import BackendProviderInterface, DefaultBackendProvider
from cachekit.config.decorator import DecoratorConfig, set_default_backend
from cachekit.config.nested import L1CacheConfig
from cachekit.di import DIContainer

_SELECTORS = ("CACHEKIT_API_KEY", "CACHEKIT_REDIS_URL", "CACHEKIT_MEMCACHED_SERVERS", "CACHEKIT_FILE_CACHE_DIR")
_ENV = (*_SELECTORS, "REDIS_URL")

# The presets that may run zero-config. secure (encryption), io (its own backend) and local (always
# L1-only) are out of scope.
_PRESETS: dict[str, Callable[..., Any]] = {
    "bare": lambda **kw: cache(**kw),
    "minimal": lambda **kw: cache.minimal(**kw),
    "production": lambda **kw: cache.production(**kw),
    "dev": lambda **kw: cache.dev(**kw),
    "test": lambda **kw: cache.test(**kw),
    "config": lambda **kw: cache(config=DecoratorConfig(**kw)),
}


class _MemoryBackend:
    """In-memory backend: proves a configured backend, not L1-only, served the call."""

    def __init__(self) -> None:
        self.store: dict[str, bytes] = {}

    def get(self, key: str) -> bytes | None:
        return self.store.get(key)

    def set(self, key: str, value: bytes, ttl: int | None = None, stale_ttl: int | None = None) -> None:
        self.store[key] = value

    def delete(self, key: str) -> bool:
        return self.store.pop(key, None) is not None

    def exists(self, key: str) -> bool:
        return key in self.store

    def health_check(self) -> tuple[bool, dict[str, Any]]:
        return True, {}


_counter = 0


def _named(fn: Callable[..., Any], label: str) -> Callable[..., Any]:
    """Give fn a qualname no other test uses: cache_info() counters are shared per module.qualname."""
    global _counter
    _counter += 1
    fn.__qualname__ = fn.__name__ = f"zc_{label}_{_counter}"
    return fn


def _state(wrapper: Callable[..., Any]) -> dict[str, Any]:
    """The wrapper's mode state, read from its closure (cache_info() does not expose it)."""
    names = inspect.getclosurevars(wrapper).nonlocals
    return {
        "l1_only": names["_l1_only_mode"],
        "object_cache": names["_object_cache"] is not None,
        "swr": names["_l1_swr_active"],
    }


@pytest.fixture
def nothing_configured(monkeypatch: pytest.MonkeyPatch) -> Iterator[mock.MagicMock]:
    """Clean slate: the production DefaultBackendProvider, no selector, no REDIS_URL, no default backend.

    Yields a spy on DefaultBackendProvider.get_backend that fails the test if it builds anything.
    """
    for var in _ENV:
        monkeypatch.delenv(var, raising=False)
    set_default_backend(None)
    container = DIContainer()
    saved = container._singletons.get(BackendProviderInterface)
    container._singletons[BackendProviderInterface] = DefaultBackendProvider()
    with mock.patch.object(
        DefaultBackendProvider, "get_backend", autospec=True, side_effect=AssertionError("get_backend() called")
    ) as spy:
        yield spy
    set_default_backend(None)
    if saved is None:
        container._singletons.pop(BackendProviderInterface, None)
    else:
        container._singletons[BackendProviderInterface] = saved


@pytest.fixture
def no_redis_built(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail on any Redis provider or client construction."""
    from cachekit.backends.redis import provider as redis_provider

    def refuse(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("Redis built with nothing configured")

    monkeypatch.setattr(redis_provider.RedisBackendProvider, "__init__", refuse)
    monkeypatch.setattr("redis.Redis.__init__", refuse)
    monkeypatch.setattr("redis.asyncio.Redis.__init__", refuse)


@pytest.mark.unit
@pytest.mark.usefixtures("nothing_configured", "no_redis_built")
class TestZeroConfigCaches:
    """AC1 + AC2: twenty calls with one argument run the function once; nothing dials Redis or warns."""

    @pytest.mark.parametrize("preset", list(_PRESETS))
    def test_sync_preset_runs_once(self, preset: str, caplog: pytest.LogCaptureFixture) -> None:
        calls = 0

        def fn(x: int) -> int:
            nonlocal calls
            calls += 1
            return x * 2

        wrapped = _PRESETS[preset]()(_named(fn, preset))
        with caplog.at_level(logging.WARNING, logger="cachekit"):
            for _ in range(20):
                assert wrapped(3) == 6

        assert calls == 1
        info = wrapped.cache_info()
        assert (info.misses, info.l1_hits, info.l2_hits) == (1, 19, 0)
        assert [r for r in caplog.records if r.name.startswith("cachekit") and r.levelno >= logging.WARNING] == []

    async def test_async_bare_runs_once(self, caplog: pytest.LogCaptureFixture) -> None:
        calls = 0

        async def fn(x: int) -> int:
            nonlocal calls
            calls += 1
            return x * 2

        wrapped = cache(_named(fn, "async"))
        with caplog.at_level(logging.WARNING, logger="cachekit"):
            for _ in range(20):
                assert await wrapped(3) == 6

        assert calls == 1
        info = wrapped.cache_info()
        assert (info.misses, info.l1_hits, info.l2_hits) == (1, 19, 0)
        assert [r for r in caplog.records if r.name.startswith("cachekit") and r.levelno >= logging.WARNING] == []


@pytest.mark.unit
@pytest.mark.usefixtures("nothing_configured", "no_redis_built")
class TestZeroConfigParity:
    """AC3: a zero-config function behaves as the same function written with backend=None."""

    @pytest.mark.parametrize("preset", list(_PRESETS))
    def test_mode_state_matches_backend_none(self, preset: str) -> None:
        """(a) The same L1 store and L1 SWR: on for production and dev at their default ttl, off for the rest."""
        zero = _PRESETS[preset]()(_named(lambda x: x, preset))
        explicit = _PRESETS[preset](backend=None)(_named(lambda x: x, f"{preset}_none"))
        zero(1)
        explicit(1)
        assert _state(zero) == _state(explicit)
        assert _state(zero)["l1_only"] is True
        assert _state(zero)["swr"] is (preset in ("production", "dev"))  # bare/config: no default ttl, no SWR

    def test_l1_swr_refreshes_in_background(self) -> None:
        calls = 0

        def fn() -> int:
            nonlocal calls
            calls += 1
            return calls

        wrapped = cache(ttl=2, l1=L1CacheConfig(swr_enabled=True, swr_threshold_ratio=0.2))(_named(fn, "swr"))
        assert wrapped() == 1
        time.sleep(1.0)
        assert wrapped() == 1  # stale served, refresh scheduled

        deadline = time.monotonic() + 2.0
        while calls < 2 and time.monotonic() < deadline:
            time.sleep(0.02)
        assert calls == 2

    @pytest.mark.parametrize("preset", list(_PRESETS))
    def test_values_held_by_reference(self, preset: str) -> None:
        """(c) No serialization: the cached object itself is returned, and tuples stay tuples."""
        wrapped = _PRESETS[preset]()(_named(lambda x: [x, (x,)], preset))
        first = wrapped(1)
        assert wrapped(1) is first
        assert first[1] == (1,)

    @pytest.mark.parametrize("preset", list(_PRESETS))
    def test_l1_disabled_caches_nothing(self, preset: str) -> None:
        """(d) l1_enabled=False: no cache anywhere, every call runs, and still no Redis."""
        calls = 0

        def fn(x: int) -> int:
            nonlocal calls
            calls += 1
            return x

        decorate = cache(config=DecoratorConfig(l1=L1CacheConfig(enabled=False))) if preset == "config" else None
        wrapped = (decorate or _PRESETS[preset](l1_enabled=False))(_named(fn, preset))
        for _ in range(3):
            wrapped(1)
        assert calls == 3

    @pytest.mark.parametrize("first_call", [False, True])
    def test_clear_invalidate_info_sync(self, first_call: bool) -> None:
        """(b) Before and after the first call."""
        calls = 0

        def fn(x: int) -> int:
            nonlocal calls
            calls += 1
            return x

        wrapped = cache(_named(fn, "sync_ops"))
        if first_call:
            wrapped(1)
        assert wrapped.cache_info().misses == int(first_call)
        wrapped.invalidate_cache(1)
        wrapped.cache_clear()
        assert wrapped.cache_info().misses == 0
        wrapped(1)
        wrapped(1)
        assert calls == 1 + int(first_call)
        assert wrapped.invalidate_cache(1) is not None  # NO_L2, as for backend=None
        wrapped(1)
        assert calls == 2 + int(first_call)

    @pytest.mark.parametrize("first_call", [False, True])
    async def test_clear_invalidate_info_async(self, first_call: bool) -> None:
        """(b) cache_clear() on an async zero-config function works as on backend=None (no TypeError)."""
        calls = 0

        async def fn(x: int) -> int:
            nonlocal calls
            calls += 1
            return x

        wrapped = cache(_named(fn, "async_ops"))
        if first_call:
            await wrapped(1)
        wrapped.cache_clear()
        await wrapped(1)
        await wrapped(1)
        assert calls == 1 + int(first_call)
        await wrapped.invalidate_cache(1)
        await wrapped(1)
        assert calls == 2 + int(first_call)

    def test_invalidation_outcome_matches_backend_none(self) -> None:
        zero = cache(_named(lambda x: x, "outcome"))
        explicit = cache(backend=None)(_named(lambda x: x, "outcome_none"))
        zero(1)
        explicit(1)
        assert zero.invalidate_cache(1) == explicit.invalidate_cache(1)


@pytest.mark.unit
class TestConfiguredResolutionUnchanged:
    """AC4: explicit backend=, set_default_backend(), a registered provider, one selector, REDIS_URL, then L1-only."""

    def test_explicit_backend_wins(self, nothing_configured: mock.MagicMock) -> None:
        backend = _MemoryBackend()
        wrapped = cache(backend=backend)(_named(lambda x: x, "explicit"))
        wrapped(1)
        assert backend.store
        nothing_configured.assert_not_called()

    def test_default_backend_set_after_decoration_wins(self, nothing_configured: mock.MagicMock) -> None:
        """AC5a: set_default_backend() between decoration and the first call is used (lazy resolution)."""
        wrapped = cache(_named(lambda x: x, "default_late"))
        backend = _MemoryBackend()
        set_default_backend(backend)
        wrapped(1)
        assert backend.store
        assert _state(wrapped)["l1_only"] is False

    def test_registered_provider_is_configuration(self, nothing_configured: mock.MagicMock) -> None:
        backend = _MemoryBackend()

        class _Provider(BackendProviderInterface):
            def get_backend(self) -> _MemoryBackend:
                return backend

        DIContainer()._singletons[BackendProviderInterface] = _Provider()
        wrapped = cache(_named(lambda x: x, "provider"))
        wrapped(1)
        assert backend.store

    def test_subclassed_default_provider_is_configuration(self, nothing_configured: mock.MagicMock) -> None:
        """Only the exact DefaultBackendProvider can report "nothing configured"."""
        backend = _MemoryBackend()

        class _Provider(DefaultBackendProvider):
            def get_backend(self) -> _MemoryBackend:
                return backend

        DIContainer()._singletons[BackendProviderInterface] = _Provider()
        wrapped = cache(_named(lambda x: x, "subprovider"))
        wrapped(1)
        assert backend.store

    @pytest.mark.parametrize("var", _ENV)
    def test_one_selector_resolves_a_backend(
        self, var: str, nothing_configured: mock.MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """AC4/AC5a: a selector set after decoration, before the first call, routes to get_backend()."""
        wrapped = cache(_named(lambda x: x, f"sel_{var}"))
        monkeypatch.setenv(var, "zc-cache-dir" if var == "CACHEKIT_FILE_CACHE_DIR" else "redis://example:6379")
        backend = _MemoryBackend()
        nothing_configured.side_effect = lambda self: backend
        wrapped(1)
        nothing_configured.assert_called_once()
        assert backend.store
        assert _state(wrapped)["l1_only"] is False

    def test_two_selectors_degrade_inside_the_guard(
        self,
        nothing_configured: mock.MagicMock,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
        live_breakers: list[Any],
    ) -> None:
        """Ambiguous selectors: the call runs uncached, logs and counts toward the breaker; the
        ConfigurationError never reaches the caller."""
        nothing_configured.side_effect = _real_get_backend
        monkeypatch.setenv("CACHEKIT_REDIS_URL", "redis://example:6379")
        monkeypatch.setenv("CACHEKIT_FILE_CACHE_DIR", "zc-cache-dir")
        calls = 0

        def fn(x: int) -> int:
            nonlocal calls
            calls += 1
            return x

        wrapped = cache(_named(fn, "ambiguous"))
        with caplog.at_level(logging.WARNING, logger="cachekit"):
            wrapped(1)
            wrapped(1)
        assert calls == 2
        assert nothing_configured.call_count == 2
        assert any("client_creation" in r.getMessage() for r in caplog.records)
        assert live_breakers[-1].failure_count == 2
        assert _state(wrapped)["l1_only"] is False


_real_get_backend = DefaultBackendProvider.get_backend


@pytest.mark.unit
@pytest.mark.usefixtures("no_redis_built")
class TestResolvedOnceAtFirstCall:
    """AC5b: once a first call has resolved a function to zero-config L1-only, it stays L1-only."""

    def test_config_after_first_call_does_not_move_it(
        self, nothing_configured: mock.MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls = 0

        def fn(x: int) -> int:
            nonlocal calls
            calls += 1
            return x

        wrapped = cache(_named(fn, "pinned"))
        wrapped(1)
        monkeypatch.setenv("REDIS_URL", "redis://example:6379")
        set_default_backend(_MemoryBackend())
        wrapped(1)
        assert calls == 1
        nothing_configured.assert_not_called()
        assert _state(wrapped)["l1_only"] is True

    def test_invalidation_before_first_call_settles_it(self, nothing_configured: mock.MagicMock) -> None:
        """invalidate_cache() pins the outcome, as it pins a resolved backend today."""
        wrapped = cache(_named(lambda x: x, "inv_first"))
        wrapped.invalidate_cache(1)
        assert _state(wrapped)["l1_only"] is True

    def test_cache_info_before_first_call_does_not_settle(self, nothing_configured: mock.MagicMock) -> None:
        wrapped = cache(_named(lambda x: x, "info_first"))
        wrapped.cache_info()
        backend = _MemoryBackend()
        set_default_backend(backend)
        wrapped(1)
        assert backend.store

    def test_concurrent_first_calls_settle_once(self, nothing_configured: mock.MagicMock) -> None:
        import threading

        calls = 0
        lock = threading.Lock()

        def fn(x: int) -> int:
            nonlocal calls
            with lock:
                calls += 1
            time.sleep(0.05)
            return x

        wrapped = cache(_named(fn, "concurrent"))
        barrier = threading.Barrier(8)

        def worker() -> None:
            barrier.wait()
            wrapped(1)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert calls == 1  # one store, one single-flight: no call lost to a second ObjectCache


@pytest.mark.unit
class TestEncryptedAndInteropUnchanged:
    """AC6: secure, encryption=True and interop= never run L1-only; they still resolve a backend."""

    def _failing(self, spy: mock.MagicMock) -> None:
        spy.side_effect = BackendError("no backend", error_type=BackendErrorType.TRANSIENT)

    def test_secure_resolves_a_backend(self, nothing_configured: mock.MagicMock) -> None:
        self._failing(nothing_configured)
        calls = 0

        def fn(x: int) -> int:
            nonlocal calls
            calls += 1
            return x

        wrapped = cache.secure(master_key="a" * 64)(_named(fn, "secure"))
        wrapped(1)
        wrapped(1)
        assert nothing_configured.call_count >= 1
        assert calls == 2  # degraded uncached, never L1-only plaintext
        assert _state(wrapped)["l1_only"] is False

    def test_encryption_true_resolves_a_backend(self, nothing_configured: mock.MagicMock) -> None:
        self._failing(nothing_configured)
        wrapped = cache(encryption=True, master_key="a" * 64, single_tenant_mode=True)(_named(lambda x: x, "enc"))
        wrapped(1)
        assert nothing_configured.call_count >= 1
        assert _state(wrapped)["l1_only"] is False

    def test_interop_resolves_a_backend(self, nothing_configured: mock.MagicMock) -> None:
        self._failing(nothing_configured)
        wrapped = cache(interop="zc.op", namespace="zc")(_named(lambda x: x, "interop"))
        wrapped(1)
        assert nothing_configured.call_count >= 1
        assert _state(wrapped)["l1_only"] is False


def test_is_configured_reads_every_selector(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in _ENV:
        monkeypatch.delenv(var, raising=False)
    provider = DefaultBackendProvider()
    assert provider.is_configured() is False
    for var in _ENV:
        monkeypatch.setenv(var, "x")
        assert provider.is_configured() is True
        monkeypatch.delenv(var)
    monkeypatch.setenv("CACHEKIT_REDIS_URL", "x")
    monkeypatch.setenv("CACHEKIT_API_KEY", "x")
    assert provider.is_configured() is True  # ambiguity is get_backend()'s error, not this query's
