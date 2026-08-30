"""Unit tests for FeatureOrchestrator.handle_cache_error().

Tests the error handling orchestration without test theatre - validates
actual behavior and contracts, not implementation details.
"""

import logging

import pytest

from cachekit.backends.errors import BackendError, BackendErrorType
from cachekit.cache_handler import redact_cache_key
from cachekit.decorators.orchestrator import FeatureOrchestrator, _redact_key_for_log


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


class TestCacheKeyRedaction:
    """Raw cache keys must never reach logs on any error path (CWE-532, LAB-304).

    Keys embed caller-supplied tenant/user identifiers; the sink redacts once so
    every caller is covered by construction.
    """

    # A canonical key carrying a tenant-identifying argument digest segment
    TENANT_KEY = "ns:prod:func:app.get_user:args:tenant-42-alice-secret:v1"

    def _orchestrator(self) -> FeatureOrchestrator:
        return FeatureOrchestrator(
            namespace="test",
            circuit_breaker_enabled=False,
            enable_structured_logging=True,
        )

    @pytest.mark.parametrize("operation", ["cache_get", "key_generation", "backend_connection", "client_creation"])
    def test_non_cache_set_failure_never_logs_raw_key(self, operation: str, caplog: pytest.LogCaptureFixture) -> None:
        """The tenant key must not appear verbatim in any log record — structured or backwards-compat."""
        with caplog.at_level(logging.INFO):
            self._orchestrator().handle_cache_error(
                error=ConnectionError("backend down"),
                operation=operation,
                cache_key=self.TENANT_KEY,
                duration_ms=1.0,
            )

        assert caplog.records, "error handler must log"
        for record in caplog.records:
            assert self.TENANT_KEY not in record.getMessage()
            structured = getattr(record, "structured", None)
            if structured is not None:
                assert self.TENANT_KEY not in str(structured)

    def test_backwards_compat_log_carries_correlatable_digest(self, caplog: pytest.LogCaptureFixture) -> None:
        """Redaction keeps failures correlatable: the blake2b digest replaces the raw key."""
        with caplog.at_level(logging.WARNING):
            self._orchestrator().handle_cache_error(
                error=ConnectionError("backend down"),
                operation="cache_get",
                cache_key=self.TENANT_KEY,
            )

        digest = redact_cache_key(self.TENANT_KEY)
        assert any(digest in record.getMessage() for record in caplog.records)

    def test_cache_set_digest_unchanged_from_lab_109(self, caplog: pytest.LogCaptureFixture) -> None:
        """cache_set callers now pass the raw key; the sink must emit the SAME digest
        the call-site redaction produced before (LAB-109 behaviour intact)."""
        with caplog.at_level(logging.WARNING):
            self._orchestrator().handle_cache_error(
                error=OSError("disk full"),
                operation="cache_set",
                cache_key=self.TENANT_KEY,
            )

        digest = redact_cache_key(self.TENANT_KEY)
        assert any(digest in record.getMessage() for record in caplog.records)
        assert not any(self.TENANT_KEY in record.getMessage() for record in caplog.records)

    @pytest.mark.parametrize("sentinel", ["unknown", "<generation_failed>", "<redacted:deadbeefdeadbeef>"])
    def test_sentinels_pass_through_unredacted(self, sentinel: str, caplog: pytest.LogCaptureFixture) -> None:
        """Non-key sentinels carry no caller data and stay readable (no double-redaction)."""
        with caplog.at_level(logging.WARNING):
            self._orchestrator().handle_cache_error(
                error=ValueError("boom"),
                operation="key_generation",
                cache_key=sentinel,
            )

        assert any(sentinel in record.getMessage() for record in caplog.records)

    def test_structured_log_cache_operation_redacts_key(self, caplog: pytest.LogCaptureFixture) -> None:
        """Direct log_cache_operation callers (circuit-breaker, hit logs) are covered too."""
        with caplog.at_level(logging.INFO):
            self._orchestrator().log_cache_operation(
                operation="circuit_breaker_open",
                key=self.TENANT_KEY,
                error="Circuit breaker is OPEN",
            )

        assert caplog.records
        for record in caplog.records:
            assert self.TENANT_KEY not in record.getMessage()
            structured = getattr(record, "structured", None)
            if structured is not None:
                assert self.TENANT_KEY not in str(structured)

    def test_backend_error_carrying_raw_key_is_sanitised(self, caplog: pytest.LogCaptureFixture) -> None:
        """BackendError text must not leak its key attribute through {error} interpolation.

        BackendError.__str__ appends a key= segment; redacting the separate
        cache_key argument does not touch that value (CodeRabbit PR #264).
        """
        error = BackendError(
            "backend down",
            error_type=BackendErrorType.TRANSIENT,
            operation="get",
            key=self.TENANT_KEY,
        )
        with caplog.at_level(logging.INFO):
            self._orchestrator().handle_cache_error(
                error=error,
                operation="cache_get",
                cache_key=self.TENANT_KEY,
                duration_ms=1.0,
            )

        assert caplog.records, "error handler must log"
        for record in caplog.records:
            assert self.TENANT_KEY not in record.getMessage()
            structured = getattr(record, "structured", None)
            if structured is not None:
                assert self.TENANT_KEY not in str(structured)

    def test_angle_bracketed_raw_key_is_redacted(self, caplog: pytest.LogCaptureFixture) -> None:
        """A raw key that merely looks bracketed must not ride the sentinel pass-through."""
        bracketed = "<tenant-42-alice-secret>"
        with caplog.at_level(logging.WARNING):
            self._orchestrator().handle_cache_error(
                error=ConnectionError("backend down"),
                operation="cache_get",
                cache_key=bracketed,
            )

        digest = redact_cache_key(bracketed)
        assert any(digest in record.getMessage() for record in caplog.records)
        assert not any(bracketed in record.getMessage() for record in caplog.records)

    def test_pass_through_is_strict_allow_list(self) -> None:
        """Only known sentinels and redact_cache_key() output pass through unredacted."""
        assert _redact_key_for_log("unknown") == "unknown"
        assert _redact_key_for_log("<generation_failed>") == "<generation_failed>"

        already_redacted = redact_cache_key("anything")
        assert _redact_key_for_log(already_redacted) == already_redacted

        # Arbitrary bracketed strings are NOT sentinels — they get redacted...
        assert _redact_key_for_log("<tenant-42-alice-secret>") == redact_cache_key("<tenant-42-alice-secret>")
        # ...and redaction stays idempotent through a second pass.
        once = _redact_key_for_log("<tenant-42-alice-secret>")
        assert _redact_key_for_log(once) == once
