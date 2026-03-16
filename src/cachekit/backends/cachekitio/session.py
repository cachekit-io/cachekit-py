"""Session management for cachekit.io backend with process and thread isolation.

This module provides process-scoped session tracking for cachekit.io requests.
Session IDs are generated once per process (shared across threads) and
regenerated on process restart (PID change detection).

Thread safety is achieved via threading.Lock for PID checking and UUID regeneration.
"""

from __future__ import annotations

import os
import threading
import time
import uuid

# Module-level initialization (regenerated on PID change)
_session_lock = threading.Lock()
_session_pid: int | None = None
_session_id: str | None = None
_session_start_ms: int | None = None

# Thread-local storage for header dict caching
_thread_local = threading.local()


def _ensure_session_initialized() -> None:
    """Ensure session is initialized for current process.

    Regenerates session ID and timestamp if PID changed (process restart).
    Thread-safe via lock (only first thread per process does initialization).
    """
    global _session_pid, _session_id, _session_start_ms

    current_pid = os.getpid()

    # Fast path: session already initialized for this process
    if _session_pid == current_pid and _session_id is not None:
        return

    # Slow path: need to (re)initialize for new process
    with _session_lock:
        # Double-check inside lock (another thread might have initialized)
        if _session_pid == current_pid and _session_id is not None:
            return

        # Generate new session ID for this process
        _session_pid = current_pid
        _session_id = str(uuid.uuid4())
        _session_start_ms = int(time.time() * 1000)

        # Clear thread-local cache (force header regeneration)
        if hasattr(_thread_local, "headers"):
            _thread_local.headers = None


def get_session_id() -> str:
    """Get the process-scoped session ID.

    Returns a stable UUID v4 string that is generated once per process
    and regenerated on process restart (PID change). All threads within
    the process share the same session ID.

    This ID should be included in all requests to cachekit.io to enable
    correlation of cache operations across threads and time.

    Returns:
        str: UUID v4 format session ID (e.g., "550e8400-e29b-41d4-a716-446655440000")

    Raises:
        RuntimeError: If session initialization failed (should never happen)

    Example:
        >>> session_id = get_session_id()
        >>> len(session_id)
        36
        >>> # All calls in same process return the same ID
        >>> session_id == get_session_id()
        True

    Note:
        On process restart (PID change), a new UUID is generated automatically.
        This ensures session IDs are unique per process lifetime.
    """
    _ensure_session_initialized()
    if _session_id is None:
        raise RuntimeError("Session ID not initialized (should never happen)")
    return _session_id


def get_session_start_ms() -> int:
    """Get the millisecond timestamp when the process started.

    Returns the process start time as milliseconds since Unix epoch.
    This timestamp is regenerated on process restart (PID change).

    This value enables server-side session scope detection and request
    grouping by session lifetime.

    Returns:
        int: Milliseconds since Unix epoch (e.g., 1700000000000)

    Raises:
        RuntimeError: If session initialization failed (should never happen)

    Example:
        >>> start_ms = get_session_start_ms()
        >>> start_ms > 0
        True
        >>> # All calls in same process return the same timestamp
        >>> start_ms == get_session_start_ms()
        True
    """
    _ensure_session_initialized()
    if _session_start_ms is None:
        raise RuntimeError("Session start not initialized (should never happen)")
    return _session_start_ms


def get_session_headers() -> dict[str, str]:
    """Get session headers for cachekit.io requests.

    Returns a dictionary containing X-CacheKit-Session-ID and
    X-CacheKit-Session-Start headers needed for cachekit.io API requests.

    The returned dict is cached in thread-local storage to avoid repeated
    dictionary allocations. A fresh copy is returned on each call to prevent
    accidental mutation of cached data while maintaining efficiency.

    Automatically detects process restarts (PID changes) and regenerates
    session ID when needed.

    Returns:
        dict[str, str]: Headers dict with keys:
            - X-CacheKit-Session-ID: Process session UUID
            - X-CacheKit-Session-Start: Process start milliseconds

    Example:
        >>> headers = get_session_headers()
        >>> "X-CacheKit-Session-ID" in headers
        True
        >>> "X-CacheKit-Session-Start" in headers
        True
        >>> headers["X-CacheKit-Session-Start"].isdigit()
        True
    """
    _ensure_session_initialized()

    # Thread-local cache for header dict (eliminates repeated allocation)
    if not hasattr(_thread_local, "headers") or _thread_local.headers is None:
        _thread_local.headers = {
            "X-CacheKit-Session-ID": _session_id,
            "X-CacheKit-Session-Start": str(_session_start_ms),
        }

    # Return a copy to prevent caller mutations from affecting cached data
    return dict(_thread_local.headers)


__all__ = [
    "get_session_id",
    "get_session_start_ms",
    "get_session_headers",
]
