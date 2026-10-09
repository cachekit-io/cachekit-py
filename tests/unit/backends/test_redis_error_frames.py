"""A failed Redis operation raises a BackendError that reaches no frame holding the Redis password (CWE-532).

Error trackers capture the locals of every frame on a raised exception's traceback, and on every exception chained to it, by
default (Sentry's ``include_local_variables``), and serialise an object they do not recognise by its repr. redis-py's client,
connection pool and pipeline list every connection argument in their repr, the password included, and redis-py's frames
hold them, and the AUTH arguments, as locals. So the error must reach none of those frames: not through ``__cause__``,
``__context__`` or ``original_exception``. Nor may a cachekit frame on it hold the client, the pool, a pipeline, the raw URL
or a config whose repr shows it.

Each operation fails against a loopback server speaking just enough RESP: it refuses AUTH with WRONGPASS, or completes the
handshake and drops the connection on the first command. Every frame is walked, third-party ones included.

An interrupt raised while redis-py is mid-call (``KeyboardInterrupt``, the ``SystemExit`` a worker-timeout signal raises) is
not a failure cachekit classifies: it propagates as itself, through redis-py's frames, so it must reach none of them holding
the password either. The last tests place one where a signal would land, as redis-py sends a command.

The raising frame is on the error's own traceback, so it must not keep the error, or any other exception, in a local
either: that is a reference cycle, and the frame, the payload with it, waits for the cyclic GC.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextvars
import errno
import gc
import logging
import pathlib
import pickle
import signal
import socket
import sys
import threading
import traceback
import types
import uuid
from collections.abc import Awaitable, Callable, Iterator
from concurrent.futures.thread import _WorkItem
from contextlib import contextmanager
from typing import Any, BinaryIO

import pytest
import redis
from pydantic import SecretStr

from cachekit import cache, invalidation
from cachekit.backends.errors import BackendError, BackendErrorType
from cachekit.backends.provider import DefaultBackendProvider
from cachekit.backends.redis import backend as backend_module
from cachekit.backends.redis import provider as provider_module
from cachekit.backends.redis.backend import RedisBackend
from cachekit.backends.redis.client import reset_global_pool
from cachekit.backends.redis.error_handler import RedisClientError, classify_redis_error
from cachekit.backends.redis.provider import PerRequestRedisBackend, RedisBackendProvider
from cachekit.hash_utils import redact_cache_key
from tests.unit.config.test_redacting_settings import _CACHEKIT_SRC, _cachekit_locals_holding, _held_exception

pytestmark = [pytest.mark.unit, pytest.mark.security]


def _read_command(reader: BinaryIO) -> list[bytes]:
    """One RESP command's arguments; empty once the client has closed the connection."""
    header = reader.readline()
    if not header.startswith(b"*"):
        return []
    return [reader.read(int(reader.readline()[1:]) + 2)[:-2] for _ in range(int(header[1:]))]


class _FakeRedis:
    """A loopback Redis that fails every command.

    ``refusing``: AUTH is answered with WRONGPASS. Otherwise the handshake (AUTH, PING, CLIENT, SELECT) succeeds and any
    other command drops the connection unanswered, except those ``serving`` answers. With ``hold``, a command about to
    fail sets ``holding`` and waits for ``hold`` first, so the client's call is in flight until the test lets it fail.
    """

    def __init__(
        self, *, refusing: bool, serving: dict[bytes, bytes] | None = None, hold: threading.Event | None = None
    ) -> None:
        self.refusing = refusing
        self._serving = serving or {}
        self._hold = hold
        self.holding = threading.Event()
        self._sock = socket.create_server(("127.0.0.1", 0))
        self.port = self._sock.getsockname()[1]
        self._conns: list[socket.socket] = []
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self) -> None:
        while True:
            try:
                conn, _ = self._sock.accept()
            except OSError:
                return
            self._conns.append(conn)
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn: socket.socket) -> None:
        with conn, conn.makefile("rb") as reader:
            while command := _read_command(reader):
                name = command[0].upper()
                if name == b"AUTH" and self.refusing:
                    self._wait_for_hold()
                    conn.sendall(b"-WRONGPASS invalid username-password pair or user is disabled.\r\n")
                elif name in (b"AUTH", b"CLIENT", b"SELECT"):
                    conn.sendall(b"+OK\r\n")
                elif name == b"PING":
                    conn.sendall(b"+PONG\r\n")
                elif name in self._serving:
                    conn.sendall(self._serving[name])
                else:
                    self._wait_for_hold()
                    return

    def _wait_for_hold(self) -> None:
        if self._hold is not None:
            self.holding.set()
            self._hold.wait(5)

    def refuse_from_now(self) -> None:
        """Refuse every later AUTH, and drop the open connections so the client has to authenticate again."""
        self.refusing = True
        for conn in self._conns:
            try:
                conn.shutdown(socket.SHUT_RDWR)
            except OSError as exc:
                # Closed on our side, or by the client. Anything else may leave a connection authenticated, and a
                # ``wrongpass`` case would quietly run as a ``dropped`` one.
                if exc.errno not in (errno.EBADF, errno.ENOTCONN):
                    raise

    def close(self) -> None:
        self._sock.close()


_FAILURES = {
    "wrongpass": (redis.AuthenticationError, BackendErrorType.AUTHENTICATION),
    "dropped": (redis.ConnectionError, BackendErrorType.TRANSIENT),
}


@pytest.fixture
def password() -> str:
    # Unique per test: a pool still alive from an earlier test must never lend this one its connection.
    return f"pw-SYNTHETIC-{uuid.uuid4().hex}"


@pytest.fixture
def fake_redis() -> Iterator[Callable[..., _FakeRedis]]:
    servers: list[_FakeRedis] = []

    def start(**kwargs: object) -> _FakeRedis:
        servers.append(_FakeRedis(**kwargs))  # type: ignore[arg-type]
        return servers[-1]

    yield start
    for server in servers:
        server.close()


@pytest.fixture
def redis_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[Callable[[str], None]]:
    """Points env auto-detection at a URL; the process-global pool is rebuilt from it, and dropped afterwards."""

    def point(url: str) -> None:
        for name in ("CACHEKIT_API_KEY", "CACHEKIT_MEMCACHED_SERVERS", "CACHEKIT_FILE_CACHE_DIR", "REDIS_URL"):
            monkeypatch.delenv(name, raising=False)
        monkeypatch.setenv("CACHEKIT_REDIS_URL", url)
        reset_global_pool()

    yield point
    reset_global_pool()


def _url(server: _FakeRedis, password: str) -> SecretStr:
    """Wrapped, so the test's lambdas, whose frames are on the traceback, hold it masked; unwrapped inline at each call."""
    return SecretStr(f"redis://:{password}@127.0.0.1:{server.port}/0")


def _raised(call: Callable[[], object], expected: type[BaseException] = BackendError) -> BaseException:
    """The exception ``call`` raises, caught here so the test's own frame, which holds the password, is not on its
    traceback."""
    try:
        call()
    except expected as exc:
        return exc
    pytest.fail("the operation did not fail")


def _in_cachekit(frame: types.FrameType) -> bool:
    return pathlib.Path(frame.f_code.co_filename).resolve().is_relative_to(_CACHEKIT_SRC)


def _cachekit_frames_holding_an_exception(err: BaseException) -> list[str]:
    """Every ``frame:local`` of a cachekit frame on the traceback of ``err``, or of the exceptions it chains, that
    holds an exception or a failed Future: a reference cycle through the traceback, or redis-py's exception kept
    alive."""
    found: list[str] = []
    pending: list[BaseException | None] = [err]
    seen: set[int] = set()
    while pending:
        current = pending.pop()
        if current is None or id(current) in seen:
            continue
        seen.add(id(current))
        pending += [current.__cause__, current.__context__, getattr(current, "original_exception", None)]
        tb = current.__traceback__
        while tb is not None:
            if _in_cachekit(tb.tb_frame):
                found += [
                    f"{tb.tb_frame.f_code.co_name}:{name}"
                    for name, value in tb.tb_frame.f_locals.items()
                    if _held_exception(value) is not None
                ]
            tb = tb.tb_next
    return found


def _left_for_the_cyclic_gc() -> list[str]:
    """Every cachekit frame, and every exception redis-py, cachekit or this module raised, that only the cyclic GC can
    free now. Disable the GC before the failure: a collection in between would free them unseen. Exceptions from
    elsewhere are left out: a thread another test left running may raise one meanwhile."""
    gc.set_debug(gc.DEBUG_SAVEALL)  # what collect() finds unreachable stays in gc.garbage
    try:
        gc.collect()
        return [
            f"frame {obj.f_code.co_name}" if isinstance(obj, types.FrameType) else type(obj).__name__
            for obj in gc.garbage
            if (isinstance(obj, types.FrameType) and _in_cachekit(obj))
            or (
                isinstance(obj, BaseException)
                and (type(obj).__module__ == __name__ or type(obj).__module__.partition(".")[0] in ("redis", "cachekit"))
            )
        ]
    finally:
        gc.set_debug(0)
        gc.garbage.clear()


def _assert_class_only_cause(err: BackendError, exc_type: type[Exception]) -> None:
    cause = err.original_exception
    assert isinstance(cause, RedisClientError)
    assert issubclass(cause.exc_type, exc_type)
    assert cause.__traceback__ is None
    assert err.__cause__ is cause
    assert err.__context__ is None


async def _locked(backend: PerRequestRedisBackend) -> None:
    async with backend.acquire_lock("k", timeout=5):
        pass


_BACKEND_CALLS: dict[str, tuple[str, Callable[[RedisBackend], object]]] = {
    "get": ("GET", lambda backend: backend.get("k")),
    "set": ("SET", lambda backend: backend.set("k", b"v")),
    "setex": ("SET", lambda backend: backend.set("k", b"v", ttl=5)),
    "delete": ("DELETE", lambda backend: backend.delete("k")),
    "exists": ("EXISTS", lambda backend: backend.exists("k")),
    "delete-many": ("UNLINK", lambda backend: backend._delete_many(["k"])),
}

_SHARED_CALLS: dict[str, Callable[[PerRequestRedisBackend], object]] = {
    "get": lambda backend: backend.get("k"),
    "set": lambda backend: backend.set("k", b"v"),
    "setex": lambda backend: backend.set("k", b"v", ttl=5),
    "delete": lambda backend: backend.delete("k"),
    "exists": lambda backend: backend.exists("k"),
    "track-key": lambda backend: backend.track_key("ck:reg:ns:h", "k"),
    "drain-tracked": lambda backend: backend.drain_tracked("ck:reg:ns:h", ["k"]),
    "get-ttl": lambda backend: asyncio.run(backend.get_ttl("k")),
    "refresh-ttl": lambda backend: asyncio.run(backend.refresh_ttl("k", 5)),
    "acquire-lock": lambda backend: asyncio.run(_locked(backend)),
}


@pytest.mark.parametrize("failure", _FAILURES)
@pytest.mark.parametrize("built_from", ["url", "env"])
@pytest.mark.parametrize("call", _BACKEND_CALLS, ids=_BACKEND_CALLS.keys())
def test_redis_backend_failure_reaches_no_frame_holding_the_password(
    fake_redis: Callable[..., _FakeRedis],
    redis_env: Callable[[str], None],
    password: str,
    failure: str,
    built_from: str,
    call: str,
) -> None:
    exc_type, _ = _FAILURES[failure]
    command, run = _BACKEND_CALLS[call]
    url = _url(fake_redis(refusing=failure == "wrongpass"), password)
    if built_from == "url":
        backend = RedisBackend(redis_url=url.get_secret_value())
    else:
        redis_env(url.get_secret_value())
        backend = RedisBackend()

    err = _raised(lambda: run(backend))
    assert isinstance(err, BackendError)

    # Classification unchanged: RedisBackend does not classify by exception type.
    assert err.error_type == BackendErrorType.UNKNOWN
    assert err.message == f"Redis {command} failed: {err.original_exception.exc_type.__name__}"  # type: ignore[union-attr]
    _assert_class_only_cause(err, exc_type)
    assert _cachekit_locals_holding(err, password, below_caller=True) == []
    assert _cachekit_frames_holding_an_exception(err) == []


@pytest.mark.parametrize("built_from", ["url", "env"])
def test_provider_init_failure_reaches_no_frame_holding_the_password(
    fake_redis: Callable[..., _FakeRedis], redis_env: Callable[[str], None], password: str, built_from: str
) -> None:
    """The init ping fails on a wrong password: built from a URL string, or by env auto-detection (``@cache``'s default),
    which also holds the RedisBackendConfig it read."""
    url = _url(fake_redis(refusing=True), password)
    if built_from == "url":
        err = _raised(lambda: RedisBackendProvider(url.get_secret_value()))
    else:
        redis_env(url.get_secret_value())
        err = _raised(lambda: DefaultBackendProvider().get_backend())
    assert isinstance(err, BackendError)

    assert (err.error_type, err.operation) == (BackendErrorType.AUTHENTICATION, "init")
    _assert_class_only_cause(err, redis.AuthenticationError)
    assert _cachekit_locals_holding(err, password, below_caller=True) == []
    assert _cachekit_frames_holding_an_exception(err) == []


@pytest.mark.parametrize("failure", _FAILURES)
@pytest.mark.parametrize("call", _SHARED_CALLS, ids=_SHARED_CALLS.keys())
def test_provider_backend_failure_reaches_no_frame_holding_the_password(
    fake_redis: Callable[..., _FakeRedis], password: str, failure: str, call: str
) -> None:
    """``wrongpass``: the server refuses AUTH once the provider's init ping has passed, so the operation reconnects."""
    exc_type, error_type = _FAILURES[failure]
    server = fake_redis(refusing=False)
    backend = RedisBackendProvider(_url(server, password).get_secret_value()).get_shared_backend()
    if failure == "wrongpass":
        server.refuse_from_now()

    err = _raised(lambda: _SHARED_CALLS[call](backend))  # type: ignore[arg-type]
    assert isinstance(err, BackendError)

    assert err.error_type == error_type
    _assert_class_only_cause(err, exc_type)
    assert _cachekit_locals_holding(err, password, below_caller=True) == []
    assert _cachekit_frames_holding_an_exception(err) == []


class _BlockError(Exception):
    """Raised by the caller's code inside a lock or a timeout window."""


async def _raise_in_lock(backend: PerRequestRedisBackend, exc: Exception) -> None:
    async with backend.acquire_lock("k", timeout=5) as acquired:
        assert acquired
        raise exc


async def _raise_in_window(backend: PerRequestRedisBackend, exc: Exception) -> None:
    async with backend.with_timeout("get", 100):
        raise exc


@pytest.mark.parametrize("block", [_raise_in_lock, _raise_in_window], ids=["acquire-lock", "with-timeout"])
def test_an_exception_raised_in_the_block_is_kept_whole(
    fake_redis: Callable[..., _FakeRedis], password: str, block: Callable[..., object]
) -> None:
    """The decorator re-raises a lock body's own error (a tamper or key-ring failure) from ``original_exception``, so it
    stays whole; and no frame on the way out holds the password."""
    server = fake_redis(refusing=False, serving={b"SET": b"+OK\r\n", b"EVALSHA": b":1\r\n"})  # lock taken, then released
    backend = RedisBackendProvider(_url(server, password).get_secret_value()).get_shared_backend()
    exc = _BlockError("raised in the block")

    err = _raised(lambda: asyncio.run(block(backend, exc)))  # type: ignore[arg-type]
    assert isinstance(err, BackendError)

    assert err.error_type == BackendErrorType.UNKNOWN
    assert err.original_exception is exc
    assert err.__cause__ is exc
    assert _cachekit_locals_holding(err, password, below_caller=True) == []
    assert _cachekit_frames_holding_an_exception(err) == []


async def _cancelled_mid_attempt(backend: PerRequestRedisBackend, server: _FakeRedis, hold: threading.Event) -> BaseException:
    """The CancelledError of an ``acquire_lock`` cancelled while its attempt is in flight, the attempt failing after."""
    task = asyncio.ensure_future(_locked(backend))
    await asyncio.to_thread(server.holding.wait, 5)
    task.cancel()
    await asyncio.sleep(0.05)  # the cancel reaches acquire_lock, which keeps draining the attempt
    hold.set()
    try:
        await task
    except asyncio.CancelledError as exc:
        return exc
    pytest.fail("the lock attempt was not cancelled")


@pytest.mark.parametrize("failure", _FAILURES)
def test_a_cancel_during_a_failing_lock_attempt_reaches_no_frame_holding_the_password(
    fake_redis: Callable[..., _FakeRedis], password: str, failure: str
) -> None:
    """``acquire_lock`` drains an attempt through a cancel and re-raises the cancel once the attempt has failed: no frame
    on that CancelledError keeps the failed attempt, whose redis-py exception holds the password in its frames."""
    hold = threading.Event()
    server = fake_redis(refusing=False, hold=hold)
    backend = RedisBackendProvider(_url(server, password).get_secret_value()).get_shared_backend()
    if failure == "wrongpass":
        server.refuse_from_now()

    exc = asyncio.run(_cancelled_mid_attempt(backend, server, hold))  # type: ignore[arg-type]

    assert _cachekit_locals_holding(exc, password, below_caller=True) == []
    assert _cachekit_frames_holding_an_exception(exc) == []


def test_the_blocks_exception_wins_over_a_release_that_fails_after_it(
    fake_redis: Callable[..., _FakeRedis], password: str, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A release that fails with an error redis-py did not raise (an executor shut down at exit) must not replace the
    block's own exception: the decorator re-raises that from ``original_exception``. The release failure is logged."""

    def release(self: object) -> None:
        raise RuntimeError("cannot schedule new futures after shutdown")

    monkeypatch.setattr(redis.lock.Lock, "release", release)
    server = fake_redis(refusing=False, serving={b"SET": b"+OK\r\n"})
    backend = RedisBackendProvider(_url(server, password).get_secret_value()).get_shared_backend()
    exc = _BlockError("raised in the block")

    with caplog.at_level(logging.WARNING, logger="cachekit.backends.redis.provider"):
        err = _raised(lambda: asyncio.run(_raise_in_lock(backend, exc)))  # type: ignore[arg-type]

    assert isinstance(err, BackendError)
    assert err.original_exception is exc
    assert _cachekit_frames_holding_an_exception(err) == []
    [record] = [r for r in caplog.records if r.name == "cachekit.backends.redis.provider"]
    assert record.levelno == logging.WARNING
    assert "RuntimeError" in record.getMessage()
    assert redact_cache_key("k") in record.getMessage()


async def _a_cancel_during_the_release_after_the_block_raised(
    backend: PerRequestRedisBackend, releasing: threading.Event, release: threading.Event
) -> tuple[list[str], list[str]]:
    """``acquire_lock`` cancelled while it releases the lock after its block raised: the cachekit frames on the cancel
    that hold an exception, and what only the cyclic GC can free once the cancel is caught. Checked in this coroutine:
    ``asyncio.run`` keeps the exception it returns in a cycle."""

    async def raise_in_lock() -> None:
        async with backend.acquire_lock("k", timeout=5):
            raise _BlockError("raised in the block")  # held in no local: that would be the test's own cycle

    gc.collect()
    gc.disable()
    try:
        task = asyncio.ensure_future(raise_in_lock())
        await asyncio.to_thread(releasing.wait, 5)
        task.cancel()
        await asyncio.sleep(0.05)  # the cancel reaches acquire_lock, which keeps draining the release
        release.set()
        try:
            await task
        except asyncio.CancelledError as exc:
            held = _cachekit_frames_holding_an_exception(exc)
        else:
            pytest.fail("acquire_lock was not cancelled")
        del task
        await asyncio.sleep(0)  # the task step that threw the cancel in here holds it until this coroutine yields
        return held, _left_for_the_cyclic_gc()
    finally:
        gc.enable()


def test_a_cancel_during_the_release_after_the_block_raised_holds_no_exception(
    fake_redis: Callable[..., _FakeRedis], password: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    releasing, release = threading.Event(), threading.Event()

    def wait_for_release(self: object) -> None:
        releasing.set()
        release.wait(5)

    monkeypatch.setattr(redis.lock.Lock, "release", wait_for_release)
    server = fake_redis(refusing=False, serving={b"SET": b"+OK\r\n"})
    backend = RedisBackendProvider(_url(server, password).get_secret_value()).get_shared_backend()

    held, left = asyncio.run(_a_cancel_during_the_release_after_the_block_raised(backend, releasing, release))  # type: ignore[arg-type]

    assert held == []
    # CPython 3.12 alone keeps a finished generator's frame linked to the frame that last resumed it (``f_back``): here
    # contextlib's ``__aexit__``, whose ``value`` is the block's exception, whose traceback holds acquire_lock's frame.
    # Any ``@asynccontextmanager`` whose cleanup raises after its block raised makes that cycle there, whatever it holds.
    if sys.version_info[:2] != (3, 12):
        assert left == []


# Each write fails once the backend is built: the connection drops, Redis is loading its dataset after a restart, or it
# refuses a rotated password.
_WRITE_FAILURES: dict[str, dict[bytes, bytes]] = {
    "dropped": {},
    "loading": {b"SET": b"-LOADING Redis is loading the dataset in memory\r\n"},
    "wrongpass": {},
}

_WRITERS: dict[str, Callable[[str], RedisBackend | PerRequestRedisBackend]] = {
    "redis-backend": lambda url: RedisBackend(redis_url=url),
    "provider-backend": lambda url: RedisBackendProvider(url).get_shared_backend(),
}


@pytest.mark.parametrize("failure", _WRITE_FAILURES)
@pytest.mark.parametrize("built", _WRITERS)
def test_a_failed_write_is_freed_without_the_cyclic_gc(
    fake_redis: Callable[..., _FakeRedis], password: str, built: str, failure: str
) -> None:
    """Freed by reference counting alone: its frames, the payload they hold, and redis-py's exception do not wait for the
    cyclic GC. redis-py raises a LOADING or WRONGPASS reply from a local, a reference cycle in its own frame, which the
    traceback runs through."""
    server = fake_redis(refusing=False, serving=_WRITE_FAILURES[failure])
    backend = _WRITERS[built](_url(server, password).get_secret_value())
    if failure == "wrongpass":
        server.refuse_from_now()
    gc.collect()
    gc.disable()
    try:
        _raised(lambda: backend.set("k", b"v" * 1_000_000))
        assert _left_for_the_cyclic_gc() == []
    finally:
        gc.enable()


def test_the_walk_finds_the_password_on_redis_pys_own_exception(fake_redis: Callable[..., _FakeRedis], password: str) -> None:
    """Positive control: redis-py's exception, unwrapped, carries the password in its frames' locals, so the clean
    walks above are a result, not a walk that cannot see it."""
    url = _url(fake_redis(refusing=True), password)

    exc = _raised(lambda: redis.Redis.from_url(url.get_secret_value()).get("k"), redis.AuthenticationError)

    assert _cachekit_locals_holding(exc, password, below_caller=True) != []


def test_a_cachekit_error_raised_inside_an_operation_is_kept_whole() -> None:
    """A BackendError cachekit raises itself, such as for a reply of the wrong type, reaches no redis-py frame."""
    inner = BackendError("Redis DELETE returned unexpected type: str", operation="delete", key="k")
    assert classify_redis_error(inner, operation="delete", key="k").original_exception is inner


def test_a_classified_error_round_trips_through_pickle() -> None:
    err = pickle.loads(pickle.dumps(classify_redis_error(redis.ConnectionError("x"), operation="get", key="k")))  # noqa: S301
    assert (err.error_type, err.message, err.operation, err.key) == (
        BackendErrorType.TRANSIENT,
        "Transient Redis error: ConnectionError",
        "get",
        "k",
    )
    assert isinstance(err.original_exception, RedisClientError)
    assert err.original_exception.exc_type is redis.ConnectionError


_REDIS_PY = pathlib.Path(redis.__file__).resolve().parent
_INTERRUPTS = [KeyboardInterrupt, SystemExit]


def _redis_backend(url: SecretStr) -> RedisBackend:
    return RedisBackend(redis_url=url.get_secret_value())


def _provider_backend(url: SecretStr) -> PerRequestRedisBackend:
    return RedisBackendProvider(url.get_secret_value()).get_shared_backend()  # type: ignore[return-value]


# Every operation as (build, run): ``build`` runs before the failure is placed, ``run`` while it is. The provider's init
# ping runs in ``build`` for its backends, so their operations reuse its authenticated connection; ``RedisBackend`` opens
# its first connection in the operation.
_OPERATIONS: dict[str, tuple[Callable[[SecretStr], Any], Callable[[Any], object]]] = {
    **{f"redis-backend-{name}": (_redis_backend, run) for name, (_, run) in _BACKEND_CALLS.items()},
    **{f"provider-backend-{name}": (_provider_backend, run) for name, run in _SHARED_CALLS.items()},
    "provider-init": (lambda url: url, lambda url: RedisBackendProvider(url.get_secret_value())),
}

# These report a failure instead of raising it, so only an interrupt leaves them. The invalidation listener starts on the
# caller's thread, inside a cache operation.
_NEVER_RAISING: dict[str, tuple[Callable[[SecretStr], Any], Callable[[Any], object]]] = {
    "redis-backend-health-check": (_redis_backend, lambda backend: backend.health_check()),
    "provider-backend-health-check": (_provider_backend, lambda backend: backend.health_check()),
    "invalidation-listener-start": (_provider_backend, invalidation.start_listener),
}


@contextmanager
def _interrupting_sends(interrupt: type[BaseException]) -> Iterator[None]:
    """redis-py raises ``interrupt`` as it sends a command, a new connection's AUTH included, as a signal landing there
    would. A pipeline sends its commands packed. The async operations run redis-py on an executor thread, which a signal
    does not interrupt, so there it stands in for an interrupt raised in the worker (a ``gevent.Timeout``)."""

    def send(self: object, *args: object, **kwargs: object) -> None:
        raise interrupt

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(redis.connection.AbstractConnection, "send_command", send)
        patch.setattr(redis.connection.AbstractConnection, "send_packed_command", send)
        yield


def _assert_cleared(exc: BaseException, interrupt: type[BaseException], password: str, through: BaseException) -> None:
    """``exc`` is the interrupt itself; the traceback of ``through``, it or an exception it chained, still runs through
    redis-py with each frame's file and line; and no frame on any of them holds the password."""
    assert type(exc) is interrupt  # never a BackendError
    landed = traceback.extract_tb(through.__traceback__)
    assert any(pathlib.Path(entry.filename).resolve().is_relative_to(_REDIS_PY) for entry in landed)
    assert _cachekit_locals_holding(exc, password, below_caller=True) == []


@pytest.mark.parametrize("interrupt", _INTERRUPTS)
@pytest.mark.parametrize("operation", {**_OPERATIONS, **_NEVER_RAISING})
def test_an_interrupt_mid_command_reaches_no_frame_holding_the_password(
    fake_redis: Callable[..., _FakeRedis],
    password: str,
    monkeypatch: pytest.MonkeyPatch,
    interrupt: type[BaseException],
    operation: str,
) -> None:
    monkeypatch.setattr(invalidation, "_listener_pid", None)  # a listener started earlier would make the start a no-op
    monkeypatch.setattr(invalidation, "_start_retry_at", float("-inf"))
    build, run = {**_OPERATIONS, **_NEVER_RAISING}[operation]
    built = build(_url(fake_redis(refusing=False), password))

    with _interrupting_sends(interrupt):
        exc = _raised(lambda: run(built), interrupt)

    _assert_cleared(exc, interrupt, password, through=exc)


@pytest.mark.skipif(not hasattr(signal, "setitimer"), reason="needs SIGALRM to interrupt the command")
@pytest.mark.parametrize("interrupt", _INTERRUPTS)
@pytest.mark.parametrize("built", _WRITERS)
def test_a_signal_while_redis_py_waits_for_a_reply_reaches_no_frame_holding_the_password(
    fake_redis: Callable[..., _FakeRedis], password: str, interrupt: type[BaseException], built: str
) -> None:
    """A real signal, raised in its handler while redis-py waits on the socket for the reply to a command the server
    holds: the interrupt lands in redis-py's read, as a worker timeout's does."""

    def handler(signum: int, frame: object) -> None:
        raise interrupt

    hold = threading.Event()
    backend = _WRITERS[built](_url(fake_redis(refusing=False, hold=hold), password).get_secret_value())
    previous = signal.signal(signal.SIGALRM, handler)
    signal.setitimer(signal.ITIMER_REAL, 0.3)
    try:
        exc = _raised(lambda: backend.get("k"), interrupt)
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)
        hold.set()

    assert traceback.extract_tb(exc.__traceback__)[-1].name == "handler"
    _assert_cleared(exc, interrupt, password, through=exc)


@pytest.mark.parametrize("operation", _OPERATIONS)
def test_an_interrupt_while_classifying_a_failure_clears_the_failures_frames_too(
    fake_redis: Callable[..., _FakeRedis], password: str, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    """An interrupt that lands in the operation's own handler for redis-py's failure chains that failure as its
    ``__context__``, and the failure's traceback runs through redis-py's frames, deeper than the handler's."""

    def classify(*args: object, **kwargs: object) -> None:
        raise KeyboardInterrupt

    build, run = _OPERATIONS[operation]
    server = fake_redis(refusing=False)
    built = build(_url(server, password))
    server.refuse_from_now()  # the next AUTH fails
    monkeypatch.setattr(backend_module, "_command_error", classify)
    monkeypatch.setattr(provider_module, "classify_redis_error", classify)

    exc = _raised(lambda: run(built), KeyboardInterrupt)

    assert isinstance(exc.__context__, redis.AuthenticationError)
    _assert_cleared(exc, KeyboardInterrupt, password, through=exc.__context__)


_ASYNC_OPERATIONS: dict[str, Callable[[PerRequestRedisBackend], Awaitable[object]]] = {
    "get-ttl": lambda backend: backend.get_ttl("k"),
    "refresh-ttl": lambda backend: backend.refresh_ttl("k", 5),
    "acquire-lock": _locked,
}


@pytest.mark.parametrize("operation", _ASYNC_OPERATIONS)
def test_an_async_interrupt_caught_before_the_worker_leaves_its_frame_reaches_no_frame_holding_the_password(
    fake_redis: Callable[..., _FakeRedis], password: str, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    """The executor worker that raised the interrupt can still be inside the frame that caught it, on the interrupt's
    traceback, when the operation clears the frames: ``clear_frames`` skips it as executing. That frame holds only the work
    item, whose repr shows no client, and the interrupt. Held there while the caller walks the interrupt."""
    release = threading.Event()
    set_exception = concurrent.futures.Future.set_exception

    def held(self: concurrent.futures.Future[object], exception: BaseException | None) -> None:
        set_exception(self, exception)
        if threading.current_thread() is not threading.main_thread():
            release.wait(5)  # between the worker's set_exception and its ``self = None``

    async def walked_while_held(backend: PerRequestRedisBackend, secret: SecretStr) -> list[str]:
        try:
            await _ASYNC_OPERATIONS[operation](backend)
        except KeyboardInterrupt as exc:
            try:
                running = [frame for frame, _ in traceback.walk_tb(exc.__traceback__) if frame.f_code is _WorkItem.run.__code__]
                assert [type(frame.f_locals["self"]) for frame in running] == [_WorkItem]  # the race, held open
                return _cachekit_locals_holding(exc, secret.get_secret_value(), below_caller=True)
            finally:
                release.set()
        pytest.fail("the operation was not interrupted")

    backend = _provider_backend(_url(fake_redis(refusing=False), password))
    monkeypatch.setattr(concurrent.futures.Future, "set_exception", held)
    with _interrupting_sends(KeyboardInterrupt):
        assert asyncio.run(walked_while_held(backend, SecretStr(password))) == []


def _interrupted_while_handling(backend: RedisBackend) -> tuple[BaseException, types.TracebackType | None]:
    """The interrupt a ``get`` raises while this caller handles its own exception, and that exception's traceback before
    the call. A frame apart from the test's, whose ``password`` argument the handled exception's traceback would reach."""

    def fail_in_caller() -> None:
        evidence = "caller's local"
        raise ValueError

    try:
        fail_in_caller()
    except ValueError as caught:
        kept = caught.__traceback__
        with _interrupting_sends(KeyboardInterrupt):
            return _raised(lambda: backend.get("k"), KeyboardInterrupt), kept
    pytest.fail("the caller did not fail")


def test_an_interrupt_leaves_the_exception_the_caller_is_handling_whole(
    fake_redis: Callable[..., _FakeRedis], password: str
) -> None:
    """The interrupt chains the exception the caller was handling when the operation began: that one is the caller's, so
    its frames keep their locals and it keeps its traceback."""
    exc, kept = _interrupted_while_handling(_redis_backend(_url(fake_redis(refusing=False), password)))

    handled = exc.__context__
    assert isinstance(handled, ValueError)
    assert handled.__traceback__ is kept
    assert handled.__traceback__.tb_next.tb_frame.f_locals == {"evidence": "caller's local"}
    _assert_cleared(exc, KeyboardInterrupt, password, through=exc)


@pytest.mark.parametrize("interrupt", _INTERRUPTS)
def test_an_interrupt_through_the_decorator_propagates_as_itself_and_spares_the_breaker(
    fake_redis: Callable[..., _FakeRedis], password: str, interrupt: type[BaseException]
) -> None:
    def double(x: int) -> int:
        return 2 * x

    backend = _redis_backend(_url(fake_redis(refusing=False), password))
    fn = cache(backend=backend, ttl=60, l1_enabled=False, namespace=f"redis-interrupt-{uuid.uuid4().hex}")(double)

    with _interrupting_sends(interrupt):
        # In a copy of the context: the interrupt cuts the call short, before the decorator restores its own context.
        exc = _raised(lambda: contextvars.copy_context().run(fn, 1), interrupt)

    _assert_cleared(exc, interrupt, password, through=exc)
    breaker = fn.get_health_status()["circuit_breaker"]
    assert (breaker["state"], breaker["failure_count"]) == ("closed", 0)


def test_the_walk_finds_the_password_on_an_interrupt_through_redis_py(
    fake_redis: Callable[..., _FakeRedis], password: str
) -> None:
    """Positive control: an interrupt placed as above, raised through redis-py with no cachekit operation around it,
    carries the password in its frames, so the clean walks above are a result."""
    url = _url(fake_redis(refusing=False), password)

    with _interrupting_sends(KeyboardInterrupt):
        exc = _raised(lambda: redis.Redis.from_url(url.get_secret_value()).get("k"), KeyboardInterrupt)

    assert _cachekit_locals_holding(exc, password, below_caller=True) != []
