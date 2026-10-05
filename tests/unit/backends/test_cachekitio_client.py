"""Unit tests for the CachekitIO HTTP client and its process-wide lease cache.

Tests for backends/cachekitio/client.py covering:
- Process-wide, per-config caching: one client for the same config on every thread; distinct clients for
  distinct keys, timeouts and pool sizes
- Client lifecycle: open while its lease is held, closed once the last lease is dropped
- Client configuration (host, timeout, Authorization and User-Agent headers, a path prefix on api_url)
- Headers on the wire: the client's own on every request, a per-request header replacing the client's
- Cleanup via close_http_clients(), after which a live backend re-leases; reset_global_client() drops without closing
- State a forked child inherits: replaced, never closed (real forks: test_cachekitio_fork.py)

Pool policy (proxies, keepalive, limits): test_cachekitio_pool_policy.py. Async methods: test_cachekitio_event_loops.py.
A closed client is one whose urllib3 pool urllib3 has disabled: ``close()`` sets ``pool.pool`` to None.
"""

from __future__ import annotations

import gc
import logging
import os
import threading
import uuid
from importlib.metadata import PackageNotFoundError, version

import pytest
from pydantic import SecretStr
from urllib3 import HTTPResponse, HTTPSConnectionPool

from cachekit.backends.cachekitio import client as client_module
from cachekit.backends.cachekitio.backend import CachekitIOBackend
from cachekit.backends.cachekitio.client import (
    ClientLease,
    HTTPClient,
    close_http_clients,
    lease_http_client,
    reset_global_client,
)
from cachekit.backends.cachekitio.config import CachekitIOBackendConfig
from tests.utils.cachekitio_fakes import FakePool, FakeRequest, fake_backend, response


@pytest.fixture
def config() -> CachekitIOBackendConfig:
    """Standard CachekitIOBackendConfig pointing at the allowed production host."""
    return CachekitIOBackendConfig(
        api_url="https://api.cachekit.io",
        api_key=SecretStr("ck_test_key"),  # noqa: S106
        timeout=5.0,
    )


@pytest.fixture(autouse=True)
def _cleanup() -> None:  # type: ignore[return]
    """Reset the process's client cache after every test."""
    yield
    reset_global_client()


def _cached() -> list[ClientLease]:
    return list(client_module._own_leases().by_key.values())


def _is_closed(client: HTTPClient) -> bool:
    return client.pool.pool is None


def _unique_key(label: str) -> str:
    # Unique per test: a backend still alive from an earlier test must never lend this one its client.
    return f"ck_test_{label}_{uuid.uuid4().hex}"


@pytest.mark.unit
class TestLeaseSyncHttpClient:
    """HTTP client factory behaviour."""

    def test_lease_holds_an_http_client_on_one_https_pool(self, config: CachekitIOBackendConfig) -> None:
        lease = lease_http_client(config)
        assert isinstance(lease.client, HTTPClient)
        assert isinstance(lease.client.pool, HTTPSConnectionPool)

    def test_same_instance_on_repeated_calls(self, config: CachekitIOBackendConfig) -> None:
        """Process-wide caching: same lease (and client) returned every time."""
        l1 = lease_http_client(config)
        l2 = lease_http_client(config)
        assert l1 is l2

    def test_distinct_keys_get_distinct_clients(self, config: CachekitIOBackendConfig) -> None:
        """Regression: a single per-thread client sent every backend's traffic under the FIRST key."""
        other = CachekitIOBackendConfig(api_url=config.api_url, api_key=SecretStr("ck_other_key"), timeout=1.0)  # noqa: S106
        l1 = lease_http_client(config)
        l2 = lease_http_client(other)
        assert l1.client is not l2.client
        assert l2.client.headers["Authorization"] == "Bearer ck_other_key"
        assert l2.client.pool.timeout.read_timeout == 1.0
        assert lease_http_client(config) is l1

    @pytest.mark.parametrize(
        "change",
        [{"api_key": SecretStr("ck_other_key")}, {"timeout": 1.0}, {"connection_pool_size": 4}],  # noqa: S106
        ids=["api-key", "timeout", "pool-size"],
    )
    def test_every_baked_in_value_keys_its_own_client(self, config: CachekitIOBackendConfig, change: dict[str, object]) -> None:
        """Each of these is fixed at client creation, so a client shared across them would apply the first one's."""
        other = CachekitIOBackendConfig(**{**config.model_dump(), **change})
        l1, l2 = lease_http_client(config), lease_http_client(other)
        assert l1.client is not l2.client
        if "connection_pool_size" in change:
            assert l2.client.pool.pool.maxsize == 4

    def test_user_agent_names_sdk_and_urllib3_versions(self, config: CachekitIOBackendConfig) -> None:
        """Edge analytics attribute traffic to an SDK release by this UA, built from installed package metadata.

        The SaaS reads only the first product token.
        """
        user_agent = lease_http_client(config).client.headers["User-Agent"]
        assert user_agent == f"cachekit-py/{version('cachekit')} urllib3/{version('urllib3')}"

    def test_user_agent_without_distribution_metadata(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A source-only or vendored install has no dist-info: the backend must still import, send a UA, and say why."""

        def missing(name: str) -> str:
            if name == "cachekit":
                raise PackageNotFoundError(name)
            return version(name)

        monkeypatch.setattr(client_module, "version", missing)
        with caplog.at_level(logging.DEBUG, logger=client_module.__name__):
            assert client_module._user_agent() == f"cachekit-py/unknown urllib3/{version('urllib3')}"
        assert [r.levelno for r in caplog.records if "distribution metadata" in r.getMessage()] == [logging.DEBUG]


@pytest.mark.unit
class TestRequestHeaders:
    """What the client puts on each request it hands the pool."""

    def test_client_headers_on_every_request(self) -> None:
        backend, pool = fake_backend(lambda request: response(404) if request.method in ("GET", "HEAD") else response(200))
        backend.get("k")
        backend.set("k", b"v", ttl=60)
        backend.exists("k")
        backend.delete("k")
        assert len(pool.requests) == 4
        for request in pool.requests:
            assert request.headers["Authorization"] == f"Bearer {backend._config.api_key.get_secret_value()}"
            assert request.headers["User-Agent"] == client_module._USER_AGENT
            assert request.headers["Content-Type"] == "application/octet-stream"
        assert pool.requests[1].headers["X-CacheKit-TTL"] == "60"  # a per-request header rides alongside the client's

    async def test_per_request_header_replaces_the_clients(self) -> None:
        """The lock POST is JSON: its Content-Type must replace the client's octet-stream, never duplicate it."""

        def handler(request: FakeRequest) -> HTTPResponse:
            return response(200, json={"lock_id": "L1"} if request.method == "POST" else {})

        backend, pool = fake_backend(handler)
        async with backend.acquire_lock("k", timeout=5.0) as acquired:
            assert acquired
        post = pool.requests[0]
        assert (post.method, post.path) == ("POST", "/v1/cache/k/lock")
        assert post.headers.getlist("Content-Type") == ["application/json"]
        assert post.headers["Authorization"] == f"Bearer {backend._config.api_key.get_secret_value()}"

    def test_api_url_path_prefixes_request_paths(self) -> None:
        """A gateway mount (``https://host/prefix/``) prefixes every path, with no doubled slash."""
        backend, pool = fake_backend(lambda request: response(200, b"v"), api_url="https://api.cachekit.io/prefix/")
        assert backend.get("k") == b"v"
        assert pool.requests[0].path == "/prefix/v1/cache/k"


@pytest.mark.unit
class TestSharedAcrossThreads:
    def test_backends_with_one_config_share_one_client_across_threads(self) -> None:
        """urllib3's pool is thread-safe, so a process opens one pool per config, whichever thread builds the backend."""
        api_key = _unique_key("threads")
        backends: list[CachekitIOBackend] = []
        lock = threading.Lock()

        def build() -> None:
            b = CachekitIOBackend(api_key=api_key)
            with lock:
                backends.append(b)

        threads = [threading.Thread(target=build) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(5)
        backends.append(CachekitIOBackend(api_key=api_key))
        assert len(backends) == 5
        assert len({id(b._lease.client) for b in backends}) == 1


@pytest.mark.unit
class TestCloseHttpClients:
    """close_http_clients() cleanup behaviour."""

    def test_closes_and_empties_the_cache(self, config: CachekitIOBackendConfig) -> None:
        """After close, the process's client cache is empty and the held client is closed."""
        lease = lease_http_client(config)
        close_http_clients()
        assert _is_closed(lease.client)
        assert not _cached()

    def test_idempotent_when_no_client(self, config: CachekitIOBackendConfig) -> None:  # noqa: ARG002
        """Calling close when no client exists does not raise."""
        close_http_clients()  # no client created yet — must not raise


@pytest.mark.unit
class TestBackendAfterClose:
    """A live backend recovers from close_http_clients(): its next request leases a new client.

    The lease machinery is real; only each client's urllib3 pool is a FakePool, so nothing leaves the process.
    """

    @pytest.fixture
    def pools(self, monkeypatch: pytest.MonkeyPatch) -> list[FakePool]:
        built: list[FakePool] = []

        def fake_pool(config: CachekitIOBackendConfig) -> FakePool:
            built.append(FakePool(lambda request: response(404) if request.method == "GET" else response(200)))
            return built[-1]

        monkeypatch.setattr(client_module, "_connection_pool", fake_pool)
        return built

    def test_sync_and_async_calls_succeed_on_a_new_client(self, pools: list[FakePool]) -> None:
        import asyncio

        backend = CachekitIOBackend(api_key=_unique_key("closed"))
        first = backend._lease
        close_http_clients()
        assert _is_closed(first.client)

        assert backend.get("k") is None
        backend.set("k", b"v")
        assert backend.delete("k") is True
        assert asyncio.run(backend.get_async("k")) is None

        assert backend._lease is not first
        assert len(pools) == 2
        assert pools[0].requests == []
        assert [r.method for r in pools[1].requests] == ["GET", "PUT", "DELETE", "GET"]

    def test_a_later_close_on_another_thread_closes_the_new_client(self, pools: list[FakePool]) -> None:  # noqa: ARG002
        backend = CachekitIOBackend(api_key=_unique_key("reclosed"))
        close_http_clients()
        releaser = threading.Thread(target=backend.get, args=("k",))
        releaser.start()
        releaser.join(5)
        assert not releaser.is_alive()
        renewed = backend._lease
        assert not _is_closed(renewed.client)

        close_http_clients()
        assert _is_closed(renewed.client)
        assert backend.get("k") is None
        assert backend._lease is not renewed


def _raise() -> None:
    raise RuntimeError("close failed")


@pytest.mark.unit
@pytest.mark.parametrize("fail_idx", [0, 1])
class TestCloseSurvivesAFailingClient:
    """One client's close raising must not leak the others or leave them cached."""

    @pytest.fixture
    def other(self, config: CachekitIOBackendConfig) -> CachekitIOBackendConfig:
        return CachekitIOBackendConfig(api_url=config.api_url, api_key=SecretStr("ck_other_key"), timeout=config.timeout)  # noqa: S106

    def test_sync(self, config: CachekitIOBackendConfig, other: CachekitIOBackendConfig, fail_idx: int) -> None:
        leases = [lease_http_client(config), lease_http_client(other)]
        leases[fail_idx].client.close = _raise  # type: ignore[method-assign]
        try:
            with pytest.raises(RuntimeError, match="close failed"):
                close_http_clients()
            assert _is_closed(leases[1 - fail_idx].client)
            assert not _cached()
        finally:
            del leases[fail_idx].client.close  # else the release finalizer later hits _raise


@pytest.mark.unit
def test_discarded_backends_do_not_accumulate_clients() -> None:
    """Regression: a strong per-config cache kept a client per distinct key forever, so rotating keys
    (or with_timeout per call) grew a long-lived process's pools without bound."""
    prefix = _unique_key("rotation")
    live = CachekitIOBackend(api_key=f"{prefix}_live")
    for i in range(20):
        CachekitIOBackend(api_key=f"{prefix}_rotated_{i}")  # built and dropped at once
    gc.collect()
    mine = [lease for key, lease in client_module._own_leases().by_key.items() if key[1].get_secret_value().startswith(prefix)]
    assert mine == [live._lease]


@pytest.mark.unit
def test_releasing_the_last_backend_closes_its_client() -> None:
    """A released client is close()d on release, not left to socket finalizers."""
    api_key = _unique_key("released")
    first = CachekitIOBackend(api_key=api_key)
    second = CachekitIOBackend(api_key=api_key)
    client = first._lease.client
    assert second._lease.client is client
    del first
    gc.collect()
    assert not _is_closed(client)  # still shared with a live backend
    del second
    gc.collect()
    assert _is_closed(client)


@pytest.mark.unit
def test_backend_built_during_a_release_on_another_thread_gets_an_open_client(monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression: a client-level __del__ runs while weak references to the client still resolve,
    so a backend built mid-release revived the dying client, which then closed under it."""
    taken, dropped, closing, resume = threading.Event(), threading.Event(), threading.Event(), threading.Event()
    real_close = HTTPClient.close

    def slow_close(self: HTTPClient) -> None:
        closing.set()
        resume.wait(5)
        real_close(self)

    # The releaser takes its own reference to the lease, then drops it only after this thread has dropped
    # every one of its own, so the release (and the close it triggers) runs on the releaser, on GIL and
    # free-threaded builds alike.
    # Handing it the last reference instead is not enough: on a free-threaded build, an object whose count
    # a non-owner thread takes to zero is queued back to its owner, and freed on this thread once it wakes.
    def release() -> None:
        lease = holder[0]._lease
        taken.set()
        dropped.wait(5)
        del lease

    api_key = _unique_key("race")
    monkeypatch.setattr(HTTPClient, "close", slow_close)
    holder = [CachekitIOBackend(api_key=api_key)]
    releaser = threading.Thread(target=release)
    releaser.start()
    assert taken.wait(5)
    holder.clear()
    dropped.set()
    assert closing.wait(5)
    rebuilt = CachekitIOBackend(api_key=api_key)
    resume.set()
    releaser.join(5)
    assert not releaser.is_alive()
    assert not _is_closed(rebuilt._lease.client)


@pytest.mark.unit
def test_rebuilt_backend_never_inherits_a_released_client() -> None:
    """Guard: the weak cache must miss once a lease is released, never hand its closed client to the next
    backend with the same key, which would then fail every call."""
    api_key = _unique_key("rebuilt")
    released = CachekitIOBackend(api_key=api_key)
    old = released._lease.client
    del released
    gc.collect()
    rebuilt = CachekitIOBackend(api_key=api_key)
    assert rebuilt._lease.client is not old
    assert not _is_closed(rebuilt._lease.client)


@pytest.mark.unit
def test_failed_close_on_release_is_logged_not_raised(monkeypatch: pytest.MonkeyPatch, config: CachekitIOBackendConfig) -> None:
    """A socket that will not close on release is logged at debug, with no free-form error text."""
    from unittest.mock import MagicMock

    def fail(self: HTTPClient) -> None:
        raise OSError("close failed with SECRET_DETAIL")

    log = MagicMock()
    monkeypatch.setattr(client_module, "_logger", log)
    monkeypatch.setattr(HTTPClient, "close", fail)
    client_module._close_released_client(HTTPClient(config), os.getpid())
    log.debug.assert_called_once()
    assert "OSError" in log.debug.call_args.kwargs["error"]
    assert "SECRET_DETAIL" not in repr(log.debug.call_args)


_PARENT_PID = -1  # no process has it, so state marked with it reads as inherited from a parent


@pytest.mark.unit
class TestStateInheritedAcrossFork:
    """A forked child's view of its parent's state, in process; real forks over TLS: test_cachekitio_fork.py."""

    def test_an_inherited_cache_starts_empty(self, config: CachekitIOBackendConfig) -> None:
        inherited = lease_http_client(config)
        client_module._leases.pid = _PARENT_PID
        lease = lease_http_client(config)
        assert lease is not inherited
        assert lease_http_client(config) is lease

    def test_an_inherited_client_is_released_unclosed(
        self, monkeypatch: pytest.MonkeyPatch, config: CachekitIOBackendConfig
    ) -> None:
        closed: list[HTTPClient] = []
        monkeypatch.setattr(HTTPClient, "close", lambda self: closed.append(self))
        client_module._close_released_client(HTTPClient(config), _PARENT_PID)
        assert closed == []

    def test_the_backend_replaces_an_inherited_lease(self) -> None:
        backend = CachekitIOBackend(api_key=_unique_key("inherited"))
        inherited = backend._lease
        inherited.pid = client_module._leases.pid = _PARENT_PID  # a child inherits both
        lease = backend._own_lease()
        assert lease is not inherited
        assert lease.pid == os.getpid()
        assert backend._own_lease() is lease


@pytest.mark.unit
class TestResetGlobalClient:
    """reset_global_client() clears all client references."""

    def test_drops_the_cache_without_closing(self, config: CachekitIOBackendConfig) -> None:
        """After reset, the cache is empty, and the held client stays open for whoever holds it."""
        lease = lease_http_client(config)  # held, so only the reset can empty the cache
        reset_global_client()
        assert not _cached()
        assert not _is_closed(lease.client)

    def test_new_client_created_after_reset(self, config: CachekitIOBackendConfig) -> None:
        """After reset, next call returns a fresh client (different object)."""
        l1 = lease_http_client(config)
        reset_global_client()
        l2 = lease_http_client(config)
        assert l1.client is not l2.client
