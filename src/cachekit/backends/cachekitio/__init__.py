"""cachekit.io backend implementation.

Provides cachekit.io storage backend implementing BaseBackend protocol.
"""

from cachekit.backends.cachekitio.backend import CachekitIOBackend
from cachekit.backends.cachekitio.config import CachekitIOBackendConfig

__all__ = [
    "CachekitIOBackend",
    "CachekitIOBackendConfig",
]
