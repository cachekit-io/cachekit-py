"""Redis backend implementation.

Provides Redis storage backend implementing BaseBackend protocol.
"""

from cachekit.hiredis_compat import block_hiredis_for_free_threading

# Before .backend imports redis-py: every cachekit path to redis-py goes through this package.
block_hiredis_for_free_threading()

from .backend import RedisBackend  # noqa: E402
from .config import RedisBackendConfig  # noqa: E402

__all__ = [
    "RedisBackend",
    "RedisBackendConfig",
]
