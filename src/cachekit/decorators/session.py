"""Session management for cache operations.

Provides unique session identifiers for tracking cache operations.
"""

from __future__ import annotations

import uuid

# Module-level session ID (lazy initialized)
_session_id: str | None = None


def get_session_id() -> str:
    """Get or create a unique session ID for cache operations.

    The session ID is a UUID4 that uniquely identifies this process/session.
    It's lazily initialized on first call and remains constant for the
    lifetime of the process.

    Returns:
        Unique session identifier string.

    Examples:
        Session ID is a valid UUID format:

        >>> import uuid
        >>> reset_session_id()  # Start fresh
        >>> session_id = get_session_id()
        >>> uuid.UUID(session_id)  # Validates UUID format  # doctest: +ELLIPSIS
        UUID('...')

        Same session ID returned on subsequent calls:

        >>> id1 = get_session_id()
        >>> id2 = get_session_id()
        >>> id1 == id2
        True
    """
    global _session_id
    if _session_id is None:
        _session_id = str(uuid.uuid4())
    return _session_id


def reset_session_id() -> None:
    """Reset session ID (primarily for testing).

    Forces a new session ID to be generated on the next get_session_id() call.

    Examples:
        Reset generates new session ID:

        >>> old_id = get_session_id()
        >>> reset_session_id()
        >>> new_id = get_session_id()
        >>> old_id != new_id
        True

        Safe to call multiple times:

        >>> reset_session_id()
        >>> reset_session_id()  # No error
    """
    global _session_id
    _session_id = None
