"""Unit tests for structured logging module."""

import logging
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

    def test_get_context(self, logger):
        """Test context generation."""
        context = logger._get_context()
        assert set(context) == {"timestamp", "thread_id"}
        assert isinstance(context["timestamp"], float)
        assert isinstance(context["thread_id"], int)

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


class TestFactoryFunction:
    """Test factory function."""

    def test_get_structured_logger(self):
        """Test get_structured_logger factory returns one cached instance per name."""
        logger1 = get_structured_logger("test1")
        assert isinstance(logger1, StructuredLogger)

        logger1_again = get_structured_logger("test1")
        assert logger1_again is logger1
