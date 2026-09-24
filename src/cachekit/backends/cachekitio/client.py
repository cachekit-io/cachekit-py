"""HTTP client factory with connection pooling and per-thread, per-config caching.

A cached client lives as long as some CachekitIOBackend uses it. A sync client is closed when
its last backend is released; an async one is not, so ``await close_async_client()`` on the
owning event loop for a clean shutdown. Both close_* helpers also close clients that live
backends still hold.
"""

from __future__ import annotations

import threading
import weakref
from contextlib import AsyncExitStack, ExitStack
from typing import TYPE_CHECKING, Any

import httpx

from cachekit.hash_utils import redact_error_for_log
from cachekit.logging import get_structured_logger

if TYPE_CHECKING:
    from cachekit.backends.cachekitio.config import CachekitIOBackendConfig

_ClientKey = tuple[str, str, float, int]

_logger = get_structured_logger(__name__)


class SyncClientLease:
    """A hold on a shared per-thread sync client, closed once the last lease for its config goes.

    Keep the lease for as long as ``.client`` is used: ``lease_sync_http_client(config).client``
    on its own drops the lease at once, and with it the client.
    """

    # The weak cache points at leases, never at clients, and a lease has no __del__. When the last
    # backend drops one, CPython clears weak references to it before any finalizer runs, so a lookup
    # on another thread sees either the live lease or a miss, never a client mid-close. A __del__ on
    # the cached object cannot promise that: it runs while weak references still resolve, so a
    # concurrent lookup revives the object and then inherits the close.
    def __init__(self, config: CachekitIOBackendConfig) -> None:
        self.client = httpx.Client(**_client_kwargs(config))
        # atexit=False: no network I/O during interpreter exit; the process reclaims the sockets.
        weakref.finalize(self, _close_released_client, self.client).atexit = False


def _close_released_client(client: httpx.Client) -> None:
    # A finalizer has no caller to report to, so the expected failure (a socket that will not close
    # cleanly) is logged, not raised. Anything else is a bug; the interpreter reports it as
    # unraisable instead of it vanishing here.
    try:
        client.close()
    except OSError as e:
        _logger.debug("Closing a released cachekit.io HTTP client failed", error=redact_error_for_log(e))


class _ThreadClients(threading.local):
    # threading.local runs __init__ once per thread, on that thread's first access.
    # Values are weak: each CachekitIOBackend holds its sync lease and async client strongly, so
    # each stays cached while some backend uses it and is dropped when the last one goes (a sync
    # client is closed then too, see SyncClientLease).
    # ponytail: a released async client is never closed — its sockets are reclaimed by their
    # finalizers, with a ResourceWarning each — and a backend built and discarded per call gets
    # no pool reuse. Hold one backend per key, or add a small strong LRU in front if per-call
    # construction matters.
    def __init__(self) -> None:
        self.sync_leases: weakref.WeakValueDictionary[_ClientKey, SyncClientLease] = weakref.WeakValueDictionary()
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


def lease_sync_http_client(config: CachekitIOBackendConfig) -> SyncClientLease:
    """Lease the per-thread sync HTTP client for this config (created on first use).

    Args:
        config: cachekit.io backend configuration

    Returns:
        SyncClientLease: its ``.client`` is the thread-local sync client for exactly this config,
        open for as long as some lease on it is held
    """
    leases = _thread_local.sync_leases
    key = _client_key(config)
    # Bind to a local first: the weak dict alone would let a fresh lease die on insertion.
    lease = leases.get(key)
    if lease is None:
        lease = leases[key] = SyncClientLease(config)
    return lease


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
    leases = _thread_local.sync_leases
    with ExitStack() as stack:
        for lease in leases.values():
            stack.callback(lease.client.close)
        leases.clear()


def reset_global_client() -> None:
    """Drop this thread's cached clients without closing them (useful for testing).

    Note: This does not properly close clients. Use close_*_client() for proper cleanup.
    """
    _thread_local.async_clients.clear()
    _thread_local.sync_leases.clear()


__all__ = [
    "get_cached_async_http_client",
    "SyncClientLease",
    "lease_sync_http_client",
    "close_async_client",
    "close_sync_client",
    "reset_global_client",
]
