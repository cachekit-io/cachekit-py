"""Process-scoped session identity for cache operations.

Single source of truth for the process session UUID (and its start
timestamp) used for SaaS session correlation — both the per-function
session IDs built by the decorator stats tracker and the fallback
``X-CacheKit-Session-ID`` header derive from the UUID minted here.

PID-aware: a forked child detects the PID change and mints a fresh
identity, so parent and child never report under one session ID. Shared
IDs with independently-reset counters read as a replay attack to the
server's session validator, which strips the session tag from otherwise
healthy telemetry.
"""

from __future__ import annotations

import os
import threading
import time
import uuid

# Module-level state (regenerated on PID change)
_session_lock = threading.Lock()
_session_pid: int | None = None
_session_id: str | None = None
_session_start_ms: int | None = None


def _reset_session_state() -> None:
    """Discard the inherited identity in a newly forked child.

    Runs from the post-fork handler while the child is still
    single-threaded, so wholesale lock replacement is safe. The lock must
    be replaced, not reused: a parent thread holding it at fork time
    leaves it permanently locked in the child.
    """
    global _session_lock, _session_pid, _session_id, _session_start_ms
    _session_lock = threading.Lock()
    _session_pid = None
    _session_id = None
    _session_start_ms = None


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_reset_session_state)


def _ensure_session_initialized() -> None:
    """Ensure session identity exists for the current process.

    Regenerates the UUID and timestamp if the PID changed (fork or process
    restart). Thread-safe via lock; only the first thread per process does
    the initialization.
    """
    global _session_pid, _session_id, _session_start_ms

    current_pid = os.getpid()

    # Fast path: session already initialized for this process
    if _session_pid == current_pid and _session_id is not None:
        return

    with _session_lock:
        # Double-check inside lock (another thread might have initialized)
        if _session_pid == current_pid and _session_id is not None:
            return

        # _session_pid is assigned LAST: the fast path above reads without the
        # lock and admits readers once pid+id are set, so all other fields must
        # already be populated by then (a reader admitted between id and
        # start_ms assignments would find start_ms still None).
        _session_start_ms = int(time.time() * 1000)
        _session_id = str(uuid.uuid4())
        _session_pid = current_pid


def get_session_id() -> str:
    """Get the process-scoped session UUID.

    Lazily initialized on first call and stable for the lifetime of the
    process; all threads share the same value. A forked child mints its
    own UUID (PID-change detection), so sessions are unique per process.

    Returns:
        Unique session identifier string (UUID v4 format).

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
    _ensure_session_initialized()
    if _session_id is None:
        raise RuntimeError("Session ID not initialized (should never happen)")
    return _session_id


def get_session_start_ms() -> int:
    """Get the millisecond timestamp when this process session started.

    Regenerated together with the session UUID on PID change, enabling
    server-side session scope detection and request grouping.

    Returns:
        Milliseconds since Unix epoch (e.g., 1700000000000).

    Examples:
        >>> start_ms = get_session_start_ms()
        >>> start_ms > 0
        True
        >>> start_ms == get_session_start_ms()
        True
    """
    _ensure_session_initialized()
    if _session_start_ms is None:
        raise RuntimeError("Session start not initialized (should never happen)")
    return _session_start_ms


__all__ = [
    "get_session_id",
    "get_session_start_ms",
]
