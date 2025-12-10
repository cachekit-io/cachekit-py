"""Unit tests for structured logging module."""

import json
import logging
import threading
import time
from unittest.mock import patch

import pytest

from cachekit.logging import (
    JsonFormatter,
    StructuredRedisLogger,
    get_structured_logger,
    mask_sensitive_patterns,
)


class TestSensitiveDataMasking:
    """Test PII masking functionality."""

    def test_mask_ssn_patterns(self):
        """Test SSN masking."""
        assert mask_sensitive_patterns("My SSN is 123-45-6789") == "My SSN is XXX-XX-XXXX"
        assert mask_sensitive_patterns("SSN: 123456789") == "SSN: XXXXXXXXX"

    def test_mask_credit_card_patterns(self):
        """Test credit card masking."""
        assert mask_sensitive_patterns("Card: 1234-5678-9012-3456") == "Card: XXXX-XXXX-XXXX-XXXX"
        assert mask_sensitive_patterns("Card: 1234567890123456") == "Card: XXXX-XXXX-XXXX-XXXX"

    def test_mask_email_addresses(self):
        """Test email masking."""
        assert mask_sensitive_patterns("Email: user@example.com") == "Email: XXX@XXX.XXX"
        assert mask_sensitive_patterns("Contact: john.doe+tag@company.co.uk") == "Contact: XXX@XXX.XXX"

    def test_mask_phone_numbers(self):
        """Test phone number masking."""
        assert mask_sensitive_patterns("Call: 123-456-7890") == "Call: XXX-XXX-XXXX"
        assert mask_sensitive_patterns("Phone: (123) 456-7890") == "Phone: (XXX) XXX-XXXX"
        assert mask_sensitive_patterns("Tel: 123.456.7890") == "Tel: XXX-XXX-XXXX"

    def test_mask_api_keys(self):
        """Test API key masking."""
        long_key = "sk_test_4eC39HqLyjWDarjtT1zdp7dc"
        assert mask_sensitive_patterns(f"API Key: {long_key}") == "API Key: XXXXX...XXXXX"

    def test_mask_jwt_tokens(self):
        """Test JWT token masking."""
        jwt = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIiwibmFtZSI6IkpvaG4gRG9lIiwiaWF0IjoxNTE2MjM5MDIyfQ.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
        assert mask_sensitive_patterns(f"Token: {jwt}") == "Token: XXX.XXX.XXX"

    def test_mask_multiple_patterns(self):
        """Test masking multiple patterns in one string."""
        text = "User email@test.com with SSN 123-45-6789 called from 555-123-4567"
        expected = "User XXX@XXX.XXX with SSN XXX-XX-XXXX called from XXX-XXX-XXXX"
        assert mask_sensitive_patterns(text) == expected

    def test_empty_string(self):
        """Test masking empty string."""
        assert mask_sensitive_patterns("") == ""
        assert mask_sensitive_patterns(None) is None


class TestStructuredRedisLogger:
    """Test StructuredRedisLogger functionality."""

    @pytest.fixture
    def logger(self):
        """Create a test logger instance."""
        return StructuredRedisLogger("test_logger", mask_sensitive=True)

    @pytest.fixture
    def logger_no_mask(self):
        """Create a test logger without masking."""
        return StructuredRedisLogger("test_logger_no_mask", mask_sensitive=False)

    def test_logger_initialization(self, logger):
        """Test logger initialization."""
        assert logger.mask_sensitive is True
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

    def test_mask_sensitive_data(self, logger, logger_no_mask):
        """Test sensitive data masking."""
        sensitive = "email@test.com"

        # With masking enabled
        assert logger._mask_sensitive_data(sensitive) == "XXX@XXX.XXX"

        # With masking disabled
        assert logger_no_mask._mask_sensitive_data(sensitive) == sensitive

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
        assert extra["cache_key"] == "user:XXX@XXX.XXX"  # Masked
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

    @patch("cachekit.logging.logging.Logger.log")
    def test_redis_operation_failed_override(self, mock_log, logger):
        """Test redis_operation_failed override."""
        error = ValueError("Test error")
        logger.redis_operation_failed("get", "test_key", error)

        mock_log.assert_called_once()
        extra = mock_log.call_args[1]["extra"]["structured"]
        assert extra["operation"] == "get"
        assert extra["error"] == "Test error"
        assert extra["error_type"] == "ValueError"

    @patch("cachekit.logging.logging.Logger.log")
    def test_cache_hit_override(self, mock_log, logger):
        """Test cache_hit override."""
        logger.cache_hit("test_key", source="memory")

        mock_log.assert_called_once()
        extra = mock_log.call_args[1]["extra"]["structured"]
        assert extra["operation"] == "get"
        assert extra["hit"] is True
        assert extra["source"] == "memory"

    @patch("cachekit.logging.logging.Logger.log")
    def test_cache_miss_override(self, mock_log, logger):
        """Test cache_miss override."""
        logger.cache_miss("test_key")

        mock_log.assert_called_once()
        extra = mock_log.call_args[1]["extra"]["structured"]
        assert extra["operation"] == "get"
        assert extra["hit"] is False

    @patch("cachekit.logging.logging.Logger.log")
    def test_cache_stored_override(self, mock_log, logger):
        """Test cache_stored override."""
        logger.cache_stored("test_key", ttl=300)

        mock_log.assert_called_once()
        extra = mock_log.call_args[1]["extra"]["structured"]
        assert extra["operation"] == "set"
        assert extra["ttl"] == 300

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


class TestJsonFormatter:
    """Test JSON formatter functionality."""

    def test_format_basic_record(self):
        """Test formatting basic log record."""
        formatter = JsonFormatter()

        record = logging.LogRecord(
            name="test.logger",
            level=logging.INFO,
            pathname="test.py",
            lineno=10,
            msg="Test message",
            args=(),
            exc_info=None,
        )

        output = formatter.format(record)
        data = json.loads(output)

        assert data["level"] == "INFO"
        assert data["logger"] == "test.logger"
        assert data["message"] == "Test message"
        assert "timestamp" in data
        assert "thread_id" in data

    def test_format_with_structured_context(self):
        """Test formatting with structured context."""
        formatter = JsonFormatter()

        record = logging.LogRecord(
            name="test.logger",
            level=logging.INFO,
            pathname="test.py",
            lineno=10,
            msg="Cache operation",
            args=(),
            exc_info=None,
        )

        # Add structured context
        record.structured = {"operation": "get", "cache_key": "test_key", "hit": True}

        output = formatter.format(record)
        data = json.loads(output)

        assert data["operation"] == "get"
        assert data["cache_key"] == "test_key"
        assert data["hit"] is True

    def test_format_with_exception(self):
        """Test formatting with exception info."""
        formatter = JsonFormatter()

        try:
            raise ValueError("Test exception")
        except ValueError:
            import sys

            exc_info = sys.exc_info()

        record = logging.LogRecord(
            name="test.logger",
            level=logging.ERROR,
            pathname="test.py",
            lineno=10,
            msg="Error occurred",
            args=(),
            exc_info=exc_info,
        )

        output = formatter.format(record)
        data = json.loads(output)

        assert "exception" in data
        assert "ValueError: Test exception" in data["exception"]


class TestFactoryFunction:
    """Test factory function."""

    def test_get_structured_logger(self):
        """Test get_structured_logger factory."""
        logger1 = get_structured_logger("test1")
        assert isinstance(logger1, StructuredRedisLogger)
        assert logger1.mask_sensitive is True

        logger2 = get_structured_logger("test2", mask_sensitive=False)
        assert isinstance(logger2, StructuredRedisLogger)
        assert logger2.mask_sensitive is False
