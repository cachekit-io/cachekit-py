"""Unit tests for structured logging module."""

import logging
import threading
import time
from unittest.mock import patch

import pytest

from cachekit.logging import (
    StructuredLogger,
    get_structured_logger,
)


class TestStructuredLogger:
    """Test StructuredLogger functionality."""

    @pytest.fixture
    def logger(self):
        """Create a test logger instance."""
        return StructuredLogger("test_logger")

    def test_logger_initialization(self, logger):
        """Test logger initialization."""
        assert hasattr(logger, "_context")
        assert isinstance(logger._context, threading.local)

    def test_trace_id_management(self, logger):
        """Test trace ID setting and clearing."""
        # Set trace ID
        trace_id = "test-trace-123"
        logger.set_trace_id(trace_id)
        assert logger._context.trace_id == trace_id

        # Clear trace ID
        logger.clear_trace_id()
        assert not hasattr(logger._context, "trace_id")

    def test_get_context(self, logger):
        """Test context generation."""
        # Without trace ID - should not include trace_id key
        context = logger._get_context()
        assert "trace_id" not in context
        assert isinstance(context["timestamp"], float)
        assert isinstance(context["thread_id"], int)

        # With trace ID
        trace_id = "test-trace-456"
        logger.set_trace_id(trace_id)
        context = logger._get_context()
        assert context["trace_id"] == trace_id

    def test_cache_key_always_redacted(self, logger):
        """cache_operation always redacts the key via digest (CWE-532, LAB-304)."""
        from unittest.mock import patch as _patch

        from cachekit.hash_utils import redact_cache_key

        sensitive = "ns:tenant-42:func:app.f:args:email@test.com:v1"
        with _patch("cachekit.logging.logging.Logger.log") as mock_log:
            logger.cache_operation("get", sensitive, hit=True)
        extra = mock_log.call_args[1]["extra"]["structured"]
        assert extra["cache_key"] == redact_cache_key(sensitive)
        assert sensitive not in str(extra)

    @patch("cachekit.logging.logging.Logger.log")
    def test_cache_operation_logging(self, mock_log, logger):
        """Test cache operation logging."""
        logger.set_trace_id("trace-789")

        logger.cache_operation(
            "get",
            "user:email@test.com",
            namespace="users",
            serializer="orjson",
            duration_ms=1.5,
            hit=True,
        )

        mock_log.assert_called_once()
        call_args = mock_log.call_args

        # Check log level
        assert call_args[0][0] == logging.INFO
        assert call_args[0][1] == "cache_operation"

        # Check structured context
        extra = call_args[1]["extra"]["structured"]
        assert extra["operation"] == "get"
        from cachekit.hash_utils import redact_cache_key

        assert extra["cache_key"] == redact_cache_key("user:email@test.com")  # Redacted digest (CWE-532)
        assert extra["namespace"] == "users"
        assert extra["serializer"] == "orjson"
        assert extra["duration_ms"] == 1.5
        assert extra["hit"] is True
        assert extra["trace_id"] == "trace-789"

    @patch("cachekit.logging.logging.Logger.log")
    def test_cache_operation_error_logging(self, mock_log, logger):
        """Test error logging."""
        logger.cache_operation("set", "key123", error="Connection timeout", error_type="TimeoutError")

        mock_log.assert_called_once()
        call_args = mock_log.call_args

        # Should log as ERROR
        assert call_args[0][0] == logging.ERROR

        # Check error context
        extra = call_args[1]["extra"]["structured"]
        assert extra["error"] == "Connection timeout"
        assert extra["error_type"] == "TimeoutError"

    def test_thread_safety(self, logger):
        """Test thread-local context isolation."""
        results = {}

        def set_and_check_trace_id(trace_id, thread_name):
            logger.set_trace_id(trace_id)
            time.sleep(0.01)  # Simulate some work
            context = logger._get_context()
            results[thread_name] = context["trace_id"]

        # Create threads with different trace IDs
        thread1 = threading.Thread(target=set_and_check_trace_id, args=("trace-1", "thread1"))
        thread2 = threading.Thread(target=set_and_check_trace_id, args=("trace-2", "thread2"))

        thread1.start()
        thread2.start()
        thread1.join()
        thread2.join()

        # Each thread should have its own trace ID
        assert results["thread1"] == "trace-1"
        assert results["thread2"] == "trace-2"


class TestFactoryFunction:
    """Test factory function."""

    def test_get_structured_logger(self):
        """Test get_structured_logger factory returns one cached instance per name."""
        logger1 = get_structured_logger("test1")
        assert isinstance(logger1, StructuredLogger)

        logger1_again = get_structured_logger("test1")
        assert logger1_again is logger1
