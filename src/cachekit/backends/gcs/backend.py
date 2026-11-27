"""Google Cloud Storage backend implementation for cachekit.

One GCS object per cache entry, in the file backend's entry format: a 14-byte header that carries
the expiry, then the payload, named by the BLAKE2b-128 hex of the cache key. Implements
BaseBackend protocol.
"""

from __future__ import annotations

import hashlib
import logging
import struct
import time
from typing import Any

# The entry format has one definition, the file backend's: an object this backend writes is the
# file that backend would write, so the header is built and judged by the same code.
from cachekit.backends.file.backend import FORMAT_VERSION, HEADER_SIZE, MAGIC, FileBackend, _has_unknown_transform
from cachekit.backends.gcs.config import GCSBackendConfig
from cachekit.backends.gcs.error_handler import classify_gcs_error
from cachekit.hash_utils import redact_error_for_log

_logger = logging.getLogger(__name__)

_CONTENT_TYPE = "application/octet-stream"


def _build_client(project: str | None) -> Any:
    """A storage client on Application Default Credentials.

    With ``project=None`` the client has no project, and does not look for one: reading and
    writing objects needs none, and credentials do not always carry one.

    Raises:
        ImportError: If google-cloud-storage is not installed.
    """
    try:
        from google.cloud import storage
    except ImportError as exc:
        raise ImportError("GCSBackend requires google-cloud-storage. Install it with: pip install 'cachekit[gcs]'") from exc
    return storage.Client(project=project)


class GCSBackend:
    """Google Cloud Storage backend implementing BaseBackend protocol.

    Each entry is one object named ``<prefix><BLAKE2b-128 hex of the key>``, holding the bytes the
    file backend would write for the same key, value and expiry. Expiry is checked on read: an
    expired entry is a miss, and the read deletes it. An entry nobody reads again stays until a
    bucket lifecycle rule removes it (see docs/backends/gcs.md).

    Authentication is Application Default Credentials only. The backend retries no request: a
    failed one raises a ``BackendError`` and the next call tries again.

    Examples:
        Create a backend (requires credentials and a bucket):

        >>> from cachekit.backends.gcs import GCSBackend, GCSBackendConfig
        >>> backend = GCSBackend(GCSBackendConfig(bucket="my-cache-bucket"))  # doctest: +SKIP
        >>> backend.set("key", b"value", ttl=60)  # doctest: +SKIP
        >>> backend.get("key")  # doctest: +SKIP
        b'value'
        >>> backend.delete("key")  # doctest: +SKIP
        True
    """

    def __init__(self, config: GCSBackendConfig | None = None) -> None:
        """Initialize GCSBackend. Builds the client and sends no request to Cloud Storage.

        Args:
            config: Optional configuration. Defaults to loading from environment.

        Raises:
            ImportError: If google-cloud-storage is not installed.
            BackendError: If Application Default Credentials cannot be found.
        """
        self._config = config or GCSBackendConfig.from_env()
        try:
            self._client = _build_client(self._config.project)
        except ImportError:
            raise
        except Exception as exc:
            raise classify_gcs_error(exc, operation="init") from exc
        # google-cloud-storage fetches each bucket's metadata in a background thread, to label its
        # trace spans, and re-checks the bucket after every 404. A cache miss is a 404, and both
        # are bucket-level gets, which an identity that can only read and write objects is denied.
        self._client._bucket_metadata_cache = None
        self._bucket = self._client.bucket(self._config.bucket)  # a handle: no bucket-level request
        self._prefix = self._config.prefix
        self._timeout = self._config.timeout

    def _blob(self, key: str) -> Any:
        """Handle for the key's object: the prefix, then the file backend's name for the key."""
        return self._bucket.blob(self._prefix + hashlib.blake2b(key.encode("utf-8"), digest_size=16).hexdigest())

    def _discard(self, blob: Any) -> None:
        """Delete an expired or corrupt object that a read just saw. Best effort: the read is a miss either way.

        The delete is conditional on the generation the read saw, so an entry a writer stored in
        between survives; a response that named no generation deletes nothing. A failed request
        (a read-only identity, a lost race, the network) is logged and dropped; anything else is
        a bug and propagates.
        """
        from google.api_core.exceptions import GoogleAPICallError
        from google.auth.exceptions import GoogleAuthError

        generation = blob.generation
        if generation is None:
            return
        try:
            blob.delete(if_generation_match=generation, timeout=self._timeout, retry=None)
        except (GoogleAPICallError, GoogleAuthError, OSError) as exc:
            _logger.debug("GCS cleanup of a dead entry failed: %s", redact_error_for_log(exc))

    def _read(self, operation: str, key: str, header_only: bool) -> bytes | None:
        """Download the key's entry in one request, or its header alone; None for a miss.

        Raises:
            BackendError: If the request fails for any reason but the object being absent.
        """
        from google.api_core.exceptions import NotFound, RequestRangeNotSatisfiable

        blob = self._blob(key)
        try:
            data: bytes = blob.download_as_bytes(
                end=HEADER_SIZE - 1 if header_only else None,
                timeout=self._timeout,
                retry=None,
            )
        except NotFound:
            return None
        except RequestRangeNotSatisfiable:
            # An empty object has no byte range to serve. Read it whole instead: that response
            # carries the generation, which the cleanup of this truncated entry is conditional on.
            return self._read(operation, key, header_only=False)
        except Exception as exc:
            raise classify_gcs_error(exc, operation=operation, key=key) from exc

        header = data[:HEADER_SIZE]
        if len(header) < HEADER_SIZE or header[0:2] != MAGIC or header[2] != FORMAT_VERSION:
            self._discard(blob)  # corrupt
            return None
        if _has_unknown_transform(header):
            return None  # a transform this reader does not implement: a miss, object left untouched
        expiry_timestamp = struct.unpack(">Q", header[6:14])[0]
        if expiry_timestamp > 0 and time.time() >= expiry_timestamp:
            self._discard(blob)
            return None
        return data

    def get(self, key: str) -> bytes | None:
        """Retrieve value from Google Cloud Storage, in one download request.

        Args:
            key: Cache key to retrieve.

        Returns:
            Bytes value if found and not expired, None otherwise.

        Raises:
            BackendError: If the request fails.
        """
        data = self._read("get", key, header_only=False)
        return None if data is None else data[HEADER_SIZE:]

    def set(self, key: str, value: bytes, ttl: int | None = None) -> None:
        """Store value in Google Cloud Storage, replacing the key's object in one upload.

        An entry of up to 8 MiB goes in a single request. A larger one is a resumable upload of
        several requests; the object is replaced only when the last completes.

        Args:
            key: Cache key to store.
            value: Bytes value to store.
            ttl: Time-to-live in seconds. None or 0 means no expiry.

        Raises:
            BackendError: If the TTL is out of range or the upload fails.
        """
        body = FileBackend._build_header(FileBackend._expiry_from_ttl(ttl)) + value
        try:
            self._blob(key).upload_from_string(body, content_type=_CONTENT_TYPE, timeout=self._timeout, retry=None)
        except Exception as exc:
            raise classify_gcs_error(exc, operation="set", key=key) from exc

    def delete(self, key: str) -> bool:
        """Delete key from Google Cloud Storage.

        Args:
            key: Cache key to delete.

        Returns:
            True if the object existed and was deleted, False if it did not exist.

        Raises:
            BackendError: If the request fails.
        """
        from google.api_core.exceptions import NotFound

        try:
            self._blob(key).delete(timeout=self._timeout, retry=None)
        except NotFound:
            return False
        except Exception as exc:
            raise classify_gcs_error(exc, operation="delete", key=key) from exc
        return True

    def exists(self, key: str) -> bool:
        """Check if key exists and is not expired, reading only the entry's 14-byte header.

        Args:
            key: Cache key to check.

        Returns:
            True if key exists and is not expired, False otherwise.

        Raises:
            BackendError: If the request fails.
        """
        return self._read("exists", key, header_only=True) is not None

    def health_check(self) -> tuple[bool, dict[str, Any]]:
        """Check that the bucket answers, by listing at most one object under the prefix.

        A listing needs ``storage.objects.list`` only, never a bucket-level get, and it fails for a
        bucket that does not exist, which an object read cannot tell from a miss.

        Returns:
            Tuple of (is_healthy, details_dict) with latency_ms and backend_type.
        """
        details: dict[str, Any] = {"backend_type": "gcs", "bucket": self._config.bucket}
        start = time.perf_counter()
        try:
            listing = self._client.list_blobs(
                self._bucket, prefix=self._prefix, max_results=1, timeout=self._timeout, retry=None
            )
            next(iter(listing), None)
        except Exception as exc:
            details["error"] = str(classify_gcs_error(exc, operation="health_check"))
        details["latency_ms"] = round((time.perf_counter() - start) * 1000, 2)
        return "error" not in details, details
