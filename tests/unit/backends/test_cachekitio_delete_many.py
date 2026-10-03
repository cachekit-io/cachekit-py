"""Whole-function invalidation on CachekitIO fans its DELETEs out, 16 at a time (LAB-7070).

The SaaS has no bulk delete, so ``_delete_many`` sends one DELETE per key on a bounded pool.
Requests go through a real httpx client on a MockTransport whose DELETEs each take ``_DELAY``
seconds, so invalidation wall time divided by ``_DELAY`` counts the round-trip waves: about
``ceil(N / 16)``, where the serial per-key path took ``N``.
"""

from __future__ import annotations

import asyncio
import math
import os
import threading
import time
from typing import Any
from unittest.mock import MagicMock, patch
from urllib.parse import unquote

import httpx
import pytest

from cachekit import cache
from cachekit.backends.cachekitio.backend import _DELETE_FANOUT, CachekitIOBackend
from cachekit.backends.errors import BackendError
from cachekit.cache_handler import _supports_multi_delete

_TEST_API_URL = "https://api.cachekit.io"
_TEST_API_KEY = "ck_test_abc123"  # pragma: allowlist secret — fake key, test fixture
_DELAY = 0.05


class _Server:
    """An in-memory cache API whose entry DELETEs take ``_DELAY`` s; tracks DELETEs in flight."""

    def __init__(self, reject: frozenset[str] = frozenset(), status: int = 429) -> None:
        self.store: dict[str, bytes] = {}
        self.deleted: list[str] = []
        self.reject, self.status = reject, status
        self.in_flight = self.peak = 0
        self._lock = threading.Lock()

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.raw_path.decode().split("?")[0]
        rest = path.removeprefix("/v1/cache/")
        if rest.endswith("/lock"):  # async miss path: always grant, always release
            return httpx.Response(200, json={"lock_id": "l"} if request.method == "POST" else {})
        key = unquote(rest)
        if request.method == "GET":
            return httpx.Response(200, content=self.store[key]) if key in self.store else httpx.Response(404)
        if request.method == "PUT":
            self.store[key] = request.read()
            return httpx.Response(200)
        assert request.method == "DELETE", request.method
        with self._lock:
            self.in_flight += 1
            self.peak = max(self.peak, self.in_flight)
        try:
            time.sleep(_DELAY)
            if key in self.reject:
                return httpx.Response(self.status, json={"error": "rejected"})
            self.store.pop(key, None)
            with self._lock:
                self.deleted.append(key)
            return httpx.Response(200)
        finally:
            with self._lock:
                self.in_flight -= 1


def _backend(server: _Server) -> CachekitIOBackend:
    transport = httpx.MockTransport(server)
    with (
        patch(
            "cachekit.backends.cachekitio.backend.lease_sync_http_client",
            return_value=MagicMock(pid=os.getpid(), client=httpx.Client(base_url=_TEST_API_URL, transport=transport)),
        ),
        patch(
            "cachekit.backends.cachekitio.backend.lease_async_http_client",
            return_value=MagicMock(pid=os.getpid(), client=httpx.AsyncClient(base_url=_TEST_API_URL, transport=transport)),
        ),
    ):
        return CachekitIOBackend(api_url=_TEST_API_URL, api_key=_TEST_API_KEY)


def _cached_keys(fn: Any) -> set[tuple[str, str]]:
    """The decorator's ``_cached_keys`` closure cell, reached through nested closures."""
    seen: set[int] = set()
    stack = [fn]
    while stack:
        f = stack.pop()
        if id(f) in seen or not hasattr(f, "__code__"):
            continue
        seen.add(id(f))
        for var, cell in zip(f.__code__.co_freevars, f.__closure__ or (), strict=True):
            if var == "_cached_keys":
                return cell.cell_contents
            stack.append(cell.cell_contents)
    raise AssertionError("_cached_keys not found")


def test_backend_takes_the_multi_key_path() -> None:
    assert _supports_multi_delete(_backend(_Server()))


def test_empty_batch_sends_nothing() -> None:
    server = _Server()
    assert _backend(server)._delete_many([]) == set()
    assert server.deleted == []


@pytest.mark.parametrize("n", [1, 16, 100])
def test_sync_invalidate_runs_in_ceil_n_over_16_waves(n: int) -> None:
    server = _Server()

    @cache(backend=_backend(server), ttl=60, namespace=f"fanout_sync_{n}", l1_enabled=False)
    def f(x: int) -> int:
        return x

    for i in range(n):
        f(i)
    assert len(server.store) == n

    start = time.perf_counter()
    f.invalidate_cache()
    waves = (time.perf_counter() - start) / _DELAY

    waves_expected = math.ceil(n / _DELETE_FANOUT)
    assert waves_expected - 0.5 < waves < waves_expected + 2, waves  # serial: n waves
    assert sorted(server.deleted) == sorted(set(server.deleted))  # each key DELETEd exactly once
    assert len(server.deleted) == n and server.store == {}
    assert server.peak == min(n, _DELETE_FANOUT)
    assert _cached_keys(f) == set()


@pytest.mark.asyncio
async def test_ainvalidate_cache_takes_the_fan_out() -> None:
    n = 48
    server = _Server()

    @cache(backend=_backend(server), ttl=60, namespace="fanout_async", l1_enabled=False)
    async def f(x: int) -> int:
        return x

    for i in range(n):
        await f(i)
    assert len(server.store) == n

    start = time.perf_counter()
    await f.ainvalidate_cache()
    waves = (time.perf_counter() - start) / _DELAY

    assert waves < math.ceil(n / _DELETE_FANOUT) + 2, waves
    assert server.peak == _DELETE_FANOUT
    assert server.store == {} and _cached_keys(f) == set()


@pytest.mark.parametrize("status", [429, 404, 503])
def test_rejected_keys_are_returned_exactly_and_stay_tracked(status: int) -> None:
    keys = [f"k{i}" for i in range(40)]
    reject = frozenset(keys[::7])
    server = _Server(reject=reject, status=status)

    failed = _backend(server)._delete_many(keys)

    assert failed == reject
    assert sorted(server.deleted) == sorted(set(keys) - reject)

    # Through the decorator: exactly the rejected entries stay tracked for the next sweep.
    server = _Server()

    @cache(backend=_backend(server), ttl=60, namespace=f"fanout_reject_{status}", l1_enabled=False)
    def f(x: int) -> int:
        return x

    for i in range(40):
        f(i)
    tracked = {key for _, key in _cached_keys(f)}
    server.reject = frozenset(sorted(tracked)[::7])

    f.invalidate_cache()

    assert {key for _, key in _cached_keys(f)} == server.reject
    assert set(server.store) == server.reject


def test_unexpected_error_propagates_after_every_delete_finished() -> None:
    server = _Server()
    backend = _backend(server)
    calls: list[str] = []

    def delete(key: str) -> bool:
        calls.append(key)
        if key == "boom":
            raise RuntimeError("bug")
        if key == "down":
            raise BackendError("down")
        return True

    with patch.object(backend, "delete", side_effect=delete), pytest.raises(RuntimeError, match="bug"):
        backend._delete_many(["a", "boom", "down", "b"])
    assert sorted(calls) == ["a", "b", "boom", "down"]


def test_deletes_see_the_callers_context() -> None:
    """The metrics headers read contextvars, so each worker runs in a copy of the caller's context."""
    import contextvars

    var: contextvars.ContextVar[str] = contextvars.ContextVar("var", default="unset")
    backend = _backend(_Server())
    seen: list[str] = []

    def delete(key: str) -> bool:
        seen.append(var.get())
        return True

    var.set("caller")
    with patch.object(backend, "delete", side_effect=delete):
        assert backend._delete_many(["a", "b", "c"]) == set()
    assert seen == ["caller"] * 3


def test_runs_from_a_thread_with_a_running_event_loop() -> None:
    """A sync invalidate_cache() called inside async code still fans out (no event loop needed)."""
    server = _Server()
    backend = _backend(server)

    async def main() -> set[str]:
        return backend._delete_many(["a", "b"])

    assert asyncio.run(main()) == set()
    assert sorted(server.deleted) == ["a", "b"]
