"""Backend error classification for circuit breaker decisions.

This module provides error classification logic to distinguish between
transient errors (which should trigger circuit breaker) and permanent errors
(which indicate configuration issues).

Works with BackendError abstraction for backend-agnostic reliability.
"""

import logging

from cachekit.backends.errors import BackendError, BackendErrorType

logger = logging.getLogger(__name__)


class BackendErrorClassifier:
    """Classifies backend errors for circuit breaker decisions.

    This classifier works with BackendError abstraction, using the error_type
    field to make circuit breaker decisions without backend-specific knowledge.

    Examples:
        Transient errors trigger circuit breaker:

        >>> from cachekit.backends.errors import BackendError, BackendErrorType
        >>> transient = BackendError("Connection lost", error_type=BackendErrorType.TRANSIENT)
        >>> BackendErrorClassifier.is_circuit_breaker_failure(transient)
        True

        Permanent errors do not trigger circuit breaker:

        >>> permanent = BackendError("Invalid config", error_type=BackendErrorType.PERMANENT)
        >>> BackendErrorClassifier.is_circuit_breaker_failure(permanent)
        False

        Non-BackendError exceptions are ignored:

        >>> BackendErrorClassifier.is_circuit_breaker_failure(ValueError("app error"))
        False

        Get error category:

        >>> BackendErrorClassifier.get_error_category(transient)
        'transient'
        >>> BackendErrorClassifier.get_error_category(ValueError("app error"))
        'application'
    """

    @classmethod
    def is_circuit_breaker_failure(cls, error: Exception) -> bool:
        """Determine if error should trigger circuit breaker.

        Args:
            error: The exception to classify (must be BackendError)

        Returns:
            True if the error should count as a circuit breaker failure,
            False if it should be ignored by the circuit breaker
        """
        # Only BackendError with error_type field is supported
        if isinstance(error, BackendError):
            # Transient errors and timeouts should trigger circuit breaker
            if error.error_type in (BackendErrorType.TRANSIENT, BackendErrorType.TIMEOUT):
                return True
            # Permanent and authentication errors should not trigger circuit breaker
            if error.error_type in (BackendErrorType.PERMANENT, BackendErrorType.AUTHENTICATION):
                return False
            # Unknown errors should trigger circuit breaker (conservative approach)
            if error.error_type == BackendErrorType.UNKNOWN:
                return True

        # Non-BackendError exceptions should not trigger circuit breaker
        # (these are application logic errors, not infrastructure failures)
        return False

    @classmethod
    def get_error_category(cls, error: Exception) -> str:
        """Get a human-readable category for the error.

        Args:
            error: The exception to categorize (must be BackendError)

        Returns:
            String category: 'transient', 'permanent', 'timeout', 'authentication', 'unknown', or 'application'
        """
        # Only BackendError with error_type field is supported
        if isinstance(error, BackendError):
            return error.error_type.value

        return "application"
