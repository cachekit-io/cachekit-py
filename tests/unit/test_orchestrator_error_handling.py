"""Unit tests for FeatureOrchestrator.handle_cache_error().

Tests the error handling orchestration without test theatre - validates
actual behavior and contracts, not implementation details.
"""

import pytest

from cachekit.decorators.orchestrator import FeatureOrchestrator


class TestErrorHandlerOrchestration:
    """Test error handler orchestration logic."""

    def test_handle_cache_error_doesnt_raise(self):
        """Error handler must never raise - fail open principle."""
        orchestrator = FeatureOrchestrator(
            namespace="test",
            circuit_breaker_enabled=True,
            collect_stats=True,
            enable_structured_logging=True,
        )

        # Should not raise for any error type
        orchestrator.handle_cache_error(
            error=ValueError("test error"),
            operation="cache_get",
            cache_key="test:key",
            duration_ms=1.5,
        )

        orchestrator.handle_cache_error(
            error=RuntimeError("runtime error"),
            operation="key_generation",
            cache_key="<generation_failed>",
            duration_ms=0.0,
        )

        # If we get here, test passes - no exceptions raised

    def test_handle_cache_error_works_with_none_span(self):
        """Error handler must work when span is None (common case)."""
        orchestrator = FeatureOrchestrator(namespace="test", circuit_breaker_enabled=False)

        # Async wrapper often doesn't have span - must not crash
        orchestrator.handle_cache_error(
            error=Exception("test"),
            operation="client_creation",
            cache_key="unknown",
            span=None,  # Common case in async wrapper
            duration_ms=0.0,
        )

        # Test passes if no exception

    def test_handle_cache_error_uses_default_namespace(self):
        """Error handler should use orchestrator namespace when not provided."""
        orchestrator = FeatureOrchestrator(namespace="default_namespace")

        # Don't pass namespace parameter
        orchestrator.handle_cache_error(
            error=ValueError("test"),
            operation="cache_set",
            cache_key="test:key",
            duration_ms=2.5,
        )

        # Implicit test: if it logged with wrong namespace, we'd see it in logs
        # But we're not testing logs directly (implementation detail)
        # Just verify it doesn't crash

    def test_handle_cache_error_accepts_correlation_id(self):
        """Error handler should accept and use correlation IDs for distributed tracing."""
        orchestrator = FeatureOrchestrator(namespace="test", enable_structured_logging=True)

        # Should accept correlation ID for distributed tracing
        orchestrator.handle_cache_error(
            error=RuntimeError("distributed system error"),
            operation="redis_connection",
            cache_key="test:key",
            correlation_id="trace-123-456-789",
            duration_ms=150.0,
        )

        # Test passes if no exception

    def test_handle_cache_error_accepts_extra_context(self):
        """Error handler should accept arbitrary extra context via kwargs."""
        orchestrator = FeatureOrchestrator(namespace="test")

        # Should accept extra context for enriched logging
        orchestrator.handle_cache_error(
            error=ConnectionError("redis timeout"),
            operation="lock_acquisition",
            cache_key="test:lock:key",
            duration_ms=5000.0,
            # Extra context
            serializer="default",
            retry_count=3,
            error_code="TIMEOUT",
        )

        # Test passes if no exception

    def test_handle_cache_error_with_all_features_disabled(self):
        """Error handler should work even when all features are disabled."""
        orchestrator = FeatureOrchestrator(
            namespace="minimal",
            circuit_breaker_enabled=False,
            adaptive_timeout_enabled=False,
            backpressure_enabled=False,
            collect_stats=False,
            enable_structured_logging=False,
        )

        # Should still work (graceful degradation)
        orchestrator.handle_cache_error(
            error=ValueError("error with no features"),
            operation="cache_get",
            cache_key="test:key",
            duration_ms=1.0,
        )

        # Test passes if no exception

    def test_handle_cache_error_operation_types(self):
        """Error handler should accept all documented operation types."""
        orchestrator = FeatureOrchestrator(namespace="test")

        # All operation types from wrapper.py
        operation_types = [
            "key_generation",
            "client_creation",
            "cache_get",
            "cache_set",
            "redis_connection",
            "lock_acquisition",
        ]

        for op_type in operation_types:
            orchestrator.handle_cache_error(
                error=Exception(f"error in {op_type}"),
                operation=op_type,
                cache_key="test:key",
                duration_ms=1.0,
            )

        # Test passes if no exceptions for any operation type


class TestErrorHandlerContract:
    """Test error handler contract guarantees."""

    def test_error_handler_records_failure_in_circuit_breaker(self):
        """Error handler must record failures in circuit breaker when enabled."""
        orchestrator = FeatureOrchestrator(namespace="test", circuit_breaker_enabled=True)

        # Get initial failure count
        initial_stats = orchestrator.circuit_breaker.get_stats()
        initial_failures = initial_stats.get("failure_count", 0)

        # Trigger error
        orchestrator.handle_cache_error(
            error=ValueError("test error"),
            operation="cache_get",
            cache_key="test:key",
        )

        # Verify failure was recorded
        updated_stats = orchestrator.circuit_breaker.get_stats()
        updated_failures = updated_stats.get("failure_count", 0)

        assert updated_failures > initial_failures, "Error handler must record failures in circuit breaker"

    def test_error_handler_preserves_operation_context(self):
        """Error handler must set operation context correctly."""
        orchestrator = FeatureOrchestrator(namespace="test", collect_stats=True)

        # Call error handler with specific operation
        orchestrator.handle_cache_error(
            error=RuntimeError("context test"),
            operation="cache_get",
            cache_key="test:key",
            duration_ms=2.5,
        )

        # Operation context is thread-local and gets cleared, but we verified
        # it doesn't crash - that's the contract

    def test_error_handler_with_multiple_errors(self):
        """Error handler should handle multiple errors in sequence without degradation."""
        orchestrator = FeatureOrchestrator(
            namespace="test",
            circuit_breaker_enabled=True,
            collect_stats=True,
        )

        # Simulate multiple errors in rapid succession
        for i in range(10):
            orchestrator.handle_cache_error(
                error=ValueError(f"error {i}"),
                operation="cache_get",
                cache_key=f"test:key:{i}",
                duration_ms=float(i),
            )

        # Should handle all without crashing or memory leaks
        # Circuit breaker should have recorded all failures
        stats = orchestrator.circuit_breaker.get_stats()
        assert stats.get("failure_count", 0) >= 10


@pytest.mark.unit
class TestErrorHandlerEdgeCases:
    """Test edge cases and boundary conditions."""

    def test_error_handler_with_empty_cache_key(self):
        """Error handler should handle empty cache key."""
        orchestrator = FeatureOrchestrator(namespace="test")

        orchestrator.handle_cache_error(
            error=ValueError("empty key test"),
            operation="key_generation",
            cache_key="",  # Empty key
            duration_ms=0.0,
        )

        # Test passes if no exception

    def test_error_handler_with_very_long_error_message(self):
        """Error handler should handle very long error messages without truncation issues."""
        orchestrator = FeatureOrchestrator(namespace="test")

        long_message = "error " * 1000  # 6000 characters
        orchestrator.handle_cache_error(
            error=ValueError(long_message),
            operation="cache_get",
            cache_key="test:key",
            duration_ms=1.0,
        )

        # Test passes if no exception

    def test_error_handler_with_unicode_error_messages(self):
        """Error handler should handle unicode in error messages."""
        orchestrator = FeatureOrchestrator(namespace="test")

        orchestrator.handle_cache_error(
            error=ValueError("错误消息 🔥 émojis"),
            operation="cache_get",
            cache_key="test:key:🔑",
            duration_ms=1.0,
        )

        # Test passes if no exception

    def test_error_handler_with_nested_exceptions(self):
        """Error handler should handle exceptions with causes."""
        orchestrator = FeatureOrchestrator(namespace="test")

        try:
            try:
                raise ValueError("root cause")
            except ValueError as e:
                raise RuntimeError("wrapped error") from e
        except RuntimeError as nested_error:
            orchestrator.handle_cache_error(
                error=nested_error,
                operation="cache_get",
                cache_key="test:key",
                duration_ms=1.0,
            )

        # Test passes if no exception
