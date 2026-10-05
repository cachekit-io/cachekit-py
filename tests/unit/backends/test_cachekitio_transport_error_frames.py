"""A failed cachekit.io request raises a BackendError that reaches no frame holding the API key (CWE-532).

Error trackers capture the locals of every frame on a raised exception's traceback, and on every exception chained to it, by
default (Sentry's ``include_local_variables``), and serialise containers item by item; their scrubbers match top-level
variable names, never a value inside a dict. urllib3's request frames (``urlopen``, ``_make_request``) hold the request
headers, ``Authorization: Bearer <key>`` included, so the error must reach none of them: not through ``__cause__``,
``__context__`` or ``original_exception``.

Each request fails on a real loopback socket, so urllib3 leaves the frames a production failure leaves, and every frame is
walked, third-party ones included.
"""

from __future__ import annotations

import asyncio
import shutil
import socket
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from cachekit.backends.cachekitio import config as config_module
from cachekit.backends.cachekitio.backend import CachekitIOBackend
from cachekit.backends.errors import BackendError, BackendErrorType
from tests.unit.config.test_redacting_settings import _holds, _secret_forms

pytestmark = [pytest.mark.unit, pytest.mark.security]

_CALLS: dict[str, Callable[[CachekitIOBackend], object]] = {
    "get": lambda backend: backend.get("k"),
    "set": lambda backend: backend.set("k", b"v"),
    "get_async": lambda backend: asyncio.run(backend.get_async("k")),
    "set_async": lambda backend: asyncio.run(backend.set_async("k", b"v")),
}


@pytest.fixture(params=["refused", "stalled"])
def peer(request: pytest.FixtureRequest) -> Iterator[tuple[int, BackendErrorType]]:
    """A loopback port on which every request fails in transport, and the error type that failure classifies as.

    refused: bound but not listening, so the connect is refused. stalled: listening but never accepting, so the TLS
    handshake times out.
    """
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        if request.param == "stalled":
            sock.listen()
        yield sock.getsockname()[1], BackendErrorType.TRANSIENT if request.param == "refused" else BackendErrorType.TIMEOUT


@pytest.fixture(params=[str, str.encode], ids=["str-key", "bytes-key"])
def connect(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> Callable[[int], tuple[CachekitIOBackend, str]]:
    """Builds a backend for a loopback port, its API key passed as a str or as bytes, and returns it with that key."""
    monkeypatch.setattr(config_module, "is_private_ip", lambda hostname: False)
    monkeypatch.setenv("CACHEKIT_ALLOW_CUSTOM_HOST", "true")

    def build(port: int) -> tuple[CachekitIOBackend, str]:
        # Unique per test: a backend still alive from an earlier test must never lend this one its client.
        api_key = f"ck_live_SYNTHETIC_{uuid.uuid4().hex}"
        return CachekitIOBackend(api_url=f"https://127.0.0.1:{port}", api_key=request.param(api_key), timeout=0.5), api_key

    return build


def _raised(call: Callable[[], object]) -> BackendError:
    """The BackendError ``call`` raises, caught here so the test's own frame, which holds the key, is not on its traceback."""
    try:
        call()
    except BackendError as exc:
        return exc
    pytest.fail("the request did not fail")


def _frames_holding(exc: BaseException, secret: str) -> list[str]:
    """Every ``function:local`` that holds ``secret``, on the traceback of ``exc`` or of any exception reachable from it
    through ``__cause__``, ``__context__`` or ``original_exception``, whoever's frame it is."""
    texts, raw = _secret_forms(secret)
    found: list[str] = []
    seen: set[int] = set()
    pending: list[BaseException | None] = [exc]
    while pending:
        current = pending.pop()
        if current is None or id(current) in seen:
            continue
        seen.add(id(current))
        pending += [current.__cause__, current.__context__, getattr(current, "original_exception", None)]
        tb = current.__traceback__
        while tb is not None:
            found += [
                f"{tb.tb_frame.f_code.co_name}:{name}"
                for name, value in tb.tb_frame.f_locals.items()
                if _holds(value, texts, raw)
            ]
            tb = tb.tb_next
    return found


@pytest.mark.parametrize("call", _CALLS.values(), ids=_CALLS.keys())
def test_transport_failure_reaches_no_frame_holding_the_api_key(
    peer: tuple[int, BackendErrorType],
    connect: Callable[[int], tuple[CachekitIOBackend, str]],
    call: Callable[[CachekitIOBackend], object],
) -> None:
    port, error_type = peer
    backend, api_key = connect(port)

    err = _raised(lambda: call(backend))

    assert err.error_type == error_type
    assert _frames_holding(err, api_key) == []


@pytest.mark.skipif(shutil.which("openssl") is None, reason="needs openssl for the loopback certificate")
@pytest.mark.parametrize("mode", ["sync", "async"])
def test_error_status_reaches_no_frame_holding_the_api_key(
    monkeypatch: pytest.MonkeyPatch,
    fake_saas: tuple[int, Path],
    connect: Callable[[int], tuple[CachekitIOBackend, str]],
    mode: str,
) -> None:
    """A request answered with an error status chains the HTTPStatusError holding the response: no request frame either.

    The loopback fake answers POST with 405.
    """
    port, cert = fake_saas
    monkeypatch.setenv("SSL_CERT_FILE", str(cert))  # OpenSSL's default trust store reads it
    backend, api_key = connect(port)

    if mode == "sync":
        err = _raised(lambda: backend._request_sync("POST", "k"))
    else:
        err = _raised(lambda: asyncio.run(backend._request_async("POST", "k")))

    assert err.message == "Client error: HTTP 405"
    assert _frames_holding(err, api_key) == []
