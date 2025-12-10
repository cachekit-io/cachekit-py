"""Backend error types and classification.

This module defines error hierarchies for backend operations, enabling
circuit breaker and retry logic to make correct decisions without
Redis-specific exception handling.
"""

from __future__ import annotations

from enum import Enum
from typing import Optional


class BackendErrorType(str, Enum):
    """Error classification for circuit breaker and retry decisions.

    Inherits from str for JSON serialization and string comparisons.
    Each error type dictates a different retry/recovery strategy:

    - TRANSIENT: Temporary failure, retry with exponential backoff + jitter
    - PERMANENT: Unfixable error, fail fast without retry
    - TIMEOUT: Operation exceeded time limit, configurable retry strategy
    - AUTHENTICATION: Credential/auth issue, alert operations team
    - UNKNOWN: Unclassified error, assume transient and log for investigation

    Example:
        >>> error = BackendError("Connection lost", error_type=BackendErrorType.TRANSIENT)
        >>> if error.is_transient:
        ...     # Retry with exponential backoff
        ...     pass
    """

    TRANSIENT = "transient"
    PERMANENT = "permanent"
    TIMEOUT = "timeout"
    AUTHENTICATION = "authentication"
    UNKNOWN = "unknown"


class BackendError(Exception):
    """Base exception for all backend operations.

    The error_type field enables circuit breaker and retry logic to make
    correct decisions without inspecting exception types. This approach
    works with any backend (Redis, HTTP, DynamoDB, etc.).

    Designed for serializability across network boundaries. Contains only
    simple types (str, enum) and operation context for debugging.

    Attributes:
        message: Human-readable error message
        error_type: Error classification (see BackendErrorType)
        original_exception: The original exception that caused this error (if any)
        operation: The operation that failed (get, set, delete, exists)
        key: The cache key involved in the operation (optional, for debugging)

    Example:
        >>> from redis import ConnectionError as RedisConnectionError
        >>> try:
        ...     # Some Redis operation
        ...     pass
        ... except RedisConnectionError as exc:
        ...     raise BackendError(
        ...         "Redis connection failed",
        ...         error_type=BackendErrorType.TRANSIENT,
        ...         original_exception=exc,
        ...         operation="get",
        ...         key="user:123"
        ...     )
    """

    def __init__(
        self,
        message: str,
        error_type: BackendErrorType = BackendErrorType.UNKNOWN,
        original_exception: Optional[Exception] = None,
        operation: str | None = None,
        key: str | None = None,
    ):
        """Initialize BackendError with error classification and context.

        Args:
            message: Human-readable error message
            error_type: Error classification for retry/recovery logic
            original_exception: The original exception that caused this error
            operation: The operation that failed (get, set, delete, exists)
            key: The cache key involved in the operation
        """
        self.message = message
        self.error_type = error_type
        self.original_exception = original_exception
        self.operation = operation
        self.key = key
        super().__init__(self._format_message())

    def _format_message(self) -> str:
        """Format error message with operation context."""
        parts = [self.message]
        if self.operation:
            parts.append(f"operation={self.operation}")
        if self.key:
            # Truncate key for security/readability
            key_display = self.key[:50] + "..." if len(self.key) > 50 else self.key
            parts.append(f"key={key_display}")
        if self.error_type:
            parts.append(f"type={self.error_type.value}")
        return " | ".join(parts)

    @property
    def is_transient(self) -> bool:
        """Should trigger exponential backoff retry.

        Example:
            >>> error = BackendError("Temp failure", error_type=BackendErrorType.TRANSIENT)
            >>> if error.is_transient:
            ...     # Retry with backoff
            ...     pass
        """
        return self.error_type == BackendErrorType.TRANSIENT

    @property
    def is_permanent(self) -> bool:
        """Should fail fast, no retry.

        Example:
            >>> error = BackendError("Invalid key", error_type=BackendErrorType.PERMANENT)
            >>> if error.is_permanent:
            ...     # Don't retry, log and alert
            ...     pass
        """
        return self.error_type == BackendErrorType.PERMANENT

    @property
    def is_timeout(self) -> bool:
        """Configurable retry strategy.

        Example:
            >>> error = BackendError("Operation timeout", error_type=BackendErrorType.TIMEOUT)
            >>> if error.is_timeout:
            ...     # Retry with increased timeout
            ...     pass
        """
        return self.error_type == BackendErrorType.TIMEOUT

    @property
    def is_authentication(self) -> bool:
        """Should alert operations team.

        Example:
            >>> error = BackendError("Invalid creds", error_type=BackendErrorType.AUTHENTICATION)
            >>> if error.is_authentication:
            ...     # Alert ops, don't retry
            ...     pass
        """
        return self.error_type == BackendErrorType.AUTHENTICATION

    def __repr__(self) -> str:
        """Developer-friendly representation."""
        return f"BackendError({self.message!r}, error_type={self.error_type.value!r})"


class CapabilityNotAvailableError(BackendError):
    """Raised when code requires an optional protocol that backend doesn't implement.

    Used for hard requirements that can't be gracefully degraded. For example,
    if code requires distributed locking but the backend doesn't support it.

    Example:
        >>> from cachekit.backends.errors import CapabilityNotAvailableError
        >>> # Usage pattern:
        >>> # if not hasattr(backend, 'acquire_lock'):
        >>> #     raise CapabilityNotAvailableError("Backend doesn't support locking")
    """

    def __init__(self, message: str):
        """Initialize with permanent error classification.

        Args:
            message: Human-readable error message explaining missing capability
        """
        super().__init__(message, error_type=BackendErrorType.PERMANENT)
