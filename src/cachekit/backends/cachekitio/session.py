"""Session headers for cachekit.io requests.

Process session identity (UUID + start timestamp, PID-aware) lives in
:mod:`cachekit.decorators.session` — the single source of truth shared
with the decorator stats tracker. This module only assembles the SaaS
HTTP headers from it.
"""

from __future__ import annotations

import threading

from cachekit.decorators.session import get_session_id, get_session_start_ms

# Thread-local storage for header dict caching
_thread_local = threading.local()


def get_session_headers() -> dict[str, str]:
    """Get session headers for cachekit.io requests.

    Returns a dictionary containing X-CacheKit-Session-ID and
    X-CacheKit-Session-Start headers needed for cachekit.io API requests.

    The returned dict is cached in thread-local storage to avoid repeated
    dictionary allocations and revalidated against the current session ID,
    so a process restart or fork (PID change) transparently regenerates it.
    A fresh copy is returned on each call to prevent accidental mutation of
    cached data while maintaining efficiency.

    Returns:
        dict[str, str]: Headers dict with keys:
            - X-CacheKit-Session-ID: Process session UUID
            - X-CacheKit-Session-Start: Session start milliseconds

    Example:
        >>> headers = get_session_headers()
        >>> "X-CacheKit-Session-ID" in headers
        True
        >>> "X-CacheKit-Session-Start" in headers
        True
        >>> headers["X-CacheKit-Session-Start"].isdigit()
        True
    """
    session_id = get_session_id()
    cached = getattr(_thread_local, "headers", None)
    if cached is None or cached["X-CacheKit-Session-ID"] != session_id:
        _thread_local.headers = {
            "X-CacheKit-Session-ID": session_id,
            "X-CacheKit-Session-Start": str(get_session_start_ms()),
        }
    return dict(_thread_local.headers)


__all__ = [
    "get_session_id",
    "get_session_start_ms",
    "get_session_headers",
]
