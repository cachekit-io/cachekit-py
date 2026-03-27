"""Memcached backend for cachekit.

Provides Memcached storage backend using pymemcache with consistent hashing
for multi-server support. Thread-safe via HashClient connection pooling.

Public API:
    - MemcachedBackend: Main backend implementation
    - MemcachedBackendConfig: Configuration class

Example:
    >>> from cachekit.backends.memcached import MemcachedBackend, MemcachedBackendConfig
    >>> config = MemcachedBackendConfig(servers=["127.0.0.1:11211"])
    >>> backend = MemcachedBackend(config)  # doctest: +SKIP
    >>> backend.set("key", b"value", ttl=60)  # doctest: +SKIP
"""

from __future__ import annotations

from cachekit.backends.memcached.backend import MemcachedBackend
from cachekit.backends.memcached.config import MemcachedBackendConfig

__all__ = [
    "MemcachedBackend",
    "MemcachedBackendConfig",
]
