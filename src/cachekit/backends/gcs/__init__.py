"""Google Cloud Storage backend for cachekit.

Stores each cache entry as one GCS object, in the bytes and under the name the file backend
uses, and authenticates with Application Default Credentials. Requires the ``gcs`` extra:
``pip install 'cachekit[gcs]'``.

Public API:
    - GCSBackend: Main backend implementation
    - GCSBackendConfig: Configuration class

Example:
    >>> from cachekit.backends.gcs import GCSBackend, GCSBackendConfig
    >>> config = GCSBackendConfig(bucket="my-cache-bucket", prefix="cachekit/")
    >>> backend = GCSBackend(config)  # doctest: +SKIP
    >>> backend.set("key", b"value", ttl=60)  # doctest: +SKIP
"""

from __future__ import annotations

from cachekit.backends.gcs.backend import GCSBackend
from cachekit.backends.gcs.config import GCSBackendConfig

__all__ = [
    "GCSBackend",
    "GCSBackendConfig",
]
