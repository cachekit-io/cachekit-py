"""CachekitIOBackend across consecutive event loops (asyncio.run per job, Celery, test suites).

An httpx.AsyncClient's pooled connections belong to the loop that opened them, so a client reused on a
later loop fails its next request with RuntimeError('Event loop is closed'). The backend filed that as
UNKNOWN, and the lock path read it as "held": with a live pooled connection the server granted a lock
the client never saw, the caller polled the full blocking_timeout and left the lock orphaned.

These tests drive the real backend over real sockets: httpx.MockTransport has no connection pool, so
it cannot reproduce the bug.
"""

from __future__ import annotations

import asyncio
import gc
import json
import threading
import time
import uuid
import weakref
from collections.abc import Iterator
from concurrent.futures import Future
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import httpx
import pytest

from cachekit.backends.cachekitio import client as client_module
from cachekit.backends.cachekitio.backend import LOCK_ID_HEADER, CachekitIOBackend

_KEY = "ns:t:func:m.f:args:" + "a" * 64 + ":1s"
_LOOPS = 5


class _FakeSaaS(ThreadingHTTPServer):
    """SaaS lock semantics: a held, unexpired lock answers lock_id null; DELETE releases only on a matching lock id."""

    daemon_threads = True

    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), _Handler)
        self.mutex = threading.Lock()
        self.locks: dict[str, tuple[str, float]] = {}
        self.lock_posts = 0

    def held(self) -> list[str]:
        now = time.monotonic()
        with self.mutex:
            return [path for path, (_, expiry) in self.locks.items() if expiry > now]


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"  # keep-alive, so the client pools connections
    server: _FakeSaaS

    def _reply(self, body: dict[str, Any]) -> None:
        data = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self) -> None:
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        now = time.monotonic()
        with self.server.mutex:
            self.server.lock_posts += 1
            current = self.server.locks.get(self.path)
            if current is not None and current[1] > now:
                lock_id = None
            else:
                lock_id = str(uuid.uuid4())
                self.server.locks[self.path] = (lock_id, now + body["timeout_ms"] / 1000)
        self._reply({"lock_id": lock_id})

    def do_DELETE(self) -> None:
        with self.server.mutex:
            current = self.server.locks.get(self.path)
            if current is not None and current[0] == self.headers.get(LOCK_ID_HEADER):
                del self.server.locks[self.path]
        self._reply({})

    def do_GET(self) -> None:
        self._reply({"ttl": 60})

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 — BaseHTTPRequestHandler's name
        pass


def _api_key() -> str:
    # Unique per test: a backend still alive from an earlier test must never lend this one its client.
    return f"ck_test_loops_{uuid.uuid4().hex}"


def _slots_for(api_key: str) -> list[Any]:
    return [slot for key, slot in client_module._clients().async_slots.items() if key[1] == api_key]


@pytest.fixture
def saas() -> Iterator[_FakeSaaS]:
    server = _FakeSaaS()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()


def _point_clients_at(monkeypatch: pytest.MonkeyPatch, saas: _FakeSaaS, keepalive_expiry: float) -> None:
    # Config validation (rightly) refuses a plain-HTTP loopback URL, so the client kwargs are redirected
    # instead; the backend, its lease and httpx's real connection pool all run unchanged.
    real_kwargs = client_module._client_kwargs

    def local_kwargs(config: Any, transport_cls: Any) -> dict[str, Any]:
        kwargs = real_kwargs(config, transport_cls)
        kwargs["base_url"] = f"http://127.0.0.1:{saas.server_address[1]}"
        kwargs["http2"] = False
        kwargs["limits"] = httpx.Limits(max_connections=10, max_keepalive_connections=10, keepalive_expiry=keepalive_expiry)
        kwargs["mounts"] = None  # the keepalive mount would bypass the overrides above
        return kwargs

    monkeypatch.setattr(client_module, "_client_kwargs", local_kwargs)


async def _one_job(backend: CachekitIOBackend, saas: _FakeSaaS, first: str) -> dict[str, Any]:
    # The loop's first request is the one a stale pooled connection fails, so each call takes a turn first.
    ttl = await backend.get_ttl(_KEY) if first == "ttl" else None
    posts_before = saas.lock_posts
    attempts: list[str | None] = []
    real_try = backend._try_acquire_lock

    async def counted(lock_key: str, timeout: float) -> str | None:
        # Counted client-side too: past keepalive the failed attempt never reaches the server.
        attempts.append(await real_try(lock_key, timeout))
        return attempts[-1]

    backend._try_acquire_lock = counted  # type: ignore[method-assign]
    try:
        # The async @cache miss path: lock_timeout 30 s, blocking_timeout 5 s.
        async with backend.acquire_lock(_KEY, timeout=30.0, blocking_timeout=5.0) as got_lock:
            lock_posts = saas.lock_posts - posts_before
    finally:
        del backend._try_acquire_lock
    # The SWR revalidation lease: one attempt, no polling; got False means the refresh is skipped.
    async with backend.acquire_lock(_KEY, timeout=30.0, blocking_timeout=None) as got_lease:
        pass
    if first != "ttl":
        ttl = await backend.get_ttl(_KEY)
    return {
        "ttl": ttl,
        "got_lock": got_lock,
        "lock_attempts": len(attempts),
        "lock_posts": lock_posts,
        "got_lease": got_lease,
        "held": saas.held(),
    }


@pytest.mark.unit
@pytest.mark.parametrize("first", ["lock", "ttl"])
@pytest.mark.parametrize(
    ("gap", "keepalive_expiry"),
    [
        pytest.param(0.0, 5.0, id="gap-0s-live-pooled-connection"),
        # Same regime as a gap past httpx's 5 s keepalive, without the 5 s: the pooled connection expires first.
        pytest.param(0.3, 0.1, id="gap-past-keepalive"),
        pytest.param(5.5, 5.0, id="gap-5.5s-default-keepalive", marks=pytest.mark.slow),
    ],
)
def test_every_new_loop_locks_on_its_first_attempt(
    monkeypatch: pytest.MonkeyPatch, saas: _FakeSaaS, gap: float, keepalive_expiry: float, first: str
) -> None:
    _point_clients_at(monkeypatch, saas, keepalive_expiry)
    backend = CachekitIOBackend(api_key=_api_key())
    results = []
    for _ in range(3):
        results.append(asyncio.run(_one_job(backend, saas, first)))
        time.sleep(gap)
    expected = {"ttl": 60, "got_lock": True, "lock_attempts": 1, "lock_posts": 1, "got_lease": True, "held": []}
    assert results == [expected] * 3


@pytest.mark.unit
def test_finished_loops_and_their_clients_are_released(monkeypatch: pytest.MonkeyPatch, saas: _FakeSaaS) -> None:
    """One cached client per thread and config; a finished loop is not kept alive past the next one."""
    _point_clients_at(monkeypatch, saas, keepalive_expiry=5.0)
    api_key = _api_key()
    backend = CachekitIOBackend(api_key=api_key)
    loops: list[weakref.ref[asyncio.AbstractEventLoop]] = []

    async def job(b: CachekitIOBackend) -> None:
        loops.append(weakref.ref(asyncio.get_running_loop()))
        assert await b.get_ttl(_KEY) == 60

    for _ in range(_LOOPS):
        asyncio.run(job(backend))
    gc.collect()
    # The last loop's client stays cached for the next job, and its pooled connection holds that loop.
    assert [ref() is None for ref in loops] == [True] * (_LOOPS - 1) + [False]
    assert len(_slots_for(api_key)) == 1
    del backend
    gc.collect()
    assert all(ref() is None for ref in loops)
    assert _slots_for(api_key) == []


@pytest.mark.unit
def test_threads_running_their_own_loops_never_share_a_client(monkeypatch: pytest.MonkeyPatch, saas: _FakeSaaS) -> None:
    _point_clients_at(monkeypatch, saas, keepalive_expiry=5.0)
    backend = CachekitIOBackend(api_key=_api_key())
    clients: list[httpx.AsyncClient] = []
    ready = threading.Barrier(2)

    async def job() -> None:
        ready.wait(5)  # both loops running at once
        clients.append(backend._async_lease.client)
        assert await backend.get_ttl(_KEY) == 60

    def run(future: Future[None]) -> None:
        # Forwarded, not handled: result() below re-raises it on the test thread with its own traceback.
        try:
            asyncio.run(job())
        except BaseException as exc:
            future.set_exception(exc)
        else:
            future.set_result(None)

    # Daemon threads, not a pool: a hung worker fails here as TimeoutError and is left behind, where a
    # pool's shutdown would wait for it and hang the test.
    futures: list[Future[None]] = [Future(), Future()]
    for future in futures:
        threading.Thread(target=run, args=(future,), daemon=True).start()
    for future in futures:
        future.result(timeout=10)
    assert len(clients) == 2
    assert clients[0] is not clients[1]


@pytest.mark.unit
def test_construction_builds_no_async_client(monkeypatch: pytest.MonkeyPatch) -> None:
    built: list[httpx.AsyncClient] = []
    real_init = httpx.AsyncClient.__init__

    def spy(self: httpx.AsyncClient, *args: Any, **kwargs: Any) -> None:
        built.append(self)
        real_init(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", spy)
    backend = CachekitIOBackend(api_key=_api_key())
    assert backend._sync_lease.client is not None
    assert built == []
