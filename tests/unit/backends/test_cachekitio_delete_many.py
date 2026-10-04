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
from collections.abc import Callable
from types import SimpleNamespace
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
_HOLD_STALL = 10.0


class _Server:
    """An in-memory cache API whose entry DELETEs take ``_DELAY`` s; tracks DELETEs in flight.

    ``threads`` holds the id of every thread that sent a DELETE. With ``hold``, DELETEs wait until
    ``hold`` of them are in flight at once, then none waits again. A pool starts workers lazily
    and reuses one that went idle, so without the hold a slow submit loop lets an early DELETE
    finish and the pool never starts all its workers. Held, no worker goes idle before the last
    starts, so a 16-worker pool uses exactly 16 threads however the host schedules it; a DELETE
    sent outside the pool adds one more.

    A wave that cannot fill (fewer workers, a per-key loop) stops gaining DELETEs for good, and a
    starved submitter only for a while; nothing the server sees tells the two apart. So the hold
    gives up only after ``_HOLD_STALL`` s in which no DELETE arrived, however long the wave took.
    """

    def __init__(self, reject: frozenset[str] = frozenset(), status: int = 429, hold: int = 0) -> None:
        self.store: dict[str, bytes] = {}
        self.deleted: list[str] = []
        self.reject, self.status = reject, status
        self.in_flight = self.peak = 0
        self.threads: set[int] = set()
        self.hold = hold
        self._released = threading.Event()
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
            self.threads.add(threading.get_ident())
            self.in_flight += 1
            self.peak = max(self.peak, self.in_flight)
            if self.in_flight >= self.hold:
                self._released.set()
            seen = self.in_flight
        try:
            while not self._released.wait(_HOLD_STALL):
                with self._lock:  # held, nothing finishes: in_flight grows exactly when a DELETE arrives
                    if self.in_flight == seen:
                        self._released.set()  # cannot fill: let the rest through, the peak assertion reports it
                    seen = self.in_flight
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


def _backend(server: Callable[[httpx.Request], httpx.Response]) -> CachekitIOBackend:
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
    server = _Server(hold=_DELETE_FANOUT)

    @cache(backend=_backend(server), ttl=60, namespace="fanout_async", l1_enabled=False)
    async def f(x: int) -> int:
        return x

    for i in range(n):
        await f(i)
    assert len(server.store) == n

    await f.ainvalidate_cache()

    # Fan-out shape from the server, not the clock (LAB-7889): the held first wave puts 16 DELETEs
    # in flight on 16 pool threads. A key deleted outside the pool (per-key loop, serial tail)
    # brings a 17th thread; a pool of fewer than 16 never fills the hold and peaks below 16.
    assert len(server.threads) == _DELETE_FANOUT, len(server.threads)
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


# ---- Pacing: a rate-limited fan-out waits out Retry-After instead of failing keys ----------

_RTT = 0.2  # virtual seconds per DELETE round trip
_REAL_RTT = 0.01  # real seconds each DELETE holds its thread, so the fan-out's DELETEs overlap


class _Clock:
    """Virtual time that only sleeps and DELETEs move; neither real time nor thread scheduling does.

    A sleep returns at once and adds its seconds; a DELETE takes ``_RTT``. The thread that made
    the clock (the test's, which also runs the paced phase) reads shared time, and the bucket
    refills on it. Shared time moves by every sleep, by ``_RTT`` per DELETE from the test's
    thread, and by ``_RTT / _DELETE_FANOUT`` per fan-out DELETE: the fan-out runs
    ``_DELETE_FANOUT`` DELETEs per round trip whichever workers the host lets send them.

    Any other thread is a fan-out worker with its own time: it starts where the test's thread
    last read the clock, just before the pool, and moves by ``_RTT`` per DELETE of its own. So
    every fan-out round trip the backend measures is exactly ``_RTT``, and so is the pacing
    deadline it derives from them.
    """

    def __init__(self) -> None:
        self._now = self._read = 0.0
        self._owner = threading.get_ident()
        self._worker = threading.local()
        self.sleeps: list[float] = []
        self._lock = threading.Lock()

    def monotonic(self) -> float:
        if threading.get_ident() == self._owner:
            self._read = self._now
            return self._now
        if not hasattr(self._worker, "now"):
            self._worker.now = self._read
        return self._worker.now

    def sleep(self, seconds: float) -> None:
        assert threading.get_ident() == self._owner, "only the paced phase sleeps, on the test's thread"
        with self._lock:
            self.sleeps.append(seconds)
            self._now += seconds

    def round_trip(self) -> float:
        """End this thread's DELETE; returns shared time, for the bucket."""
        with self._lock:
            if threading.get_ident() == self._owner:
                self._now += _RTT
            else:
                self._worker.now = self.monotonic() + _RTT
                self._now += _RTT / _DELETE_FANOUT
            return self._now


class _Limited:
    """A cache API behind a token bucket: ``tokens`` now, refilling at ``per_minute``, capped at ``burst``.

    An admitted DELETE takes one token; a denied one costs nothing and answers 429 with the
    whole seconds until the next token, or with no Retry-After at all when ``quota`` is set.
    GETs miss and PUTs succeed, unlimited, so the decorator can write the keys first.
    """

    def __init__(self, clock: _Clock, tokens: float, per_minute: float, burst: float, quota: bool = False) -> None:
        self.clock, self.tokens, self.rate, self.burst, self.quota = clock, tokens, per_minute / 60, burst, quota
        self.deleted: list[str] = []
        self.sent: list[str] = []
        self.first_429: float | None = None  # real time the first rate-limited answer went out
        self.late_peak = 0  # peak DELETEs in flight among those started after it
        self._late_in_flight = 0
        self._last = clock.monotonic()
        self._lock = threading.Lock()

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(404)
        if request.method == "PUT":
            return httpx.Response(200)
        key = unquote(request.url.raw_path.decode().removeprefix("/v1/cache/"))
        with self._lock:
            self.sent.append(key)
            late = self.first_429 is not None and time.monotonic() > self.first_429 + _REAL_RTT / 2
            if late:
                self._late_in_flight += 1
                self.late_peak = max(self.late_peak, self._late_in_flight)
        try:
            time.sleep(_REAL_RTT)
            with self._lock:
                now = self.clock.round_trip()
                self.tokens = min(self.burst, self.tokens + (now - self._last) * self.rate)
                self._last = now
                if self.tokens >= 1:
                    self.tokens -= 1
                    self.deleted.append(key)
                    return httpx.Response(200)
                if self.quota:
                    return httpx.Response(429, headers={"X-CacheKit-Deny-Reason": "quota"})
                if self.first_429 is None:
                    self.first_429 = time.monotonic()
                wait = math.ceil((1 - self.tokens) / self.rate)
                return httpx.Response(429, headers={"Retry-After": str(wait)})
        finally:
            if late:
                with self._lock:
                    self._late_in_flight -= 1


@pytest.fixture
def clock() -> Any:
    fake = _Clock()
    with patch("cachekit.backends.cachekitio.backend.time", SimpleNamespace(monotonic=fake.monotonic, sleep=fake.sleep)):
        yield fake


def test_cobels_case_waits_out_the_rate_limit_and_deletes_every_key(clock: _Clock) -> None:
    """Free tier, 90 tokens left, 100/min: the unpaced fan-out left about 9 of 100 keys live."""
    server = _Limited(clock, tokens=90, per_minute=100, burst=200)
    backend = _backend(server)
    keys = [f"k{i}" for i in range(100)]

    assert backend._delete_many(keys) == set()

    assert sorted(server.deleted) == sorted(keys)
    assert clock.sleeps  # the rate limit was hit and waited out
    assert server.late_peak <= 1  # no concurrent DELETE after the first rate-limited answer


def test_cobels_case_through_the_decorator_leaves_nothing_tracked(clock: _Clock) -> None:
    server = _Limited(clock, tokens=90, per_minute=100, burst=200)

    @cache(backend=_backend(server), ttl=60, namespace="fanout_paced", l1_enabled=False)
    def f(x: int) -> int:
        return x

    for i in range(100):
        f(i)

    f.invalidate_cache()

    assert len(server.deleted) == 100 and clock.sleeps
    assert _cached_keys(f) == set()


def test_startup_shaped_bucket_deletes_every_key(clock: _Clock) -> None:
    """A burst well under N with a refill faster than the serial rate (5/s): every key goes."""
    server = _Limited(clock, tokens=20, per_minute=1000, burst=20)
    keys = [f"k{i}" for i in range(80)]

    assert _backend(server)._delete_many(keys) == set()

    assert sorted(server.deleted) == sorted(keys)
    assert server.late_peak <= 1


def test_spent_budget_stops_at_the_deadline_and_returns_exactly_the_rest(
    clock: _Clock, caplog: pytest.LogCaptureFixture
) -> None:
    server = _Limited(clock, tokens=0, per_minute=100, burst=200)
    keys = [f"k{i}" for i in range(50)]
    start = clock.monotonic()

    with caplog.at_level("WARNING"):
        failed = _backend(server)._delete_many(keys)

    assert failed == set(keys) - set(server.deleted)
    assert 0 < len(server.deleted) < len(keys)
    # The deadline is about the serial loop's time (50 x 200 ms), not a wait for every key.
    assert clock.monotonic() - start < len(keys) * _RTT * 2
    unsent = [key for key in keys if key in failed and key not in server.sent]
    assert unsent  # keys past the deadline are failed without being sent
    assert any("rate limit" in record.getMessage() for record in caplog.records)


def test_quota_deny_sends_each_key_once_and_never_sleeps(clock: _Clock) -> None:
    server = _Limited(clock, tokens=0, per_minute=0.001, burst=200, quota=True)
    keys = [f"k{i}" for i in range(40)]

    assert _backend(server)._delete_many(keys) == set(keys)

    assert sorted(server.sent) == sorted(keys)
    assert clock.sleeps == []
