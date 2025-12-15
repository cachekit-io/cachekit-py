"""File-based backend for local disk caching.

This module provides a production-ready filesystem-based cache backend with:
- Thread-safe operations using reentrant locks and file-level locking
- Atomic writes via write-then-rename pattern
- LRU eviction based on disk usage thresholds
- TTL-based expiration with secure header format
- Security features (O_NOFOLLOW, symlink prevention)

Public API:
    - FileBackend: Main backend implementation
    - FileBackendConfig: Configuration class

Example:
    >>> from cachekit.backends.file import FileBackend, FileBackendConfig
    >>> config = FileBackendConfig(cache_dir="/tmp/cachekit")
    >>> backend = FileBackend(config)
    >>> backend.set("key", b"value", ttl=60)
    >>> data = backend.get("key")
"""

from __future__ import annotations

from cachekit.backends.file.backend import FileBackend
from cachekit.backends.file.config import FileBackendConfig

__all__ = [
    "FileBackend",
    "FileBackendConfig",
]
