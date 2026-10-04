"""The bytes CachekitIOBackend puts on the wire: each operation's request line, and the headers that carry meaning.

The SaaS routes on the request target and reads the bearer key, TTL, lock and metrics headers, so a transport
change must leave those byte-identical. The expected request lines below were captured from the httpx 0.28.1
client this SDK used before urllib3, against the same server, and urllib3 reproduces every one. The rest of the
head differs only where the transport speaks for itself: the User-Agent's second token, and httpx's
``Accept: */*``, ``Accept-Encoding: gzip, deflate`` and ``Connection: keep-alive``, which urllib3 replaces with
``Accept-Encoding: identity`` (values are LZ4-compressed already, and HTTP/1.1 keeps connections alive by default).

A raw TLS server records each request head as received, so nothing between the client and the socket can
normalise it away.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import socket
import ssl
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

from cachekit.backends.cachekitio import config as config_module
from cachekit.backends.cachekitio.backend import CachekitIOBackend
from cachekit.backends.cachekitio.client import _USER_AGENT

pytestmark = [
    pytest.mark.unit,
    pytest.mark.skipif(shutil.which("openssl") is None, reason="needs openssl for the loopback certificate"),
]

_API_KEY = "ck_test_request_lines"  # pragma: allowlist secret — fake key, test fixture
_CANONICAL = "ns:app:func:mod.fn:args:" + "ab" * 32 + ":1s"
_ENCODED = "ns%3Aapp%3Afunc%3Amod.fn%3Aargs%3A" + "ab" * 32 + "%3A1s"
# Every reserved character a custom key can carry, plus a space, a plus and a non-ASCII letter.
_ODD = "a/b?c#d%e f+é"
_ODD_ENCODED = "a%2Fb%3Fc%23d%25e%20f%2B%C3%A9"
_BODY = json.dumps({"lock_id": "lock-1", "ttl": 5, "success": True, "version": "x"}).encode()


class _CaptureServer:
    """Answers every request 200 with a JSON body that satisfies each operation, and keeps each raw head."""

    def __init__(self, cert: Path, key: Path) -> None:
        self.heads: list[bytes] = []
        self._ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        self._ctx.load_cert_chain(cert, key)
        self._listener = socket.create_server(("127.0.0.1", 0))
        self.port = self._listener.getsockname()[1]
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self) -> None:
        while True:
            try:
                raw, _ = self._listener.accept()
            except OSError:
                return  # closed
            try:
                conn = self._ctx.wrap_socket(raw, server_side=True)
            except (ssl.SSLError, OSError):
                continue
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn: ssl.SSLSocket) -> None:
        buf = b""
        with conn:
            while True:
                while b"\r\n\r\n" not in buf:
                    data = conn.recv(65536)
                    if not data:
                        return
                    buf += data
                head, buf = buf.split(b"\r\n\r\n", 1)
                length = next(
                    (
                        int(v)
                        for n, _, v in (line.partition(b":") for line in head.split(b"\r\n")[1:])
                        if n.lower() == b"content-length"
                    ),
                    0,
                )
                while len(buf) < length:
                    buf += conn.recv(65536)
                buf = buf[length:]
                self.heads.append(head)
                body = b"" if head.startswith(b"HEAD ") else _BODY
                conn.sendall(
                    b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: %d\r\n\r\n" % len(_BODY) + body
                )

    def close(self) -> None:
        self._listener.close()


@pytest.fixture
def server(fake_saas: tuple[int, Path]) -> Iterator[_CaptureServer]:
    _, cert = fake_saas
    capture = _CaptureServer(cert, cert.parent / "key.pem")
    yield capture
    capture.close()


@pytest.fixture
def backend(monkeypatch: pytest.MonkeyPatch, fake_saas: tuple[int, Path], server: _CaptureServer) -> CachekitIOBackend:
    _, cert = fake_saas
    monkeypatch.setattr(config_module, "is_private_ip", lambda hostname: False)
    monkeypatch.setenv("CACHEKIT_ALLOW_CUSTOM_HOST", "true")
    monkeypatch.setenv("SSL_CERT_FILE", str(cert))
    return CachekitIOBackend(api_url=f"https://127.0.0.1:{server.port}", api_key=_API_KEY)


def _run_every_operation(backend: CachekitIOBackend, key: str) -> None:
    backend.get(key)
    backend.get_with_freshness(key)
    backend.set(key, b"\x00value", ttl=60, stale_ttl=30)
    backend.delete(key)
    backend.exists(key)

    async def async_ops() -> None:
        await backend.get_async(key)
        await backend.set_async(key, b"\x00value", ttl=60, stale_ttl=30)
        await backend.delete_async(key)
        await backend.exists_async(key)
        async with backend.acquire_lock(key, timeout=2.5) as acquired:
            assert acquired
        await backend.get_ttl(key)
        await backend.refresh_ttl(key, 90)
        await backend.health_check_async()

    asyncio.run(async_ops())
    backend.health_check()


def _expected_lines(encoded: str) -> list[str]:
    target = f"/v1/cache/{encoded}"
    return [
        f"GET {target} HTTP/1.1",
        f"GET {target} HTTP/1.1",
        f"PUT {target} HTTP/1.1",
        f"DELETE {target} HTTP/1.1",
        f"HEAD {target} HTTP/1.1",
        f"GET {target} HTTP/1.1",
        f"PUT {target} HTTP/1.1",
        f"DELETE {target} HTTP/1.1",
        f"HEAD {target} HTTP/1.1",
        f"POST {target}/lock HTTP/1.1",
        f"DELETE {target}/lock HTTP/1.1",
        f"GET {target}/ttl HTTP/1.1",
        f"PATCH {target}/ttl HTTP/1.1",
        "GET /v1/cache/health HTTP/1.1",
        "GET /v1/cache/health HTTP/1.1",
    ]


def _headers(head: bytes) -> dict[str, str]:
    lines = head.decode("latin-1").split("\r\n")[1:]
    return {name.lower(): value for name, _, value in (line.partition(": ") for line in lines)}


@pytest.mark.parametrize(("key", "encoded"), [(_CANONICAL, _ENCODED), (_ODD, _ODD_ENCODED)], ids=["canonical", "custom"])
def test_request_lines_are_byte_identical(backend: CachekitIOBackend, server: _CaptureServer, key: str, encoded: str) -> None:
    _run_every_operation(backend, key)
    assert [head.split(b"\r\n", 1)[0].decode("latin-1") for head in server.heads] == _expected_lines(encoded)


def test_headers_that_carry_meaning(backend: CachekitIOBackend, server: _CaptureServer) -> None:
    _run_every_operation(backend, _CANONICAL)
    heads = [_headers(head) for head in server.heads]
    common = {
        "accept-encoding": "identity",
        "authorization": f"Bearer {_API_KEY}",
        "host": f"127.0.0.1:{server.port}",
        "user-agent": _USER_AGENT,
        "x-cachekit-l1-status": "disabled",
    }
    for raw, headers in zip(server.heads, heads, strict=True):
        assert {name: headers.get(name) for name in common} == common
        # No name twice: a per-request header replaces the client's own, never joins it.
        assert len(headers) == raw.count(b"\r\n")
    get, _, put, delete, head, *_ = heads
    lock_post, lock_delete, ttl_get, ttl_patch = heads[9:13]
    assert get["content-type"] == "application/octet-stream" and "content-length" not in get
    assert {
        name: put.get(name) for name in ("content-type", "content-length", "x-cachekit-ttl", "x-ttl", "x-cachekit-stale-ttl")
    } == {
        "content-type": "application/octet-stream",
        "content-length": "6",
        "x-cachekit-ttl": "60",
        "x-ttl": "60",
        "x-cachekit-stale-ttl": "30",
    }
    assert "content-length" not in delete and "content-length" not in head
    assert lock_post["content-type"] == "application/json"
    assert lock_post["content-length"] == str(len(json.dumps({"timeout_ms": 2500})))
    assert lock_delete["x-cachekit-lock-id"] == "lock-1"
    assert "x-cachekit-lock-id" not in ttl_get
    assert ttl_patch["content-type"] == "application/json"
    assert ttl_patch["content-length"] == str(len(json.dumps({"ttl": 90})))
