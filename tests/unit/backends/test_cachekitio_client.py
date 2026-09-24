"""Unit tests for CachekitIO HTTP client factory.

Tests for backends/cachekitio/client.py covering:
- Thread-local, per-config caching (same client for the same config; distinct clients for distinct keys)
- Client configuration (base_url, timeout, Authorization header)
- Cleanup via close_sync_client() and close_async_client()
- reset_global_client() clears thread-local references
- New client created after reset
"""

from __future__ import annotations

import pytest
import pytest_asyncio  # noqa: F401
from pydantic import SecretStr

from cachekit.backends.cachekitio.client import (
    close_async_client,
    close_sync_client,
    get_cached_async_http_client,
    get_sync_http_client,
    reset_global_client,
)
from cachekit.backends.cachekitio.config import CachekitIOBackendConfig


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
    """Reset all thread-local client state after every test."""
    yield
    reset_global_client()


@pytest.mark.unit
class TestGetSyncHttpClient:
    """Sync HTTP client factory behaviour."""

    def test_returns_httpx_client(self, config: CachekitIOBackendConfig) -> None:
        """Factory returns an httpx.Client instance."""
        import httpx

        client = get_sync_http_client(config)
        assert isinstance(client, httpx.Client)

    def test_same_instance_on_repeated_calls(self, config: CachekitIOBackendConfig) -> None:
        """Thread-local caching: same object returned every time within a thread."""
        c1 = get_sync_http_client(config)
        c2 = get_sync_http_client(config)
        assert c1 is c2

    def test_base_url_configured(self, config: CachekitIOBackendConfig) -> None:
        """Client base_url matches config.api_url."""
        client = get_sync_http_client(config)
        # httpx stores base_url as a URL object; compare string representation
        assert str(client.base_url).rstrip("/") == config.api_url.rstrip("/")

    def test_timeout_configured(self, config: CachekitIOBackendConfig) -> None:
        """Client timeout matches config.timeout."""
        client = get_sync_http_client(config)
        assert client.timeout.read == config.timeout

    def test_authorization_header(self, config: CachekitIOBackendConfig) -> None:
        """Authorization header is Bearer <api_key>."""
        client = get_sync_http_client(config)
        auth_header = client.headers.get("authorization", "")
        assert auth_header == f"Bearer {config.api_key.get_secret_value()}"

    def test_distinct_keys_get_distinct_clients(self, config: CachekitIOBackendConfig) -> None:
        """Regression: a single per-thread client sent every backend's traffic under the FIRST key."""
        other = CachekitIOBackendConfig(api_url=config.api_url, api_key=SecretStr("ck_other_key"), timeout=1.0)  # noqa: S106
        c1 = get_sync_http_client(config)
        c2 = get_sync_http_client(other)
        assert c1 is not c2
        assert c2.headers["authorization"] == "Bearer ck_other_key"
        assert c2.timeout.read == 1.0
        assert get_sync_http_client(config) is c1


@pytest.mark.unit
class TestGetCachedAsyncHttpClient:
    """Async HTTP client factory behaviour."""

    def test_returns_httpx_async_client(self, config: CachekitIOBackendConfig) -> None:
        """Factory returns an httpx.AsyncClient instance."""
        import httpx

        client = get_cached_async_http_client(config)
        assert isinstance(client, httpx.AsyncClient)

    def test_same_instance_on_repeated_calls(self, config: CachekitIOBackendConfig) -> None:
        """Thread-local caching: same object returned every time within a thread."""
        c1 = get_cached_async_http_client(config)
        c2 = get_cached_async_http_client(config)
        assert c1 is c2

    def test_authorization_header(self, config: CachekitIOBackendConfig) -> None:
        """Authorization header is Bearer <api_key>."""
        client = get_cached_async_http_client(config)
        auth_header = client.headers.get("authorization", "")
        assert auth_header == f"Bearer {config.api_key.get_secret_value()}"


@pytest.mark.unit
class TestCloseSyncClient:
    """close_sync_client() cleanup behaviour."""

    def test_empties_this_threads_sync_cache(self, config: CachekitIOBackendConfig) -> None:
        """After close, this thread's sync client cache is empty."""
        from cachekit.backends.cachekitio import client as client_module

        client = get_sync_http_client(config)
        close_sync_client()
        assert client.is_closed
        assert not client_module._thread_local.sync_clients

    def test_idempotent_when_no_client(self, config: CachekitIOBackendConfig) -> None:  # noqa: ARG002
        """Calling close when no client exists does not raise."""
        close_sync_client()  # no client created yet — must not raise


def _raise() -> None:
    raise RuntimeError("close failed")


async def _araise() -> None:
    raise RuntimeError("close failed")


@pytest.mark.unit
@pytest.mark.parametrize("fail_idx", [0, 1])
class TestCloseSurvivesAFailingClient:
    """One client's close raising must not leak the others or leave them cached."""

    @pytest.fixture
    def other(self, config: CachekitIOBackendConfig) -> CachekitIOBackendConfig:
        return CachekitIOBackendConfig(api_url=config.api_url, api_key=SecretStr("ck_other_key"), timeout=config.timeout)  # noqa: S106

    def test_sync(self, config: CachekitIOBackendConfig, other: CachekitIOBackendConfig, fail_idx: int) -> None:
        from cachekit.backends.cachekitio import client as client_module

        clients = [get_sync_http_client(config), get_sync_http_client(other)]
        clients[fail_idx].close = _raise  # type: ignore[method-assign]
        with pytest.raises(RuntimeError, match="close failed"):
            close_sync_client()
        assert clients[1 - fail_idx].is_closed
        assert not client_module._thread_local.sync_clients

    async def test_async(self, config: CachekitIOBackendConfig, other: CachekitIOBackendConfig, fail_idx: int) -> None:
        from cachekit.backends.cachekitio import client as client_module

        clients = [get_cached_async_http_client(config), get_cached_async_http_client(other)]
        clients[fail_idx].aclose = _araise  # type: ignore[method-assign]
        with pytest.raises(RuntimeError, match="close failed"):
            await close_async_client()
        assert clients[1 - fail_idx].is_closed
        assert not client_module._thread_local.async_clients


@pytest.mark.unit
def test_discarded_backends_do_not_accumulate_clients() -> None:
    """Regression: a strong per-config cache kept a client pair per distinct key forever, so
    rotating keys (or with_timeout per call) grew a long-lived thread's pools without bound."""
    import gc

    from cachekit.backends.cachekitio import client as client_module
    from cachekit.backends.cachekitio.backend import CachekitIOBackend

    live = CachekitIOBackend(api_key="ck_live_tenant")  # pragma: allowlist secret
    for i in range(20):
        CachekitIOBackend(api_key=f"ck_rotated_{i}")  # pragma: allowlist secret
    gc.collect()
    assert list(client_module._thread_local.sync_clients.values()) == [live._sync_client]
    assert list(client_module._thread_local.async_clients.values()) == [live._async_client]


@pytest.mark.unit
class TestResetGlobalClient:
    """reset_global_client() clears all client references."""

    def test_clears_sync_thread_local(self, config: CachekitIOBackendConfig) -> None:
        """After reset, this thread's sync client cache is empty."""
        from cachekit.backends.cachekitio import client as client_module

        client = get_sync_http_client(config)  # held, so only the reset can empty the cache
        reset_global_client()
        assert not client_module._thread_local.sync_clients
        assert not client.is_closed

    def test_clears_async_thread_local(self, config: CachekitIOBackendConfig) -> None:
        """After reset, this thread's async client cache is empty."""
        from cachekit.backends.cachekitio import client as client_module

        client = get_cached_async_http_client(config)  # held, so only the reset can empty the cache
        reset_global_client()
        assert not client_module._thread_local.async_clients
        assert not client.is_closed

    def test_new_sync_client_created_after_reset(self, config: CachekitIOBackendConfig) -> None:
        """After reset, next call returns a fresh client (different object)."""
        c1 = get_sync_http_client(config)
        reset_global_client()
        c2 = get_sync_http_client(config)
        assert c1 is not c2

    def test_new_async_client_created_after_reset(self, config: CachekitIOBackendConfig) -> None:
        """After reset, next call returns a fresh async client (different object)."""
        c1 = get_cached_async_http_client(config)
        reset_global_client()
        c2 = get_cached_async_http_client(config)
        assert c1 is not c2
