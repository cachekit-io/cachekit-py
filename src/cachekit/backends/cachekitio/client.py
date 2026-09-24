"""HTTP client factory with connection pooling and per-thread, per-config caching.

A cached client lives as long as some CachekitIOBackend uses it; call close_sync_client() /
close_async_client() for a graceful shutdown of the calling thread's clients.
"""

from __future__ import annotations

import threading
import weakref
from contextlib import AsyncExitStack, ExitStack
from typing import TYPE_CHECKING, Any

import httpx

if TYPE_CHECKING:
    from cachekit.backends.cachekitio.config import CachekitIOBackendConfig

_ClientKey = tuple[str, str, float, int]


class _ThreadClients(threading.local):
    # threading.local runs __init__ once per thread, on that thread's first access.
    # Values are weak: each CachekitIOBackend holds its clients strongly, so a client stays
    # cached while some backend uses it and drops out when the last one is collected.
    # ponytail: a released client is never close()d — its sockets are reclaimed by their
    # finalizers (at the next cyclic GC pass for HTTP/2, with a ResourceWarning each), and a
    # backend built and discarded per call gets no pool reuse. Hold one backend per key, or
    # add a small strong LRU in front if per-call construction matters.
    def __init__(self) -> None:
        self.sync_clients: weakref.WeakValueDictionary[_ClientKey, httpx.Client] = weakref.WeakValueDictionary()
        self.async_clients: weakref.WeakValueDictionary[_ClientKey, httpx.AsyncClient] = weakref.WeakValueDictionary()


# Per-thread client caches, keyed by the config values baked into a client. The key
# matters: Authorization, base_url and timeout are fixed at client creation, so one
# client per thread would send every backend's traffic under the FIRST key constructed
# on that thread — cross-tenant writes and reads with no error anywhere.
_thread_local = _ThreadClients()


def _client_key(config: CachekitIOBackendConfig) -> _ClientKey:
    return (config.api_url, config.api_key.get_secret_value(), config.timeout, config.connection_pool_size)


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
    clients = _thread_local.async_clients
    key = _client_key(config)
    # Bind to a local first: the weak dict alone would let a fresh client die on insertion.
    client = clients.get(key)
    if client is None:
        client = clients[key] = httpx.AsyncClient(**_client_kwargs(config))
    return client


def get_sync_http_client(config: CachekitIOBackendConfig) -> httpx.Client:
    """Get the per-thread sync HTTP client for this config (created on first use).

    Args:
        config: cachekit.io backend configuration

    Returns:
        httpx.Client: Thread-local sync HTTP client for exactly this config
    """
    clients = _thread_local.sync_clients
    key = _client_key(config)
    client = clients.get(key)
    if client is None:
        client = clients[key] = httpx.Client(**_client_kwargs(config))
    return client


# The exit stack runs every pushed close even when an earlier one raises, then re-raises:
# one failing client can neither leak the rest nor leave closed clients in the cache.
async def close_async_client() -> None:
    """Close this thread's async client instances (useful for cleanup)."""
    clients = _thread_local.async_clients
    async with AsyncExitStack() as stack:
        for client in clients.values():
            stack.push_async_callback(client.aclose)
        clients.clear()


def close_sync_client() -> None:
    """Close this thread's sync client instances (useful for cleanup)."""
    clients = _thread_local.sync_clients
    with ExitStack() as stack:
        for client in clients.values():
            stack.callback(client.close)
        clients.clear()


def reset_global_client() -> None:
    """Drop this thread's cached clients without closing them (useful for testing).

    Note: This does not properly close clients. Use close_*_client() for proper cleanup.
    """
    _thread_local.async_clients.clear()
    _thread_local.sync_clients.clear()


__all__ = [
    "get_cached_async_http_client",
    "get_sync_http_client",
    "close_async_client",
    "close_sync_client",
    "reset_global_client",
]
