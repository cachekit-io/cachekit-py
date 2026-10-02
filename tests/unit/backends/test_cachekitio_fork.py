"""CachekitIOBackend across fork(): no process sends on, or reads from, another process's pooled connection.

A forked child inherits its parent's pooled TLS connections, and with them one TLS session's keys and
sequence numbers. Whichever process writes second on that session desynchronises it, so its next request
fails; raced, one process can read the other's response, a value for a different key. These tests drive
the real backend over real TLS sockets, HTTP/1.1 and HTTP/2 by ALPN, against tests/performance/loopback_saas.py:
plain-HTTP loopback cannot reproduce the bug, because a socket shared in sequence is harmless without TLS.
Every request checks the value it gets back, so a response delivered to the wrong process fails too.
"""

from __future__ import annotations

import asyncio
import gc
import json
import logging
import os
import shutil
import signal
import ssl
import subprocess
import sys
import threading
import traceback
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from cachekit.backends.cachekitio import client as client_module
from cachekit.backends.cachekitio.backend import CachekitIOBackend
from cachekit.backends.errors import BackendError

pytestmark = [
    pytest.mark.unit,
    pytest.mark.skipif(not hasattr(os, "fork"), reason="needs os.fork"),
    pytest.mark.skipif(shutil.which("openssl") is None, reason="needs openssl for the loopback certificate"),
]

_REQUESTS = 5
_FAKE = Path(__file__).parents[2] / "performance" / "loopback_saas.py"
_PROTOCOLS = [pytest.param(False, id="h1"), pytest.param(True, id="h2")]


@pytest.fixture(scope="module")
def fake_saas(tmp_path_factory: pytest.TempPathFactory) -> Iterator[tuple[int, Path]]:
    tmp = tmp_path_factory.mktemp("fork-tls")
    cert, key = tmp / "cert.pem", tmp / "key.pem"
    subprocess.run(  # noqa: S603 (trusted: literal openssl argv)
        ["openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1", "-nodes", "-days", "1"]
        + ["-subj", "/CN=127.0.0.1", "-addext", "subjectAltName=IP:127.0.0.1", "-keyout", str(key), "-out", str(cert)],
        check=True,
        capture_output=True,
    )
    proc = subprocess.Popen(  # noqa: S603 (trusted: this interpreter and a repo script)
        [sys.executable, str(_FAKE), "0", str(cert), str(key), "2"], stdout=subprocess.PIPE, text=True
    )
    assert proc.stdout is not None
    line = proc.stdout.readline().split()
    if line[:1] != ["ready"]:
        proc.kill()
        pytest.fail(f"loopback fake failed to start (exit {proc.wait()})")
    yield int(line[1]), cert
    proc.kill()
    proc.wait()


def _backend(monkeypatch: pytest.MonkeyPatch, fake_saas: tuple[int, Path], http2: bool) -> CachekitIOBackend:
    # Config validation (rightly) refuses a loopback URL, so the client kwargs are redirected instead; the
    # backend, its lease and httpx's real connection pool run unchanged. A long keepalive keeps the parent's
    # pooled connection alive across the fork whatever the timing.
    port, cert = fake_saas
    real_kwargs = client_module._client_kwargs

    def local_kwargs(config: Any) -> dict[str, Any]:
        kwargs = real_kwargs(config)
        kwargs["base_url"] = f"https://127.0.0.1:{port}"
        kwargs["verify"] = ssl.create_default_context(cafile=str(cert))
        kwargs["http2"] = http2
        kwargs["limits"] = httpx.Limits(max_connections=10, max_keepalive_connections=10, keepalive_expiry=60.0)
        return kwargs

    monkeypatch.setattr(client_module, "_client_kwargs", local_kwargs)
    # Unique per test: a backend still alive from an earlier test must never lend this one its client.
    backend = CachekitIOBackend(api_key=f"ck_test_fork_{uuid.uuid4().hex}")
    # Warm the pool: the parent's connection is open and idle when the fork lands.
    response = backend._request_sync("GET", "warm", miss_on_404=True)
    assert response.http_version == ("HTTP/2" if http2 else "HTTP/1.1")
    return backend


def _exchange(backend: CachekitIOBackend, tag: str) -> dict[str, int]:
    """Write then read back distinct keys, counting transport errors and values that are not the key's own."""
    errors = wrong = 0
    for i in range(_REQUESTS):
        key, value = f"{tag}-{i}", f"{tag}:{i}".encode()
        try:
            backend.set(key, value)
            wrong += backend.get(key) != value
        except BackendError:
            errors += 1
    return {"errors": errors, "wrong": wrong}


async def _exchange_async(backend: CachekitIOBackend, tag: str) -> dict[str, int]:
    errors = wrong = 0
    for i in range(_REQUESTS):
        key, value = f"{tag}-{i}", f"{tag}:{i}".encode()
        try:
            await backend.set_async(key, value)
            wrong += await backend.get_async(key) != value
        except BackendError:
            errors += 1
    return {"errors": errors, "wrong": wrong}


def _libc_fork() -> int:
    """fork() from C, skipping Python's at-fork hooks as uWSGI does without --py-call-osafterfork."""
    import ctypes

    return ctypes.PyDLL(None).fork()  # PyDLL keeps the GIL through the call


class _Child:
    """A forked child that runs ``job`` once the parent calls ``go()``, and sends back its result."""

    def __init__(self, job: Callable[[], Any], fork: Callable[[], int] = os.fork) -> None:
        go_read, self._go = os.pipe()
        self._result, result_write = os.pipe()
        self.pid = fork()
        if self.pid == 0:  # the child never returns to pytest
            try:
                signal.alarm(10)  # a request hung on a desynchronised connection ends the child, not the run
                os.close(self._go)
                os.read(go_read, 1)
                os.write(result_write, json.dumps(job()).encode())
                os._exit(0)
            except BaseException:
                traceback.print_exc()
                sys.stderr.flush()
            finally:
                os._exit(2)
        os.close(go_read)
        os.close(result_write)

    def go(self) -> None:
        os.write(self._go, b"x")
        os.close(self._go)

    def result(self) -> Any:
        with os.fdopen(self._result) as pipe:
            data = pipe.read()
        _, status = os.waitpid(self.pid, 0)
        if status != 0:
            pytest.fail(f"forked child hung or failed (exit code {os.waitstatus_to_exitcode(status)})")
        return json.loads(data)


def _run(backend: CachekitIOBackend, order: str, fork: Callable[[], int] = os.fork) -> list[dict[str, int]]:
    tag = uuid.uuid4().hex
    if order == "child-then-parent":
        child = _Child(lambda: _exchange(backend, f"{tag}-child"), fork)
        child.go()
        return [child.result(), _exchange(backend, f"{tag}-parent")]
    if order == "parent-then-child":
        child = _Child(lambda: _exchange(backend, f"{tag}-child"), fork)
        parent = _exchange(backend, f"{tag}-parent")
        child.go()
        return [parent, child.result()]
    assert order == "two-children"
    first = _Child(lambda: _exchange(backend, f"{tag}-first"), fork)
    second = _Child(lambda: _exchange(backend, f"{tag}-second"), fork)
    first.go()
    first_result = first.result()
    second.go()
    return [first_result, second.result(), _exchange(backend, f"{tag}-parent")]


_CLEAN = {"errors": 0, "wrong": 0}


@pytest.mark.parametrize("http2", _PROTOCOLS)
@pytest.mark.parametrize("order", ["child-then-parent", "parent-then-child", "two-children"])
def test_each_process_uses_its_own_connection(
    monkeypatch: pytest.MonkeyPatch, fake_saas: tuple[int, Path], http2: bool, order: str
) -> None:
    backend = _backend(monkeypatch, fake_saas, http2)
    results = _run(backend, order)
    assert results == [_CLEAN] * len(results)


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="needs glibc fork() via ctypes")
@pytest.mark.parametrize("http2", _PROTOCOLS)
def test_a_fork_that_skips_at_fork_hooks_is_detected(
    monkeypatch: pytest.MonkeyPatch, fake_saas: tuple[int, Path], http2: bool
) -> None:
    """The owner-PID check alone covers it: a fork from C runs none of Python's at-fork hooks."""
    backend = _backend(monkeypatch, fake_saas, http2)
    results = _run(backend, "child-then-parent", fork=_libc_fork)
    assert results == [_CLEAN, _CLEAN]


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="needs glibc fork() via ctypes")
@pytest.mark.parametrize("http2", _PROTOCOLS)
def test_a_fork_from_c_while_a_parent_thread_holds_the_logging_lock(
    monkeypatch: pytest.MonkeyPatch, fake_saas: tuple[int, Path], http2: bool
) -> None:
    """A fork from C also skips logging's at-fork lock reset, so the child's re-lease must not take that lock.

    The child inherits logging's module lock held by a thread that does not exist there; building its new
    client must not wait on it (the child's alarm ends a hang).
    """
    backend = _backend(monkeypatch, fake_saas, http2)
    held, release = threading.Event(), threading.Event()

    def hold() -> None:
        with logging._lock:  # type: ignore[attr-defined]  # a parent thread inside logging at fork
            held.set()
            release.wait(30)

    holder = threading.Thread(target=hold, daemon=True)
    holder.start()
    try:
        assert held.wait(5)
        child = _Child(lambda: _exchange(backend, f"{uuid.uuid4().hex}-child"), _libc_fork)
    finally:
        release.set()
        holder.join(5)
    child.go()
    assert child.result() == _CLEAN


@pytest.mark.parametrize("http2", _PROTOCOLS)
def test_async_child_on_the_inherited_loop_gets_a_new_client(
    monkeypatch: pytest.MonkeyPatch, fake_saas: tuple[int, Path], http2: bool
) -> None:
    """A child that keeps running its parent's loop object would pass the per-loop check with the parent's client."""
    backend = _backend(monkeypatch, fake_saas, http2)
    tag = uuid.uuid4().hex
    loop = asyncio.new_event_loop()
    try:

        async def warm() -> httpx.AsyncClient:
            await backend.set_async(f"{tag}-warm", b"w")
            return backend._async_lease.client

        parent_client = loop.run_until_complete(warm())

        async def child_job() -> dict[str, Any]:
            result: dict[str, Any] = await _exchange_async(backend, f"{tag}-child")
            # parent_client stays referenced, so a new client cannot reuse its id.
            result["parents_client"] = backend._async_lease.client is parent_client
            return result

        # Child first, then parent: the two processes never run the shared loop object at once.
        child = _Child(lambda: loop.run_until_complete(child_job()))
        child.go()
        results = [child.result(), loop.run_until_complete(_exchange_async(backend, f"{tag}-parent"))]
    finally:
        loop.close()
    assert results == [{**_CLEAN, "parents_client": False}, _CLEAN]


def test_async_child_on_a_new_loop(monkeypatch: pytest.MonkeyPatch, fake_saas: tuple[int, Path]) -> None:
    backend = _backend(monkeypatch, fake_saas, http2=True)
    tag = uuid.uuid4().hex
    asyncio.run(backend.set_async(f"{tag}-warm", b"w"))
    child = _Child(lambda: asyncio.run(_exchange_async(backend, f"{tag}-child")))
    child.go()
    results = [child.result(), asyncio.run(_exchange_async(backend, f"{tag}-parent"))]
    assert results == [_CLEAN, _CLEAN]


def test_child_drops_inherited_clients_without_closing_them(
    monkeypatch: pytest.MonkeyPatch, fake_saas: tuple[int, Path]
) -> None:
    """close() would take the inherited pool's lock, which a parent thread may have held at fork."""
    backend = _backend(monkeypatch, fake_saas, http2=True)
    closed: list[httpx.Client] = []
    real_close = httpx.Client.close

    def spy(self: httpx.Client) -> None:
        closed.append(self)
        real_close(self)

    monkeypatch.setattr(httpx.Client, "close", spy)

    def child_job() -> dict[str, Any]:
        inherited = backend._sync_lease.client
        result: dict[str, Any] = _exchange(backend, f"{uuid.uuid4().hex}-child")
        gc.collect()  # the replaced lease is unreferenced now; its finalizer runs here at the latest
        result["closed"] = len(closed)
        result["replaced"] = backend._sync_lease.client is not inherited
        return result

    child = _Child(child_job)
    child.go()
    assert child.result() == {**_CLEAN, "closed": 0, "replaced": True}
    assert _exchange(backend, f"{uuid.uuid4().hex}-parent") == _CLEAN
