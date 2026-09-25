"""The circuit breaker recovers on the live ``@cache`` path (LAB-5326).

The breaker's own state machine was always tested on a bare ``CircuitBreaker``,
never through ``@cache`` / ``FeatureOrchestrator``. That hid three defects that
together kept a breaker opened by the decorator OPEN until process restart:

1. ``FeatureOrchestrator.should_allow_request()`` read the state instead of
   asking for admission, so the OPEN -> HALF_OPEN timeout never ran.
2. The sync wrapper recorded its own fail-fast rejection as a failure, sliding
   the OPEN window forward on every call (and reopening HALF_OPEN).
3. The default ``CircuitBreakerConfig`` admitted 1 HALF_OPEN probe but needed
   3 successes to close.

These tests drive a real decorated function on a frozen clock, as
``test_circuit_breaker_race_conditions.py`` does. The breaker is opened through
client-creation failures — the path that reaches ``record_failure`` today — and
captured by spying on the orchestrator's ``CircuitBreaker`` constructor. Async
tests make no calls while the breaker is OPEN before the timeout: the async
fail-fast currently escapes as ``UnboundLocalError``, a separate defect.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Optional

import pytest
import time_machine

from cachekit import cache
from cachekit.decorators import orchestrator as orchestrator_module
from cachekit.decorators import wrapper as wrapper_module
from cachekit.decorators.orchestrator import FeatureOrchestrator
from cachekit.reliability.circuit_breaker import CircuitBreaker, CircuitBreakerConfig, CircuitState

_DEFAULTS = CircuitBreakerConfig()
_TIMEOUT = _DEFAULTS.timeout_seconds
_EPOCH = 1_000_000.0  # Frozen wall clock; any fixed instant works


class _CountingBackend:
    """Healthy in-memory backend that counts the operations reaching it."""

    def __init__(self) -> None:
        self.store: dict[str, bytes] = {}
        self.gets = 0
        self.sets = 0

    def get(self, key: str) -> Optional[bytes]:
        self.gets += 1
        return self.store.get(key)

    def set(self, key: str, value: bytes, ttl: Optional[int] = None) -> None:
        self.sets += 1
        self.store[key] = bytes(value)

    def delete(self, key: str) -> bool:
        return self.store.pop(key, None) is not None


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


def _open_sync(fn, resolver: _FlakyResolver, breaker: CircuitBreaker) -> None:
    """Trip the breaker with client-creation failures, then bring the backend back."""
    for i in range(_DEFAULTS.failure_threshold):
        assert fn(f"trip-{i}") == f"v:trip-{i}"  # degrades to uncached, never raises
    assert breaker.state == CircuitState.OPEN
    resolver.down = False


async def _open_async(fn, resolver: _FlakyResolver, breaker: CircuitBreaker) -> None:
    for i in range(_DEFAULTS.failure_threshold):
        assert await fn(f"trip-{i}") == f"v:trip-{i}"
    assert breaker.state == CircuitState.OPEN
    resolver.down = False


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

    def test_rejects_while_open_before_timeout(self, clock):
        orch = FeatureOrchestrator(namespace="lab5326-admit-open", backpressure_enabled=False)
        breaker = self._open(orch)

        clock.shift(timedelta(seconds=_TIMEOUT / 2))

        assert orch.should_allow_request() is False
        assert breaker.state == CircuitState.OPEN

    def test_half_open_admits_exactly_the_probe_budget_per_cycle(self, clock):
        orch = FeatureOrchestrator(namespace="lab5326-admit-budget", backpressure_enabled=False)
        breaker = self._open(orch)
        budget = breaker.config.half_open_requests

        for _cycle in range(2):
            clock.shift(timedelta(seconds=_TIMEOUT + 1))
            admitted = [orch.should_allow_request() for _ in range(budget + 2)]
            assert admitted == [True] * budget + [False] * 2
            assert breaker.state == CircuitState.HALF_OPEN
            breaker.record_failure()  # a failed probe reopens: the next cycle starts fresh
            assert breaker.state == CircuitState.OPEN


class TestSyncRejectionIsNotAFailure:
    """A call the breaker rejects runs uncached and is not recorded as a failure."""

    def test_rejected_while_open(self, resolver, backend, live_breakers, clock):
        executions: list[str] = []

        @cache(ttl=300, l1_enabled=False, namespace="lab5326-sync-reject-open")
        def fn(x: str) -> str:
            executions.append(x)
            return f"v:{x}"

        (breaker,) = live_breakers
        _open_sync(fn, resolver, breaker)
        failures = breaker.failure_count

        clock.shift(timedelta(seconds=1))
        assert fn("rejected") == "v:rejected"

        assert executions[-1] == "rejected"  # the function still ran
        assert backend.gets == backend.sets == 0  # ...uncached
        assert breaker.failure_count == failures  # ...and the rejection was not a failure
        assert breaker.state == CircuitState.OPEN

    def test_rejected_in_half_open_leaves_breaker_half_open(self, resolver, backend, live_breakers, clock):
        @cache(ttl=300, l1_enabled=False, namespace="lab5326-sync-reject-half-open")
        def fn(x: str) -> str:
            return f"v:{x}"

        (breaker,) = live_breakers
        _open_sync(fn, resolver, breaker)
        failures = breaker.failure_count

        clock.shift(timedelta(seconds=_TIMEOUT + 1))
        for _ in range(breaker.config.half_open_requests):  # every probe slot is in flight
            assert breaker.should_attempt_call()
        assert breaker.state == CircuitState.HALF_OPEN

        assert fn("no-slot") == "v:no-slot"

        assert backend.gets == backend.sets == 0
        assert breaker.failure_count == failures
        assert breaker.state == CircuitState.HALF_OPEN  # not reopened by its own rejection


class TestSlidingWindow:
    def test_steady_traffic_does_not_hold_the_breaker_open(self, resolver, backend, live_breakers, clock):
        """Calls during OPEN must not push the timeout forward.

        A test with no calls between opening and the timeout passes even when
        every rejection is recorded as a failure — this one would not.
        """

        @cache(ttl=300, l1_enabled=False, namespace="lab5326-sync-sliding")
        def fn(x: str) -> str:
            return f"v:{x}"

        (breaker,) = live_breakers
        _open_sync(fn, resolver, breaker)
        half = timedelta(seconds=_TIMEOUT / 2)

        clock.shift(half)
        fn("inside-open-window")
        assert backend.gets == 0
        clock.shift(half)
        fn("at-timeout")  # exactly on the boundary: either outcome is acceptable
        clock.shift(half)
        before = backend.gets
        fn("after-timeout")  # the first call made after timeout_seconds since opening

        assert backend.gets == before + 1
        assert breaker.state == CircuitState.HALF_OPEN


class TestRecovery:
    """Default config: after the timeout, probes reach the backend and the breaker closes."""

    def test_sync_recovers_to_closed(self, resolver, backend, live_breakers, clock):
        executions: list[str] = []

        @cache(ttl=300, l1_enabled=False, namespace="lab5326-sync-recover")
        def fn(x: str) -> str:
            executions.append(x)
            return f"v:{x}"

        (breaker,) = live_breakers
        _open_sync(fn, resolver, breaker)

        clock.shift(timedelta(seconds=_TIMEOUT + 1))
        probes = [f"probe-{i}" for i in range(_DEFAULTS.success_threshold)]  # cold keys: misses reach L2
        for key in probes:
            assert fn(key) == f"v:{key}"

        assert backend.sets == len(probes)
        assert breaker.state == CircuitState.CLOSED

        runs = len(executions)
        assert fn(probes[0]) == f"v:{probes[0]}"
        assert len(executions) == runs  # served from the backend, not recomputed

    async def test_async_recovers_to_closed(self, resolver, backend, live_breakers, clock):
        executions: list[str] = []

        @cache(ttl=300, l1_enabled=False, namespace="lab5326-async-recover")
        async def fn(x: str) -> str:
            executions.append(x)
            return f"v:{x}"

        (breaker,) = live_breakers
        await _open_async(fn, resolver, breaker)

        clock.shift(timedelta(seconds=_TIMEOUT + 1))
        probes = [f"probe-{i}" for i in range(_DEFAULTS.success_threshold)]
        for key in probes:
            assert await fn(key) == f"v:{key}"

        assert backend.sets == len(probes)
        assert breaker.state == CircuitState.CLOSED

        runs = len(executions)
        assert await fn(probes[0]) == f"v:{probes[0]}"
        assert len(executions) == runs
