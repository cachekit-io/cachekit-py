"""Unit tests for correlation tracking module."""

import threading
import time
import uuid
from unittest.mock import Mock

from cachekit.monitoring.correlation_tracking import (
    CorrelationTracker,
    LoggerIntegratedTracker,
    StructuredLoggerProtocol,
    clear_correlation_id,
    correlation_context,
    generate_correlation_id,
    get_correlation_id,
    set_correlation_id,
)


class TestCorrelationTracker:
    """Test correlation tracker functionality."""

    def test_generate_correlation_id_is_unique(self):
        """Test that generated correlation IDs are unique."""
        tracker = CorrelationTracker()

        id1 = tracker.generate_correlation_id()
        id2 = tracker.generate_correlation_id()

        assert id1 != id2
        # Validate UUID4 format
        assert uuid.UUID(id1).version == 4
        assert uuid.UUID(id2).version == 4

    def test_set_and_get_correlation_id(self):
        """Test setting and getting correlation ID."""
        tracker = CorrelationTracker()
        test_id = "test-correlation-id"

        # Initially no correlation ID
        assert tracker.get_correlation_id() is None

        # Set and get correlation ID
        tracker.set_correlation_id(test_id)
        assert tracker.get_correlation_id() == test_id

    def test_clear_correlation_id(self):
        """Test clearing correlation ID."""
        tracker = CorrelationTracker()
        test_id = "test-correlation-id"

        # Set and verify
        tracker.set_correlation_id(test_id)
        assert tracker.get_correlation_id() == test_id

        # Clear and verify
        tracker.clear_correlation_id()
        assert tracker.get_correlation_id() is None

    def test_clear_correlation_id_when_none_set(self):
        """Test clearing correlation ID when none is set."""
        tracker = CorrelationTracker()

        # Should not raise error
        tracker.clear_correlation_id()
        assert tracker.get_correlation_id() is None

    def test_thread_isolation(self):
        """Test that correlation IDs are isolated between threads."""
        tracker = CorrelationTracker()
        results = {}

        def worker(thread_id, correlation_id):
            """Worker function for thread testing."""
            tracker.set_correlation_id(correlation_id)
            # Small delay to allow other threads to run
            time.sleep(0.01)
            results[thread_id] = tracker.get_correlation_id()

        # Create multiple threads with different correlation IDs
        threads = []
        for i in range(5):
            thread_id = f"thread-{i}"
            correlation_id = f"correlation-{i}"
            thread = threading.Thread(target=worker, args=(thread_id, correlation_id))
            threads.append(thread)

        # Start all threads
        for thread in threads:
            thread.start()

        # Wait for all threads to complete
        for thread in threads:
            thread.join()

        # Verify each thread got its own correlation ID
        for i in range(5):
            thread_id = f"thread-{i}"
            expected_correlation_id = f"correlation-{i}"
            assert results[thread_id] == expected_correlation_id

    def test_correlation_context_manager(self):
        """Test correlation context manager functionality."""
        tracker = CorrelationTracker()
        test_id = "test-correlation-id"

        # Test with provided correlation ID
        with tracker.correlation_context(test_id) as context_id:
            assert context_id == test_id
            assert tracker.get_correlation_id() == test_id

        # After context, correlation ID should be cleared
        assert tracker.get_correlation_id() is None

    def test_correlation_context_manager_generates_id(self):
        """Test correlation context manager generates ID when none provided."""
        tracker = CorrelationTracker()

        with tracker.correlation_context() as context_id:
            assert context_id is not None
            assert tracker.get_correlation_id() == context_id
            # Validate UUID4 format
            assert uuid.UUID(context_id).version == 4

        # After context, correlation ID should be cleared
        assert tracker.get_correlation_id() is None

    def test_correlation_context_manager_restores_previous(self):
        """Test correlation context manager restores previous correlation ID."""
        tracker = CorrelationTracker()
        original_id = "original-id"
        context_id = "context-id"

        # Set initial correlation ID
        tracker.set_correlation_id(original_id)

        # Use context with different ID
        with tracker.correlation_context(context_id) as yielded_id:
            assert yielded_id == context_id
            assert tracker.get_correlation_id() == context_id

        # After context, original ID should be restored
        assert tracker.get_correlation_id() == original_id

    def test_correlation_context_manager_nested(self):
        """Test nested correlation context managers."""
        tracker = CorrelationTracker()
        outer_id = "outer-id"
        inner_id = "inner-id"

        with tracker.correlation_context(outer_id) as outer_yielded:
            assert outer_yielded == outer_id
            assert tracker.get_correlation_id() == outer_id

            with tracker.correlation_context(inner_id) as inner_yielded:
                assert inner_yielded == inner_id
                assert tracker.get_correlation_id() == inner_id

            # After inner context, outer ID should be restored
            assert tracker.get_correlation_id() == outer_id

        # After both contexts, no ID should be set
        assert tracker.get_correlation_id() is None


class TestLoggerIntegratedTracker:
    """Test logger integrated tracker functionality."""

    def test_initialization_without_logger(self):
        """Test initialization without structured logger."""
        tracker = LoggerIntegratedTracker()
        assert tracker.structured_logger is None

    def test_initialization_with_logger(self):
        """Test initialization with structured logger."""
        mock_logger = Mock(spec=StructuredLoggerProtocol)
        tracker = LoggerIntegratedTracker(mock_logger)
        assert tracker.structured_logger is mock_logger

    def test_set_correlation_id_syncs_with_logger(self):
        """Test that setting correlation ID syncs with structured logger."""
        mock_logger = Mock(spec=StructuredLoggerProtocol)
        tracker = LoggerIntegratedTracker(mock_logger)
        test_id = "test-correlation-id"

        tracker.set_correlation_id(test_id)

        # Verify both tracker and logger are updated
        assert tracker.get_correlation_id() == test_id
        mock_logger.set_trace_id.assert_called_once_with(test_id)

    def test_clear_correlation_id_syncs_with_logger(self):
        """Test that clearing correlation ID syncs with structured logger."""
        mock_logger = Mock(spec=StructuredLoggerProtocol)
        tracker = LoggerIntegratedTracker(mock_logger)
        test_id = "test-correlation-id"

        # Set and clear
        tracker.set_correlation_id(test_id)
        tracker.clear_correlation_id()

        # Verify both tracker and logger are updated
        assert tracker.get_correlation_id() is None
        mock_logger.clear_trace_id.assert_called_once()

    def test_set_correlation_id_without_logger(self):
        """Test setting correlation ID when no logger is configured."""
        tracker = LoggerIntegratedTracker()
        test_id = "test-correlation-id"

        # Should not raise error
        tracker.set_correlation_id(test_id)
        assert tracker.get_correlation_id() == test_id

    def test_clear_correlation_id_without_logger(self):
        """Test clearing correlation ID when no logger is configured."""
        tracker = LoggerIntegratedTracker()
        test_id = "test-correlation-id"

        tracker.set_correlation_id(test_id)
        # Should not raise error
        tracker.clear_correlation_id()
        assert tracker.get_correlation_id() is None

    def test_set_structured_logger(self):
        """Test setting structured logger after initialization."""
        tracker = LoggerIntegratedTracker()
        mock_logger = Mock(spec=StructuredLoggerProtocol)

        tracker.set_structured_logger(mock_logger)
        assert tracker.structured_logger is mock_logger

    def test_set_structured_logger_syncs_existing_id(self):
        """Test that setting logger syncs existing correlation ID."""
        tracker = LoggerIntegratedTracker()
        test_id = "test-correlation-id"
        mock_logger = Mock(spec=StructuredLoggerProtocol)

        # Set correlation ID before logger
        tracker.set_correlation_id(test_id)

        # Set logger - should sync existing ID
        tracker.set_structured_logger(mock_logger)
        mock_logger.set_trace_id.assert_called_once_with(test_id)

    def test_set_structured_logger_no_sync_when_no_id(self):
        """Test that setting logger doesn't sync when no correlation ID exists."""
        tracker = LoggerIntegratedTracker()
        mock_logger = Mock(spec=StructuredLoggerProtocol)

        tracker.set_structured_logger(mock_logger)
        mock_logger.set_trace_id.assert_not_called()


class TestModuleLevelFunctions:
    """Test module-level convenience functions."""

    def test_generate_correlation_id(self):
        """Test module-level generate_correlation_id function."""
        id1 = generate_correlation_id()
        id2 = generate_correlation_id()

        assert id1 != id2
        # Validate UUID4 format
        assert uuid.UUID(id1).version == 4
        assert uuid.UUID(id2).version == 4

    def test_set_and_get_correlation_id(self):
        """Test module-level set/get correlation ID functions."""
        test_id = "test-module-correlation-id"

        # Clear any existing ID first
        clear_correlation_id()
        assert get_correlation_id() is None

        set_correlation_id(test_id)
        assert get_correlation_id() == test_id

    def test_clear_correlation_id_function(self):
        """Test module-level clear correlation ID function."""
        test_id = "test-module-correlation-id"

        set_correlation_id(test_id)
        assert get_correlation_id() == test_id

        clear_correlation_id()
        assert get_correlation_id() is None

    def test_correlation_context_function(self):
        """Test module-level correlation context function."""
        test_id = "test-module-context-id"

        # Clear any existing ID first
        clear_correlation_id()

        with correlation_context(test_id) as context_id:
            assert context_id == test_id
            assert get_correlation_id() == test_id

        assert get_correlation_id() is None

    def test_correlation_context_function_generates_id(self):
        """Test module-level correlation context generates ID when none provided."""
        # Clear any existing ID first
        clear_correlation_id()

        with correlation_context() as context_id:
            assert context_id is not None
            assert get_correlation_id() == context_id
            # Validate UUID4 format
            assert uuid.UUID(context_id).version == 4

        assert get_correlation_id() is None


class TestIntegrationScenarios:
    """Test realistic integration scenarios."""

    def test_request_scoped_correlation(self):
        """Test typical request-scoped correlation tracking."""
        tracker = CorrelationTracker()

        # Simulate handling multiple requests
        request_ids = ["req-1", "req-2", "req-3"]
        results = {}

        def handle_request(request_id):
            """Simulate request handling with correlation tracking."""
            with tracker.correlation_context() as correlation_id:
                # Simulate cache operations during request
                results[request_id] = {"correlation_id": correlation_id, "cache_operations": []}

                # Multiple cache operations should share correlation ID
                for _ in range(3):
                    current_id = tracker.get_correlation_id()
                    results[request_id]["cache_operations"].append(current_id)
                    assert current_id == correlation_id

        # Handle requests
        for request_id in request_ids:
            handle_request(request_id)

        # Verify each request had unique correlation ID
        correlation_ids = [results[req]["correlation_id"] for req in request_ids]
        assert len(set(correlation_ids)) == len(correlation_ids)  # All unique

        # Verify all cache operations within request shared correlation ID
        for request_id in request_ids:
            cache_ops = results[request_id]["cache_operations"]
            expected_id = results[request_id]["correlation_id"]
            assert all(op_id == expected_id for op_id in cache_ops)

    def test_concurrent_request_isolation(self):
        """Test that concurrent requests have isolated correlation IDs."""
        tracker = CorrelationTracker()
        results = {}

        def handle_concurrent_request(request_id):
            """Handle request with correlation tracking."""
            with tracker.correlation_context() as correlation_id:
                # Store initial correlation ID
                results[request_id] = correlation_id

                # Simulate some work
                time.sleep(0.01)

                # Verify correlation ID is still the same
                assert tracker.get_correlation_id() == correlation_id
                assert results[request_id] == correlation_id

        # Start multiple concurrent requests
        threads = []
        request_ids = [f"concurrent-req-{i}" for i in range(10)]

        for request_id in request_ids:
            thread = threading.Thread(target=handle_concurrent_request, args=(request_id,))
            threads.append(thread)

        # Start all threads
        for thread in threads:
            thread.start()

        # Wait for completion
        for thread in threads:
            thread.join()

        # Verify all requests had unique correlation IDs
        correlation_ids = list(results.values())
        assert len(set(correlation_ids)) == len(correlation_ids)

    def test_logger_integration_workflow(self):
        """Test complete workflow with logger integration."""
        mock_logger = Mock(spec=StructuredLoggerProtocol)
        tracker = LoggerIntegratedTracker(mock_logger)

        # Simulate request with multiple cache operations
        with tracker.correlation_context() as correlation_id:
            # Logger should be updated when context starts
            mock_logger.set_trace_id.assert_called_with(correlation_id)

            # Simulate cache operations that might log
            for _ in range(3):
                current_id = tracker.get_correlation_id()
                assert current_id == correlation_id

        # Logger should be cleared when context ends
        mock_logger.clear_trace_id.assert_called_once()
