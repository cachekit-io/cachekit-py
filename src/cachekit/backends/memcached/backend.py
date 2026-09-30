"""Memcached backend implementation for cachekit.

Thread-safe Memcached backend using pymemcache HashClient with consistent hashing
for multi-server support. Implements BaseBackend protocol.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Optional

from cachekit.backends.errors import BackendError, BackendErrorType
from cachekit.backends.memcached.config import MAX_MEMCACHED_TTL, MemcachedBackendConfig
from cachekit.backends.memcached.error_handler import classify_memcached_error
from cachekit.hash_utils import redact_error_for_log

_logger = logging.getLogger(__name__)

# Keys per pipelined delete_many send. pymemcache writes the whole send before reading any
# reply; at ~10 bytes a reply, 1,000 keys stay far below a socket buffer.
_PIPELINE_KEYS = 1_000


def _parse_server(server: str) -> tuple[str, int]:
    """Parse 'host:port' string into (host, port) tuple.

    Args:
        server: Server address in 'host:port' format.

    Returns:
        Tuple of (host, port).
    """
    host, port_str = server.rsplit(":", 1)
    return (host, int(port_str))


class MemcachedBackend:
    """Memcached storage backend implementing BaseBackend protocol.

    Uses pymemcache HashClient for consistent-hashing across multiple servers.
    Thread-safe via HashClient's internal connection pooling.

    Examples:
        Create backend with defaults (requires running Memcached):

        >>> backend = MemcachedBackend()  # doctest: +SKIP
        >>> backend.set("key", b"value", ttl=60)  # doctest: +SKIP
        >>> backend.get("key")  # doctest: +SKIP
        b'value'
        >>> backend.delete("key")  # doctest: +SKIP
        True

        Create with explicit config:

        >>> from cachekit.backends.memcached.config import MemcachedBackendConfig
        >>> config = MemcachedBackendConfig(servers=["mc1:11211", "mc2:11211"])
        >>> backend = MemcachedBackend(config)  # doctest: +SKIP
    """

    def __init__(self, config: MemcachedBackendConfig | None = None) -> None:
        """Initialize MemcachedBackend.

        Args:
            config: Optional configuration. Defaults to loading from environment.
        """
        from pymemcache.client.hash import HashClient

        self._config = config or MemcachedBackendConfig.from_env()
        servers = [_parse_server(s) for s in self._config.servers]

        self._client: HashClient = HashClient(
            servers=servers,
            connect_timeout=self._config.connect_timeout,
            timeout=self._config.timeout,
            max_pool_size=self._config.max_pool_size,
            retry_attempts=self._config.retry_attempts,
        )
        self._key_prefix = self._config.key_prefix

    @property
    def key_prefix(self) -> str:
        """Wire-level key prefix (contract for interop mode's fail-closed guard).

        Backends that rewrite keys on the wire MUST expose the prefix here so
        interop mode can reject them: a prefixed key is invisible to other SDKs
        and breaks cross-SDK key identity (see cachekit.interop).
        """
        return self._key_prefix

    def _prefixed_key(self, key: str) -> str:
        """Apply key prefix if configured."""
        if self._key_prefix:
            return f"{self._key_prefix}{key}"
        return key

    def get(self, key: str) -> Optional[bytes]:
        """Retrieve value from Memcached.

        Args:
            key: Cache key to retrieve.

        Returns:
            Bytes value if found, None if key doesn't exist.

        Raises:
            BackendError: If Memcached operation fails.
        """
        try:
            result = self._client.get(self._prefixed_key(key))
            if result is None:
                return None
            # pymemcache returns bytes by default
            return bytes(result) if not isinstance(result, bytes) else result
        except Exception as exc:
            raise classify_memcached_error(exc, operation="get", key=key) from exc

    def set(self, key: str, value: bytes, ttl: Optional[int] = None) -> None:
        """Store value in Memcached.

        Args:
            key: Cache key to store.
            value: Bytes value to store.
            ttl: Time-to-live in seconds. None or 0 means no expiry.
                 Clamped to 30-day Memcached maximum.

        Raises:
            BackendError: If Memcached operation fails.
        """
        # Guard client-side against oversized items. Memcached rejects items over its
        # item-size limit (default 1 MiB), but with noreply that rejection is never read —
        # the call appears to succeed and the entry is silently never cached. Fail loudly
        # instead, so the caller can compress, shard, or switch backends.
        max_size = self._config.max_item_size_bytes
        if max_size and len(value) > max_size:
            raise BackendError(
                message=(
                    # No raw key in the message — it reaches log sinks via str(e)
                    # (CWE-532); the key= segment _format_message appends carries
                    # the redacted digest for correlation.
                    f"Value is {len(value)} bytes, which exceeds the Memcached "
                    f"max item size of {max_size} bytes. Memcached cannot store it. Enable "
                    f"compression, use a larger-payload backend (Redis/SaaS/File), or raise both "
                    f"the server's -I limit and CACHEKIT_MEMCACHED_MAX_ITEM_SIZE_BYTES."
                ),
                error_type=BackendErrorType.PERMANENT,
                operation="set",
                key=key,
            )

        expire = 0
        if ttl is not None and ttl > 0:
            expire = min(ttl, MAX_MEMCACHED_TTL)

        try:
            # noreply=False so an oversized/error reply from the server is read and surfaced
            # rather than silently swallowed (HashClient defaults to noreply=True).
            self._client.set(self._prefixed_key(key), value, expire=expire, noreply=False)
        except Exception as exc:
            raise classify_memcached_error(exc, operation="set", key=key) from exc

    def delete(self, key: str) -> bool:
        """Delete key from Memcached.

        Args:
            key: Cache key to delete.

        Returns:
            True if key existed and was deleted, False otherwise.

        Raises:
            BackendError: If Memcached operation fails.
        """
        try:
            return bool(self._client.delete(self._prefixed_key(key), noreply=False))
        except Exception as exc:
            raise classify_memcached_error(exc, operation="delete", key=key) from exc

    def _delete_many(self, keys: list[str]) -> set[str]:
        """Delete many keys with pipelined round trips per server (internal: whole-function invalidation).

        ``HashClient.delete_many`` sends one ``delete`` per key, so this groups the keys by
        server itself, the way ``HashClient.get_many`` does, and sends each group through
        that server's ``delete_many`` with ``noreply=False``, which reads a reply for every
        key. ``NOT_FOUND`` counts as deleted. A send whose call raises, or that the client
        skips (server in its retry window), is reported failed as a whole: pymemcache
        cannot say which of its keys, if any, were deleted; those keys stay tracked for the
        next sweep. Sends carry at most ``_PIPELINE_KEYS`` keys, so the server's replies
        never back up behind a send that has not finished.

        Only pymemcache's own failures (``MemcacheError``, ``OSError``) are absorbed. Anything
        else, such as an ``AttributeError`` from a pymemcache release that renamed the
        HashClient internals used here, raises, so the caller falls back to per-key deletes.

        Returns:
            The keys not confirmed deleted.

        Raises:
            Exception: Any error that is not a Memcached or socket failure.
        """
        from pymemcache.exceptions import MemcacheError

        get_client = self._client._get_client  # bound outside the trys: drift must raise
        run = self._client._safely_run_func
        failed: set[str] = set()
        groups: dict[Any, dict[str, str]] = {}  # client -> {wire key: key}
        for key in keys:
            wire_key = self._prefixed_key(key)
            try:
                client = get_client(wire_key)
            except MemcacheError as exc:  # invalid key, or every server down
                _logger.debug("Memcached delete skipped a key: %s", redact_error_for_log(exc))
                client = None
            if client is None:
                failed.add(key)
            else:
                groups.setdefault(client, {})[wire_key] = key
        for client, group in groups.items():
            wire_keys = list(group)
            for i in range(0, len(wire_keys), _PIPELINE_KEYS):
                chunk = wire_keys[i : i + _PIPELINE_KEYS]
                try:
                    acked = run(client, client.delete_many, False, chunk, noreply=False)
                except (MemcacheError, OSError) as exc:
                    _logger.debug("Memcached delete_many failed for %d key(s): %s", len(chunk), redact_error_for_log(exc))
                    acked = False
                if not acked:
                    failed.update(group[k] for k in chunk)
        return failed

    def exists(self, key: str) -> bool:
        """Check if key exists in Memcached.

        Memcached has no native EXISTS command; uses GET and checks for None.

        Args:
            key: Cache key to check.

        Returns:
            True if key exists, False otherwise.

        Raises:
            BackendError: If Memcached operation fails.
        """
        try:
            return self._client.get(self._prefixed_key(key)) is not None
        except Exception as exc:
            raise classify_memcached_error(exc, operation="exists", key=key) from exc

    async def refresh_ttl(self, key: str, ttl: int) -> bool:
        """Refresh a key's TTL via the Memcached ``touch`` command.

        Memcached ships ONLY this half of TTLInspectableBackend, by design: the classic
        text/binary protocol has no command to *read* a key's remaining TTL, and pymemcache's
        ``HashClient`` exposes no meta protocol (``mg <key> t``, memcached >= 1.6). Without a
        ``get_ttl``, Memcached is not a TTLInspectableBackend, so ``refresh_ttl_on_get`` does
        NOT auto-refresh on it (the decorator warns once and no-ops — see
        docs/backends/memcached.md). This method is still callable directly to extend a key's
        life, exactly like the ``touch`` command it wraps.

        Args:
            key: Cache key to refresh.
            ttl: New TTL in seconds. 0 means no expiry; values are clamped to the 30-day
                Memcached maximum, matching ``set``.

        Returns:
            True if the key existed and its TTL was updated, False if the key was not found.

        Raises:
            BackendError: If the Memcached operation fails.
        """
        expire = 0
        if ttl > 0:
            expire = min(ttl, MAX_MEMCACHED_TTL)

        try:
            # noreply=False so the server's hit/miss reply is read (matches set/delete);
            # touch returns True if the expiry was updated, False if the key was not found.
            return bool(self._client.touch(self._prefixed_key(key), expire=expire, noreply=False))
        except Exception as exc:
            raise classify_memcached_error(exc, operation="refresh_ttl", key=key) from exc

    def health_check(self) -> tuple[bool, dict[str, Any]]:
        """Check Memcached health by pinging each server with a get.

        HashClient doesn't expose stats(), so we probe with a benign get
        to verify connectivity.

        Returns:
            Tuple of (is_healthy, details_dict) with latency_ms and backend_type.
        """
        start = time.perf_counter()
        try:
            # HashClient has no stats() — probe with a harmless get
            self._client.get(self._prefixed_key("__cachekit_health__"))
            elapsed_ms = (time.perf_counter() - start) * 1000
            return (
                True,
                {
                    "backend_type": "memcached",
                    "latency_ms": round(elapsed_ms, 2),
                    "configured_servers": len(self._config.servers),
                },
            )
        except Exception as exc:
            elapsed_ms = (time.perf_counter() - start) * 1000
            return (
                False,
                {
                    "backend_type": "memcached",
                    "latency_ms": round(elapsed_ms, 2),
                    "error": str(exc),
                    "configured_servers": len(self._config.servers),
                },
            )
