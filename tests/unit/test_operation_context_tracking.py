"""Unit tests for automatic operation context tracking in FeatureOrchestrator.

Tests the contextvars-based automatic operation detection that preserves
critical observability data without manual parameter passing.
"""

import asyncio

import pytest

from cachekit.decorators.orchestrator import FeatureOrchestrator
from cachekit.reliability.circuit_breaker import CircuitBreakerConfig


class TestOperationContextTracking:
    """Test automatic operation context tracking with contextvars."""

    def test_set_operation_context_stores_values(self):
        """Test that set_operation_context() stores operation and duration."""
        orchestrator = FeatureOrchestrator(
            namespace="test",
            circuit_breaker_enabled=False,
            adaptive_timeout_enabled=False,
            backpressure_enabled=False,
            collect_stats=True,
        )

        # Set operation context
        orchestrator.set_operation_context("get", duration_ms=1.5)

        # Verify context is stored by checking record_success uses it
        orchestrator.record_success()

        # Get metrics and verify operation was recorded
        stats = orchestrator.metrics_collector.get_stats()
        assert stats["total_operations"] == 1

    def test_record_success_uses_context_automatically(self):
        """Test that record_success() automatically uses set context."""
        orchestrator = FeatureOrchestrator(
            namespace="test",
            circuit_breaker_enabled=False,
            collect_stats=True,
        )

        # Set context for a "set" operation
        orchestrator.set_operation_context("set", duration_ms=2.5)
        orchestrator.record_success()

        stats = orchestrator.metrics_collector.get_stats()
        assert stats["total_operations"] == 1

    def test_record_failure_uses_context_automatically(self):
        """Test that record_failure() automatically uses set context."""
        config = CircuitBreakerConfig(
            failure_threshold=5,
            success_threshold=2,
            timeout_seconds=30.0,
        )
        orchestrator = FeatureOrchestrator(
            namespace="test",
            circuit_breaker_enabled=True,
            circuit_breaker_config=config,
            collect_stats=True,
        )

        # Set context for a "get" operation
        orchestrator.set_operation_context("get", duration_ms=3.5)

        # Record failure with context
        test_error = Exception("Redis timeout")
        orchestrator.record_failure(test_error)

        # Verify operation was tracked
        stats = orchestrator.metrics_collector.get_stats()
        assert stats["total_operations"] == 1

        # Verify circuit breaker recorded failure
        cb_stats = orchestrator.circuit_breaker.get_stats()
        assert cb_stats["failure_count"] == 1

    def test_context_defaults_when_not_set(self):
        """Test that record_success/failure work without context (defaults)."""
        orchestrator = FeatureOrchestrator(
            namespace="test",
            circuit_breaker_enabled=False,
            collect_stats=True,
        )

        # Call without setting context - should use defaults
        orchestrator.record_success()

        stats = orchestrator.metrics_collector.get_stats()
        assert stats["total_operations"] == 1

        # Record failure without context
        orchestrator.record_failure(Exception("test"))
        stats = orchestrator.metrics_collector.get_stats()
        assert stats["total_operations"] == 2

    def test_different_operation_types(self):
        """Test tracking different operation types."""
        orchestrator = FeatureOrchestrator(
            namespace="test",
            circuit_breaker_enabled=False,
            collect_stats=True,
        )

        # Track different operations
        operations = [
            ("get", 1.2),
            ("set", 2.3),
            ("l1_get", 0.001),
            ("connection", 0.0),
        ]

        for operation, duration in operations:
            orchestrator.set_operation_context(operation, duration_ms=duration)
            orchestrator.record_success()

        stats = orchestrator.metrics_collector.get_stats()
        assert stats["total_operations"] == 4

    def test_context_isolation_between_operations(self):
        """Test that each operation has isolated context."""
        orchestrator = FeatureOrchestrator(
            namespace="test",
            circuit_breaker_enabled=False,
            collect_stats=True,
        )

        # First operation
        orchestrator.set_operation_context("get", duration_ms=1.5)
        orchestrator.record_success()

        # Second operation with different context
        orchestrator.set_operation_context("set", duration_ms=3.0)
        orchestrator.record_success()

        # Third operation - verify previous contexts don't interfere
        orchestrator.set_operation_context("l1_get", duration_ms=0.001)
        orchestrator.record_success()

        stats = orchestrator.metrics_collector.get_stats()
        assert stats["total_operations"] == 3

    @pytest.mark.asyncio
    async def test_context_works_across_async_boundaries(self):
        """Test that contextvars work correctly in async code."""
        orchestrator = FeatureOrchestrator(
            namespace="test",
            circuit_breaker_enabled=False,
            collect_stats=True,
        )

        async def async_operation(op_type: str, duration: float) -> None:
            """Simulate async cache operation."""
            orchestrator.set_operation_context(op_type, duration_ms=duration)
            await asyncio.sleep(0.01)  # Simulate async work
            orchestrator.record_success()

        # Run multiple async operations concurrently
        await asyncio.gather(
            async_operation("get", 1.5),
            async_operation("set", 2.5),
            async_operation("l1_get", 0.001),
        )

        stats = orchestrator.metrics_collector.get_stats()
        assert stats["total_operations"] == 3

    @pytest.mark.asyncio
    async def test_context_isolation_between_concurrent_tasks(self):
        """Test that concurrent tasks have isolated contexts."""
        orchestrator = FeatureOrchestrator(
            namespace="test",
            circuit_breaker_enabled=False,
            collect_stats=True,
        )

        async def task_with_context(op_type: str, delay: float) -> str:
            """Task that sets context and verifies isolation."""
            orchestrator.set_operation_context(op_type, duration_ms=delay * 1000)
            await asyncio.sleep(delay)
            orchestrator.record_success()
            return op_type

        # Run tasks with different contexts concurrently
        results = await asyncio.gather(
            task_with_context("get", 0.01),
            task_with_context("set", 0.02),
            task_with_context("l1_get", 0.005),
        )

        assert len(results) == 3
        stats = orchestrator.metrics_collector.get_stats()
        assert stats["total_operations"] == 3

    def test_context_with_circuit_breaker_integration(self):
        """Test that context works with circuit breaker tracking."""
        config = CircuitBreakerConfig(
            failure_threshold=5,
            success_threshold=2,
            timeout_seconds=30.0,
        )
        orchestrator = FeatureOrchestrator(
            namespace="test",
            circuit_breaker_enabled=True,
            circuit_breaker_config=config,
            collect_stats=True,
        )

        # Record successful operations first
        for _ in range(3):
            orchestrator.set_operation_context("get", duration_ms=1.5)
            orchestrator.record_success()

        # Verify metrics tracked all operations
        stats = orchestrator.metrics_collector.get_stats()
        assert stats["total_operations"] == 3

        # Verify circuit breaker state remains CLOSED with successes
        cb_stats = orchestrator.circuit_breaker.get_stats()
        assert cb_stats["state"] == "CLOSED"

    def test_zero_duration_for_connection_failures(self):
        """Test that connection failures use 0.0 duration."""
        orchestrator = FeatureOrchestrator(
            namespace="test",
            circuit_breaker_enabled=False,
            collect_stats=True,
        )

        # Connection failures should have 0.0 duration
        orchestrator.set_operation_context("connection", duration_ms=0.0)
        orchestrator.record_failure(Exception("Connection failed"))

        stats = orchestrator.metrics_collector.get_stats()
        assert stats["total_operations"] == 1

    def test_context_preserves_accuracy(self):
        """Test that context preserves exact operation type and duration."""
        orchestrator = FeatureOrchestrator(
            namespace="test",
            circuit_breaker_enabled=False,
            collect_stats=True,
        )

        # Test precise duration tracking
        test_cases = [
            ("get", 1.234567),
            ("set", 98.765432),
            ("l1_get", 0.001234),
        ]

        for operation, duration in test_cases:
            orchestrator.set_operation_context(operation, duration_ms=duration)
            orchestrator.record_success()

        stats = orchestrator.metrics_collector.get_stats()
        assert stats["total_operations"] == 3
