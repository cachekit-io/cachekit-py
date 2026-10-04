"""CachekitIOBackend across consecutive event loops (asyncio.run per job, Celery, test suites) and concurrent coroutines.

Under httpx every loop needed an AsyncClient of its own, since pooled connections belong to the loop that opened them:
a client reused on a later loop failed with RuntimeError('Event loop is closed'), and the lock path read that as
"held", so the server granted a lock the client never saw and the caller polled the full blocking_timeout. Now
every async method sends on the backend's one thread-safe client through ``asyncio.to_thread``, and the client
holds nothing bound to a loop. These tests pin that: every async method goes through that client, on any number of
successive loops and on many coroutines at once, and over real pooled sockets a new loop locks on its first attempt.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
import uuid
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest
from urllib3 import HTTPResponse

from cachekit.backends.cachekitio import client as client_module
from cachekit.backends.cachekitio.backend import LOCK_ID_HEADER, CachekitIOBackend
from tests.utils.cachekitio_fakes import FakePool, FakeRequest, fake_backend, response

_KEY = "ns:t:func:m.f:args:" + "a" * 64 + ":1s"
_PATH = "/v1/cache/ns%3At%3Afunc%3Am.f%3Aargs%3A" + "a" * 64 + "%3A1s"
_LOOPS = 5


# ---------------------------------------------------------------------------
# Every async method, on the fake pool
# ---------------------------------------------------------------------------


class _Api:
    """Answers each SaaS endpoint and records the thread each request was sent from."""

    def __init__(self) -> None:
        self.threads: set[int] = set()
        self._lock = threading.Lock()

    def __call__(self, request: FakeRequest) -> HTTPResponse:
        with self._lock:
            self.threads.add(threading.get_ident())
        if request.path.endswith("/lock"):
            return response(200, json={"lock_id": "L1"} if request.method == "POST" else {})
        if request.path.endswith("/ttl"):
            return response(200, json={"ttl": 60} if request.method == "GET" else {})
        if request.path == "/v1/cache/health":
            return response(200, json={"version": "9.9.9"})
        if request.method == "GET":
            return response(200, b"value")
        return response(200, json={"success": True})


_EXPECTED_REQUESTS = [
    ("GET", _PATH),
    ("PUT", _PATH),
    ("HEAD", _PATH),
    ("DELETE", _PATH),
    ("GET", "/v1/cache/health"),
    ("POST", f"{_PATH}/lock"),
    ("DELETE", f"{_PATH}/lock"),
    ("GET", f"{_PATH}/ttl"),
    ("PATCH", f"{_PATH}/ttl"),
]


async def _every_async_method(backend: CachekitIOBackend) -> dict[str, Any]:
    results: dict[str, Any] = {
        "client": backend._lease.client,
        "get": await backend.get_async(_KEY),
        "set": await backend.set_async(_KEY, b"value", ttl=60),
        "exists": await backend.exists_async(_KEY),
        "delete": await backend.delete_async(_KEY),
        "health": (await backend.health_check_async())[0],
    }
    async with backend.acquire_lock(_KEY, timeout=5.0) as acquired:
        results["lock"] = acquired
    results["ttl"] = await backend.get_ttl(_KEY)
    results["refresh"] = await backend.refresh_ttl(_KEY, 120)
    return results


def _sent(pool: FakePool) -> list[tuple[str, str]]:
    return [(r.method, r.path) for r in pool.requests]


@pytest.mark.unit
def test_every_async_method_sends_on_the_backends_client_on_each_new_loop() -> None:
    """Successor of the per-loop AsyncClient: one client, nothing bound to a loop, so a later loop needs nothing new."""
    api = _Api()
    backend, pool = fake_backend(api)
    client = backend._lease.client
    expected = {
        "client": client,
        "get": b"value",
        "set": None,
        "exists": True,
        "delete": True,
        "health": True,
        "lock": True,
        "ttl": 60,
        "refresh": True,
    }
    for _ in range(_LOOPS):
        assert asyncio.run(_every_async_method(backend)) == expected
    assert _sent(pool) == _EXPECTED_REQUESTS * _LOOPS
    # The fake pool sits on the backend's own client, so every request above went through it, under its key.
    assert all(r.headers["Authorization"] == client.headers["Authorization"] for r in pool.requests)
    # Sent from worker threads: a blocking urllib3 call on the loop's thread would stall every other coroutine.
    assert threading.get_ident() not in api.threads


@pytest.mark.unit
async def test_concurrent_async_ops_share_the_client_without_error() -> None:
    """Many coroutines at once each run their request on a worker thread, all on the one thread-safe pool."""
    backend, pool = fake_backend(_Api())
    rounds = 25
    results = await asyncio.gather(*(_every_async_method(backend) for _ in range(rounds)))
    assert all(r["get"] == b"value" and r["lock"] is True and r["ttl"] == 60 and r["refresh"] is True for r in results)
    assert sorted(_sent(pool)) == sorted(_EXPECTED_REQUESTS * rounds)


# ---------------------------------------------------------------------------
# Real sockets: the lock path on successive loops
# ---------------------------------------------------------------------------


class _FakeSaaS(ThreadingHTTPServer):
    """SaaS lock semantics: a held, unexpired lock answers lock_id null; DELETE releases only on a matching lock id."""

    daemon_threads = True

    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), _Handler)
        self.mutex = threading.Lock()
        self.locks: dict[str, tuple[str, float]] = {}
        self.lock_posts = 0
        self.connections = 0

    def held(self) -> list[str]:
        now = time.monotonic()
        with self.mutex:
            return [path for path, (_, expiry) in self.locks.items() if expiry > now]


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"  # keep-alive, so the client pools connections
    server: _FakeSaaS

    def setup(self) -> None:
        super().setup()
        with self.server.mutex:
            self.server.connections += 1

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


@pytest.fixture
def saas() -> Iterator[_FakeSaaS]:
    server = _FakeSaaS()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()


def _backend_on(saas: _FakeSaaS) -> CachekitIOBackend:
    """A backend whose client's pool is the SDK's own pool policy, pointed at the loopback server.

    Config validation (rightly) refuses a plain-HTTP loopback URL, so only the pool is built from an
    unvalidated copy of the config; the backend, its lease and client, and urllib3's real pool run unchanged.
    """
    # Unique per test: a backend still alive from an earlier test must never lend this one its client.
    backend = CachekitIOBackend(api_key=f"ck_test_loops_{uuid.uuid4().hex}")
    client = backend._lease.client
    client.pool.close()
    local = backend._config.model_copy(update={"api_url": f"http://127.0.0.1:{saas.server_address[1]}"})
    client.pool = client_module._connection_pool(local)  # type: ignore[assignment]
    return backend


async def _one_job(backend: CachekitIOBackend, saas: _FakeSaaS, first: str) -> dict[str, Any]:
    # The loop's first request is the one a stale pooled connection would fail, so each call takes a turn first.
    ttl = await backend.get_ttl(_KEY) if first == "ttl" else None
    posts_before = saas.lock_posts
    attempts: list[str | None] = []
    real_try = backend._try_acquire_lock

    async def counted(lock_key: str, timeout: float) -> str | None:
        # Counted client-side too: a failed attempt may never reach the server.
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
def test_every_new_loop_locks_on_its_first_attempt(saas: _FakeSaaS, first: str) -> None:
    """Back-to-back loops on live pooled connections: each locks at once and leaves nothing held."""
    backend = _backend_on(saas)
    results = [asyncio.run(_one_job(backend, saas, first)) for _ in range(3)]
    expected = {"ttl": 60, "got_lock": True, "lock_attempts": 1, "lock_posts": 1, "got_lease": True, "held": []}
    assert results == [expected] * 3
    # One pooled connection carried every request of every loop: the regime in which httpx's loop-bound pool broke.
    assert saas.connections == 1
