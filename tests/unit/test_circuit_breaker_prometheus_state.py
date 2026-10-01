"""The breaker's state reaches Prometheus as ``circuit_breaker_state`` (LAB-6403).

The breaker used to write only an in-process dict, so the documented alert
``circuit_breaker_state{state="OPEN"} > 0`` could never fire. The gauge now
counts live breakers per namespace and state: a healthy function in the same
namespace cannot mask an OPEN one, and a collected breaker leaves the count.
"""

from __future__ import annotations

import gc
import subprocess
import sys
import textwrap
import uuid
from datetime import timedelta
from typing import Any, Optional

import pytest
import time_machine

prometheus_client = pytest.importorskip("prometheus_client")

from cachekit import cache  # noqa: E402
from cachekit.decorators import wrapper as wrapper_module  # noqa: E402
from cachekit.reliability import async_metrics  # noqa: E402
from cachekit.reliability.circuit_breaker import CircuitBreaker, CircuitBreakerConfig, CircuitState  # noqa: E402

_STATES = ("CLOSED", "OPEN", "HALF_OPEN")
_TIMEOUT = 1.0
_PAST_TIMEOUT = timedelta(seconds=_TIMEOUT + 1)


@pytest.fixture
def clock():
    with time_machine.travel(1_000_000.0, tick=False) as traveller:
        yield traveller


def _value(namespace: str, state: str) -> float:
    return prometheus_client.REGISTRY.get_sample_value("circuit_breaker_state", {"namespace": namespace, "state": state}) or 0.0


def _counts(namespace: str) -> dict[str, float]:
    return {state: _value(namespace, state) for state in _STATES}


def _namespace() -> str:
    return f"lab6403-{uuid.uuid4().hex}"


def _breaker(namespace: str, **config: Any) -> CircuitBreaker:
    return CircuitBreaker(CircuitBreakerConfig(**config), namespace=namespace)


def _open(breaker: CircuitBreaker) -> None:
    for _ in range(breaker.config.failure_threshold):
        breaker.record_failure()
    assert breaker.state == CircuitState.OPEN


class _Backend:
    def __init__(self) -> None:
        self.store: dict[str, bytes] = {}

    def get(self, key: str) -> Optional[bytes]:
        return self.store.get(key)

    def set(self, key: str, value: bytes, ttl: Optional[int] = None) -> None:
        self.store[key] = bytes(value)

    def delete(self, key: str) -> bool:
        return self.store.pop(key, None) is not None


class TestDecoratedFunction:
    def test_open_breaker_is_exported(self, monkeypatch: pytest.MonkeyPatch):
        def unreachable() -> Any:
            raise ConnectionError("backend unreachable")

        monkeypatch.setattr(wrapper_module, "_resolve_lazy_backend", unreachable)
        namespace = _namespace()

        @cache(ttl=300, l1_enabled=False, namespace=namespace)
        def fn(x: int) -> int:
            return x

        for i in range(CircuitBreakerConfig().failure_threshold):
            assert fn(i) == i  # degrades to uncached, never raises

        exposition = prometheus_client.generate_latest(prometheus_client.REGISTRY).decode()
        assert f'circuit_breaker_state{{namespace="{namespace}",state="OPEN"}} 1.0' in exposition

    def test_healthy_sibling_does_not_mask_open_breaker(self, monkeypatch: pytest.MonkeyPatch):
        namespace = _namespace()
        broken = _breaker(namespace)
        _open(broken)

        monkeypatch.setattr(wrapper_module, "_resolve_lazy_backend", lambda: _Backend())

        @cache(ttl=300, l1_enabled=False, namespace=namespace)
        def sibling(x: int) -> int:
            return x

        assert sibling(1) == 1
        assert _counts(namespace) == {"CLOSED": 1.0, "OPEN": 1.0, "HALF_OPEN": 0.0}


class TestStateMachine:
    def test_counts_follow_transitions_and_sum_to_live_breakers(self, clock):
        namespace = _namespace()
        breaker = _breaker(namespace, timeout_seconds=_TIMEOUT, half_open_requests=1, success_threshold=1)
        other = _breaker(namespace)
        assert _counts(namespace) == {"CLOSED": 2.0, "OPEN": 0.0, "HALF_OPEN": 0.0}

        _open(breaker)
        assert _counts(namespace) == {"CLOSED": 1.0, "OPEN": 1.0, "HALF_OPEN": 0.0}

        clock.shift(_PAST_TIMEOUT)
        assert breaker.should_attempt_call()
        assert breaker.state == CircuitState.HALF_OPEN
        assert _counts(namespace) == {"CLOSED": 1.0, "OPEN": 0.0, "HALF_OPEN": 1.0}

        breaker.record_success()
        assert breaker.state == CircuitState.CLOSED
        assert _counts(namespace) == {"CLOSED": 2.0, "OPEN": 0.0, "HALF_OPEN": 0.0}
        assert other.state == CircuitState.CLOSED

    def test_half_open_cycle_restart_leaves_counts(self, clock):
        namespace = _namespace()
        breaker = _breaker(namespace, timeout_seconds=_TIMEOUT, half_open_requests=1)
        _open(breaker)
        clock.shift(_PAST_TIMEOUT)
        assert breaker.should_attempt_call()  # OPEN -> HALF_OPEN, spends the budget
        before = _counts(namespace)
        assert before == {"CLOSED": 0.0, "OPEN": 0.0, "HALF_OPEN": 1.0}

        clock.shift(_PAST_TIMEOUT)
        assert breaker.should_attempt_call()  # spent cycle past timeout: HALF_OPEN -> HALF_OPEN
        assert breaker.state == CircuitState.HALF_OPEN
        assert _counts(namespace) == before

    def test_reset_on_closed_breaker_leaves_counts(self):
        namespace = _namespace()
        breaker = _breaker(namespace)
        breaker.reset()
        assert _counts(namespace) == {"CLOSED": 1.0, "OPEN": 0.0, "HALF_OPEN": 0.0}

    def test_reset_from_open(self):
        namespace = _namespace()
        breaker = _breaker(namespace)
        _open(breaker)
        breaker.reset()
        assert _counts(namespace) == {"CLOSED": 1.0, "OPEN": 0.0, "HALF_OPEN": 0.0}


def test_collected_breaker_leaves_the_count():
    namespace = _namespace()
    breaker = _breaker(namespace)
    _open(breaker)
    assert _value(namespace, "OPEN") == 1.0

    del breaker
    gc.collect()
    assert _counts(namespace) == {"CLOSED": 0.0, "OPEN": 0.0, "HALF_OPEN": 0.0}


def test_host_owned_name_does_not_break_transitions(monkeypatch: pytest.MonkeyPatch, clock):
    monkeypatch.setitem(async_metrics._metrics_cache, "circuit_breaker_state", async_metrics._NoopMetric())
    breaker = _breaker(_namespace(), timeout_seconds=_TIMEOUT, success_threshold=1)
    _open(breaker)
    clock.shift(_PAST_TIMEOUT)
    assert breaker.should_attempt_call()
    breaker.record_success()
    assert breaker.state == CircuitState.CLOSED
    del breaker
    gc.collect()


# Each order runs in a fresh interpreter: the gauge registers once per process.
_COLLECTOR_FIRST = """
collector = AsyncMetricsCollector(sync_mode=False, auto_detect_mode=False)
collector.record_cache_operation(operation="get", namespace="ns", success=True, duration_ms=1.0)
collector.shutdown()
breaker = CircuitBreaker(CircuitBreakerConfig(), namespace="ns")
"""
_BREAKER_FIRST = """
breaker = CircuitBreaker(CircuitBreakerConfig(), namespace="ns")
collector = AsyncMetricsCollector(sync_mode=False, auto_detect_mode=False)
collector.record_cache_operation(operation="get", namespace="ns", success=True, duration_ms=1.0)
collector.shutdown()
"""


@pytest.mark.parametrize("order", [_COLLECTOR_FIRST, _BREAKER_FIRST], ids=["collector-first", "breaker-first"])
def test_breaker_and_collectors_share_one_gauge(order: str):
    script = (
        textwrap.dedent(
            """
        import logging, sys
        from prometheus_client import REGISTRY
        from cachekit.reliability.async_metrics import AsyncMetricsCollector
        from cachekit.reliability.circuit_breaker import CircuitBreaker, CircuitBreakerConfig

        warnings = []
        logging.getLogger("cachekit").addHandler(logging.Handler())
        logging.getLogger("cachekit").handlers[-1].emit = lambda r: warnings.append(r.getMessage())
        """
        )
        + textwrap.dedent(order)
        + textwrap.dedent(
            """
        families = [m for m in REGISTRY.collect() if m.name == "circuit_breaker_state"]
        assert len(families) == 1, families
        assert REGISTRY.get_sample_value("circuit_breaker_state", {"namespace": "ns", "state": "CLOSED"}) == 1.0
        assert not [w for w in warnings if "already registered" in w], warnings
        """
        )
    )
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=60)  # noqa: S603 (trusted: sys.executable + literal code)
    assert result.returncode == 0, result.stderr
