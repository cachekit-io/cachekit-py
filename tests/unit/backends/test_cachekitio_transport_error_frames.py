"""A failed cachekit.io request raises a BackendError that reaches no frame holding the API key (CWE-532).

Error trackers capture the locals of every frame on a raised exception's traceback, and on every exception chained to it, by
default (Sentry's ``include_local_variables``), and serialise containers item by item; their scrubbers match top-level
variable names, never a value inside a dict. urllib3's request frames (``urlopen``, ``_make_request``) hold the request
headers, ``Authorization: Bearer <key>`` included, so the error must reach none of them: not through ``__cause__``,
``__context__`` or ``original_exception``.
An interrupt raised mid-request (``SystemExit``, ``KeyboardInterrupt``) propagates as itself, so it must reach none of them
either.

Each request fails on a real loopback socket, so urllib3 leaves the frames a production failure leaves, and every frame is
walked, third-party ones included.
"""

from __future__ import annotations

import asyncio
import shutil
import signal
import socket
import traceback
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path
from types import SimpleNamespace, TracebackType

import pytest

from cachekit.backends.cachekitio import config as config_module
from cachekit.backends.cachekitio.backend import CachekitIOBackend
from cachekit.backends.errors import BackendError, BackendErrorType
from tests.unit.config.test_redacting_settings import _cachekit_locals_holding

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
def connect(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> Callable[..., tuple[CachekitIOBackend, str]]:
    """Builds a backend for a loopback port, its API key passed as a str or as bytes, and returns it with that key."""
    monkeypatch.setattr(config_module, "is_private_ip", lambda hostname: False)
    monkeypatch.setenv("CACHEKIT_ALLOW_CUSTOM_HOST", "true")

    def build(port: int, timeout: float = 0.5) -> tuple[CachekitIOBackend, str]:
        # Unique per test: a backend still alive from an earlier test must never lend this one its client.
        api_key = f"ck_live_SYNTHETIC_{uuid.uuid4().hex}"
        return CachekitIOBackend(api_url=f"https://127.0.0.1:{port}", api_key=request.param(api_key), timeout=timeout), api_key

    return build


def _raised(call: Callable[[], object]) -> BackendError:
    """The BackendError ``call`` raises, caught here so the test's own frame, which holds the key, is not on its traceback."""
    try:
        call()
    except BackendError as exc:
        return exc
    pytest.fail("the request did not fail")


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
    assert _cachekit_locals_holding(err, api_key, below_caller=True) == []


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
    assert _cachekit_locals_holding(err, api_key, below_caller=True) == []


@pytest.mark.skipif(not hasattr(signal, "setitimer"), reason="needs SIGALRM to interrupt the request")
@pytest.mark.parametrize("interrupt", [SystemExit, KeyboardInterrupt])
def test_interrupt_during_request_reaches_no_frame_holding_the_api_key(
    connect: Callable[..., tuple[CachekitIOBackend, str]],
    interrupt: type[BaseException],
) -> None:
    """An interrupt raised while a sync request stalls (a worker timeout's SystemExit, Ctrl-C) is not a transport failure:
    it propagates as itself, landing where it landed, but reaches no frame holding the key."""

    def handler(signum: int, frame: object) -> None:
        raise interrupt(1)

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen()
        backend, api_key = connect(sock.getsockname()[1], timeout=30)  # stalls far past the interrupt
        previous = signal.signal(signal.SIGALRM, handler)
        signal.setitimer(signal.ITIMER_REAL, 0.3)
        try:
            exc = _interrupted(lambda: backend.get("k"), interrupt)
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, previous)

    assert type(exc) is interrupt
    landed = traceback.extract_tb(exc.__traceback__)
    assert landed[-1].name == "handler"
    assert "urlopen" in [entry.name for entry in landed]
    assert _cachekit_locals_holding(exc, api_key, below_caller=True) == []


def _interrupted(call: Callable[[], object], interrupt: type[BaseException]) -> BaseException:
    """The interrupt ``call`` raises, caught here so the test's own frame, which holds the key, is not on its traceback."""
    try:
        call()
    except interrupt as exc:
        return exc
    pytest.fail("the request was not interrupted")


def test_interrupt_chained_during_request_reaches_no_frame_holding_the_api_key(
    monkeypatch: pytest.MonkeyPatch, connect: Callable[..., tuple[CachekitIOBackend, str]]
) -> None:
    """An interrupt that lands while the client handles its own failure chains that failure, whose frames hold the key too.

    The caller's own handled exception, which the interrupt also chains, keeps its locals: it is not the request's.
    """
    backend, api_key = connect(1)

    def make_request() -> None:
        authorization = f"Bearer {api_key}"  # the local urllib3's _make_request holds
        raise TimeoutError(len(authorization))

    def request(method: str, url: str, **kwargs: object) -> None:
        try:
            make_request()
        except TimeoutError:
            raise KeyboardInterrupt  # noqa: B904 - chains the TimeoutError as __context__, as an interrupt does

    def fail_in_caller() -> None:
        evidence = "caller's local"  # noqa: F841 - must survive
        raise ValueError

    monkeypatch.setattr(backend, "_own_lease", lambda: SimpleNamespace(client=SimpleNamespace(request=request)))
    try:
        fail_in_caller()
    except ValueError:
        exc = _interrupted(lambda: backend.get("k"), KeyboardInterrupt)

    assert isinstance(exc.__context__, TimeoutError)
    request_frames = [
        tb.tb_frame
        for err in (exc, exc.__context__)
        for tb in _walk(err.__traceback__)
        if tb.tb_frame.f_code is make_request.__code__
    ]
    assert len(request_frames) == 1  # only on the chained failure's traceback, not the interrupt's
    assert request_frames[0].f_locals == {}
    handled = exc.__context__.__context__
    assert isinstance(handled, ValueError)
    assert handled.__traceback__.tb_next.tb_frame.f_locals == {"evidence": "caller's local"}


def _walk(tb: TracebackType | None) -> Iterator[TracebackType]:
    while tb is not None:
        yield tb
        tb = tb.tb_next
