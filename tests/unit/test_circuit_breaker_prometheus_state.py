"""The breaker's state reaches Prometheus as ``circuit_breaker_state`` (LAB-6403).

The breaker used to write only an in-process dict, so the documented alert
``circuit_breaker_state{state="OPEN"} > 0`` could never fire. The gauge now
counts live breakers per namespace and state: a healthy function in the same
namespace cannot mask an OPEN one, and a collected breaker leaves the count.
"""

from __future__ import annotations

import gc
import os
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


def test_collected_breaker_leaves_the_count():
    namespace = _namespace()
    breaker = _breaker(namespace)
    _open(breaker)
    assert _value(namespace, "OPEN") == 1.0

    del breaker
    gc.collect()
    assert _counts(namespace) == {"CLOSED": 0.0, "OPEN": 0.0, "HALF_OPEN": 0.0}


def test_host_owned_name_does_not_break_the_breaker(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setitem(async_metrics._metrics_cache, "circuit_breaker_state", async_metrics._NoopMetric())
    retired = _breaker(_namespace())
    del retired
    gc.collect()
    _open(_breaker(_namespace()))  # retires the dead namespace through _NoopMetric.remove


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
        from prometheus_client import REGISTRY
        from cachekit.reliability.async_metrics import AsyncMetricsCollector
        from cachekit.reliability.circuit_breaker import CircuitBreaker, CircuitBreakerConfig
        """
        )
        + textwrap.dedent(order)
        + textwrap.dedent(
            """
        families = [m for m in REGISTRY.collect() if m.name == "circuit_breaker_state"]
        assert len(families) == 1, families
        assert REGISTRY.get_sample_value("circuit_breaker_state", {"namespace": "ns", "state": "CLOSED"}) == 1.0
        """
        )
    )
    result = _run(script)
    assert result.returncode == 0, result.stderr
    assert "already registered" not in result.stderr  # cachekit's warning reaches stderr via the last-resort handler


_TRANSIENT_NAMESPACES = """
import gc
from prometheus_client import REGISTRY
from cachekit.reliability.circuit_breaker import CircuitBreaker, CircuitBreakerConfig, _live_breakers

def series():
    return sum(1 for m in REGISTRY.collect() if m.name == "circuit_breaker_state" for _ in m.samples)

for i in range(2000):
    breaker = CircuitBreaker(CircuitBreakerConfig(), namespace=f"tenant{i}")
    breaker.cycle = breaker  # only cyclic GC frees it
    del breaker
gc.collect()
CircuitBreaker(CircuitBreakerConfig(), namespace="tenant0")  # the next breaker retires the dead ones
assert list(_live_breakers) == ["tenant0"], len(_live_breakers)
assert series() == 3, series()
"""


def test_collected_namespaces_leave_no_series():
    """Transient namespaces are retired, so scrape size tracks live namespaces, not every one ever seen."""
    result = _run(_TRANSIENT_NAMESPACES)
    assert result.returncode == 0, result.stderr


def _run(
    script: str, env: Optional[dict[str, str]] = None, cwd: Optional[os.PathLike[str]] = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=60, env=env, cwd=cwd)  # noqa: S603 (trusted: sys.executable + literal code)


_CYCLIC_GC = """
import gc, threading
from prometheus_client import REGISTRY, generate_latest
from cachekit.reliability.async_metrics import _metrics_cache
from cachekit.reliability.circuit_breaker import CircuitBreaker, CircuitBreakerConfig

def open_cyclic(namespace):
    breaker = CircuitBreaker(CircuitBreakerConfig(), namespace=namespace)
    breaker.cycle = breaker  # only cyclic GC can free it, at any allocation
    for _ in range(CircuitBreakerConfig().failure_threshold):
        breaker.record_failure()

# Collection while prometheus_client holds the gauge's lock, as it does inside labels().
open_cyclic("held")
with _metrics_cache["circuit_breaker_state"]._lock:
    gc.collect()

# Collection interleaved with new series and concurrent scrapes.
gc.set_threshold(1)
stop = threading.Event()
def scrape():
    while not stop.is_set():
        generate_latest(REGISTRY)
scraper = threading.Thread(target=scrape)
scraper.start()
for i in range(2000):
    open_cyclic(f"ns{i % 50}")
stop.set()
scraper.join()

gc.collect()
for ns in ["held"] + [f"ns{i}" for i in range(50)]:
    assert (REGISTRY.get_sample_value("circuit_breaker_state", {"namespace": ns, "state": "OPEN"}) or 0.0) == 0.0, ns
"""


def test_cyclic_garbage_collection_cannot_deadlock():
    """A breaker freed by cyclic GC mid-metric-update or mid-scrape leaves the count without hanging."""
    result = _run(_CYCLIC_GC)
    assert result.returncode == 0, result.stderr


def test_multiprocess_mode_exports_no_false_value(tmp_path):
    """The multiprocess collector would export a function gauge as 0, so the series is left out."""
    script = textwrap.dedent(
        """
        from prometheus_client import CollectorRegistry, generate_latest
        from prometheus_client.multiprocess import MultiProcessCollector
        from cachekit.reliability.circuit_breaker import CircuitBreaker, CircuitBreakerConfig
        breaker = CircuitBreaker(CircuitBreakerConfig(), namespace="ns")
        for _ in range(CircuitBreakerConfig().failure_threshold):
            breaker.record_failure()
        registry = CollectorRegistry()
        MultiProcessCollector(registry)
        assert b"circuit_breaker_state" not in generate_latest(registry), generate_latest(registry)
        """
    )
    result = _run(script, env={**os.environ, "PROMETHEUS_MULTIPROC_DIR": str(tmp_path)})
    assert result.returncode == 0, result.stderr


_FOLLOWS_PROMETHEUS_CLIENT = """
import os
from prometheus_client import REGISTRY, values
from cachekit.reliability.circuit_breaker import CircuitBreaker, CircuitBreakerConfig

os.environ.setdefault("PROMETHEUS_MULTIPROC_DIR", os.getcwd())  # after import: too late to matter
breaker = CircuitBreaker(CircuitBreakerConfig(), namespace="ns")
for _ in range(CircuitBreakerConfig().failure_threshold):
    breaker.record_failure()
print(values.ValueClass is not values.MutexValue,
      REGISTRY.get_sample_value("circuit_breaker_state", {"namespace": "ns", "state": "OPEN"}))
"""


@pytest.mark.parametrize(
    ("at_start", "expected"),
    [({"PROMETHEUS_MULTIPROC_DIR": ""}, "True None"), ({}, "False 1.0")],
    ids=["empty-at-start", "set-after-import"],
)
def test_multiprocess_detection_follows_prometheus_client(tmp_path, at_start: dict[str, str], expected: str):
    """prometheus_client picks its mode once, at import, by whether the variable is present."""
    env = {k: v for k, v in os.environ.items() if k.lower() != "prometheus_multiproc_dir"}
    result = _run(_FOLLOWS_PROMETHEUS_CLIENT, env={**env, **at_start}, cwd=tmp_path)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == expected


_C_FORK = """
import ctypes, os, threading, time
from cachekit.reliability import circuit_breaker as cb

held, release = threading.Event(), threading.Event()
def hold():
    with cb._live_lock():
        held.set()
        release.wait()
threading.Thread(target=hold, daemon=True).start()
held.wait()

pid = ctypes.CDLL(None, use_errno=True).fork()  # C-level fork: at-fork hooks do not run
if pid == 0:
    breaker = cb.CircuitBreaker(cb.CircuitBreakerConfig(), namespace="child")
    os._exit(0 if cb._count_in_state("child", cb.CircuitState.CLOSED) == 1 else 3)
release.set()
deadline = time.monotonic() + 20
while time.monotonic() < deadline:
    done, status = os.waitpid(pid, os.WNOHANG)
    if done:
        raise SystemExit(os.waitstatus_to_exitcode(status))
    time.sleep(0.05)
os.kill(pid, 9)
raise SystemExit("child hung on an inherited lock")
"""


@pytest.mark.skipif(not hasattr(os, "fork"), reason="needs fork")
def test_c_level_fork_child_does_not_inherit_a_held_lock():
    result = _run(_C_FORK)
    assert result.returncode == 0, result.stderr
