"""Per-request Redis backend with tenant isolation.

This module implements the hybrid singleton pool + per-request wrapper pattern
for multi-tenant caching. All Code-Craftsman fixes (#1-#10) are applied.

Architecture:
- Singleton: Connection pool (expensive, created once in __init__)
- Backend wrapper (cheap ~50ns): bound to one tenant, or — as RedisBackendProvider hands
  it out — reading tenant_context at every operation, so it is safe to hold across requests
- Tenant isolation: Via URL-encoded tenant_id in key prefix (t:{tenant}:{key})
"""

from __future__ import annotations

import asyncio
import functools
import logging
import os
import socket
import threading
import uuid
import weakref
from collections.abc import AsyncIterator, Iterable
from contextlib import asynccontextmanager
from contextvars import ContextVar
from typing import Any, Optional, TypeVar
from urllib.parse import quote as url_encode

import redis
from pydantic import SecretStr
from redis.commands.core import Script
from redis.exceptions import LockNotOwnedError

from cachekit.backends._uninterrupted import _await_uninterrupted
from cachekit.backends.base import BaseBackend
from cachekit.backends.errors import BackendError, UnsupportedTenantError
from cachekit.backends.redis.config import RedisBackendConfig
from cachekit.backends.redis.error_handler import classify_redis_error
from cachekit.config.validation import hide_secret
from cachekit.hash_utils import redact_cache_key, redact_error_for_log
from cachekit.invalidation import _pid_lock

logger = logging.getLogger(__name__)

# Module-level ContextVar for async-safe tenant isolation. Any other type raises TypeError
# (see _encode_tenant); through @cache, before the function runs.
tenant_context: ContextVar[str | bytes | int | uuid.UUID | None] = ContextVar("tenant_context", default=None)

T = TypeVar("T")

# Key registry (KeyTrackableBackend). A tracking set lives 7 days past its last write.
_TRACKING_SET_TTL_SECONDS = 604_800
# Members popped (and keys unlinked) per script call. Bounds each atomic step well under
# lua-time-limit: a script that has written cannot be SCRIPT KILLed, so unbounded work in
# one call can leave every other client on this Redis answered with BUSY.
_DRAIN_CHUNK = 10_000
# 10M members. Past this a write storm is outrunning the drain; stop and warn.
_DRAIN_MAX_ROUNDS = 1_000
_DRAIN_WARN_KEYS = 100_000
# KEYS[1] = scoped tracking set; ARGV[1] = tenant key prefix; ARGV[2] = chunk size.
# Members are stored raw and the prefix is applied here, so whatever a member says, the
# key unlinked is always inside this backend's own tenant prefix. A member leaves the set
# only after its own UNLINK: Redis does not roll back a script that errors midway, so
# removing members first (SPOP) would lose every one whose key a failed call never reached.
# replicate_commands() lets writes follow the random SRANDMEMBER on a 6.x server configured
# with lua-replicate-commands no; effects replication is the default from 5.0, and 7.0+
# keeps the call as a no-op.
_DRAIN_SCRIPT = """
redis.replicate_commands()
local members = redis.call('SRANDMEMBER', KEYS[1], ARGV[2])
for i = 1, #members do
    redis.call('UNLINK', ARGV[1] .. members[i])
    redis.call('SREM', KEYS[1], members[i])
end
return members
"""

# The invalidation listener's connection PINGs after this many idle seconds, which keeps idle-timeout
# proxies and NAT gateways from dropping it. redis-py does not wait for the reply, so a connection
# whose peer goes silent is found only when TCP gives up on it: after _LISTENER_USER_TIMEOUT_MS of an
# unacknowledged PING on Linux, after TCP's own retransmission limit (about 15 minutes) elsewhere.
_LISTENER_HEALTH_CHECK_SECONDS = 10
_LISTENER_USER_TIMEOUT_MS = 30_000

# Pool -> (the PID that opened its windows, the socket_timeout it had before its first open with_timeout()
# window, each open window's timeout by its token, in opening order). The pool runs at the newest open
# window's timeout and gets the configured one back when its last window closes, in whatever order the
# windows close, and listener_pool() clones the configured value, never a window's, whichever backend
# object opened the window on the shared pool. Weak: pools come and go with their clients.
_open_windows: weakref.WeakKeyDictionary[redis.ConnectionPool, tuple[int, float | None, dict[object, float]]] = (
    weakref.WeakKeyDictionary()
)
# Guards _open_windows and the socket_timeout it governs: one pool is shared by every thread, and a
# window can open and close on any thread's event loop.
_windows_locks: dict[int, threading.Lock] = {}  # see _pid_lock


def _set_socket_timeout(pool: redis.ConnectionPool, timeout: float | None) -> None:
    if timeout is not None:
        pool.connection_kwargs["socket_timeout"] = timeout
    else:
        pool.connection_kwargs.pop("socket_timeout", None)


def _windows_here(pool: redis.ConnectionPool) -> tuple[float | None, dict[object, float]] | None:
    """The pool's open-window record, if this process opened it. Caller holds the windows lock.

    A forked child inherits its parent's record, but not the tasks holding those windows, so none of them
    can close here: the record is dropped and the configured timeout put back. The at-fork hook below does
    this at fork; this check covers a fork made from C, which runs no hook (uWSGI without
    --py-call-uwsgi-fork-hooks), on the pool's next window or listener clone.
    """
    record = _open_windows.get(pool)
    if record is None:
        return None
    pid, configured, windows = record
    if pid == os.getpid():
        return configured, windows
    del _open_windows[pool]
    _set_socket_timeout(pool, configured)
    return None


def _drop_inherited_windows() -> None:
    # Runs in the child while it is single-threaded, so it takes no lock.
    for pool in list(_open_windows):
        _windows_here(pool)


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_drop_inherited_windows)


def _encode_tenant(tenant_id: object) -> str:
    """URL-encode a tenant id for the key prefix (Fix #2: no ':' collision).

    Apps set int / UUID tenant ids, so both are accepted in canonical form (and typed so on
    ``tenant_context`` and the constructor). int by exact type: no driver returns an int subclass, and a
    bool or IntEnum tenant is a caller bug whose ``str()`` is not canonical (``str(True)`` is
    'True'; an IntEnum's differs between Python versions), so it fails closed. UUID with the
    subclasses drivers return (asyncpg, uuid6), formatted by the base class's ``__str__``, so a
    subclass's ``__str__`` override cannot map two UUIDs to one prefix. The accessors that reads
    (``int``; ``hex`` on 3.14) are trusted on purpose: asyncpg's UUID leaves the stdlib ``int``
    slot empty and supplies them itself, so never read the slot directly. Anything else except
    str / bytes raises TypeError (fail closed): the ``str()`` of an arbitrary object, e.g. a
    default repr embedding ``id()``, can map two tenants to one prefix. The TypeError is an
    ``UnsupportedTenantError``: through ``@cache`` it reaches the caller before the decorated
    function runs, sync and async alike, and before the breaker and L1 checks once the function
    has its backend — never degraded to an uncached call or counted against the circuit breaker
    every tenant of the function shares. A type check, not isolation: L1 is not tenant-scoped.

    The encoding is by text, not by type: ``1``, ``"1"`` and ``b"1"`` share one prefix, as do a
    UUID and ``str(uuid)``, so one tenant read as int in one place and str in another stays one
    tenant. A type-tagged encoding would split such a tenant and move the released str / bytes
    prefixes.
    """
    if type(tenant_id) is int:
        tenant_id = str(tenant_id)
    elif isinstance(tenant_id, uuid.UUID):
        tenant_id = uuid.UUID.__str__(tenant_id)
    if not isinstance(tenant_id, (str, bytes)):
        raise UnsupportedTenantError(f"tenant_id must be str, bytes, int or UUID, not {type(tenant_id).__name__}")
    return url_encode(tenant_id, safe="")


class PerRequestRedisBackend:
    """Tenant-scoped Redis backend over a shared client.

    Cheap enough to build per request, but one instance can also serve every request:
    ``RedisBackendProvider.get_shared_backend()`` returns one object shared for the life of the
    process, scoped per operation (``follow_context``, below).

    Implements all Code-Craftsman fixes:
    - Fix #1: Accepts shared Redis client (not creating per operation)
    - Fix #2: URL-encodes tenant IDs to prevent ':' collision
    - Fix #3: Uses centralized classify_redis_error()
    - Fix #4: No request_id parameter (YAGNI)
    - Fix #5: health_check() doesn't leak tenant_id
    - Fix #6: Implements ALL optional protocols completely
    - Fix #9: Fail-fast validation - raises RuntimeError if tenant_id is None

    A failed operation raises its BackendError outside the ``except`` block, from the
    cause ``classify_redis_error`` keeps, and from a frame that holds neither the client
    nor its pool nor a pipeline in a local: their reprs list the password, and an error
    tracker sends the locals of every frame on a raised error's traceback (CWE-532). The
    frame deletes its ``error`` local as the error leaves (see ``kept_cause``).

    Tenant scoping format: t:{url_encoded_tenant_id}:{key}

    Constructed directly, the backend is bound to ``tenant_id``. With ``follow_context=True`` —
    what RedisBackendProvider hands out — each operation is instead scoped to the tenant set in
    ``tenant_context`` for the calling context, and ``tenant_id`` is used only when that context
    has none. Such an instance can be held across requests (the decorator keeps its backend for
    the life of the process) without serving one tenant's requests as another's (LAB-4773); a
    ContextVar read is per thread and per asyncio task, so sharing is race-free.

    Examples:
        Key scoping with URL-encoded tenant ID:

        >>> from unittest.mock import Mock
        >>> mock_client = Mock()
        >>> backend = PerRequestRedisBackend(mock_client, tenant_id="org:123")
        >>> backend._scoped_key("user:456")
        't:org%3A123:user:456'

        Special characters in tenant_id are URL-encoded:

        >>> backend2 = PerRequestRedisBackend(Mock(), tenant_id="tenant/with:special@chars")
        >>> backend2._scoped_key("key")
        't:tenant%2Fwith%3Aspecial%40chars:key'

        With ``follow_context=True`` the calling context's tenant wins; a direct binding does not
        follow it:

        >>> shared = PerRequestRedisBackend(Mock(), tenant_id="default", follow_context=True)
        >>> token = tenant_context.set("org:999")
        >>> shared.key_prefix, backend.key_prefix
        ('t:org%3A999:', 't:org%3A123:')
        >>> tenant_context.reset(token)
        >>> shared.key_prefix
        't:default:'

        None tenant_id raises RuntimeError (fail-fast):

        >>> PerRequestRedisBackend(Mock(), tenant_id=None)  # doctest: +IGNORE_EXCEPTION_DETAIL
        Traceback (most recent call last):
            ...
        RuntimeError: tenant_id cannot be None...
    """

    def __init__(
        self,
        client: redis.Redis,
        tenant_id: str | bytes | int | uuid.UUID | None,
        *,
        follow_context: bool = False,
    ):
        """Initialize per-request backend wrapper.

        Args:
            client: Shared Redis client (singleton from provider)
            tenant_id: Tenant for key scoping (None = fail-fast) — with follow_context, only
                the fallback for a calling context that has no tenant set
            follow_context: Scope each operation to tenant_context's tenant when one is set

        Raises:
            RuntimeError: If tenant_id is None (fail-fast validation - Fix #9)
            TypeError: If tenant_id is not str, bytes, int or UUID (see _encode_tenant)
        """
        # Fix #9: Fail-fast validation
        if tenant_id is None:
            raise RuntimeError(
                "tenant_id cannot be None. Set tenant context via tenant_context.set() "
                "or ensure tenant_extractor returns non-None value."
            )

        # Fix #1: Accept shared client (not creating per operation)
        self._client = client

        # Fix #2: URL-encode tenant ID to prevent ':' collision
        self._tenant_id = _encode_tenant(tenant_id)
        self._follow_context = follow_context

        # Registered on first drain. Per instance, not module-global: a Script holds its client.
        self._drain_script: Optional[Script] = None

    @property
    def key_prefix(self) -> str:
        """Wire-level key prefix (contract for interop mode's fail-closed guard).

        Every key this wrapper touches is rewritten to ``t:{tenant}:{key}`` —
        invisible to other SDKs reading the same Redis, so interop mode MUST
        reject this backend (see cachekit.interop.ensure_interop_backend_compatible).
        Backends that rewrite keys on the wire MUST expose the prefix here. An encrypted
        cache binds it into the AAD ahead of the cache key, so one tenant's entry copied
        under another tenant's prefix fails authentication.

        With follow_context, resolved per access from ``tenant_context`` (see class docstring).
        """
        tenant_id = tenant_context.get() if self._follow_context else None
        return f"t:{self._tenant_id if tenant_id is None else _encode_tenant(tenant_id)}:"

    def _scoped_key(self, key: str) -> str:
        """Generate tenant-scoped key with URL-encoded tenant ID.

        Format: t:{url_encoded_tenant_id}:{key}

        Args:
            key: Original cache key

        Returns:
            Tenant-scoped key with URL-encoded tenant ID

        Examples:
            Standard key scoping:

            >>> from unittest.mock import Mock
            >>> backend = PerRequestRedisBackend(Mock(), "org:123")
            >>> backend._scoped_key("user:456")
            't:org%3A123:user:456'

            Keys with colons are preserved (only tenant_id is encoded):

            >>> backend._scoped_key("cache:user:profile:settings")
            't:org%3A123:cache:user:profile:settings'
        """
        return f"{self.key_prefix}{key}"

    def get(self, key: str) -> Optional[bytes]:
        """Retrieve value from Redis storage with tenant scoping.

        Args:
            key: Cache key to retrieve (will be tenant-scoped)

        Returns:
            Bytes value if found, None if key doesn't exist

        Raises:
            BackendError: If Redis operation fails (classified via Fix #3)
        """
        scoped_key = self._scoped_key(key)
        try:
            value = self._client.get(scoped_key)
            if value is not None:
                # Handle both bytes and str responses
                if isinstance(value, str):
                    return value.encode("utf-8")
                if isinstance(value, bytes):
                    return value
            return None
        except Exception as exc:
            # Fix #3: Use centralized error classification
            error = classify_redis_error(exc, operation="get", key=key)
        try:
            raise error from error.original_exception
        finally:
            del error

    def set(self, key: str, value: bytes, ttl: Optional[int] = None) -> None:
        """Store value in Redis storage with tenant scoping.

        Args:
            key: Cache key to store (will be tenant-scoped)
            value: Bytes value to store
            ttl: Time-to-live in seconds (None = no expiry)

        Raises:
            BackendError: If Redis operation fails (classified via Fix #3)
        """
        scoped_key = self._scoped_key(key)
        try:
            if ttl is not None and ttl > 0:
                self._client.setex(scoped_key, ttl, value)
            else:
                self._client.set(scoped_key, value)
            return
        except Exception as exc:
            # Fix #3: Use centralized error classification
            error = classify_redis_error(exc, operation="set", key=key)
        try:
            raise error from error.original_exception
        finally:
            del error

    def delete(self, key: str) -> bool:
        """Delete key from Redis storage with tenant scoping.

        Args:
            key: Cache key to delete (will be tenant-scoped)

        Returns:
            True if key was deleted, False if key didn't exist

        Raises:
            BackendError: If Redis operation fails (classified via Fix #3)
        """
        scoped_key = self._scoped_key(key)
        try:
            result = self._client.delete(scoped_key)
            if not isinstance(result, int):
                raise BackendError(
                    message=f"Redis DELETE returned unexpected type: {type(result).__name__}",
                    operation="delete",
                    key=key,
                )
            return result > 0
        except Exception as exc:
            # Fix #3: Use centralized error classification
            error = classify_redis_error(exc, operation="delete", key=key)
        try:
            raise error from error.original_exception
        finally:
            del error

    def exists(self, key: str) -> bool:
        """Check if key exists in Redis storage with tenant scoping.

        Args:
            key: Cache key to check (will be tenant-scoped)

        Returns:
            True if key exists, False otherwise

        Raises:
            BackendError: If Redis operation fails (classified via Fix #3)
        """
        scoped_key = self._scoped_key(key)
        try:
            result = self._client.exists(scoped_key)
            if not isinstance(result, int):
                raise BackendError(
                    message=f"Redis EXISTS returned unexpected type: {type(result).__name__}",
                    operation="exists",
                    key=key,
                )
            return result > 0
        except Exception as exc:
            # Fix #3: Use centralized error classification
            error = classify_redis_error(exc, operation="exists", key=key)
        try:
            raise error from error.original_exception
        finally:
            del error

    def health_check(self) -> tuple[bool, dict[str, Any]]:
        """Check Redis backend health status.

        Fix #5: Does NOT leak tenant_id in health check response.
        Returns generic Redis health without tenant-specific info.

        Returns:
            Tuple of (is_healthy, details_dict)
        """
        try:
            import time

            start = time.time()
            self._client.ping()
            latency_ms = (time.time() - start) * 1000

            info = self._client.info()
            if not isinstance(info, dict):
                raise BackendError(
                    message=f"Redis INFO returned unexpected type: {type(info).__name__}",
                    operation="health_check",
                    key="N/A",
                )

            return (
                True,
                {
                    "backend_type": "redis",
                    "latency_ms": round(latency_ms, 2),
                    "version": info.get("redis_version", "unknown"),
                    "used_memory_human": info.get("used_memory_human", "unknown"),
                    "connected_clients": info.get("connected_clients", 0),
                },
            )
        except Exception as exc:
            # Fix #3: Use centralized error classification
            error = classify_redis_error(exc, operation="health_check")
            return (
                False,
                {
                    "backend_type": "redis",
                    "latency_ms": -1,
                    "error": error.message,
                    "error_type": error.error_type.value,
                },
            )

    # Fix #6: Implement ALL optional protocols completely

    async def get_ttl(self, key: str) -> Optional[int]:
        """Get remaining TTL on key (TTLInspectableBackend protocol).

        Args:
            key: Cache key to inspect (will be tenant-scoped)

        Returns:
            Remaining TTL in seconds, or None if key doesn't exist or has no expiry

        Raises:
            BackendError: If Redis operation fails
        """
        scoped_key = self._scoped_key(key)
        try:
            ttl = await asyncio.to_thread(self._client.ttl, scoped_key)  # sync client: keep the round trip off the loop
            if not isinstance(ttl, int):
                raise BackendError(
                    message=f"Redis TTL returned unexpected type: {type(ttl).__name__}",
                    operation="get_ttl",
                    key=key,
                )
            # Redis TTL returns:
            # -2 if key doesn't exist
            # -1 if key exists but has no expiry
            # >0 for remaining TTL in seconds
            if ttl == -2 or ttl == -1:
                return None
            return ttl if ttl > 0 else None
        except Exception as exc:
            error = classify_redis_error(exc, operation="get_ttl", key=key)
        try:
            raise error from error.original_exception
        finally:
            del error

    async def refresh_ttl(self, key: str, ttl: int) -> bool:
        """Refresh TTL on existing key (TTLInspectableBackend protocol).

        Args:
            key: Cache key to refresh (will be tenant-scoped)
            ttl: New TTL in seconds

        Returns:
            True if key existed and TTL was refreshed, False if key doesn't exist

        Raises:
            BackendError: If Redis operation fails
        """
        scoped_key = self._scoped_key(key)
        try:
            result = await asyncio.to_thread(self._client.expire, scoped_key, ttl)
            # Redis EXPIRE returns 1 if TTL was set, 0 if key doesn't exist
            return bool(result)
        except Exception as exc:
            error = classify_redis_error(exc, operation="refresh_ttl", key=key)
        try:
            raise error from error.original_exception
        finally:
            del error

    @asynccontextmanager
    async def acquire_lock(
        self,
        key: str,
        timeout: float,
        blocking_timeout: Optional[float] = None,
    ) -> AsyncIterator[bool]:
        """Acquire distributed lock (LockableBackend protocol).

        Args:
            key: Bare cache key (will be tenant-scoped and ``:lock``-suffixed internally).
                The LockableBackend protocol passes the same key as ``get``/``set``/``delete``;
                the ``:lock`` namespace is a Redis-backend implementation detail kept
                on-wire for zero-migration compatibility with existing deployments.
            timeout: How long to hold lock (seconds) before auto-release
            blocking_timeout: Max time to wait for lock (None = non-blocking)

        Yields:
            True if lock acquired, False if timeout waiting

        Raises:
            BackendError: If a Redis operation fails, or the block raises. An exception the
                block raised is kept whole as ``original_exception``; a redis-py failure
                keeps only its class (see ``classify_redis_error``).

        Note:
            Each acquisition attempt is one non-blocking ``SET NX`` round-trip run via
            ``loop.run_in_executor()`` and drained through ``_await_uninterrupted`` (so a
            cancellation cannot drop a round-trip that still completes); the wait between
            attempts is an ``asyncio.sleep`` on the event loop, never a sleep inside an
            executor thread. A blocking ``Lock.acquire`` run in the executor would pin one
            executor thread per waiter for up to ``blocking_timeout``. The default executor
            has only ``min(32, cpu_count + 4)`` threads (8 when ``cpu_count`` is 4), so once
            concurrent misses on one key reach that size the holder's own
            ``get``/``set``/``release`` — also executor calls — queue behind the waiters,
            every waiter times out, and all of them recompute.
            Sets thread_local=False because attempts and release may run on different
            executor threads.
            Cancellation is drained, not raced: an in-flight attempt or release round-trip
            always runs to completion, a lock the attempt wins is released, and only then is
            the ``CancelledError`` re-raised.
        """
        # Derive the on-wire Redis lock name from the bare cache key: ``<scoped_key>:lock``.
        # Keeping this suffix on the wire preserves compatibility with existing Redis
        # deployments — the lock identity didn't change, only the protocol boundary
        # (the wrapper no longer pollutes the cache_key passed in).
        # Resolve it here, in the caller's context: run_in_executor does not carry contextvars,
        # so the executor callables below must never read tenant_context themselves.
        scoped_key = f"{self._scoped_key(key)}:lock"
        body_error: Optional[Exception] = None
        try:
            from redis.lock import Lock

            lock = Lock(
                self._client,
                name=scoped_key,
                timeout=timeout,
                thread_local=False,  # attempts and release may land on different executor threads
            )

            loop = asyncio.get_running_loop()
            deadline = None if blocking_timeout is None else loop.time() + blocking_timeout
            token = uuid.uuid4().hex  # one token for the whole acquisition, however many attempts

            def _release_sync() -> None:
                # Catch inside the executor callable, not around _release(): once a cancellation has
                # landed, _await_uninterrupted re-raises it and an error left on the future would only
                # surface as asyncio's "exception was never retrieved" at GC.
                try:
                    lock.release()
                except LockNotOwnedError as e:
                    logger.debug(
                        "Redis lock already expired or taken over before release: %s", redact_error_for_log(e)
                    )  # nothing to orphan
                except redis.RedisError as e:
                    logger.warning(
                        "Redis lock release for %s failed (%s); the key lives until its TTL",
                        redact_cache_key(key),
                        redact_error_for_log(e),
                    )

            async def _release() -> None:
                # Drained: a cancel landing while this still queues for a thread must not drop the release.
                await _await_uninterrupted(loop.run_in_executor(None, _release_sync))

            while True:
                # Drained: a cancel cannot stop the thread's SET NX from winning, only hide that it did.
                attempt = loop.run_in_executor(None, functools.partial(lock.acquire, blocking=False, token=token))
                try:
                    acquired = await _await_uninterrupted(attempt)
                except asyncio.CancelledError:
                    # The attempt has finished. One that failed (e.g. a Redis ConnectionError) cannot
                    # have won; log it rather than let it mask the cancellation.
                    if (err := attempt.exception()) is not None:
                        logger.warning(
                            "Redis lock attempt for %s failed (%s) while acquire_lock was being cancelled",
                            redact_cache_key(key),
                            redact_error_for_log(err),
                        )
                    elif attempt.result():
                        await _release()
                    attempt = err = None  # hold no redis-py exception on the way out (see classify_redis_error)
                    raise
                # Same give-up rule as redis-py's Lock.acquire: stop once the next attempt
                # would land past the deadline. blocking_timeout=None means a single attempt.
                if acquired or deadline is None or loop.time() + lock.sleep > deadline:
                    break
                await asyncio.sleep(lock.sleep)
            try:
                yield acquired
            except Exception as exc:
                body_error = exc  # raised by the caller's code in the block, not by redis-py
                raise
            finally:
                # Release lock if acquired (also run in thread pool)
                if acquired:
                    await _release()
            return
        except Exception as exc:
            if body_error is not None and exc is not body_error:
                # _release_sync logs a redis-py failure itself; anything else would vanish behind the block's exception.
                logger.warning(
                    "Redis lock release for %s failed (%s) after the block raised; raising the block's exception",
                    redact_cache_key(key),
                    redact_error_for_log(exc),
                )
            # The block's own exception wins, even over a release that failed after it, and is kept whole.
            failed = exc if body_error is None else body_error
            error = classify_redis_error(failed, operation="acquire_lock", key=key, keep_exception=failed is body_error)
        except BaseException:
            # A cancel during the release after the block raised: the block's exception, on whose traceback this frame
            # is, must not stay in a local here either.
            attempt = body_error = None
            raise
        try:
            raise error from error.original_exception
        finally:
            # Hold no exception on the way out: a failed attempt's Future holds redis-py's exception whole.
            del error, failed
            attempt = body_error = None

    def track_key(self, registry_id: str, key: str) -> None:
        """Record a written cache key in the registry's tracking set (KeyTrackableBackend protocol).

        One pipelined round-trip: ``SADD`` the key, then ``EXPIRE`` the set to 7 days, so a set
        lives until seven days after the last write to its function. The set name is
        tenant-scoped; the member is stored RAW — ``drain_tracked`` applies the tenant prefix
        itself, so no member can name a key outside this tenant.

        Args:
            registry_id: Unscoped registry id (``ck:reg:{namespace}:{hash}``)
            key: Raw cache key, exactly as passed to ``set``

        Raises:
            BackendError: If the Redis round-trip fails
        """
        scoped_reg = self._scoped_key(registry_id)
        try:
            # Chained, never bound to a local: a pipeline's repr lists the password. A pipelined
            # command returns its pipeline; redis-py's annotations give the reply type instead.
            self._client.pipeline(transaction=False).sadd(scoped_reg, key).expire(  # pyright: ignore[reportAttributeAccessIssue]
                scoped_reg, _TRACKING_SET_TTL_SECONDS
            ).execute()
            return
        except Exception as exc:
            error = classify_redis_error(exc, operation="track_key", key=registry_id)
        try:
            raise error from error.original_exception
        finally:
            del error

    def drain_tracked(self, registry_id: str, local_keys: Iterable[str]) -> set[str]:
        """Delete every tracked key of a registry, then every local key the drain missed
        (KeyTrackableBackend protocol).

        Each script call atomically pops at most 10 000 members: it ``UNLINK``s
        ``{tenant prefix}{member}`` for each, then removes the member from the set, so a member
        is never dropped before its key is. Calls repeat until one pops a short chunk. A
        write landing during the drain is either popped by a later call or stays in the set
        for the next drain — never orphaned. Past 1 000 calls the drain stops with a WARNING
        and the remaining members wait for the next drain.

        Then every key of ``local_keys`` that no call popped is ``UNLINK``ed, up to 10 000
        keys per command. These are normal: another process's drain already popped keys this
        process still remembers, a ``track_key`` failed, or the key predates tracking.

        Args:
            registry_id: Unscoped registry id (``ck:reg:{namespace}:{hash}``)
            local_keys: The caller's snapshot of the raw keys it knows about

        Returns:
            Raw keys deleted — decoded popped members plus all of ``local_keys``

        Raises:
            BackendError: If the script or an ``UNLINK`` batch fails (``NOSCRIPT`` is
                handled by redis-py re-loading the script). Keys already unlinked stay
                deleted; members whose keys were not unlinked stay in the set for the next
                drain.

        Requires a single-instance (or primary/replica) Redis server >= 5.0 and, for
        restricted ACL users, the ``@scripting`` category. Redis Cluster and sharding proxies
        (one endpoint in front of several shards) are unsupported: the script unlinks keys it
        does not declare, which a cluster rejects and a proxy cannot route.
        """
        scoped_reg = self._scoped_key(registry_id)
        try:
            if self._drain_script is None:
                self._drain_script = self._client.register_script(_DRAIN_SCRIPT)
            out: set[str] = set()
            undecodable = 0
            for _ in range(_DRAIN_MAX_ROUNDS):
                popped = self._drain_script(keys=[scoped_reg], args=[self.key_prefix, _DRAIN_CHUNK])
                if not isinstance(popped, list):
                    raise BackendError(
                        message=f"Redis drain script returned unexpected type: {type(popped).__name__}",
                        operation="drain_tracked",
                        key=registry_id,
                    )
                for member in popped:
                    try:
                        out.add(member.decode("utf-8") if isinstance(member, bytes) else member)
                    except UnicodeDecodeError:
                        # Not a key this SDK wrote (keys are str); the script already unlinked it.
                        undecodable += 1
                if len(popped) < _DRAIN_CHUNK:
                    break
            else:
                logger.warning(
                    "Key registry drain for %s stopped after %d rounds; remaining members wait for the next drain",
                    redact_cache_key(registry_id),
                    _DRAIN_MAX_ROUNDS,
                )
            if undecodable:
                # One line per drain, never per member, never the member bytes: a set stuffed with
                # garbage must not become a log flood. Not raised — the members are already
                # unlinked, and raising would only discard the valid keys' L1 evictions.
                logger.warning(
                    "Key registry drain for %s unlinked %d undecodable members; the tracking set was written outside cachekit",
                    redact_cache_key(registry_id),
                    undecodable,
                )
            stragglers = [k for k in local_keys if k not in out]
            for i in range(0, len(stragglers), _DRAIN_CHUNK):
                chunk = stragglers[i : i + _DRAIN_CHUNK]
                self._client.unlink(*(self._scoped_key(k) for k in chunk))
                out.update(chunk)
        except Exception as exc:
            error = classify_redis_error(exc, operation="drain_tracked", key=registry_id)
        else:
            logger.log(
                logging.WARNING if len(out) > _DRAIN_WARN_KEYS else logging.INFO,
                "Key registry drained %d keys for %s",
                len(out),
                redact_cache_key(registry_id),
            )
            return out
        try:
            raise error from error.original_exception
        finally:
            del error

    def listener_pool(self) -> redis.ConnectionPool:
        """A one-connection pool for the invalidation listener, cloned from this backend's pool.

        A clone, never a pool rebuilt from the URL or from ``connection_kwargs`` alone: redis-py
        keeps the transport in ``connection_class`` (``SSLConnection`` for ``rediss://``,
        ``UnixDomainSocketConnection`` for ``unix://``), outside ``connection_kwargs``, so a rebuilt
        pool would fall back to a plaintext TCP ``Connection`` and send the password in the clear.
        The clone keeps the configured ``socket_timeout`` even inside a ``with_timeout()`` window.

        The listener holds its one connection for the life of the process, so the connection PINGs
        after 10 idle seconds and a TCP or TLS connection also sets TCP keepalive (a Unix socket takes
        no keepalive option): idle-timeout proxies keep it. On Linux a TCP or TLS connection also sets
        ``TCP_USER_TIMEOUT`` to 30 s, unless the backend's pool sets its own, so a peer that stops
        acknowledging the PING fails the connection and the listener reconnects. Replies stay bytes,
        whatever the backend's client decodes: events are MessagePack.

        Examples:
            >>> import redis
            >>> pool = redis.ConnectionPool.from_url("rediss://:pw@cache.example:6380/0")  # pragma: allowlist secret
            >>> backend = PerRequestRedisBackend(redis.Redis(connection_pool=pool), "default")
            >>> clone = backend.listener_pool()  # no connection is made until the listener subscribes
            >>> clone.connection_class is redis.SSLConnection, clone.max_connections
            (True, 1)
            >>> clone.connection_kwargs["password"] == pool.connection_kwargs["password"]
            True
            >>> clone.connection_kwargs["socket_keepalive"], clone.connection_kwargs["health_check_interval"]
            (True, 10)
        """
        source = self._client.connection_pool
        with _pid_lock(_windows_locks):
            window = _windows_here(source)
            kwargs = {
                **source.connection_kwargs,
                "health_check_interval": _LISTENER_HEALTH_CHECK_SECONDS,
                "decode_responses": False,
            }
        if window is not None:
            kwargs["socket_timeout"] = window[0]
        if not issubclass(source.connection_class, redis.UnixDomainSocketConnection):
            kwargs["socket_keepalive"] = True
            if hasattr(socket, "TCP_USER_TIMEOUT"):  # Linux only
                configured = kwargs.get("socket_keepalive_options") or {}
                kwargs["socket_keepalive_options"] = {socket.TCP_USER_TIMEOUT: _LISTENER_USER_TIMEOUT_MS, **configured}
        return redis.ConnectionPool(connection_class=source.connection_class, max_connections=1, **kwargs)

    @asynccontextmanager
    async def with_timeout(
        self,
        operation: str,
        timeout_ms: int,
    ) -> AsyncIterator[None]:
        """Set timeout for operations (TimeoutConfigurableBackend protocol).

        Redis supports per-socket timeout, applied here as best-effort.
        Note: This is coarser-grained than per-operation timeout.

        Windows may overlap, through any backend on the same pool: the pool runs at the newest open
        window's timeout, and gets its configured timeout back when the last one closes, whatever
        order they close in.

        Args:
            operation: Operation name (get, set, delete, etc.)
            timeout_ms: Timeout in milliseconds

        Raises:
            BackendError: If the block raises. The block's exception is kept whole as
                ``original_exception`` and classified by its type (see ``classify_redis_error``):
                a BackendError raised in the block, a cachekit operation's timeout included,
                classifies as UNKNOWN
        """
        # Redis socket timeout is set globally on client
        # This is a best-effort implementation (coarser-grained)
        timeout_sec = timeout_ms / 1000.0
        # The pool is never bound to a local: its repr lists the password (see the class docstring).
        token = object()
        with _pid_lock(_windows_locks):
            if _windows_here(self._client.connection_pool) is None:
                configured = self._client.connection_pool.connection_kwargs.get("socket_timeout")
                _open_windows[self._client.connection_pool] = (os.getpid(), configured, {})
            _open_windows[self._client.connection_pool][2][token] = timeout_sec
            self._client.connection_pool.connection_kwargs["socket_timeout"] = timeout_sec
        try:
            yield
        except Exception as exc:
            # Only the caller's code in the block raises here: its exception is kept whole, so raising
            # inside the except chains nothing else.
            raise classify_redis_error(exc, operation=operation, keep_exception=True) from exc
        finally:
            # Restoring the value this window displaced would be wrong when windows close out of order:
            # an inner window that outlives its outer one would leave the outer's timeout on the pool.
            with _pid_lock(_windows_locks):
                record = _windows_here(self._client.connection_pool)
                # None, or no token: a window opened before a fork, closing in the child, which dropped it.
                if record is not None and record[1].pop(token, None) is not None:
                    configured, windows = record
                    if windows:
                        self._client.connection_pool.connection_kwargs["socket_timeout"] = next(reversed(windows.values()))
                    else:
                        del _open_windows[self._client.connection_pool]
                        _set_socket_timeout(self._client.connection_pool, configured)


class RedisBackendProvider:
    """Provider for Redis backend with singleton pool + tenant-scoped wrappers.

    Fix #1: Creates connection pool ONCE in __init__ (expensive).
    Creates singleton Redis client from pool.

    Both methods return a PerRequestRedisBackend that reads tenant_context per operation;
    they differ only in the tenant used when the calling context has none:

    - get_shared_backend(): ``"default"`` — single-tenant mode. What env auto-detection uses;
      use it for anything held across requests (a decorator's ``backend=``, a custom
      BackendProvider).
    - get_backend(): the tenant current at the call, which must be set (raises otherwise) —
      for a request's own backend, which keeps its tenant when handed to a worker thread.
      Held across requests, it serves every context with no tenant set as that tenant.

    A thread-pool job that sets tenant_context must reset it: a reused worker thread keeps
    the last value, and operations follow it.
    """

    def __init__(
        self,
        redis_url: str | SecretStr,
        pool_size: Optional[int] = None,
        config: Optional[RedisBackendConfig] = None,
    ) -> None:
        """Initialize provider with singleton connection pool.

        Fix #1: Creates pool ONCE (expensive operation).

        Args:
            redis_url: Redis connection URL, as a string or a SecretStr
            pool_size: Connection pool size; overrides config.connection_pool_size
            config: Pool and socket settings; defaults to RedisBackendConfig.from_env().
                Its redis_url field is ignored: the redis_url argument wins.

        Raises:
            BackendError: If Redis connection fails
        """
        redis_url = hide_secret(redis_url)  # may carry a password: unwrapped only inside the pool builder (CWE-532)
        try:
            # Fix #1: Create connection pool ONCE. Shared builder wires the
            # finite socket timeouts, so the ping below fails fast on an
            # unreachable Redis instead of blocking on the OS TCP timeout.
            from cachekit.backends.redis.client import create_connection_pool

            self._pool = create_connection_pool(redis_url, config, max_connections=pool_size)

            # Create singleton Redis client from pool
            self._client = redis.Redis(connection_pool=self._pool)

            # Validate connection works
            self._client.ping()
            return
        except Exception as exc:
            error = classify_redis_error(exc, operation="init")
        try:
            raise error from error.original_exception
        finally:
            del error

    def get_backend(self) -> BaseBackend:
        """Get a backend whose fallback tenant is the current one (cheap: ~50ns).

        Extracts tenant_id from ContextVar; operations still follow the calling context.

        Returns:
            PerRequestRedisBackend with tenant isolation

        Raises:
            RuntimeError: If tenant_context is not set (fail-fast - Fix #9)
            TypeError: If tenant_context holds a type other than str, bytes, int or UUID
        """
        # Extract tenant from ContextVar
        tenant_id = tenant_context.get()

        # Create per-request wrapper (cheap: ~50ns)
        # Fix #9: Fail-fast validation happens in PerRequestRedisBackend.__init__
        return PerRequestRedisBackend(self._client, tenant_id, follow_context=True)

    def get_shared_backend(self) -> BaseBackend:
        """Get one backend for every request: a context with no tenant set is scoped to "default".

        Needs no tenant at the call, so it suits a backend built at startup and held for the
        life of the process (``@cache(backend=...)``, env auto-detection).
        """
        return PerRequestRedisBackend(self._client, "default", follow_context=True)

    def close(self) -> None:
        """Close connection pool and cleanup resources."""
        try:
            self._pool.disconnect()
        except Exception as e:
            # Best effort cleanup - log but don't raise
            logger.debug("Error closing Redis connection pool: %s", redact_error_for_log(e))
