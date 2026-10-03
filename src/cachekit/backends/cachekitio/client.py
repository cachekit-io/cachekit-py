"""HTTP client factory with connection pooling and per-thread, per-config caching.

A cached client lives as long as some CachekitIOBackend uses it. A sync client is closed when
its last backend is released; an async one is not, so ``await close_async_client()`` on the
owning event loop for a clean shutdown. Both close_* helpers also close clients that live
backends still hold. An async client is also bound to the event loop it was first used on:
the next loop on the same thread gets a new one.

A forked child never sees its parent's cached clients: their pooled connections share the parent's TLS
sessions, so a request on one from both processes desynchronises it. The caches are owned by a PID, and
a child (``os.fork()``, or a fork from C that runs no at-fork hook) starts with empty ones.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
import weakref
from contextlib import AsyncExitStack, ExitStack
from importlib.metadata import version
from typing import TYPE_CHECKING, Any

import httpx

from cachekit.hash_utils import redact_error_for_log
from cachekit.logging import get_structured_logger

if TYPE_CHECKING:
    from cachekit.backends.cachekitio.config import CachekitIOBackendConfig

_ClientKey = tuple[str, str, float, int]

# Edge analytics group SaaS traffic by User-Agent; without this every request reads as a bare python-httpx client.
# The version comes from the installed distribution, so it cannot drift from the release.
_USER_AGENT = f"cachekit-py/{version('cachekit')} httpx/{httpx.__version__}"

_logger = get_structured_logger(__name__)


class SyncClientLease:
    """Sole owner of a cached per-thread sync client, which is closed once its last holder drops the lease.

    Keep the lease for as long as ``.client`` is used: ``lease_sync_http_client(config).client``
    on its own drops the lease at once, and with it the client.

    A lease belongs to the process that built it (``.pid``). In a forked child, ``.client`` is still the
    parent's client, on the parent's connections: lease again there. CachekitIOBackend does so before
    every request.
    """

    # The weak cache points at leases, never at clients, and a lease has no __del__. A backend can be
    # released on a thread other than the one that built it; CPython clears weak references to the
    # lease before any finalizer runs, so a lookup racing that release sees either the live lease or a
    # miss, never a client mid-close. A __del__ on the cached object cannot promise that: it runs while
    # weak references still resolve, so the racing lookup revives the object and inherits the close.
    def __init__(self, config: CachekitIOBackendConfig) -> None:
        self.pid = os.getpid()
        self.client = httpx.Client(**_client_kwargs(config))
        # atexit=False: exit-time finalizers run while daemon threads (stale-while-revalidate) are still
        # alive, so closing then could pull a client out from under an in-flight request. The process
        # reclaims the sockets at exit anyway.
        weakref.finalize(self, _close_released_client, self.client, self.pid).atexit = False


def _close_released_client(client: httpx.Client, owner_pid: int) -> None:
    # A forked child drops an inherited lease unclosed: the client's connections are its parent's too,
    # and close() takes the pool lock, which a parent thread may have held at fork. Garbage collection
    # closes the child's copies of the sockets, which sends nothing.
    if os.getpid() != owner_pid:
        return
    # A finalizer has no caller to report to, so the expected failure (a socket that will not close
    # cleanly) is logged, not raised. Anything else is a bug; the interpreter reports it as
    # unraisable instead of it vanishing here.
    try:
        client.close()
    except OSError as e:
        _logger.debug("Closing a released cachekit.io HTTP client failed", error=redact_error_for_log(e))


class _LoopBoundClient:
    """One thread's async client for one config, rebuilt whenever the running event loop changes.

    An httpx.AsyncClient's pooled connections belong to the loop that opened them: on any later loop
    the next request raises RuntimeError('Event loop is closed'). A thread runs one loop at a time, so
    one slot per thread and config is enough. The slot holds the loop weakly, but a used client's
    connections hold their loop, so the last loop lives until this slot is rebuilt or released.
    """

    def __init__(self, config: CachekitIOBackendConfig) -> None:
        self._config = config
        self._loop: weakref.ref[asyncio.AbstractEventLoop] | None = None
        self._client: httpx.AsyncClient | None = None

    def get(self) -> httpx.AsyncClient:
        loop = asyncio.get_running_loop()
        if self._client is None or self._loop is None or self._loop() is not loop:
            # The replaced client is dropped unclosed: its loop has finished, so its connections
            # cannot be awaited closed (the same ResourceWarning as a released async client).
            self._client = httpx.AsyncClient(**_client_kwargs(self._config))
            self._loop = weakref.ref(loop)
        return self._client

    def take(self) -> httpx.AsyncClient | None:
        """Detach the current client, so the next get() builds a new one."""
        client, self._client, self._loop = self._client, None, None
        return client


class AsyncClientLease:
    """A backend's handle on the async clients for its config: ``.client`` is the one for this thread's running loop.

    Building a lease builds no client; the first async call on each loop does. The lease holds the
    slot of every thread it has been used on (dropped at thread exit), so the shared per-thread slot
    lives while some backend uses it, and no client is ever handed to a thread or loop it was not
    built on.

    Like a SyncClientLease, it belongs to the process that built it (``.pid``): in a forked child the
    forking thread's held slot is still the parent's, so lease again there, as CachekitIOBackend does
    before every request.
    """

    def __init__(self, config: CachekitIOBackendConfig) -> None:
        self.pid = os.getpid()
        self._config = config
        self._held = threading.local()

    @property
    def client(self) -> httpx.AsyncClient:
        """The async client bound to the running event loop. Raises RuntimeError with no running loop."""
        slot: _LoopBoundClient | None = getattr(self._held, "slot", None)
        if slot is None:
            slots = _clients().async_slots
            key = _client_key(self._config)
            slot = slots.get(key)
            if slot is None:
                slot = slots[key] = _LoopBoundClient(self._config)
            self._held.slot = slot
        return slot.get()


class _ThreadClients(threading.local):
    # threading.local runs __init__ once per thread, on that thread's first access.
    # Values are weak: each CachekitIOBackend holds its sync lease strongly and its async lease holds
    # this thread's slot, so each stays cached while some backend uses it and is dropped when the
    # last one goes (a sync client is closed then too, see SyncClientLease). No __del__ or aclose
    # finalizer on either, see SyncClientLease.
    # ponytail: a released async client is never closed — a finalizer cannot await aclose() — so its
    # sockets are reclaimed by their finalizers, with a ResourceWarning each; and a backend built and
    # discarded per call gets no pool reuse. Hold one backend per key, or add a small strong LRU in
    # front if per-call construction matters.
    def __init__(self) -> None:
        self.pid = os.getpid()
        self.sync_leases: weakref.WeakValueDictionary[_ClientKey, SyncClientLease] = weakref.WeakValueDictionary()
        self.async_slots: weakref.WeakValueDictionary[_ClientKey, _LoopBoundClient] = weakref.WeakValueDictionary()


# Per-thread client caches, keyed by the config values baked into a client. The key
# matters: Authorization, base_url and timeout are fixed at client creation, so one
# client per thread would send every backend's traffic under the FIRST key constructed
# on that thread — cross-tenant writes and reads with no error anywhere.
_thread_local = _ThreadClients()


def _clients() -> _ThreadClients:
    """This thread's client caches, emptied the first time this thread reads them in a forked child.

    An owner-PID check rather than an os.register_at_fork hook: uWSGI forks without running Python's
    at-fork hooks. Each thread's caches carry their own owner PID, so only that thread reads or resets
    them, and no thread can discard another's. The inherited caches are dropped, never closed (see
    _close_released_client).
    """
    if _thread_local.pid != os.getpid():
        _thread_local.__init__()  # this thread's caches start over, as a new thread's do
    return _thread_local


def _client_key(config: CachekitIOBackendConfig) -> _ClientKey:
    return (config.api_url, config.api_key.get_secret_value(), config.timeout, config.connection_pool_size)


# Looked up once: getLogger() takes logging's module lock, and a fork from C (uWSGI) skips logging's at-fork
# reset, so a child re-leasing its clients would hang on a lock a parent thread held at fork. Reading the
# level takes no lock, and the parent pinned it when it built the client being replaced. An application that
# unsets it after that makes the child's re-lease pin it again with setLevel(), which does take the lock.
_hpack_logger = logging.getLogger("hpack")


def _pin_hpack_logger() -> None:
    # hpack (httpx's HTTP/2 header encoder) logs every header block it encodes at DEBUG, and that block
    # decodes back to the Authorization bearer key and X-CacheKit-Lock-Id (CWE-532). A root logger at
    # DEBUG would publish the key, so hold hpack at INFO while its level is unset. A level the application
    # sets, before or after a client is built, wins: setting DEBUG is an explicit opt-in (SECURITY.md).
    if _hpack_logger.level == logging.NOTSET:
        _hpack_logger.setLevel(logging.INFO)


def _client_kwargs(config: CachekitIOBackendConfig) -> dict[str, Any]:
    # Every client carries the bearer key, so no client is built before the pin.
    _pin_hpack_logger()
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
            "User-Agent": _USER_AGENT,
        },
    }


def lease_async_http_client(config: CachekitIOBackendConfig) -> AsyncClientLease:
    """Lease the async HTTP clients for this config (each created on first use on its thread and loop).

    Args:
        config: cachekit.io backend configuration

    Returns:
        AsyncClientLease: its ``.client`` is the async client for exactly this config, this thread
        and the running event loop
    """
    return AsyncClientLease(config)


def lease_sync_http_client(config: CachekitIOBackendConfig) -> SyncClientLease:
    """Lease the per-thread sync HTTP client for this config (created on first use).

    Args:
        config: cachekit.io backend configuration

    Returns:
        SyncClientLease: its ``.client`` is the thread-local sync client for exactly this config,
        open for as long as the lease is held
    """
    leases = _clients().sync_leases
    key = _client_key(config)
    # Bind to a local first: the weak dict alone would let a fresh lease die on insertion.
    lease = leases.get(key)
    if lease is None:
        lease = leases[key] = SyncClientLease(config)
    return lease


# The exit stack runs every pushed close even when an earlier one raises, then re-raises:
# one failing client can neither leak the rest nor leave closed clients in the cache.
async def close_async_client() -> None:
    """Close this thread's async client instances (useful for cleanup).

    A backend that is used again afterwards gets a new client, never a closed one.
    """
    async with AsyncExitStack() as stack:
        for slot in list(_clients().async_slots.values()):
            client = slot.take()
            if client is not None:
                stack.push_async_callback(client.aclose)


def close_sync_client() -> None:
    """Close this thread's sync client instances (useful for cleanup)."""
    leases = _clients().sync_leases
    with ExitStack() as stack:
        for lease in leases.values():
            stack.callback(lease.client.close)
        leases.clear()


def reset_global_client() -> None:
    """Drop this thread's cached clients without closing them (useful for testing).

    Note: This does not properly close clients. Use close_*_client() for proper cleanup.
    """
    clients = _clients()
    for slot in list(clients.async_slots.values()):
        slot.take()
    clients.sync_leases.clear()


__all__ = [
    "AsyncClientLease",
    "lease_async_http_client",
    "SyncClientLease",
    "lease_sync_http_client",
    "close_async_client",
    "close_sync_client",
    "reset_global_client",
]
