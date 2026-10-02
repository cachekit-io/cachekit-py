"""L1 in-memory cache implementation with TTL respect and memory bounds.

This module provides a thread-safe L1 cache that sits in front of Redis (L2),
dramatically reducing network latency while maintaining Redis as the source of truth.
"""

import logging
import math
import os
import threading
import time
import weakref
from collections import OrderedDict
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from itertools import islice
from typing import Any, Concatenate, Optional, ParamSpec

from cachekit.hash_utils import redact_error_for_log, redact_key_for_log

# Default L1 entry lifetime when the caller supplies no TTL. Shared with the
# decorator's LAB-557 backfill bound: the server's Fresh-For may only ever
# SHORTEN the L1 lifetime relative to this default, never extend it.
DEFAULT_L1_TTL_SECONDS = 300

# Keys removed per lock acquisition in invalidate_many.
_INVALIDATE_BATCH = 1_000

logger = logging.getLogger(__name__)

_P = ParamSpec("_P")


@dataclass
class CacheEntry:
    """L1 cache entry with value, TTL, and size tracking.

    Storage: Stores bytes (encrypted or plaintext msgpack) for unified
    encrypted-at-rest architecture. Decryption/deserialization happens at
    read time in CacheHandler, not storage time.
    """

    value: bytes
    expires_at: float
    size_bytes: int

    def is_expired(self) -> bool:
        """Check if entry has expired."""
        return time.time() >= self.expires_at


class _L1State:
    """The entries, their byte total, and the lock guarding both.

    One object so a fork reset can replace all three with a single attribute store: a thread
    still inside a critical section finishes on the state it bound, and nothing is shared across
    the swap.
    """

    __slots__ = ("cache", "lock", "memory_bytes")

    def __init__(self) -> None:
        self.cache: OrderedDict[str, CacheEntry] = OrderedDict()
        self.lock = threading.RLock()
        self.memory_bytes = 0

    def remove(self, key: str) -> None:
        """Remove key if present, keeping memory_bytes in step; call with lock held."""
        entry = self.cache.pop(key, None)
        if entry is not None:
            self.memory_bytes -= entry.size_bytes

    def remove_all(self, keys: Iterable[str]) -> None:
        """Remove every key present; call with lock held."""
        for key in keys:
            self.remove(key)

    def empty(self) -> None:
        """Remove every entry; call with lock held."""
        self.cache.clear()
        self.memory_bytes = 0


class L1Cache:
    """Thread-safe L1 in-memory cache with TTL and memory management.

    Key features:
    - Thread-safe with RLock for concurrent access
    - Respects Redis TTL (entries expire at Redis TTL time)
    - Memory bounded (100MB default limit)
    - LRU eviction when memory limit reached
    - Fast lookups (~50ns for hits)
    - Background TTL synchronization
    - Stores bytes (encrypted or plaintext msgpack), not Python objects

    Storage Architecture:
    - Stores serialized bytes for unified encrypted-at-rest
    - Supports both encrypted bytes (when encryption enabled) and plaintext msgpack
    - Decryption/deserialization happens at read time (not storage time)

    This cache eliminates the 1,000μs network latency for cache hits
    while maintaining eventual consistency with Redis.
    """

    def __init__(
        self,
        max_memory_mb: int = 100,
        ttl_buffer_seconds: float = 1.0,
        namespace: str = "default",
        before_store: Optional[Callable[[], None]] = None,
    ):
        """Initialize L1 cache.

        Args:
            max_memory_mb: Maximum memory usage in MB (default 100MB)
            ttl_buffer_seconds: Buffer time before Redis TTL expiry (default 1s)
            namespace: Cache namespace for isolation
            before_store: Called before each store. L1CacheManager passes its fork take-over
                here: decorators capture this cache once and never call the manager again.
        """
        self.max_memory_bytes = max_memory_mb * 1024 * 1024
        self.ttl_buffer_seconds = ttl_buffer_seconds
        self.namespace = namespace
        self._before_store = before_store

        # Every critical section binds this once: `s = self._state; with s.lock: ...`. Reading
        # self._state again inside one would mix states if a fork reset replaced it meanwhile.
        self._state = _L1State()

        # Performance metrics. Kept outside _state so a fork reset does not zero them; a holder
        # finishing on a replaced state may race one increment, which only skews stats.
        self._hits = 0
        self._misses = 0
        self._evictions = 0
        self._expired_evictions = 0

        logger.info(
            "L1Cache initialized: namespace=%s, max_memory=%dMB, ttl_buffer=%.1fs",
            namespace,
            max_memory_mb,
            ttl_buffer_seconds,
        )

    def _estimate_size(self, value: bytes) -> int:
        """Estimate memory size of bytes value.

        Simplified from recursive sys.getsizeof() - faster and more accurate
        for bytes storage.

        Args:
            value: Bytes to estimate size for

        Returns:
            Size in bytes
        """
        return len(value)

    def get(self, key: str) -> tuple[bool, Optional[bytes]]:
        """Get value from L1 cache if present and not expired.

        Args:
            key: Cache key

        Returns:
            Tuple of (found, value) where found is True if hit.
            Value is bytes (encrypted or plaintext msgpack), not deserialized object.
            Caller (CacheHandler) is responsible for decryption/deserialization.
        """
        s = self._state
        with s.lock:
            entry = s.cache.get(key)

            if entry is None:
                self._misses += 1
                return False, None

            # Check TTL
            if entry.is_expired():
                # Remove expired entry
                s.remove(key)
                self._misses += 1
                self._expired_evictions += 1
                return False, None

            # LRU: Move to end
            s.cache.move_to_end(key)
            self._hits += 1

            return True, entry.value

    def put(
        self,
        key: str,
        value: bytes,
        redis_ttl: Optional[float] = None,
        expires_at: Optional[float] = None,
    ) -> None:
        """Store value in L1 cache with TTL.

        Args:
            key: Cache key
            value: Bytes to cache (encrypted or plaintext msgpack, not deserialized object)
            redis_ttl: TTL in seconds from Redis (used to calculate expiry)
            expires_at: Absolute expiry timestamp (overrides redis_ttl)

        Raises:
            TypeError: if `value` is not exactly `bytes`. L1 stores raw bytes only; a memoryview
                (e.g. an mmap-backed view from the File backend) or a mutable bytearray must never
                be stored — the former would pin a mapped file's inode for the whole TTL, the
                latter could mutate underneath the cache (#171 blocker C). Loud-fail a regression
                rather than silently alias.
        """
        # Runtime guard: the annotation says bytes, but callers reach here across dynamic
        # boundaries (backend.get returns, decorator paths) where the type isn't enforced.
        if not isinstance(value, bytes):  # pyright: ignore[reportUnnecessaryIsInstance]
            raise TypeError(
                f"L1Cache.put requires bytes, got {type(value).__name__}. "
                "Storing a memoryview/bytearray in L1 is forbidden: an mmap-backed view would pin "
                "the mapped file for the entry's TTL. Materialize to bytes before caching."
            )

        # Calculate expiry time
        current_time = time.time()
        if expires_at is not None:
            expiry = expires_at - self.ttl_buffer_seconds
        elif redis_ttl is not None:
            expiry = current_time + redis_ttl - self.ttl_buffer_seconds
        else:
            expiry = current_time + DEFAULT_L1_TTL_SECONDS - self.ttl_buffer_seconds

        # Skip caching if the effective TTL is non-finite (NaN/inf would create an
        # immortal entry that never expires) or too short (would expire immediately).
        if not math.isfinite(expiry) or expiry <= current_time:
            logger.debug(
                "Skipping L1 cache for key %s - non-finite or too-short TTL (effective expiry: %r)",
                redact_key_for_log(key),
                expiry,
            )
            return

        # Before the first _state use: the take-over replaces a state whose lock fork orphaned.
        if self._before_store is not None:
            self._before_store()

        # Estimate size
        size = self._estimate_size(value)

        # Reject entries that cannot fit even in an empty cache. Storing one would push L1
        # permanently over its budget, and a multi-GB serialized DataFrame envelope is a
        # direct OOM vector (it would also evict every other useful entry on the way in).
        # The value is still available from L2; we only decline to mirror it in L1. If a
        # smaller entry for this key was cached, drop it so L1 stops serving the stale value.
        if size > self.max_memory_bytes:
            self._on_current_state(_L1State.remove, key)
            logger.debug(
                "Skipping L1 cache for key %s - value %d bytes exceeds L1 budget %d bytes (served from L2 only)",
                redact_key_for_log(key),
                size,
                self.max_memory_bytes,
            )
            return

        s = self._state
        with s.lock:
            # Drop any old entry first: left in place, eviction could count its bytes a second time
            s.remove(key)

            # Evict entries if needed to make room
            self._evict_for_space(s, size)

            # Store new entry
            entry = CacheEntry(value=value, expires_at=expiry, size_bytes=size)
            s.cache[key] = entry
            s.memory_bytes += size

            # Move to end (most recently used)
            s.cache.move_to_end(key)

    def _reset_lock_after_fork(self, timeout: float = 1.0) -> None:
        """Replace the state if its lock is unavailable after fork; call only from the take-over of a
        child no at-fork hook reached (_forked_without_hooks).

        A parent thread holding the lock at fork does not exist in the child, so it never releases.
        _is_owned() first: a thread started in the child can reuse the dead holder's ident and so
        "own" its hold. The timeout waits out a child thread briefly holding the lock legitimately.
        Neither probe proves the holder dead: a live child thread may hold the lock past the timeout.
        So the reset never clears or swaps anything in use. It publishes a fresh empty state, and
        whichever thread holds the old lock finishes on the old state. The entries are dropped either
        way (an orphaned holder may have left them half-updated); L2 still has them. Nothing is
        logged: the same fork skipped logging's own at-fork lock reset.
        """
        s = self._state
        owned = s.lock._is_owned()  # pyright: ignore[reportAttributeAccessIssue, reportUnknownMemberType]
        if not owned and s.lock.acquire(timeout=timeout):
            s.lock.release()
            return
        self._state = _L1State()

    def _on_current_state(self, mutate: Callable[Concatenate[_L1State, _P], None], *args: _P.args, **kwargs: _P.kwargs) -> None:
        """Run mutate(state, *args, **kwargs) under the state lock, again on any state a fork reset published meanwhile.

        A removal queued behind a lock that a reset replaced would otherwise miss the fresh state,
        and a stale value put there after the reset would outlive its invalidation.
        """
        while True:
            s = self._state
            with s.lock:
                mutate(s, *args, **kwargs)
            if self._state is s:
                return

    def _evict_for_space(self, s: _L1State, needed_bytes: int) -> None:
        """Evict LRU entries to make space for new entry.

        Args:
            s: The state the caller bound and holds the lock of
            needed_bytes: Bytes needed for new entry
        """
        # Check if we need to evict
        if s.memory_bytes + needed_bytes <= self.max_memory_bytes:
            return

        # Evict LRU entries until we have space
        entries_to_remove = []

        for key, entry in s.cache.items():
            if s.memory_bytes + needed_bytes <= self.max_memory_bytes:
                break

            entries_to_remove.append(key)
            s.memory_bytes -= entry.size_bytes
            self._evictions += 1

        # Remove entries
        for key in entries_to_remove:
            s.cache.pop(key, None)

        if entries_to_remove:
            logger.debug("L1Cache evicted %d entries to free %d bytes", len(entries_to_remove), needed_bytes)

    def invalidate(self, key: str) -> None:
        """Invalidate (remove) entry from L1 cache.

        Args:
            key: Key to invalidate
        """
        self._on_current_state(_L1State.remove, key)

    def invalidate_many(self, keys: Iterable[str]) -> None:
        """Invalidate (remove) several entries, taking the lock once per 1 000 keys.

        For whole-function invalidation, which can evict millions of keys at once. Every get
        and put in this namespace waits on the lock, so no single hold covers the whole list.

        Args:
            keys: Keys to invalidate; keys not in the cache are ignored

        Examples:
            >>> l1 = L1Cache(namespace="docs")
            >>> l1.put("a", b"1", redis_ttl=60)
            >>> l1.put("b", b"2", redis_ttl=60)
            >>> l1.invalidate_many(["a", "b", "never-cached"])
            >>> l1.get("a")
            (False, None)
        """
        it = iter(keys)
        while batch := list(islice(it, _INVALIDATE_BATCH)):
            self._on_current_state(_L1State.remove_all, batch)

    def clear(self) -> None:
        """Clear all entries from L1 cache."""
        self._on_current_state(_L1State.empty)
        logger.info("L1Cache cleared for namespace: %s", self.namespace)

    def cleanup_expired(self) -> int:
        """Remove expired entries from cache.

        Returns:
            Number of entries removed
        """
        current_time = time.time()
        expired_keys = []

        s = self._state
        with s.lock:
            for key, entry in s.cache.items():
                if current_time >= entry.expires_at:
                    expired_keys.append(key)

            for key in expired_keys:
                s.remove(key)
                self._expired_evictions += 1

        if expired_keys:
            logger.debug("L1Cache cleaned up %d expired entries", len(expired_keys))

        return len(expired_keys)

    def get_stats(self) -> dict[str, Any]:
        """Get cache statistics.

        Returns:
            Dictionary of cache metrics
        """
        s = self._state
        with s.lock:
            total_requests = self._hits + self._misses
            hit_rate = self._hits / total_requests if total_requests > 0 else 0.0

            return {
                "namespace": self.namespace,
                "entries": len(s.cache),
                "memory_used_mb": s.memory_bytes / (1024 * 1024),
                "memory_limit_mb": self.max_memory_bytes / (1024 * 1024),
                "memory_usage_percent": (s.memory_bytes / self.max_memory_bytes) * 100,
                "hits": self._hits,
                "misses": self._misses,
                "hit_rate": hit_rate,
                "evictions": self._evictions,
                "expired_evictions": self._expired_evictions,
                "total_requests": total_requests,
            }

    def __repr__(self) -> str:
        """String representation of cache state."""
        stats = self.get_stats()
        return (
            f"L1Cache(namespace={self.namespace}, "
            f"entries={stats['entries']}, "
            f"memory={stats['memory_used_mb']:.1f}/{stats['memory_limit_mb']}MB, "
            f"hit_rate={stats['hit_rate']:.1%})"
        )


class L1CacheManager:
    """Manager for multiple L1 cache instances by namespace."""

    def __init__(self, default_max_memory_mb: int | None = None):
        """Initialize L1 cache manager.

        Args:
            default_max_memory_mb: Default memory limit per namespace. None reads
                l1_max_size_mb from global settings (env: CACHEKIT_L1_MAX_SIZE_MB),
                so the configured budget is actually enforced (issue #163).
        """
        if default_max_memory_mb is None:
            from cachekit.config.singleton import get_settings

            default_max_memory_mb = get_settings().l1_max_size_mb
        self._caches: dict[str, L1Cache] = {}
        self._lock = threading.Lock()
        self._default_max_memory_mb = default_max_memory_mb

        # Background cleanup thread state
        self._cleanup_thread: Optional[threading.Thread] = None
        self._cleanup_interval = 30.0
        self._stop_cleanup = threading.Event()

        # Process that owns _lock, _stop_cleanup and _cleanup_thread. Every method touching
        # them calls _take_over_if_forked() first, or a forked child uses parent state.
        self._owner_pid = os.getpid()
        self._fork_locks: dict[int, threading.Lock] = {}
        _managers.add(self)

    def _take_over_if_forked(self) -> None:
        """Take over inherited state in a forked child; restart cleanup if the parent ran it and hooks ran.

        Threads don't survive fork(): a prefork child (Gunicorn --preload, Celery prefork)
        inherits _cleanup_thread dead, and _lock/_stop_cleanup and each L1Cache state lock as
        parent state a parent thread may have held at fork. An owner-PID check rather than an
        os.register_at_fork hook: uWSGI forks without running Python's at-fork hooks, and a
        thread started inside one is unsafe. L1Cache.put runs this (not get: getpid() is a
        syscall costing about an L1 hit), so any child that grows its L1 gets a live cleanup
        thread, unless no at-fork hook ran. A thread the parent had stopped stays stopped. Decorated functions get() before
        they put(), so _empty_caches_after_fork gives every cache a fresh state, and so a free lock,
        before that first get() on os.fork() servers; without at-fork hooks (uWSGI unless
        --py-call-osafterfork) a get() on an orphaned cache lock before the first put still hangs.
        The take-over resets cache locks only when that hook did not run in this PID
        (_forked_without_hooks): after it, a held cache lock belongs to a live child thread. Without
        hooks it cannot tell a dead holder from a live child thread holding a cache lock past the
        1 s probe, and then drops that cache's entries; the holder finishes unharmed on the state
        it bound (L1Cache._reset_lock_after_fork). --py-call-osafterfork avoids that drop too.
        A child no hook reached was forked from C, which also skips CPython's own after-fork repair:
        a thread started there can hang in Thread.start() or crash the interpreter. So the take-over
        starts no cleanup thread in it, start_background_cleanup refuses there too (also for a manager
        first built in that child, which never takes over), and expired entries are evicted on read or
        under the memory bound instead. None of it logs, the lock reset's drop included: the same fork
        skips logging's at-fork reset, so a handler lock a parent thread held would hang the child.
        """
        pid = os.getpid()
        if self._owner_pid == pid:
            return
        # Keyed by PID so no parent thread can have held it at fork; setdefault is atomic
        # with or without the GIL, so concurrent first puts in a child take over once.
        with self._fork_locks.setdefault(pid, threading.Lock()):
            if self._owner_pid == pid:
                return
            self._lock = threading.Lock()
            self._stop_cleanup = threading.Event()
            # Only where no at-fork hook ran (uWSGI): after the hook, a held cache lock belongs to a
            # live child thread, and resetting it would drop that cache's entries for nothing.
            if _forked_without_hooks():
                for cache in self._caches.values():
                    cache._reset_lock_after_fork()
                self._cleanup_thread = None  # dead, and no thread may start here
            elif self._cleanup_thread is not None:
                try:
                    self._spawn_cleanup_thread(self._cleanup_interval)  # replaces the dead thread on success
                except RuntimeError as e:  # how Thread.start() refuses: "can't start new thread" at a pids cap
                    # Taken over regardless, so puts don't retry. Anything else is a bug: it propagates
                    # with the take-over uncommitted, and the next put retries it in full.
                    self._cleanup_thread = None
                    logger.warning(
                        "L1 cleanup thread restart after fork failed: %s; expired entries are now evicted only on read",
                        redact_error_for_log(e),
                    )
            self._owner_pid = pid

    def get_cache(self, namespace: str = "default", max_size_mb: int | None = None) -> L1Cache:
        """Get or create L1 cache for namespace.

        Args:
            namespace: Cache namespace
            max_size_mb: Memory budget for this namespace's cache. Only applied on
                first creation — namespaces share one L1Cache instance, so the first
                configuration wins. None uses the manager default.

        Returns:
            L1Cache instance for namespace
        """
        self._take_over_if_forked()
        with self._lock:
            cache = self._caches.get(namespace)
            if cache is None:
                budget = max_size_mb if max_size_mb is not None else self._default_max_memory_mb
                cache = L1Cache(max_memory_mb=budget, namespace=namespace, before_store=self._take_over_if_forked)
                self._caches[namespace] = cache
                logger.info("Created L1 cache for namespace: %s (max_memory=%dMB)", namespace, budget)
            elif max_size_mb is not None and cache.max_memory_bytes != max_size_mb * 1024 * 1024:
                # Explicit so a conflicting config isn't silently ignored: the namespace
                # cache already exists with a different budget and cannot be resized.
                logger.warning(
                    "L1 cache for namespace %r already exists with a %d byte budget; "
                    "ignoring requested %d MB (first configuration wins per namespace)",
                    namespace,
                    cache.max_memory_bytes,
                    max_size_mb,
                )
            return cache

    def start_background_cleanup(self, interval_seconds: float = 30.0) -> None:
        """Start background thread to clean up expired entries.

        Starts nothing in a child forked without at-fork hooks (see _take_over_if_forked).

        Args:
            interval_seconds: Cleanup interval in seconds
        """
        self._take_over_if_forked()
        if _forked_without_hooks():
            return
        if self._cleanup_thread is not None and self._cleanup_thread.is_alive():
            logger.warning("Background cleanup already running")
            return
        self._spawn_cleanup_thread(interval_seconds)

    def _spawn_cleanup_thread(self, interval_seconds: float) -> None:
        def cleanup_worker():
            logger.info("L1 cache background cleanup started (interval: %.1fs)", interval_seconds)

            while not self._stop_cleanup.wait(interval_seconds):
                try:
                    total_cleaned = 0

                    with self._lock:
                        for cache in self._caches.values():
                            cleaned = cache.cleanup_expired()
                            total_cleaned += cleaned

                    if total_cleaned > 0:
                        logger.debug("Background cleanup removed %d expired entries", total_cleaned)

                except Exception as e:
                    logger.error("Error in background cleanup: %s", redact_error_for_log(e))

            logger.info("L1 cache background cleanup stopped")

        self._cleanup_interval = interval_seconds
        self._stop_cleanup.clear()
        thread = threading.Thread(target=cleanup_worker, daemon=True)
        thread.start()
        # Published only once started: stop_background_cleanup would join() an unstarted thread and raise.
        self._cleanup_thread = thread

    def stop_background_cleanup(self) -> None:
        """Stop background cleanup thread."""
        self._take_over_if_forked()
        if self._cleanup_thread is None:
            return

        self._stop_cleanup.set()
        self._cleanup_thread.join(timeout=5.0)
        self._cleanup_thread = None

    def get_all_stats(self) -> dict[str, dict[str, Any]]:
        """Get statistics for all cache namespaces.

        Returns:
            Dictionary mapping namespace to stats
        """
        self._take_over_if_forked()
        with self._lock:
            return {namespace: cache.get_stats() for namespace, cache in self._caches.items()}

    def clear_all(self) -> None:
        """Clear all L1 caches."""
        self._take_over_if_forked()
        with self._lock:
            for cache in self._caches.values():
                cache.clear()
            logger.info("Cleared all L1 caches")


# Every live manager, for the at-fork hook. Weak: tests and callers may create and drop managers.
_managers: "weakref.WeakSet[L1CacheManager]" = weakref.WeakSet()

# The processes a thread may start in: the one that imported this module, and the latest child
# _empty_caches_after_fork ran in. Any other PID was forked from C, without at-fork hooks.
# So import cachekit before any fork made from C: a process that first imports it after such a fork
# (uWSGI --lazy-apps without --py-call-osafterfork) is taken for safe, and cleanup starts a thread there.
# No check made at import can tell it apart: a uWSGI master forks from its main thread, so the child's
# thread idents match a fresh process's, and a fresh process may import on any thread.
_import_pid = os.getpid()
_hooked_pid: Optional[int] = None

# The L1 states a forked child inherited, kept referenced so their pages stay shared with the parent
# (see _empty_caches_after_fork). Never read.
_inherited_states: list[_L1State] = []


def _forked_without_hooks() -> bool:
    """Whether this process was forked without at-fork hooks (uWSGI unless --py-call-osafterfork)."""
    pid = os.getpid()
    return pid != _import_pid and pid != _hooked_pid


def _empty_caches_after_fork() -> None:
    """Give every cache a fresh, empty state in a forked child, before the child's first get().

    A forked child starts with an empty L1. The parent's entries are not the child's to serve: an
    invalidation announced after the fork reaches the child only once it subscribes (if ever), so an
    inherited entry could outlive its invalidation for its whole L1 TTL. L2 still has every entry.
    The fresh state also retires what a parent thread left mid-update at fork: a lock it held, which
    would hang the child's first get(), and a byte count it had not settled.

    One attribute store per cache, never a clear in place: a thread that forked inside a critical
    section and returns there finishes on the state it bound. The child is single-threaded here, so
    this takes no lock, starts no thread and logs nothing. Starting the cleanup thread stays with
    _take_over_if_forked, outside the hook.

    The parent's states are kept, not freed: freeing them would write to every entry's memory, so
    each child would copy most of the parent's L1 pages as it forked, a cost close to the size of a
    warm L1 per child, paid again by every subprocess started with a preexec_fn. Kept, the pages stay
    shared with the parent.
    """
    global _hooked_pid
    for manager in list(_managers):
        for cache in list(manager._caches.values()):
            _inherited_states.append(cache._state)
            cache._state = _L1State()
    _hooked_pid = os.getpid()  # last: a hook cut short leaves the child hookless, so the take-over still resets


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_empty_caches_after_fork)


# Global L1 cache manager instance
_global_l1_manager: Optional[L1CacheManager] = None


def get_l1_cache_manager() -> L1CacheManager:
    """Get or create global L1 cache manager.

    Returns:
        Global L1CacheManager instance
    """
    global _global_l1_manager

    if _global_l1_manager is None:
        from cachekit.config.singleton import get_settings

        _global_l1_manager = L1CacheManager()
        # Start background cleanup by default, at the configured interval
        _global_l1_manager.start_background_cleanup(interval_seconds=get_settings().l1_cleanup_interval_seconds)

    return _global_l1_manager


def get_l1_cache(namespace: str = "default", max_size_mb: int | None = None) -> L1Cache:
    """Get L1 cache for namespace.

    Args:
        namespace: Cache namespace
        max_size_mb: Memory budget in MB. Applied only when this namespace's cache
            is first created (first configuration wins); None uses the settings
            default (l1_max_size_mb / CACHEKIT_L1_MAX_SIZE_MB).

    Returns:
        L1Cache instance
    """
    manager = get_l1_cache_manager()
    return manager.get_cache(namespace, max_size_mb=max_size_mb)
