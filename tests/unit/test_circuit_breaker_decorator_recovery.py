"""The circuit breaker recovers on the live ``@cache`` path (LAB-5326).

The breaker's own state machine was always tested on a bare ``CircuitBreaker``,
never through ``@cache`` / ``FeatureOrchestrator``. That hid defects that
together kept a breaker opened by the decorator from ever closing again:

1. ``FeatureOrchestrator.should_allow_request()`` read the state instead of
   asking for admission, so the OPEN -> HALF_OPEN timeout never ran.
2. Failures recorded while OPEN pushed the OPEN window forward: the sync
   wrapper's own fail-fast rejection, and failures from calls that fail before
   admission (key generation).
3. The default ``CircuitBreakerConfig`` admitted 1 HALF_OPEN probe but needed
   3 successes to close.
4. A HALF_OPEN cycle ended only on a recorded outcome, so probes that exit
   without one (cancellation, fail-closed raises, an interop rejection on the
   async lock path) held the breaker HALF_OPEN for good.

A rejected async call also escaped as ``UnboundLocalError``; it now runs the
function uncached, as a sync one does.

These tests drive a real decorated function on a frozen clock, as
``test_circuit_breaker_race_conditions.py`` does. The breaker is opened through
client-creation failures — the path that reaches ``record_failure`` today — and
captured by spying on the orchestrator's ``CircuitBreaker`` constructor.
"""

from __future__ import annotations

import asyncio
import inspect
import threading
from collections.abc import AsyncIterator, Awaitable, Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from datetime import timedelta
from typing import Any, Optional

import pytest
import time_machine

from cachekit import cache
from cachekit.backends.errors import BackendError, BackendErrorType
from cachekit.config.nested import CircuitBreakerConfig as NestedCircuitBreakerConfig
from cachekit.config.validation import ConfigurationError
from cachekit.decorators import wrapper as wrapper_module
from cachekit.decorators.orchestrator import FeatureOrchestrator
from cachekit.decorators.stats_context import get_current_function_stats
from cachekit.interop import InteropError
from cachekit.reliability.circuit_breaker import CircuitBreaker, CircuitBreakerConfig, CircuitState

_DEFAULTS = CircuitBreakerConfig()
_TIMEOUT = _DEFAULTS.timeout_seconds
_PAST_TIMEOUT = timedelta(seconds=_TIMEOUT + 1)
_EPOCH = 1_000_000.0  # Frozen wall clock; any fixed instant works


class _CountingBackend:
    """Healthy in-memory backend that counts the operations reaching it."""

    def __init__(self) -> None:
        self.store: dict[str, bytes] = {}
        self.gets = 0
        self.sets = 0
        self.key_prefix = ""  # Set to trip the interop fail-closed prefix guard

    def get(self, key: str) -> Optional[bytes]:
        self.gets += 1
        return self.store.get(key)

    def set(self, key: str, value: bytes, ttl: Optional[int] = None) -> None:
        self.sets += 1
        self.store[key] = bytes(value)

    def delete(self, key: str) -> bool:
        return self.store.pop(key, None) is not None


class _LockingBackend(_CountingBackend):
    """Adds ``acquire_lock`` so the async wrapper takes its stampede-lock path."""

    @asynccontextmanager
    async def acquire_lock(self, key: str, **_: Any) -> AsyncIterator[bool]:
        yield True


class _FlakyResolver:
    """Stands in for ``_resolve_lazy_backend``: raises while down, then returns the backend.

    Every raise is a client-creation failure, which the wrapper routes through
    ``handle_cache_error`` -> ``record_failure``.
    """

    def __init__(self, backend: _CountingBackend) -> None:
        self.backend = backend
        self.down = True
        self.calls = 0

    def __call__(self) -> _CountingBackend:
        self.calls += 1
        if self.down:
            raise ConnectionError("backend unreachable")
        return self.backend


@pytest.fixture
def backend() -> _CountingBackend:
    return _CountingBackend()


@pytest.fixture
def resolver(monkeypatch: pytest.MonkeyPatch, backend: _CountingBackend) -> _FlakyResolver:
    flaky = _FlakyResolver(backend)
    monkeypatch.setattr(wrapper_module, "_resolve_lazy_backend", flaky)
    return flaky


@pytest.fixture
def clock():
    with time_machine.travel(_EPOCH, tick=False) as traveller:
        yield traveller


@pytest.fixture(params=[False, True], ids=["sync", "async"])
def is_async(request: pytest.FixtureRequest) -> bool:
    return request.param


def _decorate(
    namespace: str,
    *,
    is_async: bool,
    executions: Optional[list[str]] = None,
    l1_enabled: bool = False,
    **cache_kwargs: Any,
):
    """``@cache`` a sync or async function returning ``v:<x>``, recording each execution."""
    runs = executions if executions is not None else []

    def body(x: str) -> str:
        runs.append(x)
        return f"v:{x}"

    async def async_body(x: str) -> str:
        return body(x)

    return cache(ttl=300, l1_enabled=l1_enabled, namespace=namespace, **cache_kwargs)(async_body if is_async else body)


async def _call(fn: Callable[[str], Any], x: str) -> Any:
    """Call a decorated function, awaiting it when it is async."""
    result = fn(x)
    return await result if inspect.isawaitable(result) else result


async def _open(fn: Callable[[str], Any], resolver: _FlakyResolver, breaker: CircuitBreaker) -> None:
    """Trip the breaker with client-creation failures, then bring the backend back."""
    for i in range(_DEFAULTS.failure_threshold):
        assert await _call(fn, f"trip-{i}") == f"v:trip-{i}"  # degrades to uncached, never raises
    assert breaker.state == CircuitState.OPEN
    resolver.down = False


def _trip(breaker: CircuitBreaker) -> None:
    """Open the breaker directly, for a decorator built with an explicit backend."""
    for _ in range(breaker.config.failure_threshold):
        breaker.record_failure()
    assert breaker.state == CircuitState.OPEN


async def _recovers(fn: Callable[[str], Any], backend: _CountingBackend, breaker: CircuitBreaker) -> None:
    """One cycle of cold-key probes reaches the backend and closes the breaker."""
    sets = backend.sets
    probes = [f"probe-{i}" for i in range(_DEFAULTS.success_threshold)]  # cold keys: misses reach L2
    for key in probes:
        assert await _call(fn, key) == f"v:{key}"
    assert backend.sets == sets + len(probes)
    assert breaker.state == CircuitState.CLOSED


class TestDefaultConfig:
    def test_default_probe_budget_can_reach_success_threshold(self):
        """A HALF_OPEN cycle admits enough probes to close the breaker."""
        assert _DEFAULTS.half_open_requests >= _DEFAULTS.success_threshold


class TestOrchestratorAdmission:
    """``should_allow_request()`` is the breaker's admission decision, not a state read."""

    @staticmethod
    def _open(orch: FeatureOrchestrator) -> CircuitBreaker:
        breaker = orch.circuit_breaker
        assert breaker is not None
        for _ in range(breaker.config.failure_threshold):
            breaker.record_failure()
        assert breaker.state == CircuitState.OPEN
        return breaker

    def test_half_open_admits_exactly_the_probe_budget_per_cycle(self, clock):
        orch = FeatureOrchestrator(namespace="lab5326-admit-budget", backpressure_enabled=False)
        breaker = self._open(orch)
        budget = breaker.config.half_open_requests

        for _cycle in range(2):
            clock.shift(_PAST_TIMEOUT)
            admitted = [orch.should_allow_request() for _ in range(budget + 2)]
            assert admitted == [True] * budget + [False] * 2
            assert breaker.state == CircuitState.HALF_OPEN
            breaker.record_failure()  # a failed probe reopens: the next cycle starts fresh
            assert breaker.state == CircuitState.OPEN

    def test_spent_cycle_with_no_outcome_starts_over_after_timeout(self, clock):
        """Probes that never report back do not hold the breaker HALF_OPEN for good."""
        orch = FeatureOrchestrator(namespace="lab5326-admit-expiry", backpressure_enabled=False)
        breaker = self._open(orch)
        budget = breaker.config.half_open_requests

        clock.shift(_PAST_TIMEOUT)
        assert [orch.should_allow_request() for _ in range(budget)] == [True] * budget  # none report back
        clock.shift(timedelta(seconds=_TIMEOUT / 2))
        assert orch.should_allow_request() is False  # the cycle is still inside timeout_seconds

        clock.shift(timedelta(seconds=_TIMEOUT))
        admitted = [orch.should_allow_request() for _ in range(budget + 1)]
        assert admitted == [True] * budget + [False]  # a fresh cycle with a fresh budget
        assert breaker.state == CircuitState.HALF_OPEN

    def test_old_cycle_with_probe_slots_left_is_not_restarted(self, clock):
        """Only a spent cycle starts over: an old cycle with slots left keeps its successes."""
        orch = FeatureOrchestrator(namespace="lab5326-admit-expiry-unspent", backpressure_enabled=False)
        breaker = self._open(orch)

        clock.shift(_PAST_TIMEOUT)
        assert orch.should_allow_request() is True
        breaker.record_success()

        clock.shift(_PAST_TIMEOUT)  # the cycle is now older than timeout_seconds, with slots left
        assert orch.should_allow_request() is True
        assert breaker.success_count == 1  # same cycle: the first success still counts
        breaker.record_success()
        for _ in range(breaker.config.success_threshold - 2):
            assert orch.should_allow_request() is True
            breaker.record_success()
        assert breaker.state == CircuitState.CLOSED


class TestRejectionIsNotAFailure:
    """A call the breaker rejects runs uncached, raises nothing, and is not recorded as a failure."""

    async def test_rejected_while_open(self, is_async, resolver, backend, live_breakers, clock):
        executions: list[str] = []
        fn = _decorate(f"lab5326-reject-open-{is_async}", is_async=is_async, executions=executions)
        (breaker,) = live_breakers
        await _open(fn, resolver, breaker)
        failures = breaker.failure_count

        clock.shift(timedelta(seconds=1))
        assert await _call(fn, "rejected") == "v:rejected"

        assert executions[-1] == "rejected"  # the function still ran
        assert backend.gets == backend.sets == 0  # ...uncached
        assert breaker.failure_count == failures  # ...and the rejection was not a failure
        assert breaker.state == CircuitState.OPEN

    async def test_rejected_in_half_open_leaves_breaker_half_open(self, is_async, resolver, backend, live_breakers, clock):
        fn = _decorate(f"lab5326-reject-half-open-{is_async}", is_async=is_async)
        (breaker,) = live_breakers
        await _open(fn, resolver, breaker)
        failures = breaker.failure_count

        clock.shift(_PAST_TIMEOUT)
        for _ in range(breaker.config.half_open_requests):  # every probe slot is in flight
            assert breaker.should_attempt_call()
        assert breaker.state == CircuitState.HALF_OPEN

        assert await _call(fn, "no-slot") == "v:no-slot"

        assert backend.gets == backend.sets == 0
        assert breaker.failure_count == failures
        assert breaker.state == CircuitState.HALF_OPEN  # not reopened by its own rejection


class TestSlidingWindow:
    """Nothing recorded while OPEN pushes the timeout forward.

    A test with no calls between opening and the timeout passes even when every
    rejection is recorded as a failure — these would not.
    """

    async def test_steady_traffic_does_not_hold_the_breaker_open(self, is_async, resolver, backend, live_breakers, clock):
        fn = _decorate(f"lab5326-sliding-{is_async}", is_async=is_async)
        (breaker,) = live_breakers
        await _open(fn, resolver, breaker)
        half = timedelta(seconds=_TIMEOUT / 2)

        clock.shift(half)
        await _call(fn, "inside-open-window")
        assert backend.gets == 0
        clock.shift(half)
        await _call(fn, "at-timeout")  # exactly on the boundary: either outcome is acceptable
        clock.shift(half)
        before = backend.gets
        await _call(fn, "after-timeout")  # the first call made after timeout_seconds since opening

        assert backend.gets == before + 1
        assert breaker.state == CircuitState.HALF_OPEN

    async def test_failures_recorded_while_open_do_not_extend_the_window(
        self, is_async, resolver, backend, live_breakers, clock
    ):
        """Calls whose key generation fails, before the admission check, do not extend the window."""

        def key(x: str) -> str:
            if x.startswith("unkeyable"):
                raise TypeError("cannot build a key")
            return x

        fn = _decorate(f"lab5326-sliding-keygen-{is_async}", is_async=is_async, key=key)
        (breaker,) = live_breakers
        await _open(fn, resolver, breaker)

        step = timedelta(seconds=_TIMEOUT / 3)
        for i in range(3):  # the last one lands exactly timeout_seconds after opening
            clock.shift(step)
            assert await _call(fn, f"unkeyable-{i}") == f"v:unkeyable-{i}"
        clock.shift(timedelta(seconds=1))
        before = backend.gets
        await _call(fn, "keyable")

        assert backend.gets == before + 1
        assert breaker.state == CircuitState.HALF_OPEN


class TestRecovery:
    """Default config: after the timeout, probes reach the backend and the breaker closes."""

    async def test_recovers_to_closed(self, is_async, resolver, backend, live_breakers, clock):
        executions: list[str] = []
        fn = _decorate(f"lab5326-recover-{is_async}", is_async=is_async, executions=executions)
        (breaker,) = live_breakers
        await _open(fn, resolver, breaker)

        clock.shift(_PAST_TIMEOUT)
        await _recovers(fn, backend, breaker)

        runs = len(executions)
        assert await _call(fn, "probe-0") == "v:probe-0"
        assert len(executions) == runs  # served from the backend, not recomputed


class TestKnobPathRecovery:
    """``@cache(circuit_breaker=nested CircuitBreakerConfig(...))`` recovers on the configured cooldown.

    Reads state through the public ``get_health_status()``. The breaker is tripped by
    client-creation failures, as ``_open`` does: the function's own exceptions never
    count. Recovery closes only if the nested probe budget reaches
    ``success_threshold``, so this also pins the nested default.
    """

    _KNOBS = NestedCircuitBreakerConfig(failure_threshold=2, recovery_timeout=5.0)  # far below the 30s default

    async def test_recovers_after_configured_recovery_timeout(self, is_async, resolver, backend, clock):
        executions: list[str] = []
        fn = _decorate(f"lab5326-knob-{is_async}", is_async=is_async, executions=executions, circuit_breaker=self._KNOBS)

        def breaker_state() -> str:
            return fn.get_health_status()["circuit_breaker"]["state"]

        for i in range(self._KNOBS.failure_threshold):
            assert await _call(fn, f"trip-{i}") == f"v:trip-{i}"  # degrades to uncached, never raises
        assert breaker_state() == "open"
        resolver.down = False

        clock.shift(timedelta(seconds=self._KNOBS.recovery_timeout / 2))
        gets = backend.gets
        assert await _call(fn, "inside-cooldown") == "v:inside-cooldown"
        assert backend.gets == gets  # still inside the configured cooldown: rejected

        clock.shift(timedelta(seconds=self._KNOBS.recovery_timeout))  # past recovery_timeout, under the default
        probes = [f"probe-{i}" for i in range(self._KNOBS.success_threshold)]  # cold keys: misses reach L2
        for key in probes:
            assert await _call(fn, key) == f"v:{key}"
        assert backend.gets == gets + len(probes)  # the backend is called again
        assert breaker_state() == "closed"

        runs = len(executions)
        assert await _call(fn, probes[0]) == f"v:{probes[0]}"
        assert len(executions) == runs  # served from the backend, not recomputed


class TestUnreportedProbesDoNotStrandTheBreaker:
    """A HALF_OPEN cycle whose probes all exit without an outcome starts over after the timeout.

    Each test spends a whole cycle on probes that leave through a different exit
    that records nothing, then checks a fresh cycle can still close the breaker.
    """

    @staticmethod
    async def _strand_then_recover(
        fn: Callable[[str], Any],
        backend: _CountingBackend,
        breaker: CircuitBreaker,
        clock,
        strand: Callable[[str], Awaitable[None]],
    ) -> None:
        clock.shift(_PAST_TIMEOUT)
        for i in range(breaker.config.half_open_requests):
            await strand(f"stranded-{i}")
        assert breaker.state == CircuitState.HALF_OPEN
        assert breaker.should_attempt_call() is False  # budget spent, cycle still young

        clock.shift(_PAST_TIMEOUT)
        await _recovers(fn, backend, breaker)

    async def test_cancelled_async_probes(self, resolver, backend, live_breakers, clock):
        """``CancelledError`` is a ``BaseException``: no ``except Exception`` records it."""
        never = asyncio.Event()

        @cache(ttl=300, l1_enabled=False, namespace="lab5326-strand-cancel")
        async def fn(x: str) -> str:
            if x.startswith("stranded"):
                await never.wait()
            return f"v:{x}"

        (breaker,) = live_breakers
        await _open(fn, resolver, breaker)

        async def cancel(x: str) -> None:
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(fn(x), timeout=0.01)

        await self._strand_then_recover(fn, backend, breaker, clock, cancel)

    async def test_async_lock_path_interop_output_rejection(self, resolver, live_breakers, clock):
        """Interop refuses an out-of-model return value; the lock path re-raises it unrecorded."""
        locking = resolver.backend = _LockingBackend()

        @cache(ttl=300, l1_enabled=False, namespace="lab5326-strand-interop-out", interop="op")
        async def fn(x: str) -> Any:
            if x.startswith("stranded"):
                return {x}  # a set is outside the interop data model
            return f"v:{x}"

        (breaker,) = live_breakers
        await _open(fn, resolver, breaker)

        async def reject_output(x: str) -> None:
            with pytest.raises(InteropError):
                await fn(x)

        await self._strand_then_recover(fn, locking, breaker, clock, reject_output)

    async def test_sync_interop_prefix_guard(self, resolver, backend, live_breakers, clock):
        """The fail-closed interop prefix guard raises unrecorded, and spends a probe slot only once.

        Since LAB-5351 the guard runs before admission whenever the backend is already resolved,
        so only the call that resolves the backend (behind admission) can raise with a slot spent.
        That one unreported probe leaves the cycle short of ``success_threshold``; the cycle still
        starts over once its budget is spent and the timeout has passed.
        """

        @cache(ttl=300, l1_enabled=False, namespace="lab5326-strand-prefix", interop="op")
        def fn(x: str) -> str:
            return f"v:{x}"

        (breaker,) = live_breakers
        await _open(fn, resolver, breaker)

        clock.shift(_PAST_TIMEOUT)
        backend.key_prefix = "tenant:"
        for i in range(breaker.config.half_open_requests + 1):
            with pytest.raises(ConfigurationError, match="prefix"):
                fn(f"stranded-{i}")
        backend.key_prefix = ""
        assert breaker.state == CircuitState.HALF_OPEN
        assert breaker._half_open_total_attempts == 1  # only the resolving call was admitted

        for i in range(breaker.config.half_open_requests - 1):  # spend the rest of the cycle
            assert fn(f"short-{i}") == f"v:short-{i}"
        assert breaker.state == CircuitState.HALF_OPEN
        assert breaker.should_attempt_call() is False  # budget spent, cycle still young

        clock.shift(_PAST_TIMEOUT)
        await _recovers(fn, backend, breaker)


class TestL1HitsBypassTheBreaker:
    """An L1 hit is served whatever the breaker state and records no breaker outcome (LAB-5351).

    The breaker tracks backend health, and an L1 hit never reaches the backend. When admission
    ran before the L1 lookup, an OPEN breaker recomputed keys L1 already held, and L1 hits
    spent HALF_OPEN probe slots and closed the breaker without a single backend call.
    """

    @staticmethod
    async def _warm(fn: Callable[[str], Any], resolver: _FlakyResolver) -> None:
        resolver.down = False
        assert await _call(fn, "hot") == "v:hot"  # a miss: runs once, fills L1 and L2

    async def test_l1_hit_skips_admission_and_records_nothing(
        self, is_async, resolver, live_breakers, clock, monkeypatch: pytest.MonkeyPatch
    ):
        executions: list[str] = []
        fn = _decorate(f"lab5351-spy-{is_async}", is_async=is_async, executions=executions, l1_enabled=True)
        (breaker,) = live_breakers
        await self._warm(fn, resolver)

        touched: list[str] = []
        for name in ("admit", "_on_success", "_on_failure"):  # what the orchestrator calls
            real = getattr(breaker, name)

            def spy(*args: Any, _name: str = name, _real: Any = real, **kwargs: Any) -> Any:
                touched.append(_name)
                return _real(*args, **kwargs)

            monkeypatch.setattr(breaker, name, spy)

        assert await _call(fn, "hot") == "v:hot"
        assert executions == ["hot"]  # served from L1
        assert touched == []

        assert await _call(fn, "cold") == "v:cold"  # the spies are live: a miss is admitted
        assert "admit" in touched

    async def test_open_breaker_serves_l1_hit(self, is_async, resolver, backend, live_breakers, clock):
        executions: list[str] = []
        fn = _decorate(f"lab5351-open-hit-{is_async}", is_async=is_async, executions=executions, l1_enabled=True)
        (breaker,) = live_breakers
        await self._warm(fn, resolver)
        _trip(breaker)
        gets = backend.gets

        for _ in range(3):
            assert await _call(fn, "hot") == "v:hot"

        assert executions == ["hot"]  # never recomputed
        assert backend.gets == gets
        assert breaker.state == CircuitState.OPEN

    async def test_open_breaker_l1_miss_runs_uncached(self, is_async, resolver, backend, live_breakers, clock):
        executions: list[str] = []
        fn = _decorate(f"lab5351-open-miss-{is_async}", is_async=is_async, executions=executions, l1_enabled=True)
        (breaker,) = live_breakers
        await self._warm(fn, resolver)
        _trip(breaker)
        gets, sets = backend.gets, backend.sets

        for _ in range(2):
            assert await _call(fn, "cold") == "v:cold"

        assert executions == ["hot", "cold", "cold"]  # ran each time: nothing was cached
        assert (backend.gets, backend.sets) == (gets, sets)
        assert breaker.state == CircuitState.OPEN

    async def test_half_open_l1_hits_spend_no_probe_and_do_not_close(self, is_async, resolver, backend, live_breakers, clock):
        executions: list[str] = []
        fn = _decorate(f"lab5351-half-open-{is_async}", is_async=is_async, executions=executions, l1_enabled=True)
        (breaker,) = live_breakers
        await self._warm(fn, resolver)
        _trip(breaker)

        clock.shift(_PAST_TIMEOUT)
        assert await _call(fn, "probe-0") == "v:probe-0"  # admitted: enters HALF_OPEN
        assert breaker.state == CircuitState.HALF_OPEN
        attempts, successes, gets = breaker._half_open_total_attempts, breaker.success_count, backend.gets

        for _ in range(breaker.config.success_threshold * 3):
            assert await _call(fn, "hot") == "v:hot"

        assert breaker.state == CircuitState.HALF_OPEN
        assert breaker._half_open_total_attempts == attempts
        assert breaker.success_count == successes
        assert backend.gets == gets
        assert executions == ["hot", "probe-0"]

        for i in range(1, breaker.config.success_threshold):  # only backend operations close it
            assert breaker.state == CircuitState.HALF_OPEN
            assert await _call(fn, f"probe-{i}") == f"v:probe-{i}"
        assert breaker.state == CircuitState.CLOSED

    async def test_open_interop_call_resolves_no_backend(self, is_async, resolver, live_breakers, clock):
        """Serving L1 before admission does not pull backend resolution ahead of it for interop."""
        executions: list[str] = []
        fn = _decorate(
            f"lab5351-interop-open-{int(is_async)}", is_async=is_async, executions=executions, l1_enabled=True, interop="op"
        )
        (breaker,) = live_breakers
        await _open(fn, resolver, breaker)  # leaves no backend resolved
        resolutions = resolver.calls

        for i in range(3):
            assert await _call(fn, f"rejected-{i}") == f"v:rejected-{i}"

        assert resolver.calls == resolutions
        assert executions[-3:] == ["rejected-0", "rejected-1", "rejected-2"]


class TestFunctionExceptionsDoNotCount:
    """An exception the decorated function raises is not a backend failure (LAB-5319).

    It reaches the caller unchanged and never counts toward the breaker, so a function
    that keeps raising against a healthy backend never turns caching off.
    """

    _RAISES = _DEFAULTS.failure_threshold + 2

    @staticmethod
    def _decorate_raising(namespace: str, is_async: bool, backend: _CountingBackend, make_error: Callable[[], Exception]):
        executions: list[str] = []
        raised: list[Exception] = []

        def body(x: str) -> str:
            executions.append(x)
            if x.startswith("bad"):
                raised.append(make_error())
                raise raised[-1]
            return f"v:{x}"

        async def async_body(x: str) -> str:
            return body(x)

        fn = cache(ttl=300, l1_enabled=False, namespace=namespace, backend=backend)(async_body if is_async else body)
        return fn, executions, raised

    @pytest.mark.parametrize(
        "make_error",
        [
            lambda: ValueError("negative input"),
            lambda: BackendError("raised by the function", error_type=BackendErrorType.TRANSIENT),
        ],
        ids=["application-error", "function-raised-backend-error"],
    )
    async def test_breaker_stays_closed_and_caching_continues(self, is_async, backend, live_breakers, make_error):
        fn, executions, raised = self._decorate_raising(f"lab5319-{is_async}", is_async, backend, make_error)
        (breaker,) = live_breakers

        for i in range(self._RAISES):
            with pytest.raises(Exception) as excinfo:
                await _call(fn, f"bad-{i}")
            assert excinfo.value is raised[-1]  # the very object the function raised

        assert len(raised) == self._RAISES
        assert breaker.state == CircuitState.CLOSED
        assert breaker.failure_count == 0

        runs = len(executions)
        assert await _call(fn, "fresh") == "v:fresh"
        assert await _call(fn, "fresh") == "v:fresh"
        assert len(executions) == runs + 1  # the second call is a cache hit

    @pytest.mark.parametrize(
        ("is_async", "backend_cls"),
        [(False, _CountingBackend), (True, _CountingBackend), (True, _LockingBackend)],
        ids=["sync", "async", "async-lock"],
    )
    async def test_raising_probe_gives_its_slot_to_the_next_call(self, is_async, backend_cls, live_breakers, clock):
        """A HALF_OPEN probe whose function raises records nothing and hands its slot on.

        Kept, the slot would stay spent until the cycle expires, and every call before
        then would run uncached: a raising function would still switch off its own caching.
        """
        backend = backend_cls()
        fn, _, raised = self._decorate_raising(f"lab5319-probe-{is_async}-{backend_cls.__name__}", is_async, backend, ValueError)
        (breaker,) = live_breakers
        _trip(breaker)

        clock.shift(_PAST_TIMEOUT)
        for i in range(breaker.config.half_open_requests + 2):  # more than a cycle's budget
            gets = backend.gets
            with pytest.raises(ValueError):
                await _call(fn, f"bad-{i}")
            assert backend.gets > gets  # admitted: it read L2, where a rejected call reads nothing
        assert len(raised) == breaker.config.half_open_requests + 2
        assert breaker.state == CircuitState.HALF_OPEN

        await _recovers(fn, backend, breaker)  # the same cycle still has every slot

    @pytest.mark.parametrize(
        ("is_async", "backend_cls"),
        [(False, _CountingBackend), (True, _CountingBackend), (True, _LockingBackend)],
        ids=["sync", "async", "async-lock"],
    )
    async def test_call_admitted_while_closed_gives_a_later_cycle_nothing(self, is_async, backend_cls, live_breakers, clock):
        """Calls admitted while CLOSED that raise once HALF_OPEN has spent its budget add no probe.

        Each would otherwise hand the cycle a slot it never took, so enough slow calls in
        flight would let any number of probes through to a backend still under test.
        """
        backend = backend_cls()
        stale = _DEFAULTS.half_open_requests + 2
        entered: list[str] = []
        all_in, gate = threading.Event(), threading.Event()

        def body(x: str) -> str:
            entered.append(x)
            if len(entered) == stale:
                all_in.set()
            gate.wait(timeout=10)
            raise ValueError(x)

        async def async_body(x: str) -> str:
            entered.append(x)
            if len(entered) == stale:
                all_in.set()
            while not gate.is_set():
                await asyncio.sleep(0)
            raise ValueError(x)

        namespace = f"lab5319-stale-{is_async}-{backend_cls.__name__}"
        fn = cache(ttl=300, l1_enabled=False, namespace=namespace, backend=backend)(async_body if is_async else body)
        (breaker,) = live_breakers

        with ThreadPoolExecutor(max_workers=stale) as pool:
            if is_async:
                calls = [asyncio.ensure_future(fn(f"stale-{i}")) for i in range(stale)]
                while not all_in.is_set():
                    await asyncio.sleep(0)
            else:
                calls = [asyncio.wrap_future(pool.submit(fn, f"stale-{i}")) for i in range(stale)]
                assert await asyncio.to_thread(all_in.wait, 10)
            assert breaker.state == CircuitState.CLOSED  # every stale call is in flight, admitted

            _trip(breaker)
            clock.shift(_PAST_TIMEOUT)
            for _ in range(breaker.config.half_open_requests):
                assert breaker.should_attempt_call()  # the cycle's own probes, still in flight

            gate.set()
            for outcome in await asyncio.gather(*calls, return_exceptions=True):
                assert isinstance(outcome, ValueError)

        gets = backend.gets
        with pytest.raises(ValueError):
            await _call(fn, "next")
        assert backend.gets == gets  # rejected: the budget is still spent
        assert breaker.state == CircuitState.HALF_OPEN

    async def test_backend_failures_still_open_the_breaker(self, is_async, live_breakers, monkeypatch):
        """Transient backend errors recorded through ``handle_cache_error`` (client creation) still open it."""

        def unreachable() -> _CountingBackend:
            raise BackendError("backend unreachable", error_type=BackendErrorType.TRANSIENT)

        monkeypatch.setattr(wrapper_module, "_resolve_lazy_backend", unreachable)
        fn = _decorate(f"lab5319-backend-{is_async}", is_async=is_async)
        (breaker,) = live_breakers

        for i in range(_DEFAULTS.failure_threshold):
            assert await _call(fn, f"k-{i}") == f"v:k-{i}"  # degrades to uncached, never raises
        assert breaker.state == CircuitState.OPEN


class TestInteropValueContractOnDegradedPaths:
    """Interop refuses an out-of-model return value whatever the breaker state (LAB-5375).

    A call the breaker rejects, and a call whose backend cannot be created, run the
    function uncached and never reach the store path, where the value check used to run.
    The value is now checked on those paths too: an in-model value still returns
    uncached, an out-of-model one raises ``InteropError`` as it does on a cached call.
    """

    _SCENARIOS = ["open", "half-open-spent", "open-before-resolution", "client-creation-fallback"]

    @staticmethod
    async def _degrade(scenario: str, is_async: bool, resolver: _FlakyResolver, live_breakers, clock):
        """Decorate an interop function and put it in ``scenario``; returns ``(fn, executions, breaker)``."""
        executions: list[str] = []

        def body(x: str) -> Any:
            executions.append(x)
            if x.startswith("deep"):
                return _deeply_nested()
            return {"bad": {1}} if x.startswith("bad") else f"v:{x}"  # a set is outside the data model

        async def async_body(x: str) -> Any:
            return body(x)

        kwargs: dict[str, Any] = {"interop": "op"}
        if scenario in ("open", "half-open-spent"):
            kwargs["backend"] = resolver.backend  # resolved before the breaker rejects anything
        fn = cache(ttl=300, l1_enabled=False, namespace=f"lab5375-{scenario}-{int(is_async)}", **kwargs)(
            async_body if is_async else body
        )
        (breaker,) = live_breakers

        if scenario in ("open", "half-open-spent"):
            _trip(breaker)
        if scenario == "half-open-spent":
            clock.shift(_PAST_TIMEOUT)
            for _ in range(breaker.config.half_open_requests):  # every probe slot is in flight
                assert breaker.should_attempt_call()
            assert breaker.state == CircuitState.HALF_OPEN
        if scenario == "open-before-resolution":
            await _open(fn, resolver, breaker)  # leaves no backend resolved
            del executions[:]
        # client-creation-fallback: the resolver is still down and the breaker CLOSED
        return fn, executions, breaker

    @pytest.mark.parametrize("scenario", _SCENARIOS)
    async def test_out_of_model_return_raises(self, scenario, is_async, resolver, backend, live_breakers, clock):
        fn, executions, breaker = await self._degrade(scenario, is_async, resolver, live_breakers, clock)
        state, failures = breaker.state, breaker.failure_count

        with pytest.raises(InteropError):
            await _call(fn, "bad")

        assert executions == ["bad"]  # the function ran once; its value was refused
        assert backend.gets == backend.sets == 0  # nothing reached the backend
        assert get_current_function_stats() is None  # the raise left no stats context behind
        if scenario != "client-creation-fallback":  # that one records its client failure, as before
            assert (breaker.state, breaker.failure_count) == (state, failures)

    @pytest.mark.parametrize("scenario", _SCENARIOS)
    async def test_in_model_return_still_runs_uncached(self, scenario, is_async, resolver, backend, live_breakers, clock):
        fn, executions, breaker = await self._degrade(scenario, is_async, resolver, live_breakers, clock)
        state, failures = breaker.state, breaker.failure_count

        assert await _call(fn, "good") == "v:good"

        assert executions == ["good"]
        assert backend.gets == backend.sets == 0
        assert get_current_function_stats() is None
        if scenario != "client-creation-fallback":
            assert (breaker.state, breaker.failure_count) == (state, failures)

    @pytest.mark.parametrize("scenario", _SCENARIOS)
    async def test_unencodable_in_model_return_still_runs_uncached(
        self, scenario, is_async, resolver, backend, live_breakers, clock
    ):
        """An encode failure that is not a data-model rejection degrades, as on the store path.

        Too deep a nesting raises RecursionError, not InteropError. The store path wraps it in
        SerializationError and returns the value uncached, so an uncached call returns it too.
        """
        fn, executions, _ = await self._degrade(scenario, is_async, resolver, live_breakers, clock)

        assert await _call(fn, "deep") == _deeply_nested()

        assert executions == ["deep"]
        assert backend.gets == backend.sets == 0
        assert get_current_function_stats() is None


def _deeply_nested() -> list[Any]:
    """A list nested past the encoder's recursion limit."""
    value: list[Any] = []
    for _ in range(5000):
        value = [value]
    return value
