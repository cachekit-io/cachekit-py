"""Redis backend implementation.

Provides Redis storage backend implementing BaseBackend protocol.
"""

from .backend import RedisBackend
from .config import RedisBackendConfig

__all__ = [
    "RedisBackend",
    "RedisBackendConfig",
]
