"""The CachekitIO connection pool policy: 390 s idle connections with TCP keepalive probes, env proxies kept.

Probes need a transport of our own, and httpx turns env proxies off when a client is given one, so the
keepalive transport is mounted for all:// instead. These tests pin both halves against real sockets: a
direct connection carries the keepalive options, and a client under a proxy setting still uses the proxy.
"""

from __future__ import annotations

import asyncio
import os
import socket
import socketserver
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from cachekit.backends.cachekitio import client as client_module
from cachekit.backends.cachekitio.config import CachekitIOBackendConfig

pytestmark = pytest.mark.unit

_HTTP_CLIENTS = [(httpx.Client, httpx.HTTPTransport), (httpx.AsyncClient, httpx.AsyncHTTPTransport)]


@pytest.fixture
def config() -> CachekitIOBackendConfig:
    return CachekitIOBackendConfig(api_url="https://api.cachekit.io", api_key=SecretStr("ck_test_key"))  # noqa: S106


@pytest.fixture(autouse=True)
def _no_proxy_env(monkeypatch: pytest.MonkeyPatch) -> None:
    # urllib.request.getproxies() reads every *_proxy variable in either case; a developer's own must not leak in.
    for name in list(os.environ):
        if name.lower().endswith("_proxy"):
            monkeypatch.delenv(name)


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:
        self.send_response(204)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *args: Any) -> None:
        pass


class _ConnectProxy(socketserver.ThreadingTCPServer):
    """Records each request line it receives and refuses it with a 502, so no traffic leaves the host."""

    daemon_threads = True

    def __init__(self) -> None:
        self.request_lines: list[str] = []
        proxy = self

        class Handler(socketserver.StreamRequestHandler):
            def handle(self) -> None:
                proxy.request_lines.append(self.rfile.readline().decode().strip())
                while self.rfile.readline() not in (b"\r\n", b""):
                    pass
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


def _kwargs(config: CachekitIOBackendConfig, transport_cls: Any, base_url: str | None = None) -> dict[str, Any]:
    kwargs = client_module._client_kwargs(config, transport_cls)
    if base_url is not None:
        kwargs["base_url"] = base_url  # config validation refuses loopback; the mount and limits stay as built
    return kwargs


@pytest.mark.parametrize(("client_cls", "transport_cls"), _HTTP_CLIENTS)
def test_keepalive_socket_options_are_applied(
    config: CachekitIOBackendConfig, local_server: str, client_cls: Any, transport_cls: Any
) -> None:
    seen: dict[str, int] = {}
    kwargs = _kwargs(config, transport_cls, local_server)
    assert kwargs["limits"].keepalive_expiry == 390.0

    def trace(event: str, info: dict[str, Any]) -> None:
        if event == "connection.connect_tcp.complete":
            sock = info["return_value"].get_extra_info("socket")
            seen["keepalive"] = sock.getsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE)
            if hasattr(socket, "TCP_KEEPIDLE"):
                seen["idle"] = sock.getsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE)
                seen["interval"] = sock.getsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL)
                seen["count"] = sock.getsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT)

    async def atrace(event: str, info: dict[str, Any]) -> None:
        trace(event, info)

    if client_cls is httpx.Client:
        with httpx.Client(**kwargs) as client:
            client.get("/", extensions={"trace": trace})
    else:

        async def run() -> None:
            async with httpx.AsyncClient(**kwargs) as client:
                await client.get("/", extensions={"trace": atrace})

        asyncio.run(run())

    assert seen["keepalive"] != 0
    if hasattr(socket, "TCP_KEEPIDLE"):
        assert (seen["idle"], seen["interval"], seen["count"]) == (60, 10, 3)


@pytest.mark.parametrize("variable", ["HTTPS_PROXY", "https_proxy", "ALL_PROXY"])
@pytest.mark.parametrize(("client_cls", "transport_cls"), _HTTP_CLIENTS)
def test_env_proxy_still_routes_requests(
    config: CachekitIOBackendConfig,
    connect_proxy: _ConnectProxy,
    monkeypatch: pytest.MonkeyPatch,
    variable: str,
    client_cls: Any,
    transport_cls: Any,
) -> None:
    monkeypatch.setenv(variable, f"http://127.0.0.1:{connect_proxy.server_address[1]}")
    kwargs = _kwargs(config, transport_cls)
    # No probes reach through a proxy, so the idle time drops under the shortest NAT limit.
    assert kwargs["limits"].keepalive_expiry == 200.0

    if client_cls is httpx.Client:
        with httpx.Client(**kwargs) as client, pytest.raises(httpx.ProxyError):
            client.get("/v1/cache/k")
    else:

        async def run() -> None:
            async with httpx.AsyncClient(**kwargs) as client:
                await client.get("/v1/cache/k")

        with pytest.raises(httpx.ProxyError):
            asyncio.run(run())

    assert connect_proxy.request_lines == ["CONNECT api.cachekit.io:443 HTTP/1.1"]


def test_no_proxy_alone_keeps_httpx_transports(config: CachekitIOBackendConfig, monkeypatch: pytest.MonkeyPatch) -> None:
    # httpx maps a NO_PROXY host to its own default transport, which an all:// mount cannot reach.
    monkeypatch.setenv("NO_PROXY", "api.cachekit.io")
    kwargs = _kwargs(config, httpx.HTTPTransport)
    assert kwargs["mounts"] is None
    assert kwargs["limits"].keepalive_expiry == 200.0
