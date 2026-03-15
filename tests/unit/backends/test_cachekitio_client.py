"""Unit tests for CachekitIO HTTP client factory.

Tests for backends/cachekitio/client.py covering:
- Thread-local caching (same client returned on repeated calls)
- Client configuration (base_url, timeout, Authorization header)
- Cleanup via close_sync_client() and close_async_client()
- reset_global_client() clears all thread-local references
- New client created after reset
"""

from __future__ import annotations

import pytest
import pytest_asyncio  # noqa: F401
from pydantic import SecretStr

from cachekit.backends.cachekitio.client import (
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

    def test_sets_thread_local_to_none(self, config: CachekitIOBackendConfig) -> None:
        """After close, thread-local sync_client attribute is None."""
        from cachekit.backends.cachekitio import client as client_module

        get_sync_http_client(config)
        close_sync_client()
        assert getattr(client_module._thread_local, "sync_client", None) is None

    def test_idempotent_when_no_client(self, config: CachekitIOBackendConfig) -> None:  # noqa: ARG002
        """Calling close when no client exists does not raise."""
        close_sync_client()  # no client created yet — must not raise


@pytest.mark.unit
class TestResetGlobalClient:
    """reset_global_client() clears all client references."""

    def test_clears_sync_thread_local(self, config: CachekitIOBackendConfig) -> None:
        """After reset, thread-local sync client is None."""
        from cachekit.backends.cachekitio import client as client_module

        get_sync_http_client(config)
        reset_global_client()
        assert getattr(client_module._thread_local, "sync_client", None) is None

    def test_clears_async_thread_local(self, config: CachekitIOBackendConfig) -> None:
        """After reset, thread-local async client is None."""
        from cachekit.backends.cachekitio import client as client_module

        get_cached_async_http_client(config)
        reset_global_client()
        assert getattr(client_module._thread_local, "async_client", None) is None

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
