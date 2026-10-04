"""CachekitIOBackend across fork(): no process sends on, or reads from, another process's pooled connection.

A forked child inherits its parent's pooled TLS connections, and with them one TLS session's keys and
sequence numbers. Whichever process writes second on that session desynchronises it, so its next request
fails; raced, one process can read the other's response, a value for a different key. These tests drive
the real backend and its one process-wide urllib3 client over real TLS sockets against
tests/performance/loopback_saas.py: plain-HTTP loopback cannot reproduce the bug, because a socket shared in
sequence is harmless without TLS. Every request checks the value it gets back, so a response delivered to the
wrong process fails too.

The client cache is owned by a PID (client.py, ``_own_leases``): a child, forked by ``os.fork()`` or from C
without Python's at-fork hooks, replaces the inherited cache and its lock, re-leases a client of its own, and
drops the inherited lease without closing it. Sync and async methods share that one client, since async
methods send on it through ``asyncio.to_thread``. So a child runs async methods on an event loop of its own
(``asyncio.run``): a child that keeps running its parent's loop object waits forever on that loop's inherited
default executor, whose worker threads did not survive the fork, as every async decorator's L2 operation
already did. That case is not tested.
"""

from __future__ import annotations

import asyncio
import gc
import json
import logging
import os
import shutil
import signal
import sys
import threading
import traceback
import uuid
import weakref
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from cachekit.backends.cachekitio import client as client_module
from cachekit.backends.cachekitio import config as config_module
from cachekit.backends.cachekitio.backend import CachekitIOBackend
from cachekit.backends.errors import BackendError

pytestmark = [
    pytest.mark.unit,
    pytest.mark.skipif(not hasattr(os, "fork"), reason="needs os.fork"),
    pytest.mark.skipif(shutil.which("openssl") is None, reason="needs openssl for the loopback certificate"),
]

_REQUESTS = 5


@pytest.fixture
def backend(monkeypatch: pytest.MonkeyPatch, fake_saas: tuple[int, Path]) -> CachekitIOBackend:
    # Config validation (rightly) refuses a loopback URL, so only that guard is lifted and the fake's CA trusted;
    # the backend, its lease and urllib3's real connection pool run unchanged.
    port, cert = fake_saas
    monkeypatch.setattr(config_module, "is_private_ip", lambda hostname: False)
    monkeypatch.setenv("CACHEKIT_ALLOW_CUSTOM_HOST", "true")
    monkeypatch.setenv("SSL_CERT_FILE", str(cert))  # OpenSSL's default trust store reads it
    # Unique per test: a backend still alive from an earlier test must never lend this one its client.
    backend = CachekitIOBackend(api_url=f"https://127.0.0.1:{port}", api_key=f"ck_test_fork_{uuid.uuid4().hex}")
    # Warm the pool: the parent's connection is open and idle when the fork lands.
    response = backend._request_sync("GET", "warm", miss_on_404=True)
    assert response.version_string == "HTTP/1.1"
    assert _pool(backend).num_connections == 1
    return backend


def _pool(backend: CachekitIOBackend) -> Any:
    return backend._lease.client.pool


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
    """fork() from C, skipping Python's at-fork hooks as uWSGI does without --py-call-uwsgi-fork-hooks."""
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


@pytest.mark.parametrize("order", ["child-then-parent", "parent-then-child", "two-children"])
def test_each_process_uses_its_own_connection(backend: CachekitIOBackend, order: str) -> None:
    parent_client = backend._lease.client
    results = _run(backend, order)
    assert results == [_CLEAN] * len(results)
    # The parent kept its client, and its one warm connection served every request: no child's traffic broke it.
    assert backend._lease.client is parent_client
    assert _pool(backend).num_connections == 1


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="needs glibc fork() via ctypes")
def test_a_fork_that_skips_at_fork_hooks_is_detected(backend: CachekitIOBackend) -> None:
    """The owner-PID check alone covers it: a fork from C runs none of Python's at-fork hooks."""
    results = _run(backend, "child-then-parent", fork=_libc_fork)
    assert results == [_CLEAN, _CLEAN]
    assert _pool(backend).num_connections == 1


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="needs glibc fork() via ctypes")
def test_a_fork_from_c_while_a_parent_thread_holds_the_logging_lock(backend: CachekitIOBackend) -> None:
    """A fork from C also skips logging's at-fork lock reset, so the child's re-lease must not take that lock.

    The child inherits logging's module lock held by a thread that does not exist there; building its new
    client must not wait on it (the child's alarm ends a hang).
    """
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


@pytest.mark.parametrize("fork", [pytest.param(os.fork, id="os.fork"), pytest.param(_libc_fork, id="libc-fork")])
def test_child_replaces_the_inherited_lease_cache(backend: CachekitIOBackend, fork: Callable[[], int]) -> None:
    """The process-wide cache is the parent's in a child until first use, and its lock may be held by a parent thread.

    The child must build a cache of its own, owned by its PID with a fresh lock, and lease from it; the parent's
    cache is untouched.
    """
    if fork is _libc_fork and not sys.platform.startswith("linux"):
        pytest.skip("needs glibc fork() via ctypes")
    parent_leases = client_module._own_leases()

    def child_job() -> dict[str, Any]:
        inherited = client_module._leases
        result: dict[str, Any] = _exchange(backend, f"{uuid.uuid4().hex}-child")
        own = client_module._leases
        result["inherited_was_parents"] = inherited is parent_leases and inherited.pid != os.getpid()
        result["replaced"] = own is not inherited
        result["owned_by_child"] = own.pid == os.getpid()
        result["fresh_lock"] = own.lock is not inherited.lock
        # The backend's re-lease came from the child's cache, so a second backend there shares it.
        result["cached"] = client_module.lease_http_client(backend._config) is backend._lease
        return result

    child = _Child(child_job, fork)
    child.go()
    assert child.result() == {
        **_CLEAN,
        "inherited_was_parents": True,
        "replaced": True,
        "owned_by_child": True,
        "fresh_lock": True,
        "cached": True,
    }
    assert client_module._leases is parent_leases
    assert parent_leases.pid == os.getpid()


def test_async_child_on_a_new_loop_uses_the_re_leased_client(backend: CachekitIOBackend) -> None:
    """Async methods send on the same client through asyncio.to_thread, so a child's async ops re-lease too.

    The child's async and sync requests then share its one re-leased client, never the parent's.
    """
    tag = uuid.uuid4().hex
    asyncio.run(backend.set_async(f"{tag}-warm", b"w"))
    parent_client = backend._lease.client

    def child_job() -> dict[str, Any]:
        result: dict[str, Any] = asyncio.run(_exchange_async(backend, f"{tag}-child"))
        client = backend._lease.client
        result["sync"] = _exchange(backend, f"{tag}-child-sync")
        # parent_client stays referenced, so a new client cannot reuse its id.
        result["parents_client"] = client is parent_client
        result["sync_shares_it"] = backend._lease.client is client
        return result

    child = _Child(child_job)
    child.go()
    results = [child.result(), asyncio.run(_exchange_async(backend, f"{tag}-parent"))]
    assert results == [{**_CLEAN, "sync": _CLEAN, "parents_client": False, "sync_shares_it": True}, _CLEAN]
    assert backend._lease.client is parent_client
    assert _pool(backend).num_connections == 1


def test_child_drops_inherited_clients_without_closing_them(monkeypatch: pytest.MonkeyPatch, backend: CachekitIOBackend) -> None:
    """close() would take the inherited pool's lock, which a parent thread may have held at fork."""
    closed: list[client_module.HTTPClient] = []
    real_close = client_module.HTTPClient.close

    def spy(self: client_module.HTTPClient) -> None:
        closed.append(self)
        real_close(self)

    monkeypatch.setattr(client_module.HTTPClient, "close", spy)

    def child_job() -> dict[str, Any]:
        inherited = weakref.ref(backend._lease)
        inherited_client = backend._lease.client
        result: dict[str, Any] = _exchange(backend, f"{uuid.uuid4().hex}-child")
        gc.collect()  # the replaced lease is unreferenced now; its finalizer runs here at the latest
        # The finalizer ran (the lease is gone) and its PID guard skipped the close.
        result["finalized"] = inherited() is None
        result["closed"] = len(closed)
        result["replaced"] = backend._lease.client is not inherited_client
        return result

    child = _Child(child_job)
    child.go()
    assert child.result() == {**_CLEAN, "finalized": True, "closed": 0, "replaced": True}
    assert _exchange(backend, f"{uuid.uuid4().hex}-parent") == _CLEAN
    assert _pool(backend).num_connections == 1
