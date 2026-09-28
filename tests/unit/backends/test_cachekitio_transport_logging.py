"""The HTTP/2 transport's DEBUG logs must not expose the API key or the lock token (CWE-532).

hpack, the header encoder under httpx's HTTP/2 support, logs every header block it encodes at
DEBUG. It redacts the value of a header marked sensitive (``authorization``) in one line, but the
encoded block in the next line decodes straight back to it, and ``X-CacheKit-Lock-Id`` is logged in
clear. These tests drive a real lock acquire + release through httpx -> httpcore -> h2 -> hpack over
httpcore's mock network stream and scan every record from every logger. ``httpx.MockTransport``
replaces httpcore, h2 and hpack wholesale, so a test built on it would prove nothing here.

The pin runs when a CachekitIO client is built, never at import, so "set before importing cachekit"
is the before-construction case.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Iterator

import hpack
import httpcore
import httpcore._async.connection_pool as httpcore_connection_pool
import hyperframe.frame
import pytest

from cachekit.backends.cachekitio.backend import CachekitIOBackend
from cachekit.backends.cachekitio.client import reset_global_client

_API_KEY = "ck_live_SYNTHETIC_hpack_probe"  # pragma: allowlist secret — fake key, test fixture
_LOCK_ID = "lock-SYNTHETIC-7f3a9c"
_SECRETS = {_API_KEY, _LOCK_ID}
_KEY = "ns:t:func:m.f:args:" + "a" * 64 + ":1s"


def _server_frames() -> list[bytes]:
    """The server half of one HTTP/2 connection: SETTINGS, the lock POST's reply (stream 1), the DELETE's (stream 3)."""
    # One encoder for both replies: the client decodes them against one shared dynamic table.
    encoder = hpack.Encoder()
    post_headers = encoder.encode([(b":status", b"200"), (b"content-type", b"application/json")])
    delete_headers = encoder.encode([(b":status", b"204")])
    return [
        hyperframe.frame.SettingsFrame().serialize(),
        hyperframe.frame.HeadersFrame(stream_id=1, data=post_headers, flags=["END_HEADERS"]).serialize(),
        hyperframe.frame.DataFrame(
            stream_id=1, data=json.dumps({"lock_id": _LOCK_ID}).encode(), flags=["END_STREAM"]
        ).serialize(),
        hyperframe.frame.HeadersFrame(stream_id=3, data=delete_headers, flags=["END_HEADERS", "END_STREAM"]).serialize(),
    ]


@pytest.fixture
def hpack_logger() -> Iterator[logging.Logger]:
    """The hpack logger as a fresh process has it: level unset. Its prior level is restored afterwards."""
    hpack_logger = logging.getLogger("hpack")
    saved = hpack_logger.level
    hpack_logger.setLevel(logging.NOTSET)
    yield hpack_logger
    hpack_logger.setLevel(saved)


@pytest.fixture
def make_backend(monkeypatch: pytest.MonkeyPatch, hpack_logger: logging.Logger) -> Iterator[Callable[[], CachekitIOBackend]]:
    """Build a CachekitIOBackend on a fresh client whose sockets are httpcore's mock stream.

    httpx has no hook for the network backend, so httpcore's default is swapped for the mock; everything
    above the socket is the real stack. ``setattr`` raises if httpcore renames it, rather than letting the
    test fall through to a real connection.
    """
    network = httpcore.AsyncMockBackend(_server_frames(), http2=True)
    monkeypatch.setattr(httpcore_connection_pool, "AutoBackend", lambda: network)
    reset_global_client()
    yield lambda: CachekitIOBackend(api_url="https://api.cachekit.io", api_key=_API_KEY)
    reset_global_client()


async def _lock_cycle_records(backend: CachekitIOBackend, caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    """Acquire and release a lock with the root logger at DEBUG; return every record logged meanwhile."""
    caplog.set_level(logging.DEBUG)  # root at DEBUG, as logging.basicConfig(level=logging.DEBUG) leaves it
    caplog.clear()
    async with backend.acquire_lock(_KEY, timeout=1.0) as acquired:
        assert acquired
    records = list(caplog.records)
    # Both requests completed over HTTP/2, so h2 and hpack encoded the Authorization header on both and
    # X-CacheKit-Lock-Id on the DELETE.
    requests = [r.getMessage() for r in records if r.name == "httpx"]
    assert any(m.startswith("HTTP Request: POST ") and '"HTTP/2 200' in m for m in requests), requests
    assert any(m.startswith("HTTP Request: DELETE ") and '"HTTP/2 204' in m for m in requests), requests
    return records


def _recoverable(records: list[logging.LogRecord]) -> set[str]:
    """The secrets a log reader can recover: from a message, an argument, or an hpack block an argument carries.

    Blocks are decoded in log order with one decoder per logging call site, as the peer decodes them,
    because a block may reference dynamic-table entries an earlier block on the connection added.
    """
    decoders: dict[tuple[str, object], hpack.Decoder] = {}
    found: set[str] = set()
    for record in records:
        args = record.args if isinstance(record.args, tuple) else (record.args,)
        texts = [record.getMessage(), *(repr(arg) for arg in args)]
        for arg in args:
            if not isinstance(arg, (bytes, bytearray, memoryview)):
                continue
            decoder = decoders.setdefault((record.name, record.msg), hpack.Decoder())
            try:
                texts.append(repr(decoder.decode(bytes(arg))))
            except hpack.HPACKError:
                continue  # not a header block
        found |= {secret for secret in _SECRETS if any(secret in text for text in texts)}
    return found


@pytest.mark.unit
@pytest.mark.security
class TestHpackDebugLogging:
    async def test_root_debug_exposes_neither_api_key_nor_lock_id(
        self, make_backend: Callable[[], CachekitIOBackend], caplog: pytest.LogCaptureFixture
    ) -> None:
        records = await _lock_cycle_records(make_backend(), caplog)
        assert _recoverable(records) == set()

    def test_client_construction_pins_unset_hpack_at_info(
        self, make_backend: Callable[[], CachekitIOBackend], hpack_logger: logging.Logger
    ) -> None:
        make_backend()
        assert hpack_logger.level == logging.INFO

    async def test_operator_debug_set_before_construction_is_kept(
        self,
        make_backend: Callable[[], CachekitIOBackend],
        hpack_logger: logging.Logger,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        hpack_logger.setLevel(logging.DEBUG)
        records = await _lock_cycle_records(make_backend(), caplog)
        assert hpack_logger.level == logging.DEBUG
        # Opting in brings hpack's output back, and with it both secrets — what SECURITY.md warns of.
        assert _recoverable(records) == _SECRETS

    async def test_operator_debug_set_after_construction_wins(
        self,
        make_backend: Callable[[], CachekitIOBackend],
        hpack_logger: logging.Logger,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        backend = make_backend()
        assert hpack_logger.level == logging.INFO  # the pin ran at construction, so DEBUG below overrides it
        hpack_logger.setLevel(logging.DEBUG)
        records = await _lock_cycle_records(backend, caplog)
        assert _recoverable(records) == _SECRETS
