"""Threads sharing one CachekitIOBackend never fail a request (LAB-7062).

A backend keeps the one sync client it leased at construction and sends every request on it, from
whichever thread calls: a thread pool calling a decorated function, and every async-decorator L2 op,
which StandardCacheHandler runs through asyncio.to_thread. Over HTTP/2 those threads multiplex one
connection, and httpcore's sync HTTP/2 path races there (encode/httpcore#1118): about 1% of ops under the
GIL raised ReadError or RemoteProtocolError, which the handler turned into a spurious miss or an unstored
SET. The sync client is HTTP/1.1, one request per pooled connection. The async client keeps HTTP/2: one
event loop drives it.

The backend runs with the SDK's own client kwargs (mount, limits, keepalive) against
tests/performance/loopback_saas.py, which offers both protocols by ALPN, as the edge does. Only the
loopback guard is lifted and the fake's CA trusted.
"""

from __future__ import annotations

import asyncio
import collections
import shutil
import sys
import threading
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest

from cachekit.backends.cachekitio import config as config_module
from cachekit.backends.cachekitio.backend import CachekitIOBackend
from cachekit.backends.errors import BackendError
from cachekit.cache_handler import StandardCacheHandler

pytestmark = [
    pytest.mark.unit,
    pytest.mark.skipif(shutil.which("openssl") is None, reason="needs openssl for the loopback certificate"),
]

# 3,600 ops: at the ~1.3% degraded rate measured on HTTP/2, about 47 expected failures.
_THREADS = 8
_OPS_PER_THREAD = 450
_KEY = "ns:t:func:m.f:args:" + "ab" * 32 + ":1s"
_VALUE = b"v" * 4096

# Without the GIL, httpcore's HTTP/1.1 pool races too, far more rarely: has_expired() reads _expire_at twice
# while a thread starting a request sets it to None, and the comparison raises TypeError (about 1 request in
# 1,000 at 8 threads on 3.14t). No upstream fix yet. Free-threaded support is not declared (docs/free-threading.md).
_gil_off_h11_race = pytest.mark.xfail(
    not getattr(sys, "_is_gil_enabled", lambda: True)(),
    reason="httpcore HTTP/1.1 has_expired() races without the GIL",
    strict=False,
)


@pytest.fixture
def backend(monkeypatch: pytest.MonkeyPatch, fake_saas: tuple[int, Path]) -> CachekitIOBackend:
    port, cert = fake_saas
    monkeypatch.setattr(config_module, "is_private_ip", lambda hostname: False)
    monkeypatch.setenv("CACHEKIT_ALLOW_CUSTOM_HOST", "true")
    monkeypatch.setenv("SSL_CERT_FILE", str(cert))  # httpx trusts it for the SDK's own transport mount
    # Unique per test: a backend still alive from an earlier test must never lend this one its client.
    return CachekitIOBackend(api_url=f"https://127.0.0.1:{port}", api_key=f"ck_test_{uuid.uuid4().hex}")


def _count_backend_errors(monkeypatch: pytest.MonkeyPatch, backend: CachekitIOBackend) -> collections.Counter[str]:
    """Count every BackendError a sync request raises, before the handler swallows it."""
    errors: collections.Counter[str] = collections.Counter()
    lock = threading.Lock()
    request = backend._request_sync

    def counted(*args: Any, **kwargs: Any) -> Any:
        try:
            return request(*args, **kwargs)
        except BackendError as e:
            with lock:
                errors[f"BackendError {e.error_type}"] += 1
            raise

    monkeypatch.setattr(backend, "_request_sync", counted)
    return errors


def _tally(outcomes: collections.Counter[str], lock: threading.Lock, i: int, result: object) -> None:
    if i % 4 == 3:
        key = "set ok" if result is True else "set not stored"
    else:
        key = "get hit" if result == _VALUE else "get spurious miss"
    with lock:
        outcomes[key] += 1


def _assert_clean(outcomes: collections.Counter[str], errors: collections.Counter[str]) -> None:
    assert sum(outcomes.values()) == _THREADS * _OPS_PER_THREAD
    assert set(outcomes) <= {"get hit", "set ok"}, (dict(outcomes), dict(errors))
    assert not errors, dict(errors)


@_gil_off_h11_race
def test_threads_sharing_one_backend_never_degrade(monkeypatch: pytest.MonkeyPatch, backend: CachekitIOBackend) -> None:
    handler = StandardCacheHandler(backend)
    assert handler.set(_KEY, _VALUE, ttl=60)
    errors = _count_backend_errors(monkeypatch, backend)
    outcomes: collections.Counter[str] = collections.Counter()
    lock = threading.Lock()
    start = threading.Barrier(_THREADS)

    def worker() -> None:
        start.wait()
        for i in range(_OPS_PER_THREAD):
            # One SET in four: what a miss's recompute writes, landing between reads of the same key.
            result = handler.set(_KEY, _VALUE, ttl=60) if i % 4 == 3 else handler.get(_KEY)
            _tally(outcomes, lock, i, result)

    with ThreadPoolExecutor(max_workers=_THREADS) as pool:
        for future in [pool.submit(worker) for _ in range(_THREADS)]:
            future.result()
    _assert_clean(outcomes, errors)


@_gil_off_h11_race
def test_async_l2_ops_via_to_thread_never_degrade(monkeypatch: pytest.MonkeyPatch, backend: CachekitIOBackend) -> None:
    handler = StandardCacheHandler(backend)
    assert handler.set(_KEY, _VALUE, ttl=60)
    errors = _count_backend_errors(monkeypatch, backend)
    outcomes: collections.Counter[str] = collections.Counter()
    lock = threading.Lock()

    async def worker() -> None:
        for i in range(_OPS_PER_THREAD):
            # get_async and set_async run the sync backend on the default executor (asyncio.to_thread).
            result = await (handler.set_async(_KEY, _VALUE, ttl=60) if i % 4 == 3 else handler.get_async(_KEY))
            _tally(outcomes, lock, i, result)

    async def main() -> None:
        await asyncio.gather(*(worker() for _ in range(_THREADS)))

    asyncio.run(main())
    _assert_clean(outcomes, errors)


@pytest.mark.parametrize(
    ("send", "expected"),
    [
        pytest.param(lambda b: b._request_sync("GET", "probe", miss_on_404=True), "HTTP/1.1", id="sync"),
        pytest.param(lambda b: asyncio.run(b._request_async("GET", "probe", miss_on_404=True)), "HTTP/2", id="async"),
    ],
)
def test_protocol_per_client(backend: CachekitIOBackend, send: Callable[[CachekitIOBackend], Any], expected: str) -> None:
    # The peer offers h2 first by ALPN, as the edge does; only the client's own offer decides.
    assert send(backend).http_version == expected
