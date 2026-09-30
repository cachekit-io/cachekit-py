"""A cache key that cannot be generated never changes circuit-breaker state (LAB-5376).

Key generation runs before admission and never touches the backend, so its
failure says nothing about backend health. It used to count as a breaker
failure: five unkeyable calls opened the breaker in front of a healthy backend,
and one reopened a HALF_OPEN breaker, switching caching off for every call to
the function. The call still logs, emits its metric and runs uncached.

These tests drive a real ``@cache`` function with an unkeyable argument (a plain
``object()``) on a frozen clock, and capture the live breaker by spying on the
orchestrator's ``CircuitBreaker`` constructor.
"""

from __future__ import annotations

import inspect
import logging
from collections.abc import Callable
from datetime import timedelta
from typing import Any, Optional

import pytest
import time_machine

from cachekit import cache
from cachekit.decorators import orchestrator as orchestrator_module
from cachekit.decorators import wrapper as wrapper_module
from cachekit.reliability.circuit_breaker import CircuitBreaker, CircuitBreakerConfig, CircuitState

_DEFAULTS = CircuitBreakerConfig()
_PAST_TIMEOUT = timedelta(seconds=_DEFAULTS.timeout_seconds + 1)
_EPOCH = 1_000_000.0  # Frozen wall clock; any fixed instant works


class _Backend:
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


@pytest.fixture
def backend(monkeypatch: pytest.MonkeyPatch) -> _Backend:
    healthy = _Backend()
    monkeypatch.setattr(wrapper_module, "_resolve_lazy_backend", lambda: healthy)
    return healthy


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


def _decorate(namespace: str, *, is_async: bool, executions: list[Any]):
    """``@cache`` a sync or async function returning ``v:<x>``, recording each execution."""

    def body(x: Any) -> str:
        executions.append(x)
        return f"v:{x}"

    async def async_body(x: Any) -> str:
        return body(x)

    return cache(ttl=300, l1_enabled=False, namespace=namespace)(async_body if is_async else body)


async def _call(fn: Callable[[Any], Any], x: Any) -> Any:
    result = fn(x)
    return await result if inspect.isawaitable(result) else result


async def _unkeyable(fn: Callable[[Any], Any], executions: list[Any]) -> None:
    """One call whose key cannot be generated: it runs the function, uncached, and raises nothing."""
    arg = object()
    assert await _call(fn, arg) == f"v:{arg}"
    assert executions[-1] is arg


def _open(breaker: CircuitBreaker) -> None:
    for _ in range(breaker.config.failure_threshold):
        breaker.record_failure()
    assert breaker.state == CircuitState.OPEN


class TestKeyGenerationFailureLeavesBreakerAlone:
    async def test_closed_stays_closed(self, is_async, backend, live_breakers, clock):
        executions: list[Any] = []
        fn = _decorate(f"lab5376-closed-{is_async}", is_async=is_async, executions=executions)
        (breaker,) = live_breakers

        for _ in range(_DEFAULTS.failure_threshold * 3):
            await _unkeyable(fn, executions)

        assert breaker.state == CircuitState.CLOSED
        assert breaker.failure_count == 0
        assert backend.gets == backend.sets == 0  # never reached the backend

        assert await _call(fn, "keyable") == "v:keyable"
        assert backend.sets == 1  # caching still on for keyable calls

    async def test_half_open_stays_half_open_and_still_closes(self, is_async, backend, live_breakers, clock):
        executions: list[Any] = []
        fn = _decorate(f"lab5376-half-open-{is_async}", is_async=is_async, executions=executions)
        (breaker,) = live_breakers
        _open(breaker)
        clock.shift(_PAST_TIMEOUT)

        probes = [f"probe-{i}" for i in range(_DEFAULTS.success_threshold)]  # cold keys: misses reach L2
        assert await _call(fn, probes[0]) == f"v:{probes[0]}"
        assert breaker.state == CircuitState.HALF_OPEN

        await _unkeyable(fn, executions)
        assert breaker.state == CircuitState.HALF_OPEN  # not reopened

        for key in probes[1:]:
            assert await _call(fn, key) == f"v:{key}"
        assert backend.sets == len(probes)
        assert breaker.state == CircuitState.CLOSED

    async def test_open_state_and_window_unchanged(self, is_async, backend, live_breakers, clock):
        executions: list[Any] = []
        fn = _decorate(f"lab5376-open-{is_async}", is_async=is_async, executions=executions)
        (breaker,) = live_breakers
        _open(breaker)
        opened_at = breaker._last_failure_time
        failures = breaker.failure_count

        clock.shift(timedelta(seconds=1))
        await _unkeyable(fn, executions)

        assert breaker.state == CircuitState.OPEN
        assert breaker._last_failure_time == opened_at
        assert breaker.failure_count == failures


class TestKeyGenerationFailureStillReported:
    async def test_log_and_metric_unchanged(self, is_async, backend, live_breakers, clock, monkeypatch, caplog):
        recorded: list[dict[str, Any]] = []
        real_record = orchestrator_module.AsyncMetricsCollector.record_cache_operation

        def spy(self, *args, **kwargs):
            recorded.append(kwargs)
            return real_record(self, *args, **kwargs)

        monkeypatch.setattr(orchestrator_module.AsyncMetricsCollector, "record_cache_operation", spy)
        executions: list[Any] = []
        fn = _decorate(f"lab5376-report-{is_async}", is_async=is_async, executions=executions)

        with caplog.at_level(logging.INFO):
            await _unkeyable(fn, executions)

        structured = [getattr(r, "structured", {}) for r in caplog.records]
        assert any(s.get("operation") == "key_generation_failed" for s in structured)
        assert any("Cache operation 'key_generation' failed" in r.getMessage() for r in caplog.records)
        assert any(r.get("operation") == "key_generation" and r.get("success") is False for r in recorded)
