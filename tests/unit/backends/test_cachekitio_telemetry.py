"""L1 hit-latency telemetry and the telemetry switch (protocol spec/saas-api.md, Optional Metrics Headers).

Each test names the requirement it checks. Requests go through the backend's real client on a fake pool
(tests/utils/cachekitio_fakes.py); the wrapper's draw and clock are replaced, so durations are exact.
"""

from __future__ import annotations

import asyncio
import os
import threading
import time
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cachekit import cache
from cachekit.backends.cachekitio.backend import CachekitIOBackend, _inject_metrics_headers
from cachekit.backends.cachekitio.config import CachekitIOBackendConfig, resolve_telemetry
from cachekit.backends.errors import BackendError
from cachekit.decorators import l1_latency, wrapper
from cachekit.decorators.l1_latency import (
    BUCKETS_HEADER,
    MAX_DURATION_NS,
    ONE_IN,
    ONE_IN_HEADER,
    SUM_HEADER,
    L1LatencyReport,
)
from cachekit.decorators.wrapper import _FunctionStats, _latency_report_of
from tests.utils.cachekitio_fakes import TEST_API_KEY, FakePool, FakeRequest, fake_backend, response
from tests.utils.fork_helpers import child_outcome
from tests.utils.fork_helpers import report as report_to_parent

SESSION_AND_COUNTERS = (
    "X-CacheKit-Session-ID",
    "X-CacheKit-Session-Start",
    "X-CacheKit-L1-Hits",
    "X-CacheKit-L2-Hits",
    "X-CacheKit-Misses",
    "X-CacheKit-L1-Hit-Rate",
)
HEADERS = (BUCKETS_HEADER, SUM_HEADER, ONE_IN_HEADER)
OPTIONAL = (*SESSION_AND_COUNTERS, *HEADERS)


class _Store:
    """A SaaS cache in a dict: GET/PUT/HEAD/DELETE on /v1/cache/<key>, and the health endpoint."""

    def __init__(self) -> None:
        self.data: dict[str, bytes] = {}

    def __call__(self, request: FakeRequest):
        if request.path == "/v1/cache/health":
            return response(200, json={"status": "ok"})
        key = request.path.removeprefix("/v1/cache/")
        if request.method == "PUT":
            assert request.body is not None
            self.data[key] = request.body
            return response(200, json={"success": True})
        if request.method == "DELETE":
            return response(200, json={"deleted": self.data.pop(key, None) is not None})
        if key not in self.data:
            return response(404)
        return response(200, self.data[key] if request.method == "GET" else b"")


def _ns() -> str:
    """A namespace no other test shares: the L1 cache is process-wide."""
    return f"telemetry-{uuid.uuid4().hex}"


class _Clock:
    """perf_counter_ns that advances ``step`` ns on each read."""

    def __init__(self, step: int) -> None:
        self.now = 1_000_000
        self.step = step

    def __call__(self) -> int:
        self.now += self.step
        return self.now


@pytest.fixture(autouse=True)
def _no_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CACHEKIT_TELEMETRY", raising=False)


@pytest.fixture
def store() -> _Store:
    return _Store()


@pytest.fixture
def timed(monkeypatch: pytest.MonkeyPatch) -> _Clock:
    """Every call is drawn for timing, and each one takes 300 ns as the clock sees it."""
    clock = _Clock(300)
    monkeypatch.setattr(wrapper, "_getrandbits", lambda k: 0)
    monkeypatch.setattr(wrapper, "_perf_counter_ns", clock)
    return clock


def _backend(store: _Store, **kwargs: object) -> tuple[CachekitIOBackend, FakePool]:
    return fake_backend(store, **kwargs)


def _latency(request: FakeRequest) -> dict[str, str]:
    return {name: request.headers[name] for name in HEADERS if name in request.headers}


def _sent(pool: FakePool) -> list[dict[str, str]]:
    return [_latency(request) for request in pool.requests]


ONE_HIT_300 = {
    "X-CacheKit-L1-Latency-Buckets": "0,0,1,0,0,0,0,0,0,0,0,0,0,0,0,0",
    "X-CacheKit-L1-Latency-Sum-Ns": "300",
    "X-CacheKit-L1-Latency-One-In": str(ONE_IN),
}


class TestWhatIsTimed:
    def test_api79_sync_l1_hit_rides_the_next_request(self, store: _Store, timed: _Clock) -> None:
        backend, pool = _backend(store)

        @cache(backend=backend, ttl=60, namespace=_ns())
        def f(x: int) -> int:
            return x * 2

        assert f(1) == 2  # miss: GET, PUT
        assert f(1) == 2  # L1 hit, timed: 300 ns
        assert _sent(pool) == [{}, {}]
        assert f(2) == 4  # miss: its GET carries the report, its PUT nothing
        assert _sent(pool)[2:] == [ONE_HIT_300, {}]

    async def test_api79_async_l1_hit_rides_the_next_request(self, store: _Store, timed: _Clock) -> None:
        backend, pool = _backend(store)

        @cache(backend=backend, ttl=60, namespace=_ns())
        async def f(x: int) -> int:
            return x * 2

        assert await f(1) == 2
        coro = f(1)
        timed.now += 10_000  # creating the coroutine starts no clock: its first step does
        assert await coro == 2
        before = len(pool.requests)
        assert await f(2) == 4  # a miss: its first request carries the report, nothing else does
        assert [sent for sent in _sent(pool) if sent] == [ONE_HIT_300]
        assert _sent(pool)[before] == ONE_HIT_300

    def test_api79_the_clock_spans_key_building(self, store: _Store, monkeypatch: pytest.MonkeyPatch) -> None:
        """The first clock read comes before the call builds its key, the last after the hit is served: the whole
        call as the caller sees it, not only the L1 lookup."""
        events: list[str] = []
        monkeypatch.setattr(wrapper, "_getrandbits", lambda k: 0)
        monkeypatch.setattr(wrapper, "_perf_counter_ns", lambda: events.append("clock") or 1_000 * len(events))
        backend, _ = _backend(store)

        def key(x: int) -> str:
            events.append("key")
            return f"telemetry-clock-{x}"

        @cache(backend=backend, ttl=60, namespace=_ns(), key=key)
        def f(x: int) -> int:
            return x

        f(1)
        events.clear()
        assert f(1) == 1  # hit
        assert events == ["clock", "key", "clock"]

    @pytest.mark.parametrize("mode", ["sync", "async"])
    def test_api79_the_wait_for_the_reports_lock_is_in_the_duration(
        self, mode: str, store: _Store, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The caller pays for a contended report lock before it gets its value, so that wait is part of the hit."""
        monkeypatch.setattr(wrapper, "_getrandbits", lambda k: 0)
        backend, _ = _backend(store)
        report = backend.l1_latency_report
        assert report is not None

        if mode == "sync":

            @cache(backend=backend, ttl=60, namespace=_ns())
            def f(x: int) -> int:
                return x

            call = lambda: f(1)  # noqa: E731
        else:

            @cache(backend=backend, ttl=60, namespace=_ns())
            async def g(x: int) -> int:
                return x

            call = lambda: asyncio.run(g(1))  # noqa: E731

        call()  # miss
        report.take()
        hold_ns = 50_000_000
        state = report._state
        state.lock.acquire()  # another thread mid-record
        releaser = threading.Timer(hold_ns / 1e9, state.lock.release)
        releaser.start()
        try:
            call()  # hit, blocked on the lock
        finally:
            releaser.join()
        taken = report.take()
        # The hold starts before the call (asyncio.run's setup eats into it), so not all of it is waited; without
        # the wait in the interval the hit reads well under a millisecond.
        assert taken is not None and int(taken["X-CacheKit-L1-Latency-Sum-Ns"]) >= hold_ns // 2

    def test_api80_a_miss_and_an_l2_hit_are_never_timed(self, store: _Store, timed: _Clock) -> None:
        backend, pool = _backend(store)

        @cache(backend=backend, ttl=60, l1_enabled=False, namespace=_ns())
        def f(x: int) -> int:
            return x

        f(1)  # miss
        f(1)  # L2 hit: this wrapper keeps no L1
        assert all(not sent for sent in _sent(pool))
        assert backend.l1_latency_report is not None and backend.l1_latency_report.take() is None

    def test_api80_a_joined_call_is_not_timed(self, store: _Store, timed: _Clock) -> None:
        """A caller that joins another's in-flight miss counts an L1 hit, but its duration is the other call's."""
        backend, _pool = _backend(store)
        started, release = threading.Event(), threading.Event()

        @cache(backend=backend, ttl=60, namespace=_ns())
        def f(x: int) -> int:
            started.set()
            release.wait(5)
            return x

        results: list[int] = []
        first = threading.Thread(target=lambda: results.append(f(1)))
        first.start()
        assert started.wait(5)
        second = threading.Thread(target=lambda: results.append(f(1)))
        second.start()
        time.sleep(0.2)  # the second caller is parked on the first's flight
        release.set()
        first.join(5)
        second.join(5)
        assert results == [1, 1]
        assert backend.l1_latency_report is not None
        assert backend.l1_latency_report.take() is None

    def test_api80_existence_checks_and_reads_inside_a_call_are_not_timed(self, store: _Store, timed: _Clock) -> None:
        """Backend reads made inside a wrapped call, under its stats context, time nothing: only the call itself is."""
        backend, pool = _backend(store)
        backend.set("k", b"v")

        @cache(backend=backend, ttl=60, namespace=_ns())
        def f(x: int) -> bytes | None:
            assert backend.exists("k")
            return backend.get("k")

        before = len(pool.requests)
        assert f(1) == b"v"  # a miss: its function's exists and get run inside the call
        inner = [r for r in pool.requests[before:] if r.path == "/v1/cache/k"]
        assert [r.method for r in inner] == ["HEAD", "GET"]
        assert all("X-CacheKit-L1-Hits" in r.headers for r in inner)  # they ran under the call's context
        assert backend.l1_latency_report is not None and backend.l1_latency_report.take() is None

    def test_api79_a_backend_first_resolved_by_invalidation_still_times_hits(self, store: _Store, timed: _Clock) -> None:
        """invalidate_cache can resolve a lazy backend before any call does; an L1 entry another wrapper of the same
        function wrote is then a hit before this wrapper ever installs a handler."""
        from cachekit.config.decorator import set_default_backend

        backend, pool = _backend(store)
        ns = _ns()

        def f(x: int) -> int:
            return x

        explicit = cache(backend=backend, ttl=60, namespace=ns)(f)
        explicit(1)  # miss: writes L1
        lazy = cache(ttl=60, namespace=ns)(f)  # no default backend yet: resolved at first use
        set_default_backend(backend)
        try:
            lazy.invalidate_cache(2)  # resolves the default backend, installs no handler
            assert lazy(1) == 1  # an L1 hit on the entry explicit wrote
        finally:
            set_default_backend(None)
        assert backend.l1_latency_report is not None
        taken = backend.l1_latency_report.take()
        assert taken is not None and taken[SUM_HEADER] == "300"

    def test_api77_only_a_cachekitio_backend_gets_a_report(self, store: _Store, timed: _Clock) -> None:
        """A Mock answers any attribute; a backend of another kind has none: neither is timed into."""
        backend, _ = _backend(store)
        assert _latency_report_of(backend) is backend.l1_latency_report
        assert _latency_report_of(MagicMock()) is None
        assert _latency_report_of(object()) is None

    def test_api77_hits_ride_only_their_own_backends_requests(self, store: _Store, timed: _Clock) -> None:
        a, pool_a = _backend(store)
        b, pool_b = _backend(store)

        @cache(backend=a, ttl=60, namespace=_ns())
        def f(x: int) -> int:
            return x

        f(1)
        f(1)  # hit on a
        b.get("anything")
        assert _sent(pool_b) == [{}]
        a.get("anything")
        assert _sent(pool_a)[-1] == ONE_HIT_300


class TestSampling:
    @pytest.mark.parametrize("backed", [False, True])
    def test_api81_the_draw_leaves_the_users_random_stream_alone(
        self, backed: bool, store: _Store, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Seeded user code must produce the same numbers with cached calls in between, and a worker that re-seeds
        `random` per job must not time the same call positions in every job. cachekit's log sampling draws from
        `random` below WARNING, which is not the draw under test, so its logger is pinned there."""
        import logging
        import random

        monkeypatch.setattr(logging.getLogger("cachekit"), "level", logging.WARNING)
        backend = _backend(store)[0] if backed else None

        @cache(backend=backend, ttl=60, namespace=_ns())
        def f(x: int) -> int:
            return x

        random.seed(1234)
        before = random.getstate()
        for i in range(3):
            f(i)
            f(i)
        assert random.getstate() == before

    def test_api81_a_child_forked_from_c_reseeds_at_its_first_report_use(self) -> None:
        """A fork made from C runs no at-fork hook (uWSGI): the child replays its parent's draws until its first
        report use (a request or a timed hit) reseeds them. Two children of one parent state then diverge."""
        import ctypes

        libc_fork = ctypes.PyDLL(None).fork
        report = L1LatencyReport()

        def child() -> list[int]:
            r, w = os.pipe()
            pid = libc_fork()
            if pid == 0:  # pragma: no cover - child
                os.close(r)
                try:
                    replayed = [wrapper._getrandbits(32) for _ in range(4)]  # still the parent's sequence
                    report.take()  # the child's first request
                    outcome: object = (replayed, [wrapper._getrandbits(32) for _ in range(4)])
                except BaseException as exc:  # never return into pytest from the child
                    outcome = f"child raised {exc!r}"
                report_to_parent(w, outcome)
            os.close(w)
            return child_outcome(pid, r)  # type: ignore[return-value]

        state = l1_latency.sampling_rng.getstate()
        first = child()
        l1_latency.sampling_rng.setstate(state)  # a prefork master forks every worker from one state
        second = child()
        l1_latency.sampling_rng.setstate(state)
        parent = [wrapper._getrandbits(32) for _ in range(4)]
        assert first[0] == second[0] == parent  # before the first report use: replayed, as documented
        assert first[1] != second[1]  # after it: each child draws its own sequence

    def test_api81_a_report_built_in_a_child_forked_from_c_reseeds(self) -> None:
        """A worker that builds its backend after a fork made from C never takes over an inherited state, so its
        report's construction must reseed the draw itself. Two such children of one parent state diverge at once."""
        import ctypes

        libc_fork = ctypes.PyDLL(None).fork

        def child() -> list[int]:
            r, w = os.pipe()
            pid = libc_fork()
            if pid == 0:  # pragma: no cover - child
                os.close(r)
                try:
                    L1LatencyReport()  # the worker builds its backend, and so its report
                    outcome: object = [wrapper._getrandbits(32) for _ in range(4)]
                except BaseException as exc:  # never return into pytest from the child
                    outcome = f"child raised {exc!r}"
                report_to_parent(w, outcome)
            os.close(w)
            return child_outcome(pid, r)  # type: ignore[return-value]

        state = l1_latency.sampling_rng.getstate()
        first = child()
        l1_latency.sampling_rng.setstate(state)
        second = child()
        l1_latency.sampling_rng.setstate(state)
        parent = [wrapper._getrandbits(32) for _ in range(4)]
        assert isinstance(first, list) and isinstance(second, list)
        assert first != second and first != parent and second != parent

    def test_api81_a_forked_child_draws_its_own_sequence(self) -> None:
        r, w = os.pipe()
        pid = os.fork()
        if pid == 0:  # pragma: no cover - child
            os.close(r)
            try:
                outcome: object = [wrapper._getrandbits(32) for _ in range(4)]
            except BaseException as exc:  # never return into pytest from the child
                outcome = f"child raised {exc!r}"
            report_to_parent(w, outcome)
        os.close(w)
        child = child_outcome(pid, r)
        parent = [wrapper._getrandbits(32) for _ in range(4)]
        assert isinstance(child, list) and child != parent

    def test_api81_the_draw_is_made_on_every_backed_call_before_its_outcome(
        self, store: _Store, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        draws: list[int] = []
        monkeypatch.setattr(wrapper, "_getrandbits", lambda k: draws.append(k) or 1)
        backend, pool = _backend(store)

        @cache(backend=backend, ttl=60, namespace=_ns())
        def f(x: int) -> int:
            return x

        f(1)  # miss
        f(1)  # hit
        f(2)  # miss
        assert len(draws) == 3
        assert set(draws) == {6}  # 2**-6 = 1/64
        assert all(not sent for sent in _sent(pool))  # a non-zero draw times nothing

    def test_api81_probability_is_one_in_n_and_n_is_sent(self) -> None:
        assert 1 <= ONE_IN <= 1_000_000
        report = L1LatencyReport()
        report.add(1)
        taken = report.take()
        assert taken is not None and taken["X-CacheKit-L1-Latency-One-In"] == str(ONE_IN)

    def test_api81_a_real_draw_times_about_one_call_in_n(self, store: _Store) -> None:
        backend, _ = _backend(store)

        @cache(backend=backend, ttl=60, namespace=_ns())
        def f(x: int) -> int:
            return x

        f(1)
        calls = 1_000 * ONE_IN
        for _ in range(calls):
            f(1)
        report = backend.l1_latency_report
        assert report is not None
        taken = report.take()
        assert taken is not None
        timed = sum(map(int, taken["X-CacheKit-L1-Latency-Buckets"].split(",")))
        # Binomial(1000 N, 1/N): mean 1000, sd about 31; 6 sd either way.
        assert 800 < timed < 1200


class TestClock:
    def test_api82_no_report_without_a_qualifying_clock(self, store: _Store) -> None:
        coarse = MagicMock(return_value=MagicMock(resolution=1e-6))
        with patch.object(l1_latency.time, "get_clock_info", coarse):
            backend, _ = _backend(store)
        assert backend.l1_latency_report is None


class TestReports:
    def test_api83_a_forked_child_starts_empty(self) -> None:
        """No at-fork hook is involved, as under uWSGI: the child's first use sees the inherited state's PID."""
        report = L1LatencyReport()
        report.add(500)
        r, w = os.pipe()
        pid = os.fork()
        if pid == 0:  # pragma: no cover - child
            os.close(r)
            try:
                inherited = report._state.pid != os.getpid()
                first = report.take()
                report.add(700)
                outcome: object = (inherited, first, report.take())
            except BaseException as exc:  # never return into pytest from the child
                outcome = f"child raised {exc!r}"
            report_to_parent(w, outcome)
        os.close(w)
        inherited, first, second = child_outcome(pid, r)  # type: ignore[misc]
        assert inherited is True  # the child still held the parent's state until its first use
        assert first is None
        assert second == {BUCKETS_HEADER: "0,0,0,1,0,0,0,0,0,0,0,0,0,0,0,0", SUM_HEADER: "700", ONE_IN_HEADER: str(ONE_IN)}
        taken = report.take()  # the parent's hit is still the parent's
        assert taken is not None and taken[SUM_HEADER] == "500"

    def test_api83_a_lock_held_at_the_fork_does_not_hang_the_child(self, monkeypatch: pytest.MonkeyPatch) -> None:
        report = L1LatencyReport()
        report._state.lock.acquire()  # a parent thread mid-add when the process forked
        monkeypatch.setattr(l1_latency.os, "getpid", lambda: -1)
        report.add(200)
        assert report.take() is not None

    def test_api83_racing_first_uses_in_a_hookless_child_keep_every_hit(self) -> None:
        """Child threads that reach the inherited state at once agree on one new state: the parent's hit is never
        sent, and no child hit is lost to another thread's replacement. _State construction is slowed so that every
        thread is inside the replacement at once, the schedule that lost hits when each built its own."""
        report = L1LatencyReport()
        report.add(12_345)
        threads_n, hit_ns = 8, 700
        real_init = l1_latency._State.__init__

        def slow_init(state: object) -> None:
            real_init(state)  # type: ignore[arg-type]
            time.sleep(0.02)

        r, w = os.pipe()
        pid = os.fork()
        if pid == 0:  # pragma: no cover - child
            os.close(r)
            try:
                l1_latency._State.__init__ = slow_init  # type: ignore[method-assign]
                barrier = threading.Barrier(threads_n)

                def first_use() -> None:
                    barrier.wait()
                    report.add(hit_ns)

                workers = [threading.Thread(target=first_use) for _ in range(threads_n)]
                for t in workers:
                    t.start()
                for t in workers:
                    t.join()
                outcome: object = report.take()
            except BaseException as exc:  # never return into pytest from the child
                outcome = f"child raised {exc!r}"
            report_to_parent(w, outcome)
        os.close(w)
        taken = child_outcome(pid, r)
        assert taken == {
            BUCKETS_HEADER: f"0,0,0,{threads_n},0,0,0,0,0,0,0,0,0,0,0,0",
            SUM_HEADER: str(threads_n * hit_ns),
            ONE_IN_HEADER: str(ONE_IN),
        }
        parent = report.take()
        assert parent is not None and parent[SUM_HEADER] == "12345"

    def test_a_zero_sum_report_is_not_attached_and_stays(self) -> None:
        report = L1LatencyReport()
        report.add(0)
        assert report.take() is None
        report.add(5)
        assert report.take() == {
            "X-CacheKit-L1-Latency-Buckets": "2,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0",
            "X-CacheKit-L1-Latency-Sum-Ns": "5",
            "X-CacheKit-L1-Latency-One-In": str(ONE_IN),
        }

    def test_api84_never_on_the_health_endpoint(self, store: _Store) -> None:
        backend, pool = _backend(store)
        assert backend.l1_latency_report is not None
        backend.l1_latency_report.add(300)
        assert backend.health_check()[0]
        assert _sent(pool) == [{}]
        backend.get("k")
        assert _latency(pool.requests[-1])["X-CacheKit-L1-Latency-Sum-Ns"] == "300"

    async def test_api84_never_on_the_async_health_endpoint(self, store: _Store) -> None:
        backend, pool = _backend(store)
        assert backend.l1_latency_report is not None
        backend.l1_latency_report.add(300)
        assert (await backend.health_check_async())[0]
        assert _sent(pool) == [{}]

    def test_api85_concurrent_adds_and_takes_lose_and_repeat_nothing(self) -> None:
        report = L1LatencyReport()
        taken: list[dict[str, str]] = []
        threads, per_thread = 8, 2_000
        done = threading.Event()

        def adder() -> None:
            for _ in range(per_thread):
                report.add(300)

        def taker() -> None:
            while not done.is_set():
                if (t := report.take()) is not None:
                    taken.append(t)

        reader = threading.Thread(target=taker)
        reader.start()
        workers = [threading.Thread(target=adder) for _ in range(threads)]
        for w in workers:
            w.start()
        for w in workers:
            w.join()
        done.set()
        reader.join()
        if (t := report.take()) is not None:
            taken.append(t)
        hits = [sum(map(int, t["X-CacheKit-L1-Latency-Buckets"].split(","))) for t in taken]
        assert sum(hits) == threads * per_thread
        # Every report's buckets and sum describe the same hits.
        assert all(int(t["X-CacheKit-L1-Latency-Sum-Ns"]) == 300 * n for t, n in zip(taken, hits, strict=True))

    @pytest.mark.parametrize("mode", ["sync", "async"])
    def test_api86_the_shed_write_retry_does_not_resend_the_report(self, mode: str) -> None:
        answers = [response(503, headers={"Retry-After": "1"}), response(200, json={"success": True})]
        backend, pool = fake_backend(lambda request: answers.pop(0))
        assert backend.l1_latency_report is not None
        backend.l1_latency_report.add(300)
        with patch.object(time, "sleep"), patch.object(asyncio, "sleep", AsyncMock()):
            if mode == "sync":
                backend.set("k", b"v", ttl=60)
            else:
                asyncio.run(backend.set_async("k", b"v", ttl=60))
        first, retry = _sent(pool)
        assert first["X-CacheKit-L1-Latency-Sum-Ns"] == "300"
        assert retry == {}
        assert backend.l1_latency_report.take() is None  # consumed, not added back

    def test_api86_a_failed_request_consumes_its_report(self) -> None:
        backend, pool = fake_backend(lambda request: response(500))
        assert backend.l1_latency_report is not None
        backend.l1_latency_report.add(300)
        with pytest.raises(BackendError):
            backend.get("k")
        assert _sent(pool)[0]["X-CacheKit-L1-Latency-Sum-Ns"] == "300"
        assert backend.l1_latency_report.take() is None

    def test_api86_the_client_makes_no_retry_of_its_own(self, store: _Store) -> None:
        backend, pool = _backend(store)
        backend.get("k")
        assert pool.requests[0].options.get("retries") is False


class TestEncoding:
    @pytest.mark.parametrize(
        ("duration", "index"),
        [(0, 0), (128, 0), (129, 1), (256, 1), (257, 2), (2_097_152, 14), (2_097_153, 15), (MAX_DURATION_NS, 15)],
    )
    def test_api87_bucket_bounds(self, duration: int, index: int) -> None:
        assert l1_latency.bucket_index(duration) == index

    def test_api87_a_duration_above_60s_is_discarded(self) -> None:
        report = L1LatencyReport()
        report.add(MAX_DURATION_NS + 1)
        assert report.take() is None
        report.add(MAX_DURATION_NS)
        taken = report.take()
        assert taken is not None and taken["X-CacheKit-L1-Latency-Buckets"].endswith(",1")

    def test_api87_the_sum_never_passes_2_pow_53(self) -> None:
        report = L1LatencyReport()
        report._state.sum = 2**53 - 10
        report.add(100)  # would pass the limit: dropped
        report.add(9)
        taken = report.take()
        assert taken is not None and taken["X-CacheKit-L1-Latency-Sum-Ns"] == str(2**53 - 1)


def _stats(l1: int, l2: int, misses: int) -> _FunctionStats:
    stats = _FunctionStats(function_identifier="t.f", l1_enabled=True)
    for _ in range(l1):
        stats.record_l1_hit()
    for _ in range(l2):
        stats.record_l2_hit(1.0)
    for _ in range(misses):
        stats.record_miss()
    return stats


class TestHitRate:
    @pytest.mark.parametrize(
        ("l1", "l2", "misses", "rate"),
        [(0, 0, 0, "0.000"), (1, 1, 2, "0.250"), (2, 1, 0, "0.667"), (3, 0, 0, "1.000"), (0, 0, 5, "0.000"), (1, 0, 2, "0.333")],
    )
    def test_api78_share_of_all_counted_operations(self, l1: int, l2: int, misses: int, rate: str) -> None:
        assert _inject_metrics_headers(_stats(l1, l2, misses))["X-CacheKit-L1-Hit-Rate"] == rate


ON, OFF, UNSET = "on", "off", "unset"
_SETTING = {ON: True, OFF: False, UNSET: None}
_ENV = {ON: "true", OFF: "false", UNSET: None}
# spec/saas-api.md#telemetry-switch, row = setting, column = environment
_TABLE = {
    (UNSET, UNSET): True,
    (UNSET, ON): True,
    (UNSET, OFF): False,
    (ON, UNSET): True,
    (ON, ON): True,
    (ON, OFF): False,
    (OFF, UNSET): False,
    (OFF, ON): False,
    (OFF, OFF): False,
}


class TestSwitch:
    @pytest.mark.parametrize(("setting", "env"), list(_TABLE))
    def test_api94_95_truth_table(self, setting: str, env: str, store: _Store, monkeypatch: pytest.MonkeyPatch) -> None:
        if _ENV[env] is not None:
            monkeypatch.setenv("CACHEKIT_TELEMETRY", _ENV[env])
        backend, pool = _backend(store, telemetry=_SETTING[setting])
        on = _TABLE[setting, env]
        assert (backend.l1_latency_report is not None) is on

        @cache(backend=backend, ttl=60, namespace=_ns())
        def f(x: int) -> int:
            return x

        with patch.object(wrapper, "_getrandbits", lambda k: 0):
            f(1)
            f(1)
            f(2)
        last = pool.requests[2].headers  # f(2)'s GET: inside a call, after a hit
        if on:
            assert all(name in last for name in OPTIONAL)
        else:
            assert not any(name in last for name in OPTIONAL)  # API-96
        assert "X-CacheKit-L1-Status" in last
        assert "User-Agent" in last or "user-agent" in {k.lower() for k in last}

    @pytest.mark.parametrize(
        ("raw", "parsed"),
        [
            ("false", False),
            ("0", False),
            ("off", False),
            ("no", False),
            (" OFF ", False),
            ("True", True),
            ("1", True),
            ("on", True),
            ("YES\n", True),
            ("", None),
            ("   ", None),
        ],
    )
    def test_api95_environment_values(self, raw: str, parsed: bool | None) -> None:
        from cachekit.backends.cachekitio.config import _parse_telemetry_env

        assert _parse_telemetry_env(raw) is parsed

    def test_api95_an_unknown_value_is_off_and_warns_once(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        from cachekit.backends.cachekitio import config

        monkeypatch.setattr(config, "_warned_bad_telemetry", False)
        assert resolve_telemetry(True, {"CACHEKIT_TELEMETRY": "maybe"}) is False
        assert resolve_telemetry(None, {"CACHEKIT_TELEMETRY": "nope"}) is False
        warnings = [r for r in caplog.records if "CACHEKIT_TELEMETRY" in r.getMessage()]
        assert len(warnings) == 1
        assert "maybe" not in warnings[0].getMessage()

    def test_api95_no_loader_fills_the_setting_from_the_environment(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CACHEKIT_API_KEY", TEST_API_KEY)
        monkeypatch.setenv("CACHEKIT_TELEMETRY", "false")
        assert CachekitIOBackendConfig().telemetry is None
        assert CachekitIOBackendConfig(telemetry=True).telemetry is True
        # and the argument does not override the environment's off
        backend, _ = fake_backend(_Store(), telemetry=True)
        assert backend._telemetry is False

    def test_api95_read_once_at_construction(self, store: _Store, monkeypatch: pytest.MonkeyPatch) -> None:
        backend, pool = _backend(store)
        monkeypatch.setenv("CACHEKIT_TELEMETRY", "off")

        @cache(backend=backend, ttl=60, namespace=_ns())
        def f(x: int) -> int:
            return x

        f(1)
        assert "X-CacheKit-Session-ID" in pool.requests[0].headers
        off, off_pool = _backend(store)
        monkeypatch.delenv("CACHEKIT_TELEMETRY")

        @cache(backend=off, ttl=60, namespace=_ns())
        def g(x: int) -> int:
            return x

        g(1)
        assert "X-CacheKit-Session-ID" not in off_pool.requests[0].headers

    def test_api95_a_clone_of_a_backend_the_environment_turned_off_stays_off(
        self, store: _Store, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CACHEKIT_TELEMETRY", "off")
        backend, _ = _backend(store)
        monkeypatch.delenv("CACHEKIT_TELEMETRY")
        assert backend.with_timeout(2.0)._telemetry is False

    @pytest.mark.parametrize("name", ["cachekit_telemetry", "Cachekit_Telemetry"])
    def test_api95_the_variable_name_matches_in_any_case(
        self, name: str, store: _Store, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """As every other CACHEKIT_* variable loads (case_sensitive=False): a lowercase off must not fail open."""
        monkeypatch.setenv(name, "false")
        backend, pool = _backend(store, telemetry=True)
        assert backend.l1_latency_report is None
        backend.get("k")
        assert not any(header in pool.requests[0].headers for header in OPTIONAL)

    def test_api95_a_lowercase_on_is_on(self) -> None:
        assert resolve_telemetry(None, {"cachekit_telemetry": "yes"}) is True

    def test_api96_off_accumulates_no_report(self, store: _Store, timed: _Clock) -> None:
        backend, pool = _backend(store, telemetry=False)
        assert backend.l1_latency_report is None

        @cache(backend=backend, ttl=60, namespace=_ns())
        def f(x: int) -> int:
            return x

        f(1)
        f(1)
        f(2)
        assert all(not sent for sent in _sent(pool))
