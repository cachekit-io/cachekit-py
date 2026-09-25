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
   without one (cancellation, fail-closed raises, the async lock path's
   re-raise) held the breaker HALF_OPEN for good.

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
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import timedelta
from typing import Any, Optional

import pytest
import time_machine

from cachekit import cache
from cachekit.config.validation import ConfigurationError
from cachekit.decorators import orchestrator as orchestrator_module
from cachekit.decorators import wrapper as wrapper_module
from cachekit.decorators.orchestrator import FeatureOrchestrator
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

    def __call__(self) -> _CountingBackend:
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
def live_breakers(monkeypatch: pytest.MonkeyPatch) -> list[CircuitBreaker]:
    """Capture every breaker a decorator builds (decorate AFTER requesting this)."""
    captured: list[CircuitBreaker] = []
    real = orchestrator_module.CircuitBreaker

    def spy(*args, **kwargs) -> CircuitBreaker:
        breaker = real(*args, **kwargs)
        captured.append(breaker)
        return breaker

    monkeypatch.setattr(orchestrator_module, "CircuitBreaker", spy)
    return captured


@pytest.fixture
def clock():
    with time_machine.travel(_EPOCH, tick=False) as traveller:
        yield traveller


@pytest.fixture(params=[False, True], ids=["sync", "async"])
def is_async(request: pytest.FixtureRequest) -> bool:
    return request.param


def _decorate(namespace: str, *, is_async: bool, executions: Optional[list[str]] = None, **cache_kwargs: Any):
    """``@cache`` a sync or async function returning ``v:<x>``, recording each execution."""
    runs = executions if executions is not None else []

    def body(x: str) -> str:
        runs.append(x)
        return f"v:{x}"

    async def async_body(x: str) -> str:
        return body(x)

    return cache(ttl=300, l1_enabled=False, namespace=namespace, **cache_kwargs)(async_body if is_async else body)


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
        """Key generation runs, and records its failures, before the admission check."""

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

    async def test_async_lock_path_function_raise(self, resolver, live_breakers, clock):
        """The lock path re-raises a function exception without recording it."""
        locking = resolver.backend = _LockingBackend()

        @cache(ttl=300, l1_enabled=False, namespace="lab5326-strand-lock-raise")
        async def fn(x: str) -> str:
            if x.startswith("stranded"):
                raise ValueError("probe failed inside the lock")
            return f"v:{x}"

        (breaker,) = live_breakers
        await _open(fn, resolver, breaker)

        async def raise_in_lock(x: str) -> None:
            with pytest.raises(ValueError, match="inside the lock"):
                await fn(x)

        await self._strand_then_recover(fn, locking, breaker, clock, raise_in_lock)

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
        """The fail-closed interop prefix guard raises after admission, recording nothing."""

        @cache(ttl=300, l1_enabled=False, namespace="lab5326-strand-prefix", interop="op")
        def fn(x: str) -> str:
            return f"v:{x}"

        (breaker,) = live_breakers
        await _open(fn, resolver, breaker)

        async def fail_closed(x: str) -> None:
            backend.key_prefix = "tenant:"
            try:
                with pytest.raises(ConfigurationError, match="prefix"):
                    fn(x)
            finally:
                backend.key_prefix = ""

        await self._strand_then_recover(fn, backend, breaker, clock, fail_closed)
