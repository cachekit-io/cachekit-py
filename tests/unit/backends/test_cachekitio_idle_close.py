"""A pooled connection the server closed while idle is replaced, never failed on (retries off).

urllib3 has no client-side idle expiry: an idle connection stays pooled until the server closes it, as
Cloudflare does after 400 s idle. Before reusing a pooled connection, urllib3 checks whether the peer has closed
it, and opens a new one if so. The client sends with ``retries=False``, so that check is the only thing between
an idle close and a failed request: this test makes a real TLS server close each connection soon after its last
response, waits past that, and requires the next request to succeed on a new connection.
"""

from __future__ import annotations

import shutil
import socket
import ssl
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from cachekit.backends.cachekitio import config as config_module
from cachekit.backends.cachekitio.backend import CachekitIOBackend

pytestmark = [
    pytest.mark.unit,
    pytest.mark.skipif(shutil.which("openssl") is None, reason="needs openssl for the loopback certificate"),
]

_IDLE_CLOSE_S = 0.2


class _IdleClosingServer:
    """Answers 404 to every request, and closes a connection once it has been idle for _IDLE_CLOSE_S."""

    def __init__(self, cert: Path, key: Path, *, close_notify: bool) -> None:
        self.connections = 0
        self._close_notify = close_notify
        self._ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        self._ctx.load_cert_chain(cert, key)
        self._listener = socket.create_server(("127.0.0.1", 0))
        self.port = self._listener.getsockname()[1]
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self) -> None:
        while True:
            try:
                raw, _ = self._listener.accept()
                conn = self._ctx.wrap_socket(raw, server_side=True)
            except OSError:
                if self._listener.fileno() == -1:
                    return  # closed
                continue
            self.connections += 1
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn: ssl.SSLSocket) -> None:
        conn.settimeout(_IDLE_CLOSE_S)
        buf = b""
        try:
            while True:
                try:
                    while b"\r\n\r\n" not in buf:
                        data = conn.recv(65536)
                        if not data:
                            return
                        buf += data
                except TimeoutError:
                    return  # idle: close it
                _, buf = buf.split(b"\r\n\r\n", 1)  # the client's GETs carry no body
                conn.sendall(b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\n\r\n")
        finally:
            if self._close_notify:
                try:
                    conn.unwrap().close()  # TLS close_notify, then FIN
                except (ssl.SSLError, OSError):
                    conn.close()
            else:
                conn.close()  # SSLSocket.close sends no close_notify: FIN only

    def close(self) -> None:
        self._listener.close()


@pytest.fixture(params=[True, False], ids=["close_notify", "fin_only"])
def server(request: pytest.FixtureRequest, fake_saas: tuple[int, Path]) -> Iterator[_IdleClosingServer]:
    _, cert = fake_saas
    peer = _IdleClosingServer(cert, cert.parent / "key.pem", close_notify=request.param)
    yield peer
    peer.close()


@pytest.fixture
def backend(monkeypatch: pytest.MonkeyPatch, fake_saas: tuple[int, Path], server: _IdleClosingServer) -> CachekitIOBackend:
    _, cert = fake_saas
    monkeypatch.setattr(config_module, "is_private_ip", lambda hostname: False)
    monkeypatch.setenv("CACHEKIT_ALLOW_CUSTOM_HOST", "true")
    monkeypatch.setenv("SSL_CERT_FILE", str(cert))
    for var in ("HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy"):
        monkeypatch.delenv(var, raising=False)
    api_key = "ck_test_idle_close"  # pragma: allowlist secret — fake key, test fixture
    return CachekitIOBackend(api_url=f"https://127.0.0.1:{server.port}", api_key=api_key)


def test_request_after_an_idle_close_reconnects(backend: CachekitIOBackend, server: _IdleClosingServer) -> None:
    assert backend.get("k") is None
    assert backend.get("k") is None  # back to back: the same connection
    assert server.connections == 1
    for expected in (2, 3):
        time.sleep(_IDLE_CLOSE_S * 4)  # the server has closed the pooled connection by now
        assert backend.get("k") is None
        assert server.connections == expected
