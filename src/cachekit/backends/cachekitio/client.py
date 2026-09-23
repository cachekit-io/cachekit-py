"""HTTP client factory with connection pooling and per-thread, per-config caching."""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING, Any

import httpx

if TYPE_CHECKING:
    from cachekit.backends.cachekitio.config import CachekitIOBackendConfig

# Per-thread client caches, keyed by the config values baked into a client. The key
# matters: Authorization, base_url and timeout are fixed at client creation, so one
# client per thread would send every backend's traffic under the FIRST key constructed
# on that thread — cross-tenant writes and reads with no error anywhere.
_thread_local = threading.local()

_ClientKey = tuple[str, str, float, int]


def _client_key(config: CachekitIOBackendConfig) -> _ClientKey:
    return (config.api_url, config.api_key.get_secret_value(), config.timeout, config.connection_pool_size)


def _cache(kind: str) -> dict[_ClientKey, Any]:
    cache = getattr(_thread_local, kind, None)
    if cache is None:
        cache = {}
        setattr(_thread_local, kind, cache)
    return cache


def _client_kwargs(config: CachekitIOBackendConfig) -> dict[str, Any]:
    return {
        "base_url": config.api_url,
        "timeout": config.timeout,
        "http2": True,
        "limits": httpx.Limits(
            max_connections=config.connection_pool_size,
            max_keepalive_connections=config.connection_pool_size,
        ),
        "headers": {
            "Authorization": f"Bearer {config.api_key.get_secret_value()}",
            "Content-Type": "application/octet-stream",
        },
    }


def get_cached_async_http_client(config: CachekitIOBackendConfig) -> httpx.AsyncClient:
    """Get the per-thread async HTTP client for this config (created on first use).

    Args:
        config: cachekit.io backend configuration

    Returns:
        httpx.AsyncClient: Thread-local async HTTP client for exactly this config
    """
    clients = _cache("async_clients")
    key = _client_key(config)
    if key not in clients:
        clients[key] = httpx.AsyncClient(**_client_kwargs(config))
    return clients[key]


def get_sync_http_client(config: CachekitIOBackendConfig) -> httpx.Client:
    """Get the per-thread sync HTTP client for this config (created on first use).

    Args:
        config: cachekit.io backend configuration

    Returns:
        httpx.Client: Thread-local sync HTTP client for exactly this config
    """
    clients = _cache("sync_clients")
    key = _client_key(config)
    if key not in clients:
        clients[key] = httpx.Client(**_client_kwargs(config))
    return clients[key]


async def close_async_client() -> None:
    """Close this thread's async client instances (useful for cleanup)."""
    clients = _cache("async_clients")
    for client in clients.values():
        await client.aclose()
    clients.clear()


def close_sync_client() -> None:
    """Close this thread's sync client instances (useful for cleanup)."""
    clients = _cache("sync_clients")
    for client in clients.values():
        client.close()
    clients.clear()


def reset_global_client() -> None:
    """Drop this thread's cached clients without closing them (useful for testing).

    Note: This does not properly close clients. Use close_*_client() for proper cleanup.
    """
    _cache("async_clients").clear()
    _cache("sync_clients").clear()


__all__ = [
    "get_cached_async_http_client",
    "get_sync_http_client",
    "close_async_client",
    "close_sync_client",
    "reset_global_client",
]
