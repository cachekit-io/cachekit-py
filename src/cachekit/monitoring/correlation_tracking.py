"""Correlation tracking for distributed request tracing.

This module provides utilities for generating and managing correlation IDs
across cache operations, enabling distributed tracing and request correlation
in multi-service environments.

Features:
- Unique correlation ID generation using UUID4
- Integration with structured logging infrastructure
- Thread-safe correlation context management
- Support for request-scoped correlation tracking
"""

import threading
import uuid
from collections.abc import Generator
from contextlib import contextmanager
from typing import Optional, Protocol


class StructuredLoggerProtocol(Protocol):
    """Protocol for structured loggers that support correlation tracking."""

    def set_trace_id(self, trace_id: str) -> None:
        """Set trace ID for correlation tracking."""
        ...

    def clear_trace_id(self) -> None:
        """Clear the current trace ID."""
        ...


class CorrelationTracker:
    """Manages correlation IDs for distributed request tracing.

    This class provides thread-safe correlation ID management with optional
    integration to structured logging systems for consistent request tracking
    across cache operations.

    Attributes:
        _context: Thread-local storage for correlation IDs

    Examples:
        Create tracker and generate correlation IDs:

        >>> tracker = CorrelationTracker()
        >>> corr_id = tracker.generate_correlation_id()
        >>> import uuid
        >>> uuid.UUID(corr_id)  # doctest: +ELLIPSIS
        UUID('...')

        Set and retrieve correlation ID:

        >>> tracker = CorrelationTracker()
        >>> tracker.set_correlation_id("test-123")
        >>> tracker.get_correlation_id()
        'test-123'
        >>> tracker.clear_correlation_id()
        >>> tracker.get_correlation_id() is None
        True

        Use context manager for scoped tracking:

        >>> tracker = CorrelationTracker()
        >>> with tracker.correlation_context("req-456") as corr_id:
        ...     tracker.get_correlation_id()
        'req-456'
        >>> tracker.get_correlation_id() is None
        True
    """

    def __init__(self):
        """Initialize correlation tracker with thread-local context."""
        self._context = threading.local()

    def generate_correlation_id(self) -> str:
        """Generate a unique correlation ID for request tracking.

        Uses UUID4 to ensure uniqueness across distributed systems while
        maintaining cryptographic randomness for security.

        Returns:
            A unique correlation ID string in UUID4 format
        """
        return str(uuid.uuid4())

    def set_correlation_id(self, correlation_id: str) -> None:
        """Set correlation ID for the current thread.

        Args:
            correlation_id: The correlation ID to set for this thread
        """
        self._context.correlation_id = correlation_id

    def get_correlation_id(self) -> Optional[str]:
        """Get the current thread's correlation ID.

        Returns:
            The correlation ID for this thread, or None if not set
        """
        return getattr(self._context, "correlation_id", None)

    def clear_correlation_id(self) -> None:
        """Clear the correlation ID for the current thread."""
        if hasattr(self._context, "correlation_id"):
            delattr(self._context, "correlation_id")

    @contextmanager
    def correlation_context(self, correlation_id: Optional[str] = None) -> Generator[str, None, None]:
        """Context manager for scoped correlation tracking.

        Args:
            correlation_id: Optional correlation ID to use. If None, generates a new one.

        Yields:
            The correlation ID for this context

        Example:
            with tracker.correlation_context() as correlation_id:
                # All operations in this context share the same correlation ID
                cache_operation()
        """
        if correlation_id is None:
            correlation_id = self.generate_correlation_id()

        # Save previous correlation ID if any
        previous_id = self.get_correlation_id()

        try:
            self.set_correlation_id(correlation_id)
            yield correlation_id
        finally:
            # Restore previous correlation ID
            if previous_id is not None:
                self.set_correlation_id(previous_id)
            else:
                self.clear_correlation_id()


class LoggerIntegratedTracker(CorrelationTracker):
    """Correlation tracker with structured logger integration.

    This class extends the base CorrelationTracker to automatically sync
    correlation IDs with structured logging systems for seamless request
    tracking across cache operations and log entries.

    Examples:
        Use without logger (behaves like base tracker):

        >>> tracker = LoggerIntegratedTracker()
        >>> tracker.set_correlation_id("log-trace-1")
        >>> tracker.get_correlation_id()
        'log-trace-1'

        Use with mock structured logger:

        >>> from unittest.mock import Mock
        >>> mock_logger = Mock()
        >>> tracker = LoggerIntegratedTracker(mock_logger)
        >>> tracker.set_correlation_id("synced-123")
        >>> mock_logger.set_trace_id.assert_called_once_with("synced-123")
        >>> tracker.clear_correlation_id()
        >>> mock_logger.clear_trace_id.assert_called_once()
    """

    def __init__(self, structured_logger: Optional[StructuredLoggerProtocol] = None):
        """Initialize tracker with optional structured logger integration.

        Args:
            structured_logger: Optional structured logger for automatic sync
        """
        super().__init__()
        self.structured_logger = structured_logger

    def set_correlation_id(self, correlation_id: str) -> None:
        """Set correlation ID and sync with structured logger.

        Args:
            correlation_id: The correlation ID to set for this thread
        """
        super().set_correlation_id(correlation_id)
        if self.structured_logger:
            self.structured_logger.set_trace_id(correlation_id)

    def clear_correlation_id(self) -> None:
        """Clear correlation ID and sync with structured logger."""
        super().clear_correlation_id()
        if self.structured_logger:
            self.structured_logger.clear_trace_id()

    def set_structured_logger(self, structured_logger: StructuredLoggerProtocol) -> None:
        """Set or update the structured logger integration.

        Args:
            structured_logger: The structured logger to integrate with
        """
        self.structured_logger = structured_logger

        # Sync current correlation ID if any
        current_id = self.get_correlation_id()
        if current_id and self.structured_logger:
            self.structured_logger.set_trace_id(current_id)


# Default instance for module-level usage
_default_tracker = CorrelationTracker()


def generate_correlation_id() -> str:
    """Generate a unique correlation ID using the default tracker.

    Returns:
        A unique correlation ID string in UUID4 format

    Examples:
        >>> import uuid
        >>> corr_id = generate_correlation_id()
        >>> uuid.UUID(corr_id)  # doctest: +ELLIPSIS
        UUID('...')
    """
    return _default_tracker.generate_correlation_id()


def set_correlation_id(correlation_id: str) -> None:
    """Set correlation ID using the default tracker.

    Args:
        correlation_id: The correlation ID to set for this thread

    Examples:
        >>> clear_correlation_id()  # Start fresh
        >>> set_correlation_id("my-trace-123")
        >>> get_correlation_id()
        'my-trace-123'
    """
    _default_tracker.set_correlation_id(correlation_id)


def get_correlation_id() -> Optional[str]:
    """Get the current thread's correlation ID using the default tracker.

    Returns:
        The correlation ID for this thread, or None if not set

    Examples:
        >>> clear_correlation_id()  # Ensure clean state
        >>> get_correlation_id() is None
        True
        >>> set_correlation_id("trace-abc")
        >>> get_correlation_id()
        'trace-abc'
    """
    return _default_tracker.get_correlation_id()


def clear_correlation_id() -> None:
    """Clear the correlation ID using the default tracker.

    Examples:
        >>> set_correlation_id("to-be-cleared")
        >>> get_correlation_id()
        'to-be-cleared'
        >>> clear_correlation_id()
        >>> get_correlation_id() is None
        True
    """
    _default_tracker.clear_correlation_id()


def correlation_context(correlation_id: Optional[str] = None) -> Generator[str, None, None]:
    """Context manager for scoped correlation tracking using the default tracker.

    Args:
        correlation_id: Optional correlation ID to use. If None, generates a new one.

    Yields:
        The correlation ID for this context

    Examples:
        Use with explicit correlation ID:

        >>> clear_correlation_id()  # Start fresh
        >>> with correlation_context("scoped-123") as corr_id:
        ...     corr_id
        'scoped-123'
        >>> get_correlation_id() is None  # Cleared after context
        True

        Auto-generates ID if not provided:

        >>> import uuid
        >>> with correlation_context() as corr_id:
        ...     uuid.UUID(corr_id)  # doctest: +ELLIPSIS
        UUID('...')
    """
    return _default_tracker.correlation_context(correlation_id)  # type: ignore[return-value]


__all__ = [
    "CorrelationTracker",
    "LoggerIntegratedTracker",
    "StructuredLoggerProtocol",
    "clear_correlation_id",
    "correlation_context",
    "generate_correlation_id",
    "get_correlation_id",
    "set_correlation_id",
]
