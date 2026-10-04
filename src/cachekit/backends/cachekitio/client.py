"""HTTP client for cachekit.io: one urllib3 connection pool per config, shared by every thread of a process.

urllib3's pool is thread-safe, so one client serves every thread that calls a backend, and the async backend
methods send on that same client through ``asyncio.to_thread``. A cached client lives as long as some
CachekitIOBackend uses it, and is closed when its last backend is released.

A forked child never sees its parent's cached clients: their pooled connections share the parent's TLS sessions,
so a request on one from both processes desynchronises it. The cache is owned by a PID, and a child
(``os.fork()``, or a fork from C that runs no at-fork hook) starts with an empty one.
"""

from __future__ import annotations

import logging
import os
import socket
import threading
import urllib.request
import weakref
from importlib.metadata import PackageNotFoundError, version
from typing import TYPE_CHECKING, Any
from urllib.parse import unquote

import urllib3
from urllib3.util import Timeout, make_headers, parse_url

from cachekit.hash_utils import redact_error_for_log
from cachekit.logging import get_structured_logger

if TYPE_CHECKING:
    from urllib3 import BaseHTTPResponse, HTTPSConnectionPool

    from cachekit.backends.cachekitio.config import CachekitIOBackendConfig

_ClientKey = tuple[str, str, float, int]


def _user_agent() -> str:
    # Edge analytics group SaaS traffic by User-Agent; the SaaS reads only the first product token.
    # The version comes from the installed distribution, so it cannot drift from the release. A source-only or vendored
    # copy has no distribution metadata; it still identifies as cachekit-py rather than failing the import.
    # Stdlib logger, not _logger: this runs once at import, and the structured logger samples records away.
    try:
        sdk = version("cachekit")
    except PackageNotFoundError:
        logging.getLogger(__name__).debug("No cachekit distribution metadata; User-Agent reports cachekit-py/unknown")
        sdk = "unknown"
    return f"cachekit-py/{sdk} urllib3/{version('urllib3')}"


_USER_AGENT = _user_agent()

_logger = get_structured_logger(__name__)


def _keepalive_socket_options() -> list[tuple[int, int, int]]:
    # Cloudflare closes an idle client connection at 400 s, but NAT gateways drop idle flows sooner (AWS 350 s,
    # Azure 4 min) and silently. Probes from 60 s idle keep their mappings alive and detect a dead path in about
    # 90 s, instead of a read timeout and a miss on the next request. These replace urllib3's default options,
    # so its TCP_NODELAY is repeated.
    options = [(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1), (socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)]
    # macOS names the idle option TCP_KEEPALIVE; a platform without one keeps the kernel's defaults.
    idle = getattr(socket, "TCP_KEEPIDLE", None) or getattr(socket, "TCP_KEEPALIVE", None)
    for name, value in ((idle, 60), (getattr(socket, "TCP_KEEPINTVL", None), 10), (getattr(socket, "TCP_KEEPCNT", None), 3)):
        if name is not None:
            options.append((socket.IPPROTO_TCP, name, value))
    return options


_KEEPALIVE_SOCKET_OPTIONS = _keepalive_socket_options()


def _env_proxy(host: str) -> str | None:
    """The proxy URL the environment sets for HTTPS requests to ``host``, or None to connect directly.

    urllib3 reads no proxy settings itself, so the standard library's reading of them is used:
    ``HTTPS_PROXY``, then ``ALL_PROXY``, unless ``NO_PROXY`` covers the host. ``getproxies`` also reads
    the macOS and Windows system settings. A proxy URL with no scheme is taken as ``http://``.
    """
    proxies = urllib.request.getproxies()
    proxy = proxies.get("https") or proxies.get("all")
    if not proxy or urllib.request.proxy_bypass(host):
        return None
    return proxy if "://" in proxy else f"http://{proxy}"


def _connection_pool(config: CachekitIOBackendConfig) -> HTTPSConnectionPool:
    pool_kw: dict[str, Any] = {
        "timeout": Timeout(connect=config.timeout, read=config.timeout),
        # A request that finds every connection busy waits for one, up to the timeout (pool_timeout in request()),
        # rather than opening connections past the configured size.
        "maxsize": config.connection_pool_size,
        "block": True,
        "socket_options": _KEEPALIVE_SOCKET_OPTIONS,
    }
    proxy = _env_proxy(parse_url(config.api_url).host or "")
    if proxy is None:
        manager = urllib3.PoolManager(**pool_kw)
    else:
        # urllib3 sends no credentials from the proxy URL itself.
        proxy_auth = parse_url(proxy).auth
        proxy_headers = make_headers(proxy_basic_auth=unquote(proxy_auth)) if proxy_auth else None
        manager = urllib3.ProxyManager(proxy, proxy_headers=proxy_headers, **pool_kw)
    # The config only accepts https:// URLs.
    return manager.connection_from_url(config.api_url)  # type: ignore[return-value]


class HTTPClient:
    """Sends requests to one config's API under its key. Thread-safe: share one per process."""

    def __init__(self, config: CachekitIOBackendConfig) -> None:
        # A path on api_url prefixes every request path.
        self._prefix = (parse_url(config.api_url).path or "").rstrip("/")
        self._timeout = config.timeout
        self.headers = {
            "Authorization": f"Bearer {config.api_key.get_secret_value()}",
            "Content-Type": "application/octet-stream",
            "User-Agent": _USER_AGENT,
        }
        self.pool = _connection_pool(config)

    def request(
        self, method: str, path: str, *, body: bytes | None = None, headers: dict[str, str] | None = None
    ) -> BaseHTTPResponse:
        """Send one request and read the whole response; a header in ``headers`` replaces the client's own.

        No retry and no redirect: a transport failure raises urllib3's own exception, and a 3xx is returned.
        """
        return self.pool.urlopen(
            method,
            self._prefix + path,
            body=body,
            headers={**self.headers, **headers} if headers else self.headers,
            retries=False,
            redirect=False,
            pool_timeout=self._timeout,  # type: ignore[arg-type]  # annotated int; queue.get takes a float
        )

    def close(self) -> None:
        self.pool.close()


class SyncClientLease:
    """Sole owner of a cached client, which is closed once its last holder drops the lease.

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
        self.client = HTTPClient(config)
        # atexit=False: exit-time finalizers run while daemon threads (stale-while-revalidate) are still
        # alive, so closing then could pull a client out from under an in-flight request. The process
        # reclaims the sockets at exit anyway.
        weakref.finalize(self, _close_released_client, self.client, self.pid).atexit = False


def _close_released_client(client: HTTPClient, owner_pid: int) -> None:
    # A forked child drops an inherited lease unclosed: the client's connections are its parent's too,
    # and close() takes the pool's queue lock, which a parent thread may have held at fork. Garbage
    # collection closes the child's copies of the sockets, which sends nothing.
    if os.getpid() != owner_pid:
        return
    # A finalizer has no caller to report to, so the expected failure (a socket that will not close
    # cleanly) is logged, not raised. Anything else is a bug; the interpreter reports it as
    # unraisable instead of it vanishing here.
    try:
        client.close()
    except OSError as e:
        _logger.debug("Closing a released cachekit.io HTTP client failed", error=redact_error_for_log(e))


class _Leases:
    """This process's lease cache, keyed by the config values baked into a client.

    The key matters: Authorization, the URL and the timeout are fixed at client creation, so one client
    for every backend would send every backend's traffic under the FIRST key constructed: cross-tenant
    writes and reads with no error anywhere. Values are weak: each CachekitIOBackend holds its lease
    strongly, so a lease stays cached while some backend uses it and is dropped when the last one goes.
    """

    def __init__(self) -> None:
        self.pid = os.getpid()
        self.lock = threading.Lock()
        self.by_key: weakref.WeakValueDictionary[_ClientKey, SyncClientLease] = weakref.WeakValueDictionary()


_leases = _Leases()


def _own_leases() -> _Leases:
    """This process's lease cache: a forked child replaces the inherited one, lock included.

    An owner-PID check rather than an os.register_at_fork hook: uWSGI forks without running Python's at-fork
    hooks. The PID travels with the cache and its lock in one published reference, so no thread pairs this
    process with the parent's lock, which a parent thread may have held at fork. Two child threads racing
    here each build a cache; one is kept, and the other's leases still work, unshared. Inherited leases are
    dropped, never closed (see _close_released_client).
    """
    global _leases
    leases = _leases
    if leases.pid != os.getpid():
        leases = _leases = _Leases()
    return leases


def _client_key(config: CachekitIOBackendConfig) -> _ClientKey:
    return (config.api_url, config.api_key.get_secret_value(), config.timeout, config.connection_pool_size)


def lease_sync_http_client(config: CachekitIOBackendConfig) -> SyncClientLease:
    """Lease this process's HTTP client for this config (created on first use).

    Args:
        config: cachekit.io backend configuration

    Returns:
        SyncClientLease: its ``.client`` is the client for exactly this config, open for as long as the
        lease is held
    """
    leases = _own_leases()
    key = _client_key(config)
    with leases.lock:
        # Bind to a local first: the weak dict alone would let a fresh lease die on insertion.
        lease = leases.by_key.get(key)
        if lease is None:
            lease = leases.by_key[key] = SyncClientLease(config)
    return lease


def close_sync_client() -> None:
    """Close this process's cached clients (useful for cleanup).

    A backend built earlier keeps its closed client, and its requests then fail: build a new backend.
    """
    leases = _own_leases()
    with leases.lock:
        held = list(leases.by_key.values())
        leases.by_key.clear()
    for lease in held:
        lease.client.close()


def reset_global_client() -> None:
    """Drop this process's cached clients without closing them (useful for testing).

    Note: This does not properly close clients. Use close_sync_client() for proper cleanup.
    """
    leases = _own_leases()
    with leases.lock:
        leases.by_key.clear()


__all__ = [
    "HTTPClient",
    "SyncClientLease",
    "lease_sync_http_client",
    "close_sync_client",
    "reset_global_client",
]
