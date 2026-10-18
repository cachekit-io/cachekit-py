"""The CachekitIO connection pool policy: TCP keepalive probes on every connection, env proxies honoured.

urllib3 reads no proxy settings itself, so the client reads them the way the standard library does and builds
its pool from a ProxyManager when one applies. Both halves are pinned on the pool the client builds (its
proxy, proxy headers, socket options and limits) and against real sockets: a direct connection carries the
keepalive options, and a client under a proxy setting tunnels through the proxy.
"""

from __future__ import annotations

import base64
import socket
import socketserver
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

import pytest
from pydantic import SecretStr
from urllib3 import HTTPHeaderDict
from urllib3.exceptions import ProxyError

from cachekit.backends.cachekitio import client as client_module
from cachekit.backends.cachekitio.client import HTTPClient
from cachekit.backends.cachekitio.config import CachekitIOBackendConfig
from tests.utils.cachekitio_fakes import fake_backend, response

pytestmark = pytest.mark.unit

_API_HOST = "api.cachekit.io"


@pytest.fixture
def config() -> CachekitIOBackendConfig:
    return CachekitIOBackendConfig(
        api_url=f"https://{_API_HOST}",
        api_key=SecretStr("ck_test_key"),  # noqa: S106
        timeout=3.5,
        connection_pool_size=7,
    )


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:
        self.send_response(204)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *args: Any) -> None:
        pass


class _ConnectProxy(socketserver.ThreadingTCPServer):
    """Records each request head it receives and refuses it with a 502, so no traffic leaves the host."""

    daemon_threads = True

    def __init__(self) -> None:
        self.request_targets: list[str] = []
        self.request_headers: list[HTTPHeaderDict] = []
        proxy = self

        class Handler(socketserver.StreamRequestHandler):
            def handle(self) -> None:
                # Method and target only: CPython's tunnel sends CONNECT as HTTP/1.0 before 3.12 and HTTP/1.1 after.
                method, target, _version = self.rfile.readline().decode().split()
                proxy.request_targets.append(f"{method} {target}")
                headers = HTTPHeaderDict()
                while (line := self.rfile.readline()) not in (b"\r\n", b""):
                    name, _, value = line.decode().partition(":")
                    headers.add(name.strip(), value.strip())
                proxy.request_headers.append(headers)
                self.wfile.write(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\n\r\n")

        super().__init__(("127.0.0.1", 0), Handler)


@pytest.fixture
def local_server() -> Iterator[str]:
    server = HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


@pytest.fixture
def connect_proxy() -> Iterator[_ConnectProxy]:
    proxy = _ConnectProxy()
    thread = threading.Thread(target=proxy.serve_forever, daemon=True)
    thread.start()
    yield proxy
    proxy.shutdown()
    proxy.server_close()


class TestEnvProxy:
    """Which proxy, if any, the built pool goes through, from the standard proxy variables."""

    def test_no_proxy_env_connects_directly(self, config: CachekitIOBackendConfig) -> None:
        assert HTTPClient(config).pool.proxy is None

    @pytest.mark.parametrize("variable", ["HTTPS_PROXY", "https_proxy"])
    def test_https_proxy_is_used(self, config: CachekitIOBackendConfig, monkeypatch: pytest.MonkeyPatch, variable: str) -> None:
        monkeypatch.setenv(variable, "http://proxy.example:3128")
        assert str(HTTPClient(config).pool.proxy) == "http://proxy.example:3128"

    def test_all_proxy_is_used_without_https_proxy(
        self, config: CachekitIOBackendConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ALL_PROXY", "http://all.example:3128")
        assert str(HTTPClient(config).pool.proxy) == "http://all.example:3128"

    def test_https_proxy_wins_over_all_proxy(self, config: CachekitIOBackendConfig, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ALL_PROXY", "http://all.example:3128")
        monkeypatch.setenv("HTTPS_PROXY", "http://https.example:3128")
        assert str(HTTPClient(config).pool.proxy) == "http://https.example:3128"

    @pytest.mark.parametrize("no_proxy", [_API_HOST, ".cachekit.io", "*", f"other.example, {_API_HOST}"])
    def test_no_proxy_covering_the_host_connects_directly(
        self, config: CachekitIOBackendConfig, monkeypatch: pytest.MonkeyPatch, no_proxy: str
    ) -> None:
        monkeypatch.setenv("HTTPS_PROXY", "http://proxy.example:3128")
        monkeypatch.setenv("NO_PROXY", no_proxy)
        assert HTTPClient(config).pool.proxy is None

    def test_no_proxy_for_another_host_keeps_the_proxy(
        self, config: CachekitIOBackendConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("HTTPS_PROXY", "http://proxy.example:3128")
        monkeypatch.setenv("NO_PROXY", "other.example")
        assert str(HTTPClient(config).pool.proxy) == "http://proxy.example:3128"

    def test_schemeless_proxy_is_taken_as_http(self, config: CachekitIOBackendConfig, monkeypatch: pytest.MonkeyPatch) -> None:
        """curl and requests read ``host:port`` as an http:// proxy; urllib3 would refuse it."""
        monkeypatch.setenv("HTTPS_PROXY", "proxy.local:3128")
        proxy = HTTPClient(config).pool.proxy
        assert proxy is not None
        assert (proxy.scheme, proxy.host, proxy.port) == ("http", "proxy.local", 3128)

    def test_proxy_url_credentials_become_proxy_authorization(
        self, config: CachekitIOBackendConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """urllib3 sends nothing from the proxy URL's userinfo itself; percent-escapes are decoded before encoding."""
        monkeypatch.setenv("HTTPS_PROXY", "http://us%40er:p%3Ass@proxy.example:3128")  # pragma: allowlist secret
        pool = HTTPClient(config).pool
        expected = "Basic " + base64.b64encode(b"us@er:p:ss").decode()
        assert HTTPHeaderDict(pool.proxy_headers)["Proxy-Authorization"] == expected

    def test_proxy_without_credentials_sends_no_proxy_authorization(
        self, config: CachekitIOBackendConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("HTTPS_PROXY", "http://proxy.example:3128")
        assert "Proxy-Authorization" not in HTTPHeaderDict(HTTPClient(config).pool.proxy_headers)


class TestPoolLimits:
    """Socket options, size, blocking and timeouts, on a direct pool and a proxied one alike."""

    @pytest.fixture(params=["direct", "proxied"])
    def pool(self, request: pytest.FixtureRequest, config: CachekitIOBackendConfig, monkeypatch: pytest.MonkeyPatch) -> Any:
        if request.param == "proxied":
            monkeypatch.setenv("HTTPS_PROXY", "http://proxy.example:3128")
        pool = HTTPClient(config).pool
        assert (pool.proxy is not None) == (request.param == "proxied")
        return pool

    def test_keepalive_socket_options(self, pool: Any) -> None:
        """No probes reached through a proxy under httpx; here the tunnel connection carries them too."""
        # The list replaces urllib3's default options, so its TCP_NODELAY is repeated.
        options = pool.conn_kw["socket_options"]
        assert options[:2] == [(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1), (socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)]
        if hasattr(socket, "TCP_KEEPIDLE"):
            # Probes from 60 s idle, every 10 s, 3 misses: a dead path is found in about 90 s.
            assert options[2:] == [
                (socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, 60),
                (socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, 10),
                (socket.IPPROTO_TCP, socket.TCP_KEEPCNT, 3),
            ]

    def test_size_overflow_and_timeouts(self, pool: Any, config: CachekitIOBackendConfig) -> None:
        """A request that finds every connection busy opens one more rather than queueing behind another thread."""
        assert pool.pool.maxsize == config.connection_pool_size  # the queue of pooled connections
        assert pool.block is False
        assert pool.timeout.connect_timeout == config.timeout
        assert pool.timeout.read_timeout == config.timeout
        assert (pool.scheme, pool.host, pool.port) == ("https", _API_HOST, 443)


def test_every_request_sends_once_without_redirect() -> None:
    """Retries and redirects are the backend's call, never urllib3's: a redirect would carry the bearer key elsewhere."""
    backend, pool = fake_backend(
        lambda request: response(404) if request.method in ("GET", "HEAD") else response(200), timeout=2.5
    )
    backend.get("k")
    backend.set("k", b"v")
    backend.exists("k")
    backend.delete("k")
    assert [r.options for r in pool.requests] == [{"retries": False, "redirect": False}] * 4


def test_keepalive_socket_options_are_applied(config: CachekitIOBackendConfig, local_server: str) -> None:
    """On a real socket: the options reach the kernel, not just the pool's kwargs."""
    # Config validation refuses a plain-HTTP loopback URL, so the pool is built from an unvalidated copy;
    # the pool policy itself is the SDK's own.
    pool = client_module._connection_pool(config.model_copy(update={"api_url": local_server}))
    try:
        assert pool.urlopen("GET", "/", retries=False).status == 204
        sock = pool._get_conn().sock
        assert sock is not None
        assert sock.getsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE) != 0
        assert sock.getsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY) != 0
        if hasattr(socket, "TCP_KEEPIDLE"):
            seen = tuple(
                sock.getsockopt(socket.IPPROTO_TCP, name)
                for name in (socket.TCP_KEEPIDLE, socket.TCP_KEEPINTVL, socket.TCP_KEEPCNT)
            )
            assert seen == (60, 10, 3)
    finally:
        pool.close()


@pytest.mark.parametrize("variable", ["HTTPS_PROXY", "https_proxy", "ALL_PROXY"])
def test_env_proxy_routes_requests(
    config: CachekitIOBackendConfig, connect_proxy: _ConnectProxy, monkeypatch: pytest.MonkeyPatch, variable: str
) -> None:
    monkeypatch.setenv(variable, f"http://127.0.0.1:{connect_proxy.server_address[1]}")
    client = HTTPClient(config)
    with pytest.raises(ProxyError):
        client.request("GET", "/v1/cache/k")
    assert connect_proxy.request_targets == [f"CONNECT {_API_HOST}:443"]
    assert "Proxy-Authorization" not in connect_proxy.request_headers[0]


def test_env_proxy_credentials_reach_the_proxy(
    config: CachekitIOBackendConfig, connect_proxy: _ConnectProxy, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The CONNECT itself carries Proxy-Authorization, and the bearer key never goes to the proxy in the clear."""
    proxy = f"http://us%40er:p%3Ass@127.0.0.1:{connect_proxy.server_address[1]}"  # pragma: allowlist secret
    monkeypatch.setenv("HTTPS_PROXY", proxy)
    client = HTTPClient(config)
    with pytest.raises(ProxyError):
        client.request("GET", "/v1/cache/k")
    assert connect_proxy.request_targets == [f"CONNECT {_API_HOST}:443"]
    headers = connect_proxy.request_headers[0]
    assert headers["Proxy-Authorization"] == "Basic " + base64.b64encode(b"us@er:p:ss").decode()
    assert "Authorization" not in headers
