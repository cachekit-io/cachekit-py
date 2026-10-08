"""A failed cachekit.io request raises a BackendError that reaches no frame holding the API key (CWE-532).

Error trackers capture the locals of every frame on a raised exception's traceback, and on every exception chained to it, by
default (Sentry's ``include_local_variables``), and serialise containers item by item; their scrubbers match top-level
variable names, never a value inside a dict. urllib3's request frames (``urlopen``, ``_make_request``) hold the request
headers, ``Authorization: Bearer <key>`` included, so the error must reach none of them: not through ``__cause__``,
``__context__`` or ``original_exception``.
An interrupt raised mid-request (``SystemExit``, ``KeyboardInterrupt``) propagates as itself, so it must reach none of them
either.

Each request fails on a real loopback socket, so urllib3 leaves the frames a production failure leaves, and every frame is
walked, third-party ones included. The exception: the last tests fake the client, to place an interrupt where a socket cannot.

A failed request is freed by reference counting, not by the cyclic GC: no cachekit frame on its error keeps an exception or a
failed Future in a local, and urllib3's exceptions, whose tracebacks hold the backend's frames, are not left in cycles of
their own. Either would keep the request's frames, and the body they hold, until a full collection.
"""

from __future__ import annotations

import asyncio
import gc
import shutil
import signal
import socket
import traceback
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path
from types import FrameType, SimpleNamespace

import pytest

from cachekit.backends.cachekitio import backend as backend_module
from cachekit.backends.cachekitio import config as config_module
from cachekit.backends.cachekitio.backend import CachekitIOBackend
from cachekit.backends.errors import BackendError, BackendErrorType
from tests.unit.backends.test_redis_error_frames import _cachekit_frames_holding_an_exception
from tests.unit.config.test_redacting_settings import _CACHEKIT_SRC, _cachekit_locals_holding

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


def test_a_failed_request_is_freed_without_the_cyclic_gc(
    peer: tuple[int, BackendErrorType], connect: Callable[[int], tuple[CachekitIOBackend, str]]
) -> None:
    """Freed by reference counting alone: its frames, and the body they hold, do not wait for the cyclic GC while cachekit.io
    is unreachable. A frame holds its caller, so a urllib3 exception left in a cycle keeps the backend's frames too."""
    port, _ = peer
    backend, _ = connect(port)
    gc.collect()
    gc.disable()
    try:
        err = _raised(lambda: backend.set("k", b"v" * 1_000_000))
        gc.set_debug(gc.DEBUG_SAVEALL)  # what collect() finds unreachable stays in gc.garbage
        del err
        gc.collect()
        frames = [obj for obj in gc.garbage if isinstance(obj, FrameType)]
        assert [f.f_code.co_name for f in frames if Path(f.f_code.co_filename).resolve().is_relative_to(_CACHEKIT_SRC)] == []
        assert [type(obj).__name__ for obj in gc.garbage if isinstance(obj, BaseException)] == []
    finally:
        gc.set_debug(0)
        gc.garbage.clear()
        gc.enable()


def test_a_request_failing_in_the_callers_handler_keeps_the_handled_traceback(
    peer: tuple[int, BackendErrorType], connect: Callable[[int], tuple[CachekitIOBackend, str]]
) -> None:
    """urllib3's exceptions chain the exception the caller is handling as ``__context__``: the backend drops their
    tracebacks, never the caller's."""
    port, _ = peer
    backend, _ = connect(port)
    try:
        raise ValueError("the caller's")
    except ValueError as exc:
        handled, kept = exc, exc.__traceback__
        err = _raised(lambda: backend.get("k"))

    assert kept is not None
    assert handled.__traceback__ is kept
    assert _cachekit_frames_holding_an_exception(err) == []


async def _locked(backend: CachekitIOBackend) -> None:
    async with backend.acquire_lock("k", timeout=5):
        pass


async def _cancelled_mid_attempt(backend: CachekitIOBackend) -> asyncio.CancelledError:
    """The CancelledError of an ``acquire_lock`` cancelled while its attempt is in flight, the attempt failing after."""
    task = asyncio.ensure_future(_locked(backend))
    await asyncio.sleep(0.1)  # the attempt's TLS handshake stalls until the backend's timeout
    task.cancel()
    try:
        await task
    except asyncio.CancelledError as exc:
        return exc
    pytest.fail("the lock attempt was not cancelled")


def test_a_failed_lock_attempt_holds_no_exception_in_a_cachekit_frame(
    peer: tuple[int, BackendErrorType], connect: Callable[[int], tuple[CachekitIOBackend, str]]
) -> None:
    """The failed attempt's Task holds the BackendError, whose traceback holds the frame holding the Task: a cycle."""
    port, _ = peer
    backend, _ = connect(port)

    err = _raised(lambda: asyncio.run(_locked(backend)))

    assert _cachekit_frames_holding_an_exception(err) == []


def test_a_cancel_during_a_failing_lock_attempt_holds_no_exception_in_a_cachekit_frame(
    connect: Callable[[int], tuple[CachekitIOBackend, str]],
) -> None:
    """``acquire_lock`` drains an attempt through a cancel and re-raises the cancel once the attempt has failed: no cachekit
    frame on that CancelledError keeps the failed attempt or its error."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen()
        backend, _ = connect(sock.getsockname()[1])
        exc = asyncio.run(_cancelled_mid_attempt(backend))

    assert _cachekit_frames_holding_an_exception(exc) == []


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


# The tests below fake the client's request, to place an interrupt where a real socket cannot place it on demand.
_API_KEY = f"ck_live_SYNTHETIC_{uuid.uuid4().hex}"


def _make_request() -> None:
    """Stands in for urllib3's ``_make_request``: a finished frame whose local holds the bearer key."""
    authorization = f"Bearer {_API_KEY}"  # noqa: F841 - the local a client frame holds
    raise TimeoutError("read timed out")


def _faked(monkeypatch: pytest.MonkeyPatch, request: Callable[..., object]) -> CachekitIOBackend:
    backend = CachekitIOBackend(api_key=_API_KEY)
    monkeypatch.setattr(backend, "_own_lease", lambda: SimpleNamespace(client=SimpleNamespace(request=request)))
    return backend


def _make_request_locals(exc: BaseException) -> list[dict[str, object]]:
    """The locals of every ``_make_request`` frame reachable from ``exc`` through ``__cause__`` and ``__context__``."""
    found: list[dict[str, object]] = []
    seen: set[int] = set()
    pending: list[BaseException | None] = [exc]
    while pending:
        current = pending.pop()
        if current is None or id(current) in seen:
            continue
        seen.add(id(current))
        pending += [current.__cause__, current.__context__]
        found += [dict(f.f_locals) for f, _ in traceback.walk_tb(current.__traceback__) if f.f_code is _make_request.__code__]
    return found


@pytest.mark.parametrize(
    ("then", "raised"),
    [(None, BackendError), (TimeoutError, BackendError), (KeyboardInterrupt, KeyboardInterrupt)],
    ids=["as-its-failure", "then-a-transport-failure", "then-an-interrupt"],
)
def test_a_request_re_raising_the_callers_exception_keeps_its_traceback(
    monkeypatch: pytest.MonkeyPatch, then: type[BaseException] | None, raised: type[BaseException]
) -> None:
    """A client that raises the very exception the caller is handling, as its failure or before failing with another: it
    leaves with the traceback it came in with, not one through the request's frames."""
    reused = ConnectionError("the caller's")

    def request(method: str, url: str, **kwargs: object) -> None:
        try:
            raise reused
        except ConnectionError:
            if then is None:
                raise
            raise then  # noqa: B904 - chains the caller's exception as __context__, as a client's own failure does

    backend = _faked(monkeypatch, request)
    try:
        raise reused
    except ConnectionError:
        kept = reused.__traceback__
        with pytest.raises(raised):
            backend.get("k")

    assert kept is not None
    assert reused.__traceback__ is kept


def test_a_request_failing_in_the_callers_handler_leaves_a_cleared_traceback_cleared(monkeypatch: pytest.MonkeyPatch) -> None:
    """A caller that cleared the traceback of the exception it is handling, as SECURITY.md advises, keeps it cleared. On
    Python 3.10 ``sys.exc_info()`` still reports the traceback the exception was caught with."""
    backend = _faked(monkeypatch, lambda method, url, **kwargs: _make_request())
    try:
        raise ValueError("the caller's")
    except ValueError as exc:
        exc.__traceback__ = None
        handled = exc
        _raised(lambda: backend.get("k"))

    assert handled.__traceback__ is None


def test_interrupt_chained_during_request_reaches_no_frame_holding_the_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """An interrupt that lands while the client handles its own failure chains that failure, whose frames hold the key too.

    The caller's own handled exception, which the interrupt also chains, keeps its locals: it is not the request's.
    """

    def request(method: str, url: str, **kwargs: object) -> None:
        try:
            _make_request()
        except TimeoutError:
            raise KeyboardInterrupt  # noqa: B904 - chains the TimeoutError as __context__, as an interrupt does

    def fail_in_caller() -> None:
        evidence = "caller's local"  # noqa: F841 - must survive
        raise ValueError

    backend = _faked(monkeypatch, request)
    try:
        fail_in_caller()
    except ValueError:
        exc = _interrupted(lambda: backend.get("k"), KeyboardInterrupt)

    assert isinstance(exc.__context__, TimeoutError)
    assert _make_request_locals(exc) == [{}]  # only on the chained failure's traceback, not the interrupt's
    handled = exc.__context__.__context__
    assert isinstance(handled, ValueError)
    assert handled.__traceback__.tb_next.tb_frame.f_locals == {"evidence": "caller's local"}


def test_interrupt_with_a_distinct_cause_clears_its_context_too(monkeypatch: pytest.MonkeyPatch) -> None:
    """``raise interrupt from other`` inside the client's handler: the failure it handled is still its ``__context__``."""

    def request(method: str, url: str, **kwargs: object) -> None:
        try:
            _make_request()
        except TimeoutError:
            raise KeyboardInterrupt from RuntimeError("independent cause")

    exc = _interrupted(lambda: _faked(monkeypatch, request).get("k"), KeyboardInterrupt)

    assert isinstance(exc.__cause__, RuntimeError)
    assert _make_request_locals(exc) == [{}]


def test_interrupt_while_classifying_a_transport_failure_clears_its_frames(monkeypatch: pytest.MonkeyPatch) -> None:
    """An interrupt that lands in ``_send``'s own handler for a transport failure chains that failure."""

    def classify(exc: BaseException, **kwargs: object) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(backend_module, "classify_http_error", classify)
    backend = _faked(monkeypatch, lambda method, url, **kwargs: _make_request())

    exc = _interrupted(lambda: backend.get("k"), KeyboardInterrupt)

    assert isinstance(exc.__context__, TimeoutError)
    assert _make_request_locals(exc) == [{}]


def test_interrupt_while_dropping_a_transport_failures_tracebacks_holds_no_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    """An interrupt that lands in ``_send``'s walk over a transport failure's chain, the error already built."""
    walk = backend_module._raised_during_request
    walks: list[BaseException] = []

    def interrupted(exc: BaseException, handled_before: BaseException | None) -> Iterator[BaseException]:
        walks.append(exc)
        chain = walk(exc, handled_before)
        yield next(chain)
        if len(walks) == 1:  # the transport handler's walk, not the interrupt branch's
            raise KeyboardInterrupt
        yield from chain

    monkeypatch.setattr(backend_module, "_raised_during_request", interrupted)
    backend = _faked(monkeypatch, lambda method, url, **kwargs: _make_request())

    exc = _interrupted(lambda: backend.get("k"), KeyboardInterrupt)

    assert isinstance(exc.__context__, TimeoutError)
    assert _cachekit_locals_holding(exc, _API_KEY, below_caller=True) == []
    assert _cachekit_frames_holding_an_exception(exc) == []


def test_interrupt_the_caller_was_already_handling_still_clears_its_frames(monkeypatch: pytest.MonkeyPatch) -> None:
    """A caller re-raising the interrupt it is handling (a reused ``gevent.Timeout``): the chain stop is not the interrupt."""
    reused = KeyboardInterrupt()

    def request(method: str, url: str, **kwargs: object) -> None:
        try:
            _make_request()
        except TimeoutError:
            raise reused  # noqa: B904 - chains the TimeoutError as __context__, as an interrupt does

    backend = _faked(monkeypatch, request)
    try:
        raise reused
    except KeyboardInterrupt:
        exc = _interrupted(lambda: backend.get("k"), KeyboardInterrupt)

    assert exc is reused
    assert _make_request_locals(exc) == [{}]
    assert request.__code__ in [f.f_code for f, _ in traceback.walk_tb(exc.__traceback__)]  # still shows where it landed


@pytest.mark.skipif(not hasattr(signal, "setitimer"), reason="needs SIGALRM to bound a hang")
def test_interrupt_with_a_cyclic_chain_still_propagates(monkeypatch: pytest.MonkeyPatch) -> None:
    first, second = ValueError("a"), ValueError("b")
    first.__cause__, second.__cause__ = second, first

    def request(method: str, url: str, **kwargs: object) -> None:
        try:
            _make_request()
        except TimeoutError:
            raise KeyboardInterrupt from first

    def hung(signum: int, frame: object) -> None:
        raise AssertionError("the chain walk did not terminate")

    previous = signal.signal(signal.SIGALRM, hung)
    signal.setitimer(signal.ITIMER_REAL, 5)
    try:
        exc = _interrupted(lambda: _faked(monkeypatch, request).get("k"), KeyboardInterrupt)
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)

    assert _make_request_locals(exc) == [{}]
