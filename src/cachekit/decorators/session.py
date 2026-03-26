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
