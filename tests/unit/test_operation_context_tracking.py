"""Unit tests for automatic operation context tracking in FeatureOrchestrator.

Tests the contextvars-based automatic operation detection that preserves
critical observability data without manual parameter passing. record_failure()
reads the context; record_success() feeds the circuit breaker only, because each
success site records its own fully labelled metric.
"""

import asyncio
import contextvars
from typing import Any

import pytest

from cachekit.decorators.orchestrator import FeatureOrchestrator
from cachekit.reliability.async_metrics import AsyncMetricsCollector
from cachekit.reliability.circuit_breaker import CircuitBreaker, CircuitBreakerConfig


@pytest.fixture
def recorded(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Capture every record that reaches the metrics collector."""
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(AsyncMetricsCollector, "record_cache_operation", lambda self, **kw: calls.append(kw))
    return calls


def _orchestrator(circuit_breaker_enabled: bool = False) -> FeatureOrchestrator:
    return FeatureOrchestrator(
        namespace="test",
        circuit_breaker_enabled=circuit_breaker_enabled,
        circuit_breaker_config=CircuitBreakerConfig(failure_threshold=5, success_threshold=2, timeout_seconds=30.0),
        backpressure_enabled=False,
        collect_stats=True,
    )


class TestOperationContextTracking:
    """Test automatic operation context tracking with contextvars."""

    def test_record_failure_uses_context_automatically(self, recorded: list[dict[str, Any]]):
        orchestrator = _orchestrator(circuit_breaker_enabled=True)

        orchestrator.set_operation_context("get", duration_ms=3.5)
        orchestrator.record_failure(Exception("Redis timeout"))

        assert recorded == [{"operation": "get", "namespace": "test", "success": False, "duration_ms": 3.5}]
        assert orchestrator.circuit_breaker.get_stats()["failure_count"] == 1

    def test_record_failure_defaults_when_context_not_set(self, recorded: list[dict[str, Any]]):
        # An empty Context, so no operation context set earlier on this thread leaks in.
        contextvars.Context().run(_orchestrator().record_failure, Exception("test"))

        assert [(c["operation"], c["duration_ms"]) for c in recorded] == [("cache_operation", 0.0)]

    def test_record_success_feeds_breaker_and_emits_no_metric(
        self, recorded: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
    ):
        """Every success site records its own labelled metric; a second record here double-counts.

        Feeding the breaker is record_success()'s only job: a HALF_OPEN probe slot is released
        only when its outcome reaches the breaker.
        """
        orchestrator = _orchestrator(circuit_breaker_enabled=True)
        on_success_calls: list[CircuitBreaker] = []
        monkeypatch.setattr(CircuitBreaker, "_on_success", lambda self: on_success_calls.append(self))

        for _ in range(3):
            orchestrator.set_operation_context("get", duration_ms=1.5)
            orchestrator.record_success()

        assert recorded == []
        assert on_success_calls == [orchestrator.circuit_breaker] * 3

    def test_context_isolation_between_operations(self, recorded: list[dict[str, Any]]):
        orchestrator = _orchestrator()

        for operation, duration in [("get", 1.234567), ("set", 98.765432), ("connection", 0.0)]:
            orchestrator.set_operation_context(operation, duration_ms=duration)
            orchestrator.record_failure(Exception(operation))

        assert [(c["operation"], c["duration_ms"]) for c in recorded] == [
            ("get", 1.234567),
            ("set", 98.765432),
            ("connection", 0.0),
        ]

    @pytest.mark.asyncio
    async def test_context_isolation_between_concurrent_tasks(self, recorded: list[dict[str, Any]]):
        orchestrator = _orchestrator()

        async def task_with_context(op_type: str, delay: float) -> None:
            orchestrator.set_operation_context(op_type, duration_ms=delay * 1000)
            await asyncio.sleep(delay)
            orchestrator.record_failure(Exception(op_type))

        await asyncio.gather(
            task_with_context("get", 0.01),
            task_with_context("set", 0.02),
            task_with_context("l1_get", 0.005),
        )

        # Each task's failure carries its own context, not whichever task set it last.
        assert sorted((c["operation"], c["duration_ms"]) for c in recorded) == [
            ("get", 10.0),
            ("l1_get", 5.0),
            ("set", 20.0),
        ]
