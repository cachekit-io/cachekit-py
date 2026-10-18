"""The transport's DEBUG logs must not expose the API key or the lock token (CWE-532).

A root logger at DEBUG (``logging.basicConfig(level=logging.DEBUG)``) turns on every library's debug
output, and a reader of those logs must not recover the ``Authorization: Bearer`` API key or the
``X-CacheKit-Lock-Id`` capability token from them. urllib3 logs each request line at DEBUG, path
included (``"DELETE /v1/cache/<key>/lock HTTP/1.1" 200``), but no header. That is why the lock token
rides a header and never the query string, and why the key alone may appear in a path (it is the
caller's own, percent-encoded).

These tests send a GET, a PUT and a lock-token DELETE over real sockets, through the SDK's own client,
to tests/performance/loopback_saas.py, and scan every record from every logger. They also require
urllib3's request-line records, so a logging setup that captured nothing cannot pass them.
"""

from __future__ import annotations

import logging
import shutil
import uuid
from pathlib import Path

import pytest

from cachekit.backends.cachekitio import config as config_module
from cachekit.backends.cachekitio.backend import LOCK_ID_HEADER, CachekitIOBackend

pytestmark = [
    pytest.mark.unit,
    pytest.mark.security,
    pytest.mark.skipif(shutil.which("openssl") is None, reason="needs openssl for the loopback certificate"),
]

_KEY = "ns:t:func:m.f:args:" + "a" * 64 + ":1s"
_LOCK_ID = "lock-SYNTHETIC-7f3a9c"


@pytest.fixture
def api_key() -> str:
    # Unique per test: a backend still alive from an earlier test must never lend this one its client.
    return f"ck_live_SYNTHETIC_{uuid.uuid4().hex}"


@pytest.fixture
def backend(monkeypatch: pytest.MonkeyPatch, fake_saas: tuple[int, Path], api_key: str) -> CachekitIOBackend:
    port, cert = fake_saas
    monkeypatch.setattr(config_module, "is_private_ip", lambda hostname: False)
    monkeypatch.setenv("CACHEKIT_ALLOW_CUSTOM_HOST", "true")
    monkeypatch.setenv("SSL_CERT_FILE", str(cert))  # OpenSSL's default trust store reads it
    return CachekitIOBackend(api_url=f"https://127.0.0.1:{port}", api_key=api_key)


def _texts(record: logging.LogRecord) -> list[str]:
    """Everything a log reader can see of a record: the message, its arguments, and every attribute (extras too)."""
    args = record.args if isinstance(record.args, tuple) else (record.args,)
    return [record.getMessage(), *(repr(arg) for arg in args), repr(vars(record))]


def _assert_request_logged(records: list[logging.LogRecord], method: str, path: str) -> None:
    lines = [r.getMessage() for r in records if r.name == "urllib3.connectionpool"]
    assert any(f'"{method} {path} HTTP/1.1"' in line for line in lines), (method, path, lines)


@pytest.mark.parametrize("via", ["delete_lock", "request_sync"])
async def test_root_debug_exposes_neither_api_key_nor_lock_id(
    backend: CachekitIOBackend, api_key: str, caplog: pytest.LogCaptureFixture, via: str
) -> None:
    caplog.set_level(logging.DEBUG)  # root at DEBUG, as logging.basicConfig(level=logging.DEBUG) leaves it
    caplog.clear()
    encoded = backend._encode_key(_KEY)

    backend.set(_KEY, b"value", ttl=60)
    assert backend.get(_KEY) == b"value"
    if via == "delete_lock":
        # The release path acquire_lock runs on exit; True means the DELETE got a 2xx.
        assert await backend._delete_lock(_KEY, _LOCK_ID)
    else:
        assert backend._request_sync("DELETE", f"{encoded}/lock", headers={LOCK_ID_HEADER: _LOCK_ID}).status == 200

    records = list(caplog.records)
    _assert_request_logged(records, "PUT", f"/v1/cache/{encoded}")
    _assert_request_logged(records, "GET", f"/v1/cache/{encoded}")
    _assert_request_logged(records, "DELETE", f"/v1/cache/{encoded}/lock")
    leaks = [
        (record.name, secret)
        for record in records
        for secret in (api_key, _LOCK_ID)
        if any(secret in text for text in _texts(record))
    ]
    assert leaks == []
